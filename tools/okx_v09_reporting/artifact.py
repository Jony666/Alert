"""Validate every input byte before exposing a read-only forward-paper view."""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import csv
import io
import math
from pathlib import Path, PurePosixPath
import re
import stat
import zipfile
from .common import VerificationError, at, canonical, close, now, number, read_json, sha256

CORE = {'state.json', 'snapshot.json', 'summary.json', 'positions.json', 'trades.csv', 'equity.csv', 'events.csv'}
TRADE_COLUMNS = ['id','book','strategy_id','strategy_version','config_hash','symbol','bar','side','entry_time','exit_time','entry','units_total','net_twd','net_r','planned_net_rr','fees_twd','funding_twd','hold_days','entries','exits']
EQUITY_COLUMNS = ['time','cash_twd','unrealized_twd','equity_twd','drawdown']
EVENT_COLUMNS = ['seq','id','kind','key','payload','prev','hash']
REPO = 'Jony666/Alert'
WORKFLOW = '.github/workflows/okx-v09-forward-scheduled.yml'


def archive_files(raw):
    if len(raw) > 64*1024*1024:
        raise VerificationError('compressed artifact exceeds 64 MiB')
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            members = z.infolist()
            if not members or len(members) > 1000 or sum(i.file_size for i in members) > 256*1024*1024:
                raise VerificationError('artifact size/member limit exceeded')
            files, seen = {}, set()
            for info in members:
                name = info.filename
                path = PurePosixPath(name)
                mode = (info.external_attr >> 16) & 0o170000
                if (not re.fullmatch(r'[A-Za-z0-9_./-]+', name) or path.is_absolute()
                        or '..' in path.parts or path.as_posix() != name or info.is_dir()
                        or '\\' in name or mode not in (0, stat.S_IFREG) or info.flag_bits & 1
                        or name.casefold() in seen or info.file_size > 64*1024*1024
                        or (info.file_size and info.file_size/max(1, info.compress_size) > 1000)):
                    raise VerificationError('unsafe/duplicate/encrypted artifact member')
                seen.add(name.casefold())
                files[name] = z.read(info)
            return files
    except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
        raise VerificationError('invalid or corrupt ZIP') from exc


def csv_reconcile(raw, columns, expected):
    try:
        reader = csv.DictReader(io.StringIO(raw.decode('utf-8')))
        if reader.fieldnames != columns:
            raise VerificationError('CSV schema mismatch')
        rows = list(reader)
        if len(rows) != len(expected):
            raise VerificationError('CSV record count mismatch')
        for actual, original in zip(rows, expected):
            if None in actual or any(v is None for v in actual.values()):
                raise VerificationError('CSV malformed row')
            for key in columns:
                value, cell = original.get(key), actual[key]
                if isinstance(value, (dict, list)):
                    if canonical(read_json(cell)) != canonical(value):
                        raise VerificationError('CSV nested record mismatch: ' + key)
                elif value is None:
                    if cell != '': raise VerificationError('CSV null mismatch: ' + key)
                elif isinstance(value, bool):
                    if cell != str(value): raise VerificationError('CSV boolean mismatch')
                elif isinstance(value, (int, float)):
                    try: parsed = Decimal(cell)
                    except InvalidOperation as exc: raise VerificationError('CSV numeric mismatch') from exc
                    if not parsed.is_finite() or parsed != Decimal(str(value)):
                        raise VerificationError('CSV numeric value mismatch: ' + key)
                elif cell != str(value):
                    raise VerificationError('CSV text mismatch: ' + key)
    except (UnicodeError, csv.Error) as exc:
        raise VerificationError('invalid CSV') from exc


def projection_equal(actual, expected, path='summary'):
    """Exact structure/discrete values; permit only harmless floating-point money rounding."""
    if isinstance(expected,dict):
        if not isinstance(actual,dict) or set(actual)!=set(expected):raise VerificationError(path+' keys mismatch')
        for key,value in expected.items():projection_equal(actual[key],value,path+'.'+key)
    elif isinstance(expected,list):
        if not isinstance(actual,list) or len(actual)!=len(expected):raise VerificationError(path+' list mismatch')
        for i,value in enumerate(expected):projection_equal(actual[i],value,path+'.'+str(i))
    elif isinstance(expected,float):
        close(actual,expected,path)
    elif type(actual) is not type(expected) or actual!=expected:
        raise VerificationError(path+' discrete value mismatch')


