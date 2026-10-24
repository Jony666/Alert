from copy import deepcopy
from datetime import datetime
from pathlib import Path
import fcntl,json,os,tempfile,shutil
from .freeze import digest,canonical,verify
from ..events import aware

SCHEMA=1

def event(state,kind,key,payload):
    eid=digest(dict(study=state['study_id'],kind=kind,key=key))
    if any(e['id']==eid for e in state['journal']):return False
    prev=state['journal'][-1]['hash'] if state['journal'] else 'GENESIS'
    e=dict(seq=len(state['journal'])+1,id=eid,kind=kind,key=key,payload=deepcopy(payload),prev=prev)
    e['hash']=digest(e);state['journal'].append(e);return True

def validate(state,freeze,at=None,anchor=None):
    verify(freeze)
    if state['schema_version']!=SCHEMA or state['study_id']!=freeze['study_id'] or state['config_hash']!=freeze['config_hash']:
        raise ValueError('state format/freeze mismatch; never reset/mix books')
    if state['book']!='PAPER_FORWARD':raise ValueError('actual/backtest book rejected')
    ids=set();prev='GENESIS'
    for i,e in enumerate(state['journal'],1):
        if e['seq']!=i or e['prev']!=prev or e['id'] in ids or digest({k:v for k,v in e.items() if k!='hash'})!=e['hash']:raise ValueError('journal corrupted')
        ids.add(e['id']);prev=e['hash']
    if not state['journal'] or state['journal'][0]['kind']!='INITIALIZE':raise ValueError('missing genesis')
    if anchor and (state['root_run_id']!=str(anchor['root_run_id']) or state['journal'][0]['hash']!=anchor['genesis_hash'] or state['study_id']!=anchor['study_id']):raise ValueError('anchor mismatch/rollback')
    if at and (aware(at)-aware(state['last_success_at'])).total_seconds()>freeze['max_state_age_days']*86400:raise ValueError('state expired; halt, never initialize again')
    if at and aware(state['last_success_at'])>aware(at):raise ValueError('state time rollback')
    from .engine import exposures
    exp=exposures(state)
    if len(exp)>3 or sum(e['risk_twd'] for e in exp)>2000+1e-6 or any(e['risk_twd']>1000+1e-6 or e['risk_twd']<=0 for e in exp):raise ValueError('restored risk caps violated')
    if any(sum(e['strategy']==c for e in exp)>2 for c in freeze['strategy_ids']):raise ValueError('strategy slot cap violated')
    if len({t['id'] for t in state['closed']})!=len(state['closed']):raise ValueError('duplicate closed trades')
    for t in state['closed']:
        if t['book']!='PAPER_FORWARD' or t['config_hash']!=freeze['config_hash']:raise ValueError('ledger mixing')
    expected=10000+sum(t['net_twd'] for t in state['closed'])+sum(p['net_twd'] for p in state['positions'].values())
    if abs(expected-state['cash_twd'])>1e-6:raise ValueError('cash/trade reconciliation failed')
    return True

def financial_hash(s):
    return digest({k:s[k] for k in ('cash_twd','expense_used_twd','positions','pending','closed','equity','watermarks','candidates','risk','daily_halts','max_drawdown')})

def atomic_bundle(out,files,fail_before_commit=False):
    out=Path(out);out.parent.mkdir(parents=True,exist_ok=True)
    with open(out.parent/(out.name+'.lock'),'a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise RuntimeError('concurrent writer rejected')
        temp=Path(tempfile.mkdtemp(prefix=out.name+'.tmp-',dir=out.parent))
        try:
            if out.exists():raise ValueError('immutable bundle already exists')
            for name,content in files.items():
                if Path(name).name!=name:raise ValueError('unsafe output path')
                with open(temp/name,'wb') as f:f.write(content);f.flush();os.fsync(f.fileno())
            if fail_before_commit:raise RuntimeError('injected interruption')
            os.rename(temp,out)
        finally:
            if temp.exists():shutil.rmtree(temp)


def read_bundle(path,freeze,at=None,anchor=None):
    path=Path(path);manifest=json.loads((path/'run_manifest.json').read_text())
    for name,sha in manifest['files'].items():
        if Path(name).name!=name or digest_bytes((path/name).read_bytes())!=sha:raise ValueError('bundle missing/corrupt '+name)
    state=json.loads((path/'state.json').read_text());validate(state,freeze,at,anchor)
    if manifest['state_hash']!=digest(state) or manifest['study_id']!=state['study_id'] or str(manifest['run_id'])!=str(state['last_success_run_id']):raise ValueError('manifest/state lineage mismatch')
    return state,manifest

def digest_bytes(x):
    import hashlib
    return hashlib.sha256(x).hexdigest()
