#!/usr/bin/env python3
"""RESEARCH ONLY: independent, source-attributed strategy prototypes.

Connors 2007 Improved R2: daily SPX close > SMA200; RSI2 day-1<65,
RSI2 decreasing 3 days; original buy/exit at close (RSI2>75). Crypto backtest
fills at NEXT open (no hindsight), adds ATR stop / max hold and assumed costs.

Raschke Holy Grail: 4H ADX14 previously >30+rising, 20EMA first pullback,
resting buy/sell stop above/below setup candle, swing stop, prior impulse
high/low objective. First touch, direction and expiration are modeled proxies.

No private OKX keys, no trading, no notifications, no changes to scanner.py.
This is NOT a validated strategy, a portfolio backtest or a claim of BNF rules.
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
import requests
import numpy as np
import pandas as pd

MS30=1_800_000
MSDAY=86_400_000
TPE_OFFSET=8*3_600_000
SOURCE_LINKS={
 'CONNORS_IMPROVED_R2':'https://tradingmarkets.com/recent/the_improved_r2_strategy_84_correct_with_just_6_rules_-674361',
 'RASCHKE_HOLY_GRAIL':'https://lindaraschke.net/wp-content/uploads/2013/11/august1997.pdf',
}


def timestamp_utc(dt:str)->int:
    x=datetime.fromisoformat(dt.replace('Z','+00:00'))
    if x.tzinfo is None or x.utcoffset().total_seconds()!=0:
        raise ValueError('end_utc must be a UTC timestamp')
    return int(x.timestamp()*1000)


def download_30m(inst:str,start_ms:int,end_ms:int,pause:float=.17)->pd.DataFrame:
    session=requests.Session()
    after=None;records={};pages=0
    while True:
        params={'instId':inst,'bar':'30m','limit':'300'}
        if after is not None:params['after']=str(after)
        for retry in range(4):
            try:
                response=session.get('https://www.okx.com/api/v5/market/history-candles',params=params,timeout=(8,25))
                response.raise_for_status()
                payload=response.json()
                if payload.get('code')!='0':raise RuntimeError(f'OKX error: {payload.get("code")} {payload.get("msg")}')
                raw=payload['data']
                break
            except (requests.RequestException,ValueError,RuntimeError,KeyError) as err:
                if retry==3:raise RuntimeError(f'Historical OHLCV retrieval failed for {inst}: {err}') from err
                time.sleep(1+retry*2)
        if not raw:break
        pages+=1
        if pages>900:raise RuntimeError('Pagination exceeded safety limit')
        for row in raw:
            try:
                if len(row)<9 or str(row[8])!='1':continue
                stamp=int(row[0])
                vals=tuple(float(v) for v in row[1:6])
                o,h,l,c,v=vals
                if start_ms<=stamp<end_ms and min(o,h,l,c)>0 and v>=0 and h>=max(o,c,l) and l<=min(o,c,h):
                    records[stamp]=(stamp,*vals)
            except (TypeError,ValueError,IndexError):continue
        oldest=min(int(x[0]) for x in raw)
        if oldest<start_ms:break
        if after is not None and oldest>=after:raise RuntimeError('OKX historic page cursor stalled')
        after=oldest
        if pages%40==0:print(f'  {inst}: {pages} pages, {len(records)} bars',flush=True)
        time.sleep(pause)
    if not records:raise RuntimeError(f'No historical candles for {inst}')
    d=pd.DataFrame([records[k] for k in sorted(records)],columns=['ts','open','high','low','close','volume'])
    validate_30m(d)
    # Fail closed when a listing is too new or OKX truncated history.
    if int(d.ts.iloc[0])-start_ms>MSDAY or end_ms-(int(d.ts.iloc[-1])+MS30)>MSDAY:
        raise RuntimeError(f'{inst}: historical coverage incomplete')
    return d


def validate_30m(d:pd.DataFrame):
    if d.empty:raise ValueError('Empty candles')
    if len(d)>1 and not (d.ts.diff().iloc[1:]==MS30).all():
        raise ValueError('Gaps/duplicates in 30m candles; refuse to backtest')
    if not np.isfinite(d[['open','high','low','close','volume']].to_numpy(dtype=float)).all():
        raise ValueError('Nonfinite candle values')
    if ((d.high<d[['open','low','close']].max(axis=1)) | (d.low>d[['open','high','close']].min(axis=1))).any():
        raise ValueError('Invalid OHLC data')


def aligned_bars(d:pd.DataFrame,minutes:int)->pd.DataFrame:
    if minutes%30!=0:raise ValueError('Invalid timeframe')
    mult=minutes//30; win=mult*MS30
    z=d.copy()
    z['group']=((z.ts.astype('int64')+TPE_OFFSET)//win)*win-TPE_OFFSET
    arr=[]
    for ts,g in z.groupby('group',sort=True):
        if len(g)!=mult or int(g.ts.iloc[0])!=ts or int(g.ts.iloc[-1])!=ts+(mult-1)*MS30:
            continue
        arr.append((ts,float(g.open.iloc[0]),float(g.high.max()),float(g.low.min()),float(g.close.iloc[-1]),float(g.volume.sum())))
    return pd.DataFrame(arr,columns=['ts','open','high','low','close','volume'])


def rsi_wilder(closes:pd.Series,period:int=2)->pd.Series:
    dx=closes.diff()
    gain=dx.clip(lower=0).ewm(alpha=1/period,adjust=False,min_periods=period).mean()
    loss=(-dx.clip(upper=0)).ewm(alpha=1/period,adjust=False,min_periods=period).mean()
    rs=gain/loss.replace(0,np.nan)
    rsi=100-100/(1+rs)
    rsi=rsi.mask((loss==0)&(gain>0),100.0).mask((gain==0)&(loss>0),0.0)
    return rsi.mask((gain==0)&(loss==0),50.0)


def wilder_atr(frame:pd.DataFrame,period=14):
    prior=frame.close.shift(1)
    tr=pd.concat([frame.high-frame.low,(frame.high-prior).abs(),(frame.low-prior).abs()],axis=1).max(axis=1)
    return tr.ewm(alpha=1/period,adjust=False,min_periods=period).mean()


def adx_wilder(frame:pd.DataFrame,period:int=14)->pd.DataFrame:
    z=frame.copy()
    up=z.high.diff();down=-z.low.diff()
    plus=np.where((up>down)&(up>0),up,0.0)
    minus=np.where((down>up)&(down>0),down,0.0)
    atr=wilder_atr(z,period)
    plus_sm=pd.Series(plus,index=z.index).ewm(alpha=1/period,adjust=False,min_periods=period).mean()
    minus_sm=pd.Series(minus,index=z.index).ewm(alpha=1/period,adjust=False,min_periods=period).mean()
    z['pdi']=100*plus_sm/atr.replace(0,np.nan)
    z['mdi']=100*minus_sm/atr.replace(0,np.nan)
    dx=100*(z.pdi-z.mdi).abs()/(z.pdi+z.mdi).replace(0,np.nan)
    z['adx']=dx.ewm(alpha=1/period,adjust=False,min_periods=period).mean()
    z['ema20']=z.close.ewm(span=20,adjust=False,min_periods=20).mean()
    z['atr14']=atr
    return z


def split_ranges(start_ms:int,end_ms:int,warmup:int):
    low=start_ms+warmup*MSDAY
    if low>=end_ms:raise ValueError('No time remains after warmup')
    span=end_ms-low
    return {'train':(low,low+int(span*.6)),
            'validation':(low+int(span*.6),low+int(span*.8)),
            'test_not_pristine':(low+int(span*.8),end_ms)}


def which_split(ts:int,bounds:dict):
    for key,(a,b) in bounds.items():
        if a<=ts<b:return key
    return None


def net_trade(entry:float,exit_price:float,direction:int,fee:float,funding:float,bars_elapsed:int,bars_per_8h:int):
    # Absolute fees on both notional legs, funding modeled as positive cost for
    # either side; assumes a single unlevered notional per simulated trade.
    gross=direction*(exit_price-entry)/entry
    commissions=fee*(entry+exit_price)/entry
    funding_cost=math.ceil(bars_elapsed/bars_per_8h)*funding
    return 100*(gross-commissions-funding_cost)


def connors_setup(daily:pd.DataFrame,i:int)->bool:
    if i<201:return False
    a=daily.iloc[i];p=daily.iloc[i-1];q=daily.iloc[i-2]
    return bool(np.isfinite(a.ma200) and np.isfinite(a.rsi2) and
                a.close>a.ma200 and q.rsi2<65 and q.rsi2>p.rsi2>a.rsi2)


def backtest_connors(daily:pd.DataFrame,bounds:dict,args,inst:str):
    d=daily.copy()
    d['ma200']=d.close.rolling(200,min_periods=200).mean()
    d['rsi2']=rsi_wilder(d.close,2)
    d['atr14']=wilder_atr(d)
    ledger=[];counts=Counter();n=len(d);i=202
    while i<n-1:
        signal=int(d.ts.iloc[i]+MSDAY);split=which_split(signal,bounds)
        if not split:i+=1;continue
        if not connors_setup(d,i):i+=1;continue
        counts[(split,'raw_signals')]+=1
        # Sufficient future price data only to simulate an outcome; this guard
        # is NOT part of the strategy decision.
        if i+args.connors_max_days+2>=n or d.ts.iloc[i+args.connors_max_days+1]+MSDAY>bounds[split][1]:
            counts[(split,'purged_boundary')]+=1;i+=1;continue
        entry_i=i+1
        entry=float(d.open.iloc[entry_i])*(1+args.slippage)
        atr=float(d.atr14.iloc[i])
        if not np.isfinite(atr) or atr<=0:counts[(split,'invalid_atr')]+=1;i+=1;continue
        stop=entry-args.connors_stop_atr*atr  # RESEARCHER safety addition, not original Connors
        if stop<=0:counts[(split,'invalid_stop')]+=1;i+=1;continue
        exit_i=None;exit_price=None;reason=None
        for j in range(entry_i,entry_i+args.connors_max_days):
            r=d.iloc[j]
            if r.low<=stop:
                exit_i=j;exit_price=min(float(r.open),stop)*(1-args.slippage);reason='protective_stop';break
            if float(r.rsi2)>75 or j==entry_i+args.connors_max_days-1:
                exit_i=j+1;exit_price=float(d.open.iloc[exit_i])*(1-args.slippage)
                reason='RSI2_above75' if float(r.rsi2)>75 else 'max_hold_exit'
                break
        if exit_i is None:raise AssertionError('Connors simulation had no exit')
        if d.ts.iloc[exit_i]>=bounds[split][1]:raise AssertionError('Cross-split Connors exit')
        bars=exit_i-entry_i+1
        # 1D funding proxy: at least three 8H intervals for stop-outs on
        # entry day; otherwise three intervals per full day held.
        holding_days=max(1,exit_i-entry_i)
        pct=100*((exit_price-entry)/entry - args.fee*(entry+exit_price)/entry - holding_days*3*args.funding)
        ledger.append({'strategy':'CONNORS_IMPROVED_R2_1D','symbol':inst,'side':'LONG','split':split,
                       'signal_ts':signal,'entry_ts':int(d.ts.iloc[entry_i]),'exit_ts':int(d.ts.iloc[exit_i]),
                       'entry':entry,'exit':exit_price,'stop':stop,'target':None,'net_return_pct':pct,
                       'hold_hours':bars*24,'exit_reason':reason})
        counts[(split,'trades')]+=1
        i=exit_i+1
    return ledger,counts


def raschke_setup(d:pd.DataFrame,i:int,side:int,first_touch:int=3)->bool:
    if i<60:return False
    setup=d.iloc[i];prior=d.iloc[i-1];older=d.iloc[i-2]
    if not (np.isfinite(prior.adx) and prior.adx>30 and prior.adx>older.adx and np.isfinite(setup.ema20)):
        return False
    if side==1:
        if not (prior.pdi>prior.mdi and prior.ema20>older.ema20 and prior.close>prior.ema20):return False
        if not (setup.low<=setup.ema20<=setup.high):return False
        if not all(float(d.low.iloc[k])>float(d.ema20.iloc[k]) for k in range(i-first_touch,i)):return False
    else:
        if not (prior.mdi>prior.pdi and prior.ema20<older.ema20 and prior.close<prior.ema20):return False
        if not (setup.low<=setup.ema20<=setup.high):return False
        if not all(float(d.high.iloc[k])<float(d.ema20.iloc[k]) for k in range(i-first_touch,i)):return False
    return True


def stop_target_4h(d:pd.DataFrame,i:int,side:int,trigger:float,atr:float):
    if side==1:
        stop=float(d.low.iloc[i-3:i+1].min())-0.1*atr
        target=float(d.high.iloc[i-22:i-1].max())
        if not (stop<trigger<target):return None
    else:
        stop=float(d.high.iloc[i-3:i+1].max())+0.1*atr
        target=float(d.low.iloc[i-22:i-1].min())
        if not (target<trigger<stop):return None
    risk=abs(trigger-stop)/trigger
    if not .002<=risk<=.1:return None
    return stop,target


def simulate_4h_fill(d:pd.DataFrame,setup_idx:int,side:int,trigger:float,stop:float,target:float,args):
    n=len(d)
    for ent_idx in range(setup_idx+1,min(n,setup_idx+1+args.raschke_order_valid_bars)):
        b=d.iloc[ent_idx]
        crossed=b.high>=trigger if side==1 else b.low<=trigger
        if not crossed:continue
        raw_fill=max(float(b.open),trigger) if side==1 else min(float(b.open),trigger)
        entry=raw_fill*(1+side*args.slippage)
        if not (stop<entry<target if side==1 else target<entry<stop):return None,'gap_invalidates_plan'
        for j in range(ent_idx,min(n,ent_idx+args.raschke_max_hold_4h)):
            row=d.iloc[j];op=float(row.open)
            stop_hit=(row.low<=stop if side==1 else row.high>=stop)
            target_hit=(row.high>=target if side==1 else row.low<=target)
            if stop_hit: # also when same bar crosses stop AND target: STOP FIRST
                exit_price=(min(op,stop) if side==1 else max(op,stop))*(1-side*args.slippage)
                return (entry,exit_price,ent_idx,j, 'protective_stop'),None
            if target_hit:
                exit_price=(max(op,target) if side==1 else min(op,target))*(1-side*args.slippage)
                return (entry,exit_price,ent_idx,j,'retest_impulse_extreme'),None
        # Need the next open to exit max hold. This is guaranteed by right purge.
        next_i=ent_idx+args.raschke_max_hold_4h
        if next_i>=n:return None,'insufficient_exit_candles'
        exit_price=float(d.open.iloc[next_i])*(1-side*args.slippage)
        return (entry,exit_price,ent_idx,next_i,'max_hold_exit'),None
    return None,'entry_order_unfilled'


def backtest_raschke(frame:pd.DataFrame,bounds:dict,args,inst:str):
    d=adx_wilder(frame)
    ledger=[];counts=Counter();n=len(d)
    last_exit_i={1:-1,-1:-1}
    for i in range(60,n-args.raschke_max_hold_4h-args.raschke_order_valid_bars-1):
        signal=int(d.ts.iloc[i]+4*3_600_000);split=which_split(signal,bounds)
        if not split:continue
        for side in (1,-1):
            if i<=last_exit_i[side]:continue
            if not raschke_setup(d,i,side,args.raschke_first_touch_bars):continue
            key='LONG' if side==1 else 'SHORT'
            counts[(split,key,'raw_signals')]+=1
            final_possible_exit=int(d.ts.iloc[i+args.raschke_order_valid_bars+args.raschke_max_hold_4h]+4*3_600_000)
            if final_possible_exit>bounds[split][1]:
                counts[(split,key,'purged_boundary')]+=1;continue
            atr=float(d.atr14.iloc[i]);tick=args.trigger_buffer
            if not np.isfinite(atr) or atr<=0:counts[(split,key,'invalid_atr')]+=1;continue
            trigger=float(d.high.iloc[i])*(1+tick) if side==1 else float(d.low.iloc[i])*(1-tick)
            p=stop_target_4h(d,i,side,trigger,atr)
            if p is None:counts[(split,key,'invalid_risk_target')]+=1;continue
            stop,target=p
            res,reason=simulate_4h_fill(d,i,side,trigger,stop,target,args)
            if res is None:counts[(split,key,reason)]+=1;continue
            entry,exit_price,ent_idx,exit_idx,ex_reason=res
            if int(d.ts.iloc[exit_idx])>=bounds[split][1]:raise AssertionError('Cross-split Raschke exit')
            bars=exit_idx-ent_idx+1
            net=net_trade(entry,exit_price,side,args.fee,args.funding,bars,2)
            ledger.append({'strategy':'RASCHKE_HOLY_GRAIL_4H','symbol':inst,'side':key,'split':split,
                           'signal_ts':signal,'entry_ts':int(d.ts.iloc[ent_idx]),'exit_ts':int(d.ts.iloc[exit_idx]),
                           'entry':entry,'exit':exit_price,'stop':stop,'target':target,'net_return_pct':net,
                           'hold_hours':bars*4,'exit_reason':ex_reason})
            counts[(split,key,'trades')]+=1;last_exit_i[side]=exit_idx
    return ledger,counts


def metrics(records):
    if not records:return {'trades':0,'win_rate':None,'avg_net_trade_pct':None,'profit_factor':None,
                           'illustrative_max_drawdown':None,'max_loss_streak':None,'sample_100_plus':False}
    ret=np.array([x['net_return_pct'] for x in sorted(records,key=lambda a:a['exit_ts'])]);
    profit=float(ret[ret>0].sum());loss=float(-ret[ret<0].sum())
    equity=peak=1.0;dd=0.0;losing=worst=0
    # Illustrative 1x sequential account, ignores simultaneous multi-symbol positions.
    for r in ret:
        equity=max(1e-9,equity*(1+r/100))
        peak=max(peak,equity);dd=max(dd,1-equity/peak)
        losing=losing+1 if r<0 else 0;worst=max(worst,losing)
    return {'trades':int(len(ret)),'win_rate':round(float(np.mean(ret>0)),4),
            'avg_net_trade_pct':round(float(ret.mean()),4),
            'median_net_trade_pct':round(float(np.median(ret)),4),
            'profit_factor':round(profit/loss,4) if loss else None,
            'illustrative_max_drawdown':round(dd,4),'max_loss_streak':worst,
            'sample_100_plus':len(ret)>=100,'illustrative_equity_multiple':round(equity,4)}


def run(args):
    until=timestamp_utc(args.end_utc);since=until-args.days*MSDAY
    if args.days<args.warmup+150 or args.warmup<220:raise ValueError('Need 200D SMA warmup plus sufficient eval window')
    if until>int(time.time()*1000):raise ValueError('Future end timestamp')
    if not 0<=args.fee<=.01 or not 0<=args.slippage<=.01 or not 0<=args.funding<=.01:raise ValueError('Invalid assumed costs')
    if not 0<=args.pause<=3:raise ValueError('Invalid throttle')
    bounds=split_ranges(since,until,args.warmup)
    output=Path(args.output);output.parent.mkdir(parents=True,exist_ok=True)
    cache=Path(args.cache);cache.mkdir(parents=True,exist_ok=True)
    all_trades=[];rows=[];errors={};candles=[];agg=Counter()
    for sym in args.symbols:
        inst=sym.upper() if sym.upper().endswith('-USDT-SWAP') else sym.upper()+'-USDT-SWAP'
        path=cache/f'{inst}_{since}_{until}_30m.csv'
        try:
            if path.exists():
                raw=pd.read_csv(path)
                print('Cache hit',inst,flush=True)
                validate_30m(raw)
            else:
                print('Downloading REAL OKX confirmed 30m history:',inst,flush=True)
                raw=download_30m(inst,since,until,args.pause)
                raw.to_csv(path,index=False)
            if int(raw.ts.iloc[0])-since>MSDAY or until-(int(raw.ts.iloc[-1])+MS30)>MSDAY:
                raise RuntimeError('Historical coverage too short')
            four=aligned_bars(raw,240);day=aligned_bars(raw,1440)
            if len(day)<220 or len(four)<300:raise RuntimeError('Insufficient complete 4H / daily history')
            a,ac=backtest_connors(day,bounds,args,inst)
            b,bc=backtest_raschke(four,bounds,args,inst)
            all_trades.extend(a+b);agg.update(ac);agg.update(bc)
            candles.append({'symbol':inst,'bars30m':len(raw),'bars4h':len(four),'bars1D':len(day),
                            'first_utc':datetime.fromtimestamp(int(raw.ts.iloc[0])/1000,timezone.utc).isoformat(),
                            'last_utc':datetime.fromtimestamp(int(raw.ts.iloc[-1])/1000,timezone.utc).isoformat()})
            print('OK',inst,'30m',len(raw),'Connors trades',len(a),'Raschke trades',len(b),flush=True)
        except Exception as exc:
            errors[inst]=f'{type(exc).__name__}: {exc}'
            print('FAIL',inst,errors[inst],file=sys.stderr,flush=True)
    strategies=('CONNORS_IMPROVED_R2_1D','RASCHKE_HOLY_GRAIL_4H')
    splits=tuple(bounds)
    for name in strategies:
        for split in splits:
            for side in ('ALL','LONG','SHORT'):
                x=[r for r in all_trades if r['strategy']==name and r['split']==split and (side=='ALL' or r['side']==side)]
                rows.append({'strategy':name,'split':split,'side':side,**metrics(x)})
    record_file=output.with_suffix('.trades.csv')
    cols=['strategy','symbol','side','split','signal_ts','entry_ts','exit_ts','entry','exit','stop','target','net_return_pct','hold_hours','exit_reason']
    pd.DataFrame(all_trades,columns=cols).to_csv(record_file,index=False)
    summary_file=output.with_suffix('.summary.csv')
    pd.DataFrame(rows).to_csv(summary_file,index=False)
    diagnosis_file=output.with_suffix('.diagnostic.csv')
    pd.DataFrame([{'stage':'|'.join(str(p) for p in k),'count':n} for k,n in sorted(agg.items(),key=lambda z:tuple(map(str,z[0])))],columns=['stage','count']).to_csv(diagnosis_file,index=False)
    report={'status':'RESEARCH_ONLY_NOT_VALIDATED','sources':SOURCE_LINKS,
            'data':'real OKX confirmed 30m OHLCV; derived 4H and 1D UTC+8 aligned',
            'parameters':{'end_utc':args.end_utc,'days':args.days,'warmup':args.warmup,'symbols':args.symbols,
                          'fee_per_side':args.fee,'slippage_per_side':args.slippage,'funding_per_8h':args.funding,
                          'connors_ATR_stop_added':args.connors_stop_atr,'connors_max_days_added':args.connors_max_days,
                          'raschke_setup_timeframe':'4H','raschke_first_touch_bars':args.raschke_first_touch_bars,
                          'raschke_entry_valid_bars':args.raschke_order_valid_bars,'raschke_max_hold_4h':args.raschke_max_hold_4h},
            'exclusions':['historical top200 constituents','historical spreads and L2 order book',
                          'historical funding rate','forced liquidations','multi-market correlated concurrent portfolio risk'],
            'methodology_notes':['Connors article is for SPX; next-open fills, ATR stop and max-hold are researcher additions.',
             'Raschke first-touch definition, trigger buffer, exit cap and swing target are researcher mechanical choices.',
             'Two-year history includes previously inspected dates; held-out period is NOT pristine OOS.',
             'No claims of author performance or profitability apply to these OKX adaptations.',
             'A success status means computation succeeded, not trading strategy validated.'],
            'dates':{k:[datetime.fromtimestamp(v/1000,timezone.utc).isoformat() for v in pair] for k,pair in bounds.items()},
            'candles':candles,'failed':errors,'metrics':rows,'diagnostic_counts':{'|'.join(map(str,k)):v for k,v in agg.items()},
            'all_data_downloaded':len(candles)==len(args.symbols) and not errors,
            'ready_for_live_signals':False}
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    print('FINAL '+json.dumps({'ok_symbols':len(candles),'failed_symbols':len(errors),
                              'trades':len(all_trades),'train_val_test_buckets':len(rows),'live_enabled':False},ensure_ascii=False),flush=True)
    return 0 if report['all_data_downloaded'] else 2


def arg_parser():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--symbols',nargs='+',default=['BTC','ETH','SOL','XRP'])
    p.add_argument('--days',type=int,default=730)
    p.add_argument('--warmup',type=int,default=240)
    p.add_argument('--end-utc',default='2026-10-01T00:00:00Z')
    p.add_argument('--fee',type=float,default=.0005)
    p.add_argument('--slippage',type=float,default=.0005)
    p.add_argument('--funding',type=float,default=.0001)
    p.add_argument('--pause',type=float,default=.17)
    p.add_argument('--connors-stop-atr',type=float,default=2.5)
    p.add_argument('--connors-max-days',type=int,default=20)
    p.add_argument('--raschke-first-touch-bars',type=int,default=3)
    p.add_argument('--raschke-order-valid-bars',type=int,default=2)
    p.add_argument('--raschke-max-hold-4h',type=int,default=48)
    p.add_argument('--trigger-buffer',type=float,default=.0001)
    p.add_argument('--cache',default='research_v04_cache')
    p.add_argument('--output',default='research_v04.json')
    return p


if __name__=='__main__':
    try:sys.exit(run(arg_parser().parse_args()))
    except Exception as exc:
        print('FATAL RESEARCH INTEGRITY ERROR:',str(exc),file=sys.stderr)
        sys.exit(2)
