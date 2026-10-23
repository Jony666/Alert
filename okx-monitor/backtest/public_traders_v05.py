#!/usr/bin/env python3
"""Research-only OKX adaptations of publicly described Crabel, Turtle and Minervini ideas.

Do not misrepresent crypto proxies as authors' exact original strategies.
No order execution, alerts, private keys or live-scanner changes.
Requires trader_strategies_v04.py for audited OKX pagination, alignment and ATR.
"""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys
import time

import numpy as np
import pandas as pd
import trader_strategies_v04 as base

MS30 = base.MS30
DAY = base.MSDAY
SOURCES = {
    'CRABEL_ORB_2026': 'https://tobycrabel.substack.com/p/opening-range-breakout-a-century',
    'CRABEL_OLD_STRETCH': 'https://www.yumpu.com/en/document/view/55378416/stocks-commodities-toby-crabel-orb',
    'TURTLE_S1_S2': 'https://tradingblox.com/originalturtles/originalturtlerules.htm',
    'MINERVINI_TREND': 'https://www.marketinout.com/stock-screener/minervini-trend-template',
}
STRATEGIES = ('CRABEL_2026_ORB','CRABEL_NR7_STRETCH',
              'TURTLE_S1_20_10_NO_SKIP','TURTLE_S2_55_20_NO_PYRAMID',
              'MINERVINI_TREND_VCP_PROXY')
# Specific limitations: S1 winning-breakout skip, Turtle pyramiding, correlated
# portfolio sizing omitted; IBD equity-universe relative-strength rankings,
# discretionary VCP interpretation and fundamentals omitted in Minervini proxy.


def net_return(entry:float,exit_price:float,side:int,fee:float,funding:float,hold_ms:int)->float:
    if entry<=0 or exit_price<=0 or side not in (-1,1) or hold_ms<0:
        raise ValueError('Invalid trade economics')
    funding_payments=max(1,math.ceil(hold_ms/(8*60*60*1000)))
    return 100*(side*(exit_price-entry)/entry - fee*(entry+exit_price)/entry - funding*funding_payments)


def split_bounds(start:int,end:int,warmup_days:int):
    return base.split_ranges(start,end,warmup_days)


def mark_split(ts:int,bounds:dict):return base.which_split(ts,bounds)


def clipped_market_order(open_price:float,trigger:float,side:int,slip:float)->float:
    """Never fill past a gap at the theoretical stop-order trigger."""
    return (max(open_price,trigger) if side==1 else min(open_price,trigger))*(1+side*slip)


def exit_on_level(open_price:float,level:float,side:int,slip:float)->float:
    """Conservative stop execution; adverse overnight gaps are not ignored."""
    return (min(open_price,level) if side==1 else max(open_price,level))*(1-side*slip)


def natr(daily:pd.DataFrame,period:int=20)->pd.Series:
    # Original Turtles used N, a 20-day exponential/modified TR average.
    # ATR Wilder(20) is a transparent approximation; document difference.
    return base.wilder_atr(daily,period)


def prep_daily(d:pd.DataFrame):
    x=d.copy().reset_index(drop=True)
    x['N20']=natr(x,20)
    for n in (10,20,55):
        x[f'hi{n}']=x.high.shift(1).rolling(n,min_periods=n).max()
        x[f'lo{n}']=x.low.shift(1).rolling(n,min_periods=n).min()
    for n in (20,50,150,200):
        x[f'sma{n}']=x.close.rolling(n,min_periods=n).mean()
    x['ema20']=x.close.ewm(span=20,adjust=False,min_periods=20).mean()
    x['atr14']=base.wilder_atr(x,14)
    x['low252']=x.low.rolling(252,min_periods=252).min()
    x['high252']=x.high.rolling(252,min_periods=252).max()
    return x


def day_bars(raw:pd.DataFrame,daily:pd.DataFrame):
    ts=raw.ts.to_numpy(dtype=np.int64)
    out={}
    for i,start in enumerate(daily.ts.to_numpy(dtype=np.int64)):
        lo=int(np.searchsorted(ts,start,side='left'))
        hi=int(np.searchsorted(ts,start+DAY,side='left'))
        if hi-lo!=48 or int(ts[lo])!=int(start) or int(ts[hi-1])!=int(start+DAY-MS30):
            raise ValueError(f'Incomplete daily intraday bucket at {start}')
        out[i]=(lo,hi)
    return out