def reconcile_finances(state):
    all_trades = list(state['closed']) + list(state['positions'].values())
    identifiers = set()
    for t in all_trades:
        if t['id'] in identifiers: raise VerificationError('duplicate trade across open/closed books')
        identifiers.add(t['id'])
        if t['book'] != 'PAPER_FORWARD' or t['config_hash'] != state['config_hash']:
            raise VerificationError('mixed trade book')
        if t['side'] not in (-1, 1): raise VerificationError('invalid trade side')
        for key in ('entry', 'units_total', 'risk_twd', 'planned_net_rr'):
            if number(t[key], key) <= 0: raise VerificationError('nonpositive trade ' + key)
        for key in ('net_twd', 'fees_twd', 'funding_twd', 'slippage_twd'):
            number(t[key], key)
        if any(t[k] < 0 for k in ('fees_twd','funding_twd','slippage_twd')):
            raise VerificationError('negative modeled expense')
        observed=at(state['last_processed_at'])
        entered=at(t['entry_time'])
        if entered>observed:raise VerificationError('entry is after verified observation')
        if t in state['closed'] and at(t['exit_time'])>observed:raise VerificationError('exit is after verified observation')
        fills = t['entries'] + t['exits']
        for fill in fills:
            if not entered<=at(fill['time'])<=observed:raise VerificationError('fill outside verified observation interval')
            if number(fill['price'], 'fill price') <= 0 or number(fill['units'], 'fill units') <= 0 or number(fill['fee_twd']) < 0:
                raise VerificationError('invalid execution fill')
            close(fill['units']*fill['price']*state['freeze']['fee_per_fill']*state['freeze']['fx_twd_per_usdt'],fill['fee_twd'],'frozen fill fee')
        close(sum(e['units'] for e in t['entries']), t['units_total'], 'entry units',1e-12)
        close(sum(e['price']*e['units'] for e in t['entries'])/t['units_total'], t['entry'], 'weighted entry',1e-12)
        close(sum(e['fee_twd'] for e in fills), t['fees_twd'], 'fill fees')
        for kind, expected_fills in [('ENTRY_FILL', t['entries']), ('EXIT_FILL', t['exits'])]:
            records=[e['payload'] for e in state['journal'] if e['kind']==kind and e['key'].startswith(t['id']+'|')]
            if len(records)!=len(expected_fills):raise VerificationError('fill/journal count mismatch')
            for recorded,fill in zip(records,expected_fills):
                if at(recorded['time'])!=at(fill['time']):raise VerificationError('fill/journal time mismatch')
                for field in ('price','units','fee_twd'):close(recorded[field],fill[field],'journal '+field)
                if kind=='EXIT_FILL' and recorded['reason']!=fill['reason']:raise VerificationError('exit reason/journal mismatch')
        funding=[e['payload'] for e in state['journal'] if e['kind']=='FUNDING' and e['key'].startswith(t['id']+'|')]
        close(sum(e['twd'] for e in funding),t['funding_twd'],'funding journal')
        gross = sum(t['side']*(e['price']-t['entry'])*e['units']*32 for e in t['exits'])
        close(gross-t['fees_twd']-t['funding_twd'], t['net_twd'], 'realized net; slippage already in prices')
        if t in state['closed']:
            close(sum(e['units'] for e in t['exits']), t['units_total'], 'closed exit units',1e-12)
            close(t['net_twd']/t['risk_twd'], t['net_r'], 'net R',1e-9)
            close((at(t['exit_time'])-at(t['entry_time'])).total_seconds()/86400, t['hold_days'], 'holding days',1e-9)
            if t['hold_days'] < 0: raise VerificationError('negative holding time')
        else:
            close(sum(e['units'] for e in t['exits'])+t['remaining_units'], t['units_total'], 'remaining units',1e-12)
    close(state['cash_twd'],10000+sum(t['net_twd'] for t in all_trades),'project cash')
    close(state['expense_used_twd'],sum(t['fees_twd']+t['funding_twd'] for t in all_trades),'expense reserve')
    previous_time = None
    for point in state['equity']:
        stamp = at(point['time'])
        if previous_time and stamp < previous_time: raise VerificationError('equity time rollback')
        previous_time = stamp
        for key in ('cash_twd','unrealized_twd','equity_twd','drawdown'): number(point[key], key)
        close(point['cash_twd']+point['unrealized_twd'], point['equity_twd'], 'equity sum')
        if point['drawdown'] < 0: raise VerificationError('negative drawdown')


