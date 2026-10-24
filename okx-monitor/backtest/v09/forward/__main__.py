import argparse,json,os,shutil,tempfile,time
from pathlib import Path
from .freeze import verify,now_iso,digest
from .feed import collect
from .engine import initialize,advance
from .state import read_bundle,financial_hash
from .storage import GitHubStore,resolve
from .report import write_bundle


def fault_checks(folder,freeze):
    original=(Path(folder)/'state.json').read_bytes();results={}
    with tempfile.TemporaryDirectory() as temp:
        missing=Path(temp)/'missing'
        try:read_bundle(missing,freeze)
        except (OSError,ValueError):results['missing_state_refused']=True
        copy=Path(temp)/'corrupt';shutil.copytree(folder,copy)
        (copy/'state.json').write_text('{"corrupt":true}')
        try:read_bundle(copy,freeze)
        except (OSError,ValueError,KeyError):results['corrupt_state_refused']=True
    if len(results)!=2 or (Path(folder)/'state.json').read_bytes()!=original:raise AssertionError('fault checks did not fail safely')
    results['source_bundle_unchanged']=True;return results

def parser():
    p=argparse.ArgumentParser(description='Frozen PAPER_FORWARD; no orders, no push, no deployment')
    p.add_argument('--freeze',required=True);p.add_argument('--anchor',required=True)
    p.add_argument('--mode',choices=('initialize','resume','verify_replay'),default='resume')
    p.add_argument('--out',default='forward_bundle')
    return p

def run(args):
    started=time.monotonic();started_at=now_iso()
    freeze=json.loads(Path(args.freeze).read_text());root=Path(__file__).resolve().parents[1];verify(freeze,root)
    import subprocess
    if subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()!=freeze['code_commit_sha']:raise ValueError('checkout is not frozen source commit')
    anchor=json.loads(Path(args.anchor).read_text())
    run_id=os.environ['GITHUB_RUN_ID'];attempt=int(os.environ.get('GITHUB_RUN_ATTEMPT','1'))
    run_key=run_id # event IDs stable for rerun; state source/manifest retains attempt
    store=GitHubStore(os.environ['GH_TOKEN'])
    with tempfile.TemporaryDirectory() as temp:
        folder,previous,parent=resolve(store,freeze,run_id,attempt,args.mode,anchor,Path(temp)/'restore')
        if previous:
            # Restore and verify BEFORE contacting the exchange.
            read_bundle(folder,freeze,now_iso(),anchor)
            old_snapshot=json.loads((folder/'snapshot.json').read_text())
        else:old_snapshot=None
        if args.mode=='verify_replay':
            snap=old_snapshot;faults=fault_checks(folder,freeze)
        else:
            exposures={} if previous is None else dict(previous['pending'],**previous['positions'])
            snap=collect(freeze,old_snapshot,exposures);faults={}
        at=now_iso()
        if previous is None:
            state=initialize(freeze,snap,run_key,at);diagnostics=dict(first_initialization=True,historical_trades_imported=0)
        else:
            state,diagnostics=advance(previous,freeze,snap,run_key,at,parent['completed_at'])
        if args.mode=='verify_replay':
            if financial_hash(state)!=financial_hash(previous):raise ValueError('same-input financial idempotence failed')
            diagnostics['fault_checks']=faults;diagnostics['true_new_market_observation']=False
        else:diagnostics['true_new_market_observation']=True
        diagnostics.update(started_utc=started_at,processing_seconds=time.monotonic()-started,valid_market_groups=len(snap['frames']),mode=args.mode,run_attempt=attempt,previous_run_id=parent['run_id'] if parent else None)
        manifest=write_bundle(args.out,state,snap,run_id,parent,diagnostics)
        print(json.dumps(dict(status='PAPER_FORWARD_STATE_COMMITTED',run_id=run_id,revision=state['revision'],
            state_hash=manifest['state_hash'],closed_forward_trades=len(state['closed']),open_positions=len(state['positions']),
            pending_orders=len(state['pending']),market_groups=len(snap['frames']),live_qualified=False)))
        if os.environ.get('GITHUB_OUTPUT'):
            with open(os.environ['GITHUB_OUTPUT'],'a') as f:
                f.write('study_id='+freeze['study_id']+'\n')
    return 0

if __name__=='__main__':
    try:raise SystemExit(run(parser().parse_args()))
    except Exception as e:
        # Never publish a state bundle on failure. Existing source Artifact remains intact.
        message=str(e).split('?')[0]
        Path('forward_failure.json').write_text(json.dumps(dict(status='FAIL_CLOSED_FORWARD',run_id=os.environ.get('GITHUB_RUN_ID'),run_attempt=os.environ.get('GITHUB_RUN_ATTEMPT'),time_utc=now_iso(),error_type=type(e).__name__,message=message,state_committed=False,safe_to_reinitialize=False,valid_market_groups=None),indent=2)+'\n')
        print('FAIL_CLOSED_FORWARD',type(e).__name__,message);raise SystemExit(2)
