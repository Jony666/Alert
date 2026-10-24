from dataclasses import dataclass, asdict
import hashlib
import json
import math
import numpy as np
import pandas as pd
from ..config import STRATEGIES as BASELINES, DAY
from ..strategies import features, signal as baseline_signal
from ..exits import Plan, structure_plan

NEW=('BREAKOUT_RETEST','MOMENTUM_CONTINUATION','VOL_SQUEEZE','SR_REVERSAL','MTF_TREND')
STRATEGIES=BASELINES+NEW

@dataclass(frozen=True)
class Rules:
    version: str='0.9.3'
    atr_min: float=.003
    atr_max: float=.08
    squeeze_ratio: float=.7
    volume_ratio: float=1.25
    retest_atr: float=.5
    stop_atr: float=1.5
    btc_shock_24h: float=.10
    target_extension1: float=.618
    target_extension2: float=1.618
    new_time_exit_days: int=3

RULES=Rules()
def rules_hash():return hashlib.sha256(json.dumps(asdict(RULES),sort_keys=True).encode()).hexdigest()

def aggregate(raw, step, higher):
    rows=[]
    for t,g in raw.assign(bucket=raw.ts//higher*higher).groupby('bucket',sort=True):
        if len(g)==higher//step and g.ts.iloc[0]==t and g.ts.iloc[-1]+step==t+higher:
            rows.append(dict(ts=t,open=g.open.iloc[0],high=g.high.max(),low=g.low.min(),close=g.close.iloc[-1],
                             volume=g.volume.sum(),quote_volume=g.quote_volume.sum()))
    return pd.DataFrame(rows,columns=raw.columns)

def enrich(raw, step, btc_raw, btc_context=None):
    x=features(raw,step)
    x['lo5']=raw.low.shift().rolling(5).min();x['hi5']=raw.high.shift().rolling(5).max()
    x['atr_fraction']=x.atr/x.close
    x['phase3_regime']=np.char.add(np.where(x.close>x.ema50,'UP','DOWN'),np.where(x.atr_fraction>.04,'_HIGH_ATR','_LOW_ATR'))
    x['width20']=(x.hi20-x.lo20)/x.close
    x['width_reference']=x.width20.shift().rolling(60).median()
    x['prior_break_up']=raw.close.shift()>x.hi20.shift()
    x['prior_break_down']=raw.close.shift()<x.lo20.shift()
    x['retest_high']=x.hi20.shift();x['retest_low']=x.lo20.shift()
    higher=14400000 if step<14400000 else DAY if step<DAY else 7*DAY
    h=aggregate(raw,step,higher)
    h['hfast']=h.close.ewm(span=12,adjust=False,min_periods=26).mean()
    h['hslow']=h.close.ewm(span=26,adjust=False,min_periods=26).mean()
    # Context only becomes available at the HIGHER bar's completed close.
    x=pd.merge_asof(x,h.assign(available=h.ts+higher)[['available','hfast','hslow']],on='available')
    btc=features(btc_raw,step) if btc_context is None else btc_context.copy(deep=False)
    btc['btc_direction']=np.sign(btc.close-btc.ema50)
    btc['btc_move24h']=btc.close.pct_change(max(1,DAY//step))
    return pd.merge_asof(x,btc[['available','btc_direction','btc_move24h']],on='available')

def signal(records,i,strategy):
    if strategy in BASELINES:return baseline_signal(records,i,strategy)
    if strategy not in NEW:raise ValueError('unknown phase3 strategy')
    if i<80:return 0,0.
    a,b=records[i],records[i-1]
    if not all(math.isfinite(v) for v in (a.atr_fraction,a.hfast,a.hslow,a.btc_move24h,a.width_reference)):
        return 0,0.
    if not RULES.atr_min<=a.atr_fraction<=RULES.atr_max or abs(a.btc_move24h)>=RULES.btc_shock_24h:
        return 0,0.
    up=a.close>a.ema50 and a.hfast>a.hslow
    down=a.close<a.ema50 and a.hfast<a.hslow
    side=0
    if strategy=='BREAKOUT_RETEST':
        if a.prior_break_up and a.low<=a.retest_high+RULES.retest_atr*a.atr and a.close>a.retest_high and a.close>a.open and up:side=1
        if a.prior_break_down and a.high>=a.retest_low-RULES.retest_atr*a.atr and a.close<a.retest_low and a.close<a.open and down:side=-1
    elif strategy=='MOMENTUM_CONTINUATION':
        if b.close<b.fast and a.close>a.fast and a.macd>a.signal and up:side=1
        if b.close>b.fast and a.close<a.fast and a.macd<a.signal and down:side=-1
    elif strategy=='VOL_SQUEEZE':
        if b.width20<RULES.squeeze_ratio*b.width_reference and a.volume>RULES.volume_ratio*a.vol20:
            if a.close>a.hi20 and up:side=1
            if a.close<a.lo20 and down:side=-1
    elif strategy=='SR_REVERSAL':
        if a.low<a.lo20 and a.close>a.lo20 and a.close>a.open and a.hfast>=a.hslow:side=1
        if a.high>a.hi20 and a.close<a.hi20 and a.close<a.open and a.hfast<=a.hslow:side=-1
    elif strategy=='MTF_TREND':
        if b.macd<=b.signal and a.macd>a.signal and up:side=1
        if b.macd>=b.signal and a.macd<a.signal and down:side=-1
    # All NEW strategies require completed BTC trend agreement; no per-coin tuning.
    if side and side!=a.btc_direction:return 0,0.
    return side, float(abs(a.macd-a.signal)/a.atr) if side else 0.

def plan(strategy,prior,entry,side):
    if strategy in BASELINES:return structure_plan(prior,entry,side)
    if strategy not in NEW:raise ValueError('unknown plan')
    stop=min(prior.lo5,entry-RULES.stop_atr*prior.atr) if side==1 else max(prior.hi5,entry+RULES.stop_atr*prior.atr)
    width=prior.hi55-prior.lo55
    extreme=prior.hi55 if side==1 else prior.lo55
    if strategy=='SR_REVERSAL':
        tp1=prior.hi20 if side==1 else prior.lo20
        tp2=extreme
    else:
        tp1=extreme+side*RULES.target_extension1*width
        tp2=extreme+side*RULES.target_extension2*width
    try:return Plan(entry,stop,tp1,tp2,side).validate()
    except ValueError:return None
