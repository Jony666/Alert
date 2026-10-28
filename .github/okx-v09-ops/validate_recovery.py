"""Read-only real-data recovery preview; never publishes a production forward-state Artifact."""
import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess

from v09.forward.freeze import verify, now_iso, digest
from v09.forward.state import read_bundle, financial_hash
from v09.forward.storage import GitHubStore, safe_extract
from v09.forward.feed import collect
from v09.forward.engine import advance
from v09.forward.report import write_bundle
from spec_review import load_review, apply_review, reviewed_advance, specification_differences


def download_source(review, destination):
    store = GitHubStore(os.environ['GH_TOKEN'])
    latest = store.get('/actions/workflows/okx-v09-forward-scheduled.yml/runs?branch=master&status=success&per_page=1')['workflow_runs']
    if not latest or str(latest[0]['id']) != review['source']['run_id']:
        raise ValueError('production checkpoint changed: re-review before preview; never select an older state')
    aid = review['source']['artifact_id']
    meta = store.get('/actions/artifacts/' + str(aid))
    if (meta['expired'] or str(meta['workflow_run']['id']) != review['source']['run_id']
            or meta['digest'] != 'sha256:' + review['source']['artifact_sha256']
            or meta['created_at'] != review['source']['artifact_created_at_utc']
            or meta['name'] != 'forward-state-' + review['study_id'] + '-' + review['source']['run_id'] + '-1'):
        raise ValueError('reviewed production artifact no longer intact')
    response = store.session.get('https://api.github.com/repos/Jony666/Alert/actions/artifacts/' + str(aid) + '/zip', timeout=(10,120))
    response.raise_for_status()
    if hashlib.sha256(response.content).hexdigest() != review['source']['artifact_sha256']:
        raise ValueError('source archive checksum mismatch')
    destination.write_bytes(response.content)