@dataclass(frozen=True)
class VerifiedArtifact:
    raw: bytes
    context: dict
    files: dict
    state: dict
    snapshot: dict
    summary: dict
    manifest: dict
    inventory: dict
    verified_at: str
    profile: str
    parent_verified: bool = False
    context_seal: str = ""
    inventory_seal: str = ""

    @property
    def digest(self): return sha256(self.raw)

    def assert_unchanged(self):
        if (sha256(canonical(self.context)) != self.context_seal
                or sha256(canonical(self.inventory)) != self.inventory_seal
                or set(self.files) != set(self.inventory)):
            raise VerificationError('verified view metadata changed after validation')
        if any(sha256(self.files[n]) != expected for n,expected in self.inventory.items()):
            raise VerificationError('verified input bytes changed after validation')
        for name,value in [('state.json',self.state),('snapshot.json',self.snapshot),('summary.json',self.summary),('run_manifest.json',self.manifest)]:
            if canonical(read_json(self.files[name])) != canonical(value):
                raise VerificationError('verified view changed after validation: '+name)

    def provenance(self):
        self.assert_unchanged()
        age = max(0, (now()-at(self.state['last_success_at'])).total_seconds())
        return dict(tool_input='VERIFIED_PAPER_FORWARD_ARTIFACT',artifact_id=self.context['artifact']['id'],
                    run_id=self.manifest['run_id'],revision=self.state['revision'],zip_sha256=self.digest,
                    state_hash=self.manifest['state_hash'],config_hash=self.state['config_hash'],
                    model_commit=self.state['freeze']['code_commit_sha'],profile=self.profile,
                    verified_at_utc=self.verified_at,parent_artifact_verified=self.parent_verified,
                    cloud_artifact_expired=self.context['artifact'].get('expired',False),
                    data_age_seconds=age,exceeds_production_age_limit=age>self.state['freeze']['max_state_age_days']*86400,
                    production_resume_authorized=False,
                    trust_note='SHA-256 proves byte integrity against supplied GitHub metadata; metadata is not a digital signature')