def new_trade(strategy,inst,side,split,signal_ts,entry_ts,exit_ts,entry,exit_price,stop,reason,fee,funding):
    return {'strategy':strategy,'symbol':inst,'side':'LONG' if side==1 else 'SHORT',
            'split':split,'signal_utc':datetime.fromtimestamp(signal_ts/1000,timezone.utc).isoformat(),
            'entry_ts':int(entry_ts),'exit_ts':int(exit_ts),'entry':float(entry),
            'exit':float(exit_price),'stop':float(stop) if stop is not None else None,
            'net_return_pct':float(net_return(entry,exit_price,side,fee,funding,exit_ts-entry_ts)),
            'hold_hours':round((exit_ts-entry_ts)/(3600*1000),2),'exit_reason':reason}


def crabel_levels(daily:pd.DataFrame,i:int,variant:str):
    if i<11:return None
    prev=daily.iloc[i-10:i]
    if variant=='CRABEL_2026_ORB':
        # Literal 10-day HIGH-LOW range x0.8 of prior 10 completed sessions.
        # Author's published text says 'ten-day range'; range interpretation
        # is made explicit rather than assuming an unverified implementation.
        stretch=.8*(float(prev.high.max())-float(prev.low.min()))
    elif variant=='CRABEL_NR7_STRETCH':
        # Historical stretch = 10-day mean of min(H-O,O-L); optional NR7 filter.
        seven=daily.iloc[i-7:i]
        ranges=seven.high-seven.low
        if float(ranges.iloc[-1])>float(ranges.min())+1e-12:return None
        stretch=float(np.minimum(prev.high-prev.open,prev.open-prev.low).mean())
    else:raise ValueError('Invalid ORB strategy')
    op=float(daily.open.iloc[i]);return (op+stretch,op-stretch) if stretch>0 else None


def first_touch(bars:pd.DataFrame,upper:float,lower:float):
    """First intraday 30m trigger; reject bars touching BOTH directions."""
    for ix,row in bars.iterrows():
        long=bool(row.high>=upper);short=bool(row.low<=lower)
        if long and short:return (None,ix,'ambiguous_both_directions')
        if long:return (1,ix,None)
        if short:return (-1,ix,None)
    return (None,None,'not_triggered')


def run_crabel(raw,daily,slots,bounds,args,inst,variant):
    outcome=[];counter=Counter()
    for i in range(11,len(daily)-1):
        d0=int(daily.ts.iloc[i]); nxt=int(daily.ts.iloc[i+1]);split=mark_split(d0,bounds)
        if split is None:continue
        levels=crabel_levels(daily,i,variant)
        if levels is None:continue
        counter['setups']+=1
        s,e=slots[i];side,bar_id,error=first_touch(raw.iloc[s:e],*levels)
        if error:counter[error]+=1;continue
        counter['triggered']+=1
        if nxt>=bounds[split][1]:counter['purged_cross_split']+=1;continue
        row=raw.loc[bar_id];trigger=levels[0] if side==1 else levels[1]
        entry=clipped_market_order(float(row.open),trigger,side,args.slippage)
        exit_price=float(daily.open.iloc[i+1])*(1-side*args.slippage)
        if entry<=0 or exit_price<=0:counter['bad_price']+=1;continue
        outcome.append(new_trade(variant,inst,side,split,int(row.ts),int(row.ts),nxt,entry,exit_price,None,
                                 'next_day_open_no_protective_stop',args.fee,args.funding))
        counter['filled']+=1
    return outcome,counter


def turtle_signal_from_bar(high:float,low:float,buy:float,sell:float):
    if not np.isfinite([high,low,buy,sell]).all():return (0,False)
    up=high>=buy;down=low<=sell
    if up and down:return (0,True)
    return (1 if up else -1 if down else 0,False)


