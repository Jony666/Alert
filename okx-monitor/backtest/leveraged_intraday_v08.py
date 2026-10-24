#!/usr/bin/env python3
"""V08 / RESEARCH ONLY. 5m/15m OKX USDT perpetual short-horizon strategy and isolated
leverage stress simulations. NO API keys, orders, notifications, live scanner writes.

Three independent crypto RESEARCH proxies, NOT authentic BNF instructions:
  BIAS25_REVERSAL: prior 25 completed UTC+8 days mean/std & 5/15m momentum turn;
  MACD_TREND: 12-26-9 MACD cross and completed 30m+1h EMA filters;
  RANGE_VOLUME: prior 20 bar breakout, volume + completed 30m/1h EMA filters.

Fixed historical cross-asset basket: NOT historical market-cap top 200 selection.
All fills derived from 5m/15m OHLC estimates, not order-book or actual margin records.
Liquidation is an *adversarial approximation*, not OKX's mark-price/tiered model.
"""
from __future__ import annotations
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys
import time
from dataclasses import dataclass
import requests
import numpy as np
import pandas as pd

API='https://www.okx.com/api/v5/market/history-candles'
DAY=86_400_000
H8=28_800_000
STRATEGIES=('BIAS25_REVERSAL','MACD_TREND','RANGE_VOLUME')
SYMBOLS=('BTC','ETH','SOL','XRP','ADA','DOGE','LINK','AVAX','LTC','BCH','DOT','TRX')
BARS={'5m':300_000,'15m':900_000}
LEVERAGES=(5,10,20)
COLUMNS=('ts','open','high','low','close','volume')


def ts_utc(text):
    d=datetime.fromisoformat(text.replace('Z','+00:00'))
    if d.tzinfo is None or d.utcoffset().total_seconds()!=0:raise ValueError('must be explicit UTC')
    return int(d.timestamp()*1000)


def iso(t):return datetime.fromtimestamp(int(t)/1000,timezone.utc).isoformat()


