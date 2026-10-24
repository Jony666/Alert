"""Frozen before evaluation. No post-result parameter search."""
from dataclasses import dataclass,asdict
import hashlib,json,math
from ..phase3.strategies import signal as old_signal,plan as old_plan,RULES
from ..exits import Plan
from ..config import CONFIG

CORES=('VOL_SQUEEZE','SR_REVERSAL')
EXITS=('single','split_fixed','split_protect','split_atr','fib_trend')
VARIANTS=('base','neighbor_low','neighbor_high')
FAMILY=42 # 2 cores x (5 exits + 2 neighbors) x 3 timeframes

@dataclass(frozen=True)
class Design:
    version: str='0.9.4'
    volume_neighbors: tuple=(1.15,1.35)
    stop_atr_neighbors: tuple=(1.4,1.6)
    timeout_days: int=3
    bootstrap_block_days: int=14
    family_size: int=FAMILY
    rank_min_trades: int=100
    rank_min_proxy_per_fold: int=8
    rank_win_lower: float=.60
    independent_evidence_verified: bool=False
    pristine_test: bool=False
    forward_verified: bool=False

DESIGN=Design()
def digest():return hashlib.sha256(json.dumps(asdict(DESIGN),sort_keys=True).encode()).hexdigest()

def raw_direction(x,i,strategy):
    if i<80:return 0
    a,b=x[i],x[i-1]
    if strategy=='VOL_SQUEEZE':
        if math.isfinite(b.width_reference) and b.width20<.7*b.width_reference:
            if a.close>a.hi20:return 1
            if a.close<a.lo20:return -1
    elif strategy=='SR_REVERSAL':
        if a.low<a.lo20 and a.close>a.lo20 and a.close>a.open:return 1
        if a.high>a.hi20 and a.close<a.hi20 and a.close<a.open:return -1
    else:raise ValueError('two core strategies only')
    return 0

def staged_signal(x,i,strategy,variant='base'):
    """First-failure mutually exclusive causal technical funnel; no account state."""
    side=raw_direction(x,i,strategy)
    if not side:return 0,0.,'no_raw_pattern'
    a=x[i]
    if not all(math.isfinite(v) for v in (a.atr_fraction,a.hfast,a.hslow,a.btc_move24h,a.width_reference)):
        return 0,0.,'warmup_or_context'
    if not .003<=a.atr_fraction<=.08:return 0,0.,'atr_filter'
    if abs(a.btc_move24h)>=.10:return 0,0.,'btc_shock'
    if strategy=='VOL_SQUEEZE':
        volume={'base':1.25,'neighbor_low':1.15,'neighbor_high':1.35}[variant]
        if not a.volume>volume*a.vol20:return 0,0.,'volume_filter'
        if not (a.close>a.ema50 and a.hfast>a.hslow if side==1 else a.close<a.ema50 and a.hfast<a.hslow):
            return 0,0.,'trend_filter'
    elif not (a.hfast>=a.hslow if side==1 else a.hfast<=a.hslow):
        return 0,0.,'trend_filter'
    if side!=a.btc_direction:return 0,0.,'btc_direction'
    return side,float(abs(a.macd-a.signal)/a.atr),'technical_pass'

def make_plan(strategy,variant,exit_name,prior,entry,side):
    if strategy not in CORES or exit_name not in EXITS or variant not in VARIANTS:raise ValueError('unregistered profile')
    p=old_plan(strategy,prior,entry,side)
    if p is None:return None
    stop=p.stop
    if strategy=='SR_REVERSAL' and variant!='base':
        multiple={'neighbor_low':1.4,'neighbor_high':1.6}[variant]
        stop=min(prior.lo5,entry-multiple*prior.atr) if side==1 else max(prior.hi5,entry+multiple*prior.atr)
    if exit_name=='single':
        # Full technical TP1. TP2 contributes ZERO; no distant target inflates RR.
        return Plan(entry,stop,p.tp1,p.tp1,side,(1.,0.)).validate()
    if exit_name=='fib_trend':
        width=prior.hi55-prior.lo55;extreme=prior.hi55 if side==1 else prior.lo55
        tp1=extreme+side*.618*width
        tp2=extreme+side*(2.618 if strategy=='VOL_SQUEEZE' else 1.618)*width
        try:return Plan(entry,stop,tp1,tp2,side).validate()
        except ValueError:return None
    return Plan(entry,stop,p.tp1,p.tp2,side).validate()

def mode(exit_name):return {'single':'fixed','split_fixed':'fixed','split_protect':'protect','split_atr':'atr','fib_trend':'fib'}[exit_name]

def experiments():
    cases=[]
    for core in CORES:
        for ex in EXITS:
            for split in ('train_2022','wf_2023H1','wf_2023H2','wf_2024H1','wf_2024H2','test_2025_contaminated'):
                for pressure in ('base','double_cost','missed_limits'):cases.append((core,'base',ex,split,pressure))
        for variant in VARIANTS[1:]:
            for split in ('train_2022','wf_2023H1','wf_2023H2','wf_2024H1','wf_2024H2','test_2025_contaminated'):
                for pressure in ('base','double_cost'):cases.append((core,variant,'split_atr',split,pressure))
        for split in ('wf_2024H2','test_2025_contaminated'):
            for pressure in ('gap10pct','slip2pct','liquidity_1pct','expanded_events'):cases.append((core,'base','split_atr',split,pressure))
    return cases