def simulate_turtle(raw,daily,slots,entry_day,entry_bar,side,entry_price,stop,n_days,exit_lookback,max_hold_days,args):
    # Uses only the prior COMPLETED daily channel for each intraday candle.
    tail=min(len(daily)-1,entry_day+max_hold_days)
    for d_i in range(entry_day,tail+1):
        s,e=slots[d_i]
        for j in range(max(s,entry_bar),e):
            row=raw.iloc[j]
            exit_level=float(daily[f'lo{exit_lookback}' if side==1 else f'hi{exit_lookback}'].iloc[d_i])
            stop_hit=(float(row.low)<=stop if side==1 else float(row.high)>=stop)
            channel_hit=(float(row.low)<=exit_level if side==1 else float(row.high)>=exit_level)
            if stop_hit or channel_hit:
                # Both thresholds in one candle: protective stop has priority.
                px=exit_on_level(float(row.open),stop if stop_hit else exit_level,side,args.slippage)
                return (int(row.ts),px,d_i,'protective_2N_stop' if stop_hit else 'channel_exit')
        if d_i==entry_day+max_hold_days-1 and d_i+1<len(daily):
            next_open=int(daily.ts.iloc[d_i+1])
            px=float(daily.open.iloc[d_i+1])*(1-side*args.slippage)
            return (next_open,px,d_i+1,'max_hold_research_exit')
    return None


def run_turtle(raw,daily,slots,bounds,args,inst,entry_lb,exit_lb,tag):
    outcome=[];ctr=Counter();next_eligible_day=0
    for i in range(max(entry_lb,55),len(daily)-1):
        if i<next_eligible_day:continue
        date=int(daily.ts.iloc[i]);sp=mark_split(date,bounds)
        if sp is None:continue
        buy=float(daily[f'hi{entry_lb}'].iloc[i]);sell=float(daily[f'lo{entry_lb}'].iloc[i]);N=float(daily.N20.iloc[i-1])
        if not np.isfinite([buy,sell,N]).all() or N<=0:continue
        s,e=slots[i];side=bar_id=None
        for j in range(s,e):
            row=raw.iloc[j];direction,amb=turtle_signal_from_bar(float(row.high),float(row.low),buy,sell)
            if amb:ctr['ambiguous_double_breakout']+=1;break
            if direction:
                side=direction;bar_id=j;break
        if side is None:continue
        ctr['raw_breakout']+=1
        row=raw.iloc[bar_id];threshold=buy if side==1 else sell
        entry=clipped_market_order(float(row.open),threshold,side,args.slippage)
        stop=entry-side*2.0*N
        if min(entry,stop)<=0:ctr['invalid_stop']+=1;continue
        res=simulate_turtle(raw,daily,slots,i,bar_id,side,entry,stop,N,exit_lb,args.turtle_max_days,args)
        if res is None:ctr['right_censored']+=1;continue
        exit_ts,exit_px,exit_day,reason=res
        next_eligible_day=exit_day+1
        if exit_ts>=bounds[sp][1]:ctr['purged_cross_split']+=1;continue
        outcome.append(new_trade(tag,inst,side,sp,int(row.ts),int(row.ts),exit_ts,entry,exit_px,stop,reason,args.fee,args.funding));ctr['filled']+=1
    return outcome,ctr


def trend_template_7of8(d:pd.DataFrame,i:int)->bool:
    if i<253:return False
    a=d.iloc[i];past=d.iloc[i-20]
    vals=[a.close,a.sma50,a.sma150,a.sma200,past.sma200,a.low252,a.high252]
    if not np.isfinite(vals).all():return False
    return bool(a.close>a.sma50>a.sma150>a.sma200 and a.sma200>past.sma200
                and a.close>=1.25*a.low252 and a.close>=.75*a.high252)


