from dataclasses import asdict
from datetime import datetime,timezone
from pathlib import Path
import hashlib,json,subprocess,sys
from ..config import CONFIG,STEPS
from ..phase3.strategies import RULES
from ..phase3.__main__ import SYMBOLS

IDS={'VOL_SQUEEZE':'VOLATILITY_CONTRACTION_BREAKOUT','SR_REVERSAL':'SUPPORT_RESISTANCE_REVERSAL'}

def canonical(x):return json.dumps(x,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()
def digest(x):return hashlib.sha256(canonical(x)).hexdigest()
def now_iso():return datetime.now(timezone.utc).isoformat()

def seal(f):
    f={k:v for k,v in f.items() if k not in ('config_hash','study_id','strategy_version')}
    version_body={k:v for k,v in f.items() if k!='freeze_timestamp_utc'}
    f['strategy_version']='0.9.5-'+digest(version_body)[:16]
    f['config_hash']=digest(f);f['study_id']='okx-v095-'+f['config_hash'][:16]
    return json.loads(json.dumps(f))
def files(root):
    return sorted(list(root.glob('*.py'))+list((root/'phase3').glob('*.py'))+list((root/'forward').glob('*.py')))

def create(root,sha):
    root=Path(root)
    f=dict(schema_version=1,strategy_version='0.9.5-frozen-baseline',strategy_ids=IDS,
           code_commit_sha=sha,freeze_timestamp_utc=now_iso(),financial=asdict(CONFIG),rules=asdict(RULES),
           symbols=SYMBOLS,bars=STEPS,warmup_days=210,execution='CONDITIONAL_MARKET_NEXT_FULL_OPEN',
           limit_fill_fraction_per_bar=.5,pending_ttl_bars=3,entry_band_fraction=.005,timeout_days=3,
           fx_twd_per_usdt=32.,fx_source='FROZEN_PAPER_ASSUMPTION_NOT_REAL_ACCOUNT_FX',
           fee_per_fill=.0005,slippage_per_fill=.0005,funding_bound_per_8h=.0003,
           funding_model='ADVERSE_ABSOLUTE_PUBLIC_RATE_ON_ENTRY_NOTIONAL; mark-at-funding unavailable; credits not assumed',
           quality=dict(n=0,win_ci_lower=0.,independence_verified=False,pristine_or_forward_verified=False,
                        official_cross_verified=False,status='NOT VERIFIED'),
           news_policy='UNKNOWN_COVERAGE_BLOCKS_NEW_ENTRIES',max_state_age_days=7,
           book='PAPER_FORWARD',real_orders=False,notifications=False,
           model_hashes={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in files(root)})
    return seal(f)

def verify(f,root=None):
    x={k:v for k,v in f.items() if k not in ('config_hash','study_id')}
    expected_version='0.9.5-'+digest({k:v for k,v in x.items() if k not in ('strategy_version','freeze_timestamp_utc')})[:16]
    if f['strategy_version']!=expected_version:raise ValueError('strategy version does not match code/settings')
    if digest(x)!=f['config_hash'] or f['study_id']!='okx-v095-'+f['config_hash'][:16]:raise ValueError('freeze checksum mismatch')
    fixed={'fee_per_fill':.0005,'slippage_per_fill':.0005,'funding_bound_per_8h':.0003,'limit_fill_fraction_per_bar':.5,'pending_ttl_bars':3,'entry_band_fraction':.005,'timeout_days':3,'fx_twd_per_usdt':32.,'max_state_age_days':7,'news_policy':'UNKNOWN_COVERAGE_BLOCKS_NEW_ENTRIES'}
    if any(f.get(k)!=v for k,v in fixed.items()) or canonical(f['rules'])!=canonical(asdict(RULES)) or f['strategy_ids']!=IDS:raise ValueError('unsupported settings not implemented by this frozen baseline')
    if f['execution'] not in ('CONDITIONAL_MARKET_NEXT_FULL_OPEN','LIMIT_STRICT_CROSS_PARTIAL'):raise ValueError('unregistered execution model')
    if f['book']!='PAPER_FORWARD' or f['real_orders'] or f['notifications']:raise ValueError('PAPER isolation violated')
    if set(f['strategy_ids'])!=set(IDS) or f['financial']!=asdict(CONFIG):
        # JSON tuple/list normalization is handled canonically.
        if set(f['strategy_ids'])!=set(IDS) or canonical(f['financial'])!=canonical(asdict(CONFIG)):raise ValueError('financial/strategy limits changed')
    if root:
        if set(f['model_hashes'])!={str(p.relative_to(Path(root))) for p in files(Path(root))}:raise ValueError('incomplete frozen model manifest')
        for name,sha in f['model_hashes'].items():
            p=Path(root)/name
            if not p.is_relative_to(Path(root)) or hashlib.sha256(p.read_bytes()).hexdigest()!=sha:raise ValueError('frozen model source mismatch '+name)
    return True

if __name__=='__main__':
    root=Path(__file__).resolve().parents[1]
    sha=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
    f=create(root,sha);Path(sys.argv[1]).write_text(json.dumps(f,indent=2)+'\n');print(f['study_id'])