def validate(raw,step,start=None,end=None):
    if raw.empty:raise ValueError('empty history')
    if not np.isfinite(raw[list(COLUMNS)].to_numpy(dtype=float)).all():raise ValueError('nonfinite OHLCV')
    stamp=raw.ts.to_numpy(dtype=np.int64)
    if np.any(np.diff(stamp)!=step) or np.any(stamp%step):raise ValueError('missing/duplicate/misaligned candles')
    if ((raw[['open','high','low','close']]<=0).any().any() or (raw.volume<0).any()
        or (raw.high<raw[['open','close','low']].max(axis=1)).any()
        or (raw.low>raw[['open','close','high']].min(axis=1)).any()):raise ValueError('invalid OHLCV')
    if start is not None and (int(stamp[0])!=start or int(stamp[-1])+step!=end or len(stamp)!=(end-start)//step):
        raise ValueError('not full requested calendar interval')
    return True


def parse_page(rows,start,end):
    out=[]
    for row in rows:
        if len(row)<9 or str(row[8])!='1':continue
        t=int(row[0]);o,h,l,c,v=map(float,row[1:6])
        if start<=t<end:out.append((t,o,h,l,c,v))
    return out


def download(inst,bar,start,end,pause=.2,session=None,max_pages=1500):
    """OKX `after` is strictly older than provided timestamp. Fail on partial or gaps."""
    step=BARS[bar]
    if start%step or end%step:raise ValueError('unaligned history window')
    if session is None:session=requests.Session()
    after=None;records={};pages=0
    while True:
        params={'instId':inst,'bar':bar,'limit':'300'}
        if after is not None:params['after']=str(after)
        for retry in range(4):
            try:
                r=session.get(API,params=params,timeout=(8,30))
                r.raise_for_status();payload=r.json()
                if str(payload.get('code'))!='0':raise RuntimeError('OKX response '+str(payload.get('code'))+' '+str(payload.get('msg')))
                rows=payload['data'];break
            except (requests.RequestException,ValueError,KeyError,RuntimeError) as e:
                if retry==3:raise RuntimeError(f'{inst} {bar} OKX download failed: {e}') from e
                time.sleep(1.0+2.0*retry)
        if not rows:break
        pages+=1
        if pages>max_pages:raise RuntimeError('pagination budget exceeded')
        for x in parse_page(rows,start,end):
            if x[0] in records and records[x[0]]!=x:raise ValueError('conflicting duplicate candle')
            records[x[0]]=x
        oldest=min(int(x[0]) for x in rows)
        if oldest<=start:break
        if after is not None and oldest>=after:raise RuntimeError('stalled OKX after pagination')
        after=oldest
        if pages%50==0:print('PAGES',inst,bar,pages,'bars',len(records),flush=True)
        time.sleep(pause)
    if not records:raise RuntimeError('no confirmed OHLCV')
    raw=pd.DataFrame([records[k] for k in sorted(records)],columns=COLUMNS)
    validate(raw,step,start,end)
    return raw


def aggregate(raw,step,mult):
    """Discard incomplete higher-timeframe buckets; return timestamps at period OPEN."""
    frame=raw.copy()
    frame['bucket']=(frame.ts//(step*mult))*(step*mult)
    rows=[]
    for t,g in frame.groupby('bucket',sort=True):
        if len(g)!=mult or int(g.ts.iloc[0])!=t or int(g.ts.iloc[-1])!=t+step*(mult-1):continue
        rows.append((int(t),float(g.open.iloc[0]),float(g.high.max()),float(g.low.min()),float(g.close.iloc[-1]),float(g.volume.sum())))
    return pd.DataFrame(rows,columns=COLUMNS)


def atr(df,n=14):
    c=df.close.shift(1)
    tr=pd.concat([df.high-df.low,(df.high-c).abs(),(df.low-c).abs()],axis=1).max(axis=1)
    return tr.ewm(alpha=1/n,adjust=False,min_periods=n).mean()


def rsi(df,n=14):
    dx=df.close.diff();up=dx.clip(lower=0).ewm(alpha=1/n,adjust=False,min_periods=n).mean()
    dn=(-dx.clip(upper=0)).ewm(alpha=1/n,adjust=False,min_periods=n).mean()
    rs=up/dn.replace(0,np.nan)
    return (100-100/(1+rs)).mask((dn==0)&(up>0),100).mask((up==0)&(dn>0),0).fillna(50)


def attach_completed_context(raw,step):
    """At each base bar close, only use 1h / UTC+8 1D bars that ALREADY closed."""
    x=raw.copy().reset_index(drop=True)
    if len(x)<80:raise ValueError('too few candles')
    fast=x.close.ewm(span=12,adjust=False,min_periods=26).mean()
    slow=x.close.ewm(span=26,adjust=False,min_periods=26).mean()
    x['macd']=fast-slow
    x['macd_sig']=x.macd.ewm(span=9,adjust=False,min_periods=9).mean()
    x['atr']=atr(x);x['rsi14']=rsi(x)
    x['vol20']=x.volume.shift(1).rolling(20,min_periods=20).mean()
    x['hi20']=x.high.shift(1).rolling(20,min_periods=20).max()
    x['lo20']=x.low.shift(1).rolling(20,min_periods=20).min()
    half=aggregate(raw,step,1_800_000//step)
    half['ma12']=half.close.ewm(span=12,adjust=False,min_periods=12).mean()
    half['ma26']=half.close.ewm(span=26,adjust=False,min_periods=26).mean()
    half_end=half.ts.to_numpy(dtype=np.int64)+1_800_000
    half_idx=np.searchsorted(half_end,x.ts.to_numpy(dtype=np.int64)+step,side='right')-1
    for name in ('ma12','ma26'):
        a=np.full(len(x),np.nan)
        good=half_idx>=0
        a[good]=half[name].to_numpy()[half_idx[good]]
        x['m30_'+name]=a
    hour=aggregate(raw,step,3_600_000//step)
    hour['ma20']=hour.close.ewm(span=20,adjust=False,min_periods=20).mean()
    hour['ma50']=hour.close.ewm(span=50,adjust=False,min_periods=50).mean()
    base_ends=x.ts.to_numpy(dtype=np.int64)+step
    hour_end=hour.ts.to_numpy(dtype=np.int64)+3_600_000
    hidx=np.searchsorted(hour_end,base_ends,side='right')-1
    for name in ('ma20','ma50'):
        a=np.full(len(x),np.nan)
        good=hidx>=0
        a[good]=hour[name].to_numpy()[hidx[good]]
        x['h1_'+name]=a
    # Daily UTC+8 buckets start 16:00 UTC; use day that has closed before base bar close.
    daily=raw.copy();daily['bucket']=((daily.ts+28_800_000)//DAY)*DAY-28_800_000
    d=[];perday=DAY//step
    for t,g in daily.groupby('bucket',sort=True):
        if len(g)==perday and int(g.ts.iloc[0])==t and int(g.ts.iloc[-1])==t+DAY-step:
            d.append((int(t),float(g.close.iloc[-1])))
    dc=pd.DataFrame(d,columns=['ts','close'])
    if dc.empty:
        x['bias25']=np.nan;x['z25']=np.nan
    else:
        dc['ma25']=dc.close.rolling(25,min_periods=25).mean()
        dc['sd25']=dc.close.rolling(25,min_periods=25).std()
        di=np.searchsorted(dc.ts.to_numpy(dtype=np.int64)+DAY,base_ends,side='right')-1
        ma=np.full(len(x),np.nan);sd=np.full(len(x),np.nan)
        ok=di>=0
        ma[ok]=dc.ma25.to_numpy()[di[ok]];sd[ok]=dc.sd25.to_numpy()[di[ok]]
        x['bias25']=(x.close/ma-1)*100
        x['z25']=(x.close-ma)/np.where(sd>0,sd,np.nan)
    return x


def identify_signal(df,i,strategy):
    """Signal uses base bar i (closed). Executor can only enter base bar i+1."""
    if strategy not in STRATEGIES:raise ValueError('unknown strategy')
    if i<55:return (0,0.)
    a=df.iloc[i];b=df.iloc[i-1]
    if not (math.isfinite(float(a.atr)) and a.atr>0 and math.isfinite(float(a.h1_ma50)) and math.isfinite(float(a.m30_ma26))):return (0,0.)
    if a.atr/a.close<.001:return (0,0.)
    crossup=b.macd<=b.macd_sig and a.macd>a.macd_sig
    crossdn=b.macd>=b.macd_sig and a.macd<a.macd_sig
    rising=(a.macd-a.macd_sig)>(b.macd-b.macd_sig)
    falling=(a.macd-a.macd_sig)<(b.macd-b.macd_sig)
    if strategy=='BIAS25_REVERSAL':
        if math.isfinite(float(a.z25)):
            if a.bias25<=-2.5 and a.z25<=-1.5 and a.rsi14<45 and rising and b.rsi14<a.rsi14:
                return (1,float(abs(a.z25)))
            if a.bias25>=2.5 and a.z25>=1.5 and a.rsi14>55 and falling and b.rsi14>a.rsi14:
                return (-1,float(abs(a.z25)))
    elif strategy=='MACD_TREND':
        if crossup and a.h1_ma20>a.h1_ma50 and a.m30_ma12>a.m30_ma26 and 40<a.rsi14<75:return (1,float(abs(a.macd-a.macd_sig)/a.atr))
        if crossdn and a.h1_ma20<a.h1_ma50 and a.m30_ma12<a.m30_ma26 and 25<a.rsi14<60:return (-1,float(abs(a.macd-a.macd_sig)/a.atr))
    elif strategy=='RANGE_VOLUME':
        if not math.isfinite(float(a.vol20)) or a.vol20<=0 or a.volume<1.25*a.vol20:return (0,0.)
        if a.close>a.hi20 and a.h1_ma20>a.h1_ma50 and a.m30_ma12>a.m30_ma26:return (1,float((a.close-a.hi20)/a.atr))
        if a.close<a.lo20 and a.h1_ma20<a.h1_ma50 and a.m30_ma12<a.m30_ma26:return (-1,float((a.lo20-a.close)/a.atr))
    else:raise ValueError('unknown strategy')
    return (0,0.)


def precompute_signals(df):
    signals={}
    for strat in STRATEGIES:
        side=np.zeros(len(df),dtype=np.int8);score=np.zeros(len(df),dtype=float)
        for i in range(55,len(df)):
            side[i],score[i]=identify_signal(df,i,strat)
        signals[strat]=(side,score)
    return signals


def bounds_for_range(start,end,warmup_days):
    after=start+warmup_days*DAY
    if after>=end:raise ValueError('warmup exceeds history')
    span=end-after
    return {'train':(after,after+int(.6*span)),
            'validation':(after+int(.6*span),after+int(.8*span)),
            'test_not_pristine':(after+int(.8*span),end)}


@dataclass
class Position:
    symbol:str
    side:int
    entry:float
    entry_ts:int
    units:float
    notional:float
    entry_fee:float
    atr:float
    stop:float
    target:float
    liquidation:float
    next_funding_ts:int
    funding_paid:float=0.


def liq_price(entry,side,leverage,maintenance=.005,extra=.001):
    """Approx isolated liquidation threshold only, NOT official OKX liquidation."""
    distance=1/leverage-maintenance-extra
    if distance<=0:raise ValueError('no liquidation buffer')
    return entry*(1-side*distance)


def exit_quote(p,opening,hi,lo,slip):
    """Adversarial intrabar sequence: liquidation > stop > target; gap adverse."""
    liquidated=(lo<=p.liquidation if p.side==1 else hi>=p.liquidation)
    stopped=(lo<=p.stop if p.side==1 else hi>=p.stop)
    target=(hi>=p.target if p.side==1 else lo<=p.target)
    if liquidated:level=p.liquidation;reason='approx_liquidation'
    elif stopped:level=p.stop;reason='protective_stop'
    elif target:level=p.target;reason='profit_target'
    else:return None
    if reason=='profit_target':
        fill=max(opening,level) if p.side==1 else min(opening,level)
    else:
        fill=min(opening,level) if p.side==1 else max(opening,level)
    return fill*(1-p.side*slip),reason


def trade_stats(trades):
    rets=np.asarray([z['net_return_pct_of_notional'] for z in trades],dtype=float)
    if not len(rets):return {'trades':0,'win_rate':None,'avg_trade_pct':None,'profit_factor':None}
    pos=float(rets[rets>0].sum());neg=-float(rets[rets<0].sum())
    return {'trades':len(rets),'win_rate':round(float(np.mean(rets>0)),4),
            'avg_trade_pct':round(float(rets.mean()),4),'profit_factor':round(pos/neg,4) if neg>0 else None}


def simulate(markets,strategy,leverage,split,span,step,args,cost_mult=1.):
    if strategy not in STRATEGIES or leverage not in LEVERAGES:raise ValueError('unsupported research scenario')
    symbols=sorted(markets)
    ts=markets[symbols[0]]['frame'].ts.to_numpy(dtype=np.int64)
    for sym in symbols[1:]:
        if not np.array_equal(ts,markets[sym]['frame'].ts.to_numpy(dtype=np.int64)):
            raise ValueError('multi-asset time axis misalignment')
    a=int(np.searchsorted(ts,span[0]));b=int(np.searchsorted(ts,span[1]));
    if b<=a:raise ValueError('empty split')
    fee=args.fee*cost_mult;slip=args.slippage*cost_mult
    wallet=float(args.initial_equity_twd);peak=wallet;maxdd=0.;halted=False;halt_next=False
    poss={};trades=[];curve=[];counters=Counter();maxopen=0;maxgross=0.;max_margin_frac=0.;max_risk=0.
    holdmax=int(args.hold_hours*3_600_000//step)
    def equity(idx,field='close'):
        return wallet+sum(p.units*p.side*(float(markets[s]['frame'][field].iloc[idx])-p.entry) for s,p in poss.items())
    def close(s,j,px,reason,when):
        nonlocal wallet
        p=poss.pop(s)
        exitfee=p.units*px*fee
        gain=p.units*p.side*(px-p.entry)
        wallet+=gain-exitfee
        net=gain-p.entry_fee-exitfee-p.funding_paid
        trades.append({'strategy':strategy,'leverage':leverage,'cost_mult':cost_mult,
                       'split':split,'symbol':s,'side':'LONG' if p.side==1 else 'SHORT',
                       'entry_ts':p.entry_ts,'exit_ts':when,'entry':p.entry,'exit':px,
                       'margin_twd_equivalent':p.notional/leverage,'notional_twd_equivalent':p.notional,
                       'pnl_twd_equivalent':net,'net_return_pct_of_notional':100*net/p.notional,
                       'funding_twd_equivalent':p.funding_paid,'reason':reason})
        counters['exit_'+reason]+=1
    for j in range(a,b):
        t=int(ts[j]);exited=set()
        if halt_next:
            # Account drawdown is observed at a completed candle close. Execute
            # *next* candle open, never pretend we knew the exit earlier.
            for sym,p in sorted(list(poss.items())):
                opening=float(markets[sym]['frame'].open.iloc[j])
                close(sym,j,opening*(1-p.side*slip),'portfolio_drawdown_halt',t)
                exited.add(sym)
            halted=True;halt_next=False
        # Funding is a conservative nonnegative charge to BOTH long and short positions.
        for s,p in list(poss.items()):
            while t+step>=p.next_funding_ts:
                funding=p.notional*args.funding
                p.funding_paid+=funding;wallet-=funding;p.next_funding_ts+=H8
        for s,p in sorted(list(poss.items())):
            q=markets[s]['frame'].iloc[j]
            event=exit_quote(p,float(q.open),float(q.high),float(q.low),slip)
            if event:
                close(s,j,event[0],event[1],t+step);exited.add(s)
            elif t-p.entry_ts>=holdmax*step:
                close(s,j,float(q.open)*(1-p.side*slip),'max_hold',t);exited.add(s)
        # No recycled funds/capacity from same 5/15m bar exits.
        if not halted and not exited and j>a and j>0:
            eq=equity(j,'open')
            if eq>0:
                candidates=[]
                for s in symbols:
                    if s in poss:continue
                    dirs,scores=markets[s]['signals'][strategy]
                    direction=int(dirs[j-1]);strength=float(scores[j-1])
                    if direction and strength>0:
                        candidates.append((-strength,s,direction))
                # Rankings are purely based on strategy strength, never symbol fame.
                for _,s,side in sorted(candidates):
                    if len(poss)>=args.max_positions:
                        counters['position_limit']+=1;break
                    eq=equity(j,'open')
                    if eq<=0:break
                    r=markets[s]['frame'].iloc[j];prior=markets[s]['frame'].iloc[j-1]
                    if not math.isfinite(float(prior.atr)) or prior.atr<=0:continue
                    entry=float(r.open)*(1+side*slip)
                    stop=entry-side*1.6*float(prior.atr)
                    target=entry+side*2.6*float(prior.atr)
                    riskdist=abs(stop-entry)/entry
                    if not (.002<=riskdist<=.12 and min(entry,stop,target)>0):
                        counters['invalid_stop']+=1;continue
                    liquidation=liq_price(entry,side,leverage)
                    # Reject trades where the liquidation barrier is nearer than stop.
                    if (side==1 and liquidation>=stop) or (side==-1 and liquidation<=stop):
                        counters['liquidation_before_stop_rejected']+=1;continue
                    riskheld=sum(p.units*abs(p.entry-p.stop) for p in poss.values())
                    gross=sum(p.notional for p in poss.values())
                    notional=min(eq*args.risk_fraction/riskdist,
                                 max(0,eq*args.portfolio_risk_cap-riskheld)/riskdist,
                                 eq*args.symbol_gross_cap,
                                 max(0,eq*args.portfolio_gross_cap-gross),
                                 max(0,eq*args.margin_utilization*leverage-gross))
                    if notional<eq*.05:
                        counters['risk_margin_cap_rejected']+=1;continue
                    unit=notional/entry
                    entryfee=notional*fee;wallet-=entryfee
                    poss[s]=Position(s,side,entry,t,unit,notional,entryfee,float(prior.atr),stop,target,
                                     liquidation,t+H8)
                    counters['entries']+=1
                    # If initial entry 5/15m bar touches adverse stop/approx liquidation, reject any
                    # benefit of intrabar order of events: close at WORST touched threshold.
                    event=exit_quote(poss[s],float(r.open),float(r.high),float(r.low),slip)
                    if event:
                        px,reason=event
                        close(s,j,px,'entry_bar_'+reason,t+step)
                        # Intrabar chronology unknown: do not reuse capacity freed by a
                        # new position stopped in the same bar for another symbol.
                        break
        if j==b-1:
            for s,p in sorted(list(poss.items())):
                close(s,j,float(markets[s]['frame'].close.iloc[j])*(1-p.side*slip),'split_end',t+step)
        eq=equity(j)
        peak=max(peak,eq)
        dd=max(0.,1-eq/peak) if peak>0 else 1.
        maxdd=max(maxdd,dd)
        if (dd>=args.dd_halt or eq<=0) and not halted and j<b-1:
            halt_next=True
        gross=sum(p.notional for p in poss.values())
        margin=sum(p.notional/leverage for p in poss.values())
        risk=sum(p.units*abs(p.entry-p.stop) for p in poss.values())
        maxopen=max(maxopen,len(poss));maxgross=max(maxgross,gross/max(eq,1e-9))
        max_margin_frac=max(max_margin_frac,margin/max(eq,1e-9));max_risk=max(max_risk,risk/max(eq,1e-9))
        curve.append({'strategy':strategy,'leverage':leverage,'cost_mult':cost_mult,'split':split,
                      'ts':t+step,'equity_twd_equivalent':round(eq,5),'drawdown_pct':round(dd*100,5),
                      'positions':len(poss)})
        if eq<=0:
            halted=True
    if poss:raise AssertionError('split leaked positions')
    row={'strategy':strategy,'leverage':leverage,'cost_mult':cost_mult,'split':split,
         **trade_stats(trades),'account_return_pct':round(100*(wallet/args.initial_equity_twd-1),4),
         'max_drawdown_pct':round(100*maxdd,4),'max_open_positions':maxopen,
         'peak_gross_exposure_x_equity':round(maxgross,4),
         'peak_margin_fraction_of_equity':round(max_margin_frac,4),
         'peak_initial_stop_risk_fraction':round(max_risk,4),
         'liquidation_events':sum(v for k,v in counters.items() if 'liquidation' in k and k.startswith('exit_')),
         'halted':halted,'exit_counts':dict(counters)}
    return row,trades,curve


def run(args,loader=None):
    if args.bar not in BARS:raise ValueError('unsupported base bar')
    if args.days<50 or args.warmup_days<30 or args.days<=args.warmup_days+20:raise ValueError('insufficient warmup/history')
    if not 0<args.initial_equity_twd or not 0<args.risk_fraction<=.03 or not 0<args.dd_halt<=.5:
        raise ValueError('invalid capital/risk settings')
    if not (0<=args.fee<=.01 and 0<=args.slippage<=.01 and 0<=args.funding<=.01):raise ValueError('invalid costs')
    if not 0<=args.pause<=3 or args.max_positions<1 or args.hold_hours<1:raise ValueError('invalid execution params')
    if not 0<args.margin_utilization<=.8 or not 0<args.portfolio_risk_cap<=.1:
        raise ValueError('unsafe research limits')
    if len(set(args.symbols))!=len(args.symbols):raise ValueError('duplicate symbols')
    if args.min_covered<1 or args.min_covered>len(args.symbols):raise ValueError('invalid coverage target')
    end=ts_utc(args.end_utc);step=BARS[args.bar];start=end-args.days*DAY
    if end>int(time.time()*1000):raise ValueError('end date in future')
    if start%step or end%step:raise ValueError('date must align with bar boundaries')
    out=Path(args.output);out.parent.mkdir(parents=True,exist_ok=True)
    cache=Path(args.cache);cache.mkdir(parents=True,exist_ok=True)
    if loader is None:loader=download
    coverage=[];failures={};markets={}
    for sym in args.symbols:
        inst=sym.upper() if sym.endswith('-USDT-SWAP') else sym.upper()+'-USDT-SWAP'
        c=cache/f'{inst}_{args.bar}_{start}_{end}.csv'
        try:
            if c.exists():raw=pd.read_csv(c)
            else:raw=loader(inst,args.bar,start,end,args.pause)
            validate(raw,step,start,end)
            if not c.exists():raw.to_csv(c,index=False)
            frame=attach_completed_context(raw,step)
            signals=precompute_signals(frame)
            markets[inst]={'frame':frame,'signals':signals}
            coverage.append({'symbol':inst,'bars':len(raw),'first':iso(start),'last':iso(end-step)})
            print('VERIFIED',inst,args.bar,len(raw),flush=True)
        except Exception as e:
            failures[inst]=f'{type(e).__name__}: {e}'
            print('FAILED',inst,failures[inst],flush=True)
    trades=[];equity=[];rows=[];failed_portfolios={}
    splits=bounds_for_range(start,end,args.warmup_days)
    if len(markets)>=args.min_covered:
        for strategy in STRATEGIES:
            for lev in LEVERAGES:
                for cost_mult in (1.0,2.0):
                    for split,range_ in splits.items():
                        try:
                            row,ledger,path=simulate(markets,strategy,lev,split,range_,step,args,cost_mult)
                            rows.append(row);trades.extend(ledger);equity.extend(path)
                            print('RESULT',args.bar,strategy,lev,'cost',cost_mult,split,
                                  'trades',row['trades'],'ret%',row['account_return_pct'],
                                  'dd%',row['max_drawdown_pct'],flush=True)
                        except Exception as e:
                            failed_portfolios[f'{strategy}|{lev}|{cost_mult}|{split}']=str(e)
                            print('PORTFOLIO_FAIL',strategy,lev,cost_mult,split,str(e),flush=True)
    complete=len(markets)>=args.min_covered and not failed_portfolios and len(rows)==len(STRATEGIES)*len(LEVERAGES)*2*3
    pd.DataFrame(rows).drop(columns='exit_counts',errors='ignore').to_csv(out.with_suffix('.summary.csv'),index=False)
    pd.DataFrame(trades,columns=['strategy','leverage','cost_mult','split','symbol','side','entry_ts','exit_ts','entry','exit','margin_twd_equivalent','notional_twd_equivalent','pnl_twd_equivalent','net_return_pct_of_notional','funding_twd_equivalent','reason']).to_csv(out.with_suffix('.trades.csv'),index=False)
    pd.DataFrame(equity,columns=['strategy','leverage','cost_mult','split','ts','equity_twd_equivalent','drawdown_pct','positions']).to_csv(out.with_suffix('.equity.csv'),index=False)
    report={'status':'EXPLORATORY_ONLY_NO_LIVE_ORDERS_NO_PUSH','ready_for_live':False,
            'run':{'bar':args.bar,'days':args.days,'end_utc':args.end_utc,'warmup_days':args.warmup_days,
                   'symbols':args.symbols,'min_covered':args.min_covered,'initial_equity_twd':args.initial_equity_twd,
                   'fee_per_side':args.fee,'slippage_per_side':args.slippage,'funding_per_8h':args.funding,
                   'research_leverages':LEVERAGES,'risk_fraction':args.risk_fraction,
                   'portfolio_risk_cap':args.portfolio_risk_cap,'max_positions':args.max_positions,
                   'margin_utilization':args.margin_utilization},
            'splits_utc':{k:[iso(t) for t in b] for k,b in splits.items()},
            'coverage':coverage,'failed_symbols':failures,'failed_portfolios':failed_portfolios,
            'results':rows,'complete':complete,
            'limitations':['This is a static asset basket, NOT dynamically historically reconstructed top 200. Survivorship risk.',
                           'Only confirmed OHLCV; intrabar order sequence unknown and stop/liquidation prioritized.',
                           'No real OKX mark price, tiered maintenance margin, liquidation fees, or ADL; liquidation is approximation.',
                           'Returns are in TWD-equivalent starting-capital units under a frozen FX assumption, NOT actual USDT funds.',
                           'Per-trade fee/slippage and worst-case funding are assumed and static, NOT historical exchange fees/funding.',
                           'Trade size constrained by risk budget and margin; leverage does NOT multiply free profits.',
                           'Temporal test is NOT an unseen virgin out-of-sample period; strategy multiple-testing can overfit.',
                           'Realistic orderbook depth, partial fills, cross-instrument simultaneous ticks, venue outage absent.',
                           'No real exchange orders, live scanner changes, API credentials, or push notifications.']}
    out.write_text(json.dumps(report,ensure_ascii=False,allow_nan=False,indent=2)+'\n',encoding='utf-8')
    print('FINAL',json.dumps({'bar':args.bar,'valid_symbols':len(markets),'failed_symbols':len(failures),
                              'portfolios':len(rows),'total_simulated_trades':len(trades),
                              'complete':complete,'ready_for_live':False}),flush=True)
    return 0 if complete else 2


def parser():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--symbols',nargs='+',default=list(SYMBOLS))
    p.add_argument('--bar',choices=list(BARS),default='15m')
    p.add_argument('--days',type=int,default=180)
    p.add_argument('--warmup-days',type=int,default=35)
    p.add_argument('--end-utc',default='2026-10-01T00:00:00Z')
    p.add_argument('--min-covered',type=int,default=8)
    p.add_argument('--initial-equity-twd',type=float,default=10000.0)
    p.add_argument('--risk-fraction',type=float,default=.01)
    p.add_argument('--portfolio-risk-cap',type=float,default=.035)
    p.add_argument('--symbol-gross-cap',type=float,default=3.0)
    p.add_argument('--portfolio-gross-cap',type=float,default=8.0)
    p.add_argument('--margin-utilization',type=float,default=.45)
    p.add_argument('--max-positions',type=int,default=3)
    p.add_argument('--dd-halt',type=float,default=.25)
    p.add_argument('--hold-hours',type=float,default=8.)
    p.add_argument('--fee',type=float,default=.0005)
    p.add_argument('--slippage',type=float,default=.0005)
    p.add_argument('--funding',type=float,default=.0001)
    p.add_argument('--pause',type=float,default=.2)
    p.add_argument('--cache',default='research_v08_cache')
    p.add_argument('--output',default='v08_15m.json')
    return p

if __name__=='__main__':
    try:raise SystemExit(run(parser().parse_args()))
    except Exception as e:
        print('FAIL_CLOSED_V08',type(e).__name__,str(e),file=sys.stderr)
        raise SystemExit(2)