def run(args):
    out = Path(args.out); out.mkdir(parents=True, exist_ok=False)
    review = load_review()
    source_zip = Path(args.source_zip) if args.source_zip else out/'original-source.zip'
    if args.download:
        download_source(review, source_zip)
    raw = source_zip.read_bytes()
    if hashlib.sha256(raw).hexdigest() != review['source']['artifact_sha256']:
        raise ValueError('input is not the reviewed original archive')
    if source_zip != out/'original-source.zip': (out/'original-source.zip').write_bytes(raw)
    source = out/'source'; safe_extract(raw,source)
    freeze=json.loads(Path(args.freeze).read_text()); anchor=json.loads(Path(args.anchor).read_text())
    import v09.forward.engine as engine
    model_root=Path(engine.__file__).resolve().parents[1]
    verify(freeze,model_root)
    if subprocess.check_output(['git','rev-parse','HEAD'],cwd=model_root,text=True).strip()!=review['model_commit']:
        raise ValueError('validation must execute exact frozen model commit')
    before_files={str(p.relative_to(source)):hashlib.sha256(p.read_bytes()).hexdigest() for p in source.rglob('*') if p.is_file()}
    previous,manifest=read_bundle(source,freeze,now_iso(),anchor)
    if digest(previous)!=review['source']['state_hash'] or manifest['revision']!=14:
        raise ValueError('source state differs from review')
    if previous['positions'] or previous['pending']:
        raise ValueError('source is not flat; execution review required before collecting')
    original=deepcopy(previous)
    snapshot=json.loads((source/'snapshot.json').read_text())
    fresh=collect(freeze,snapshot,{})
    at=now_iso(); run_id=os.environ.get('GITHUB_RUN_ID','LOCAL_REPAIR_VALIDATION')
    differences=specification_differences(previous['instruments'],fresh['instruments'])
    if differences!=review['changes']:
        raise ValueError('current public contract delta differs from registered review: '+json.dumps(differences))
    try:
        advance(previous,freeze,fresh,run_id,at,review['source']['artifact_created_at_utc'])
    except ValueError as exc:
        if str(exc)!='contract specification changed; review/version required': raise
    else:
        raise AssertionError('unmodified frozen engine did not reproduce the reported failure')
    migrated=apply_review(previous,fresh,review,at)
    assert financial_hash(migrated)==financial_hash(previous)
    state,diagnostics=reviewed_advance(advance,previous,freeze,fresh,run_id,at,review['source']['artifact_created_at_utc'],review)
    parent=dict(run_id=review['source']['run_id'],artifact_id=review['source']['artifact_id'],
                state_hash=digest(previous),revision=previous['revision'],completed_at=review['source']['artifact_created_at_utc'])
    diagnostics.update(validation_only=True,production_state_committed=False,true_new_market_observation=True)
    write_bundle(out/'preview-advance',state,fresh,run_id,parent,diagnostics)
    restored,_=read_bundle(out/'preview-advance',freeze,anchor=anchor)
    replay,red=reviewed_advance(advance,restored,freeze,fresh,run_id,now_iso(),state['last_success_at'],review)
    assert red['duplicate_input'] and financial_hash(replay)==financial_hash(restored)
    red.update(validation_only=True,production_state_committed=False,true_new_market_observation=False,
               note='Same-input replay within this validation run; not a second real observation')
    write_bundle(out/'preview-replay',replay,fresh,run_id,
                 dict(run_id=run_id,state_hash=digest(state),revision=state['revision']),red)
    read_bundle(out/'preview-replay',freeze,anchor=anchor)
    assert previous==original
    assert before_files=={str(p.relative_to(source)):hashlib.sha256(p.read_bytes()).hexdigest() for p in source.rglob('*') if p.is_file()}
    assert state['journal'][:len(original['journal'])]==original['journal']
    for k in ('closed','cash_twd','expense_used_twd','root_run_id','study_id','config_hash','research_start_timestamp_utc'):
        assert state[k]==original[k], k
    assert not state['positions'] and not state['pending']
    assert not any(e['kind'] in ('ENTRY_FILL','EXIT_FILL','FUNDING') for e in state['journal'][len(original['journal']):])
    assert sum(e['kind']=='CONTRACT_SPEC_REVIEW_APPLIED' for e in replay['journal'])==1
    result=dict(status='READ_ONLY_RECOVERY_PREVIEW_PASSED',production_state_committed=False,
                source_run=parent['run_id'],source_artifact=parent['artifact_id'],source_state_hash=parent['state_hash'],
                differences=differences,review_id=review['review_id'],model_commit=review['model_commit'],
                preview_revisions=[previous['revision'],state['revision'],replay['revision']],
                source_unchanged=True,journal_prefix_unchanged=True,financials_unchanged_during_migration=True,
                replay_financial_hash_unchanged=True,historical_fills_created=0,closed_forward_trades=len(state['closed']),
                pending=len(state['pending']),positions=len(state['positions']),real_public_market_groups=len(fresh['frames']),
                market_observed_at_utc=fresh['observed_at_utc'],official_effective_time_known=False,
                freeze_hash=freeze['config_hash'],qualification=freeze['quality'],news_status=fresh['news']['status'],
                note='Preview only: branch validation never replaces or advances the production checkpoint')
    (out/'validation.json').write_text(json.dumps(result,indent=2)+'\n')
    (out/'freeze.json').write_bytes(Path(args.freeze).read_bytes())
    (out/'anchor.json').write_bytes(Path(args.anchor).read_bytes())
    print(json.dumps(result,indent=2))
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'],'a') as f:
            f.write('Read-only recovery preview passed. Production ledger unchanged.\n\n```json\n'+json.dumps(result,indent=2)+'\n```\n')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--freeze',required=True);p.add_argument('--anchor',required=True)
    p.add_argument('--source-zip');p.add_argument('--download',action='store_true')
    p.add_argument('--out',required=True)
    args=p.parse_args()
    try:run(args)
    except Exception as exc:
        # Signed redirect URLs/credentials must never be copied into diagnostics.
        message=str(exc)[:4000] if 'http' not in str(exc).lower() else 'Network/API request failed; signed URLs omitted'
        print('FAIL_CLOSED_REPAIR_VALIDATION',type(exc).__name__,message)
        raise SystemExit(2)