def vcp_research_proxy(d:pd.DataFrame,i:int):
    """Disjoint 15/10/5-day contracting ranges and volumes; NOT canonical VCP."""
    if i<31:return False
    base=d.iloc[i-30:i]
    w1,w2,w3=base.iloc[:15],base.iloc[15:25],base.iloc[25:30]
    def width(w):return float((w.high.max()-w.low.min())/w.close.mean())
    a,b,c=width(w1),width(w2),width(w3)
    v=[float(w.volume.mean()) for w in (w1,w2,w3)]
    return bool(a>b*1.10 and b>c*1.10 and v[0]>v[1]>v[2]
                and d.close.iloc[i]>float(d.high.iloc[i-15:i].max())
                and d.volume.iloc[i]>=1.2*float(d.volume.iloc[i-20:i].mean()))


def simulate_minervini(d,i,entry,stop,args):
    for j in range(i+1,min(len(d)-1,i+1+args.minervini_max_days)):
        bar=d.iloc[j];stop_hit=float(bar.low)<=stop
        if stop_hit:
            return int(bar.ts),exit_on_level(float(bar.open),stop,1,args.slippage),j,'protective_swing_stop'
        if float(bar.close)<float(bar.ema20) or j==i+args.minervini_max_days:
            nx=d.iloc[j+1]
            return int(nx.ts),float(nx.open)*(1-args.slippage),j+1,'ema20_break_next_open' if bar.close<bar.ema20 else 'max_hold_exit'
    return None


def run_minervini(d,bounds,args,inst):
    tag='MINERVINI_TREND_VCP_PROXY';ctr=Counter();out=[];i=254
    while i<len(d)-args.minervini_max_days-2:
        signal=int(d.ts.iloc[i]+DAY);split=mark_split(signal,bounds)
        if split is None:i+=1;continue
        if not trend_template_7of8(d,i):i+=1;continue
        ctr['trend_template_7of8_pass']+=1
        if not vcp_research_proxy(d,i):i+=1;continue
        ctr['proxy_vcp_breakout']+=1
        nxt=i+1;entry=float(d.open.iloc[nxt])*(1+args.slippage)
        low=float(d.low.iloc[i-10:i].min());atr=float(d.atr14.iloc[i]);stop=low-.2*atr
        risk=(entry-stop)/entry if entry>0 else -1
        if not (.002<=risk<=.12 and stop>0):ctr['invalid_stop_distance']+=1;i+=1;continue
        sim=simulate_minervini(d,i,entry,stop,args)
        if sim is None:ctr['right_censored']+=1;i+=1;continue
        exit_ts,exit_px,exit_i,reason=sim
        if exit_ts>=bounds[split][1]:ctr['purged_cross_split']+=1;i=exit_i+1;continue
        out.append(new_trade(tag,inst,1,split,signal,int(d.ts.iloc[nxt]),exit_ts,entry,exit_px,stop,reason,args.fee,args.funding))
        ctr['filled']+=1;i=exit_i+1
    return out,ctr


def stats(trades:list):
    if not trades:return {'trades':0,'win_rate':None,'avg_net_trade_pct':None,'profit_factor':None,'illustrative_sequence_dd':None,'sample100':False}
    x=np.asarray([float(z['net_return_pct']) for z in sorted(trades,key=lambda a:(a['exit_ts'],a['symbol']))]);wins=float(x[x>0].sum());loss=-float(x[x<0].sum())
    equity=peak=1.0;dd=0.
    for ret in x:
        equity=max(1e-10,equity*(1+ret/100));peak=max(peak,equity);dd=max(dd,1-equity/peak)
    return {'trades':len(x),'win_rate':round(float((x>0).mean()),4),'avg_net_trade_pct':round(float(x.mean()),4),
            'median_net_trade_pct':round(float(np.median(x)),4),
            'profit_factor':round(wins/loss,4) if loss else None,
            'illustrative_sequence_dd':round(dd,4),'illustrative_equity_multiple':round(equity,4),'sample100':len(x)>=100}