def verify_artifact(raw, context, model, parent=None):
    if isinstance(raw, (str, Path)): raw = Path(raw).read_bytes()
    if isinstance(context, (str, Path)): context = read_json(Path(context).read_bytes())
    try:
        if context['schema_version'] != 1 or context['repository'] != REPO:
            raise VerificationError('source repository/context mismatch')
        art, run = context['artifact'], context['run']
        if not isinstance(art['id'], int) or isinstance(art['id'], bool) or art['id'] <= 0:
            raise VerificationError('invalid artifact identity')
        if art['digest'] != 'sha256:' + sha256(raw) or art['size_in_bytes'] != len(raw):
            raise VerificationError('outer artifact SHA-256/size mismatch')
        if (run['status'] != 'completed' or run['conclusion'] != 'success' or run['run_attempt'] != 1
                or run['head_branch'] != 'master' or run['path'] != WORKFLOW or run['workflow_id'] != 395226876
                or run['head_sha'] not in model.profile['workflow_commits']
                or run['event'] not in ('schedule','workflow_dispatch')):
            raise VerificationError('unreviewed/unsuccessful workflow source')
        if not re.fullmatch(r'[1-9][0-9]*', str(run['id'])):
            raise VerificationError('invalid source run')
        if any(art['workflow_run'][key] != run[target] for key,target in [('id','id'),('head_sha','head_sha'),('head_branch','head_branch')]):
            raise VerificationError('artifact/run provenance mismatch')
        expected_name='forward-state-'+model.freeze['study_id']+'-'+str(run['id'])+'-1'
        if art['name'] != expected_name: raise VerificationError('not a complete production state artifact')
        if at(art['created_at']) < at(run['created_at']) or at(art['expires_at']) <= at(art['created_at']):
            raise VerificationError('invalid artifact lifecycle timestamps')
        files = archive_files(raw)
        if set(files) != set(model.profile['expected_paths']):
            raise VerificationError('artifact profile file set changed; independent version review required')
        for name, expected in model.profile['fixed_files'].items():
            if sha256(files[name]) != expected: raise VerificationError('unreviewed operational/freeze asset: ' + name)
        manifest = read_json(files['run_manifest.json'])
        if manifest['schema_version'] != 1 or set(manifest['files']) != CORE:
            raise VerificationError('incomplete/unknown committed bundle manifest')
        for name, expected in manifest['files'].items():
            if sha256(files[name]) != expected: raise VerificationError('manifest checksum mismatch: ' + name)
        state, snap, summary = (read_json(files[n]) for n in ('state.json','snapshot.json','summary.json'))
        if canonical(state['freeze']) != canonical(model.freeze): raise VerificationError('unreviewed embedded freeze')
        # Archival validation deliberately does not authorize live engine restore or bypass its age check.
        model.state.validate(state,model.freeze,anchor=model.anchor)
        if (manifest['study_id'] != state['study_id'] or manifest['state_hash'] != sha256(canonical(state))
                or str(manifest['run_id']) != str(run['id']) or state['last_success_run_id'] != str(run['id'])
                or manifest['revision'] != state['revision'] or manifest['root_run_id'] != state['root_run_id']
                or manifest['genesis_hash'] != model.anchor['genesis_hash']):
            raise VerificationError('manifest/state/anchor provenance mismatch')
        if state['revision'] <= 1 or manifest['parent']['revision'] != state['revision']-1:
            raise VerificationError('invalid revision lineage')
        if canonical(read_json(files['positions.json'])) != canonical(state['positions']):
            raise VerificationError('positions projection mismatch')
        if snap['origin'] != 'REAL_OKX_PUBLIC': raise VerificationError('synthetic/historical data cannot be a forward report')
        model.engine.check_snapshot(snap,model.freeze)
        if sha256(canonical(snap)) != state['last_snapshot_hash'] or at(snap['observed_at_utc']) != at(state['last_snapshot_at']):
            raise VerificationError('snapshot/state mismatch')
        if at(state['last_success_at']) < at(snap['observed_at_utc']): raise VerificationError('observation after state commit')
        metadata={m['key']:m for m in snap['metadata']}
        if len(metadata) != len(snap['metadata']) or set(metadata) != set(snap['frames']):
            raise VerificationError('snapshot metadata coverage mismatch')
        for key, rows in snap['frames'].items():
            meta=metadata[key]
            if (meta['checksum'] != sha256(canonical(rows)) or meta['count'] != len(rows)
                    or meta['calendar_complete'] is not True or meta['confirmed_only'] is not True
                    or meta['source'] != 'https://www.okx.com/api/v5/market/history-candles'):
                raise VerificationError('unverified candle metadata: ' + key)
        reconcile_finances(state)
        recomputed = model.report.summary(state,snap)
        projection_equal(summary,recomputed)
        csv_reconcile(files['trades.csv'], TRADE_COLUMNS, state['closed'])
        csv_reconcile(files['equity.csv'], EQUITY_COLUMNS, state['equity'])
        csv_reconcile(files['events.csv'], EVENT_COLUMNS, state['journal'])
        receipt=read_json(files['operations_receipt.json'])
        if (str(receipt['run_id']) != str(run['id']) or receipt['workflow_commit'] != run['head_sha']
                or receipt['event'] != run['event'] or receipt['spec_review_payload_commit'] != model.profile['operations_commit']):
            raise VerificationError('operational version receipt mismatch')
        if set(receipt['operational_files']) != {'freeze.json','anchor.json','RETENTION.md','export_artifacts.py','scheduled_handoff.py'}:
            raise VerificationError('operational inventory incomplete')
        for name, expected in receipt['operational_files'].items():
            if sha256(files[name]) != expected: raise VerificationError('operational receipt checksum mismatch')
        review_bytes=files['contract-review-ops/contract-review.json'];review=read_json(review_bytes)
        if receipt['contract_review_file_sha256'] != sha256(review_bytes): raise VerificationError('review receipt mismatch')
        marker=state['contract_spec_reviews'][review['review_id']]
        events=[e for e in state['journal'] if e['kind']=='CONTRACT_SPEC_REVIEW_APPLIED' and e['key']==review['review_id']]
        if (marker['review_digest'] != sha256(canonical(review)) or marker['source_state_hash'] != review['source']['state_hash']
                or marker['changes'] != review['changes'] or len(events)!=1 or events[0]['payload']!=marker):
            raise VerificationError('unverified contract-spec transition')
        result=VerifiedArtifact(raw,context,files,state,snap,summary,manifest,
            {name:sha256(data) for name,data in sorted(files.items())},now().isoformat(),model.profile['profile'],bool(parent),sha256(canonical(context)),
            sha256(canonical({name:sha256(data) for name,data in sorted(files.items())})))
        if parent: verify_parent(parent,result)
        return result
    except VerificationError:
        raise
    except (KeyError, TypeError, ValueError, IndexError, ArithmeticError) as exc:
        raise VerificationError('artifact schema/state validation failed: ' + str(exc).split('?')[0]) from exc


def verify_parent(parent, current):
    parent.assert_unchanged(); current.assert_unchanged()
    p=current.manifest['parent']
    if (str(p['run_id']) != parent.manifest['run_id'] or p['artifact_id'] != parent.context['artifact']['id']
            or p['state_hash'] != parent.manifest['state_hash'] or p['revision'] != parent.state['revision']
            or at(p['completed_at']) != at(parent.context['artifact']['created_at'])
            or parent.state['revision']+1 != current.state['revision']
            or current.state['journal'][:len(parent.state['journal'])] != parent.state['journal']
            or current.state['closed'][:len(parent.state['closed'])] != parent.state['closed']):
        raise VerificationError('parent artifact/append-only history mismatch')
    if any(current.state['watermarks'][k]<v for k,v in parent.state['watermarks'].items()):
        raise VerificationError('parent watermark rollback')