def run(args,loader=None):
    end=base.timestamp_utc(args.end_utc);start=end-args.days*DAY
    if end>int(time.time()*1000):raise ValueError('Future end timestamp')
    if args.warmup_days<320 or args.days<=args.warmup_days+90:raise ValueError('Need >=320d indicator warmup + >=90d evaluation')
    if not (0<=args.fee<=.01 and 0<=args.slippage<=.01 and 0<=args.funding<=.01):raise ValueError('Cost assumption out of range')
    if args.turtle_max_days<20 or args.minervini_max_days<5:raise ValueError('Invalid max holding periods')
    if loader is None:loader=base.download_30m
    out=Path(args.output);out.parent.mkdir(parents=True,exist_ok=True)
    cache=Path(args.cache);cache.mkdir(parents=True,exist_ok=True)
    bounds=split_bounds(start,end,args.warmup_days)
    all_trades=[];all_counts=defaultdict(Counter);cover=[];failed={}
    for sym in args.symbols:
        inst=sym.upper() if sym.upper().endswith('-USDT-SWAP') else sym.upper()+'-USDT-SWAP'
        try:
            cpath=cache/f'{inst}_{start}_{end}_30m.csv'
            if cpath.exists():raw=pd.read_csv(cpath)
            else:
                print(f'Downloading real OKX 30m bars {inst} {args.days} days',flush=True)
                raw=loader(inst,start,end,args.pause);raw.to_csv(cpath,index=False)
            base.validate_30m(raw)
            if int(raw.ts.iloc[0])-start>DAY or end-(int(raw.ts.iloc[-1])+MS30)>DAY:
                raise RuntimeError('Incomplete historical coverage')
            d=base.aligned_bars(raw,1440)
            if len(d)<320:raise ValueError('Insufficient complete daily bars')
            d=prep_daily(d);slots=day_bars(raw,d)
            methods=[('CRABEL_2026_ORB',lambda:run_crabel(raw,d,slots,bounds,args,inst,'CRABEL_2026_ORB')),
                     ('CRABEL_NR7_STRETCH',lambda:run_crabel(raw,d,slots,bounds,args,inst,'CRABEL_NR7_STRETCH')),
                     ('TURTLE_S1_20_10_NO_SKIP',lambda:run_turtle(raw,d,slots,bounds,args,inst,20,10,'TURTLE_S1_20_10_NO_SKIP')),
                     ('TURTLE_S2_55_20_NO_PYRAMID',lambda:run_turtle(raw,d,slots,bounds,args,inst,55,20,'TURTLE_S2_55_20_NO_PYRAMID')),
                     ('MINERVINI_TREND_VCP_PROXY',lambda:run_minervini(d,bounds,args,inst))]
            counts_by_strategy={};trades_by_strategy={}
            for name,f in methods:
                ledger,counts=f();all_trades.extend(ledger);all_counts[(inst,name)].update(counts)
                counts_by_strategy[name]=dict(counts);trades_by_strategy[name]=len(ledger)
            cover.append({'symbol':inst,'bars30m':len(raw),'complete_daily_bars':len(d),'first_utc':datetime.fromtimestamp(int(raw.ts.iloc[0])/1000,timezone.utc).isoformat(),'last_utc':datetime.fromtimestamp(int(raw.ts.iloc[-1])/1000,timezone.utc).isoformat()})
            print('DONE '+inst+' '+json.dumps(trades_by_strategy),flush=True)
        except Exception as e:
            failed[inst]=type(e).__name__+': '+str(e)
            print('FAIL '+inst+' '+failed[inst],file=sys.stderr,flush=True)
    rows=[]
    for strategy in STRATEGIES:
        for split in bounds:
            for side in ('ALL','LONG','SHORT'):
                rr=[z for z in all_trades if z['strategy']==strategy and z['split']==split and (side=='ALL' or z['side']==side)]
                rows.append({'strategy':strategy,'split':split,'side':side,**stats(rr)})
    pd.DataFrame(rows).to_csv(out.with_suffix('.summary.csv'),index=False)
    ledger_columns=['strategy','symbol','side','split','signal_utc','entry_ts','exit_ts','entry','exit','stop','net_return_pct','hold_hours','exit_reason']
    pd.DataFrame(all_trades,columns=ledger_columns).to_csv(out.with_suffix('.trades.csv'),index=False)
    pd.DataFrame([{'symbol':k[0],'strategy':k[1],**dict(v)} for k,v in all_counts.items()]).to_csv(out.with_suffix('.diagnostic.csv'),index=False)
    report={'status':'EXPLORATORY_RESEARCH_ONLY_NOT_VALIDATED',
            'sources':SOURCES,
            'definitions':{
                'CRABEL_2026_ORB':'opening price ±0.8 times preceding 10 complete days MAX(high)-MIN(low); no protective stop; exits next day open; intrabar dual touch excluded',
                'CRABEL_NR7_STRETCH':'prior day NR7; opening price ± mean last10 min(high-open,open-low); next day open exit; research combination',
                'TURTLE_S1_20_10_NO_SKIP':'20d previous extreme entry, 10d opposite previous extreme exit, 2N stop; no skip-winning-breakout filter or pyramiding',
                'TURTLE_S2_55_20_NO_PYRAMID':'55d entry, 20d opposite exit, 2N stop; no pyramiding or multi-market portfolio unit sizing',
                'MINERVINI_TREND_VCP_PROXY':'7 price/MA/range conditions, 15/10/5d contracting price and volume windows, breakout at previous daily close, next open entry; NOT original discretionary VCP, RS ranking/earnings absent'},
            'not_implemented':['historical top200 and delisted coins','historical fee schedules and genuine funding payments','order book or liquidation','marketwide portfolio position risk','original Turtle S1 winner skip and pyramiding','official equity-universe RS rank and Minervini discretionary VCP','real account drawdown'],
            'notes':['These are crypto adaptation prototypes, not claims of faithfully reproducing exact personal trades.',
                     'Same-bar multiple trigger direction is ambiguous and skipped, protective stop precedes exit when colliding.',
                     'Slippage plus fees plus time-based approximate 8h funding deducted from simulated returns.',
                     'Intraday OHLC cannot prove tick sequence, gap handling is conservative.',
                     'Train/validation/test are temporal. Earlier studies inspected overlapping dates; test is not pristine OOS.',
                     'DD is a sequential, unleveraged trades illustration, NOT a concurrent portfolio estimate.'],
            'parameters':{'symbols':args.symbols,'days':args.days,'end_utc':args.end_utc,'warmup_days':args.warmup_days,'fee_per_side':args.fee,'slippage_per_side':args.slippage,'funding_per_8h':args.funding,'turtle_max_hold_days':args.turtle_max_days,'minervini_max_hold_days':args.minervini_max_days},
            'train_validation_test_bounds_utc':{k:[datetime.fromtimestamp(x/1000,timezone.utc).isoformat() for x in v] for k,v in bounds.items()},
            'coverage':cover,'failures':failed,'results':rows,
            'diagnostics':{k[0]+'|'+k[1]:dict(v) for k,v in all_counts.items()},
            'complete':len(cover)==len(args.symbols) and not failed,
            'ready_for_live':False}
    out.write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    print('SUMMARY '+json.dumps({'symbols_ok':len(cover),'failed':len(failed),'simulated_trades':len(all_trades),'scenarios':len(STRATEGIES),'ready_for_live':False},ensure_ascii=False),flush=True)
    return 0 if report['complete'] else 2


def parser():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--symbols',nargs='+',default=['BTC','ETH','SOL','XRP'])
    p.add_argument('--days',type=int,default=730)
    p.add_argument('--warmup-days',type=int,default=320)
    p.add_argument('--end-utc',default='2026-10-01T00:00:00Z')
    p.add_argument('--fee',type=float,default=.0005)
    p.add_argument('--slippage',type=float,default=.0005)
    p.add_argument('--funding',type=float,default=.0001)
    p.add_argument('--pause',type=float,default=.17)
    p.add_argument('--turtle-max-days',type=int,default=90)
    p.add_argument('--minervini-max-days',type=int,default=60)
    p.add_argument('--cache',default='research_v05_cache')
    p.add_argument('--output',default='trader_v05.json')
    return p

if __name__=='__main__':
    try:sys.exit(run(parser().parse_args()))
    except Exception as exc:
        print('FATAL RESEARCH INTEGRITY:',type(exc).__name__,str(exc),file=sys.stderr)
        sys.exit(2)
