#!/usr/bin/env python3
"""OKX futures historical research backtest; NEVER place orders or push signals.

Downloads confirmed 30m OHLCV from OKX history-candles, resamples to aligned
30m/1H/2H/4H/6H/8H/12H/1D. Reimplements fixed scanner.strategy_a and strategy_b conditions;
for faithful historical execution, entry is at NEXT 30m opening candle with
conservative adverse slippage. This is an exploratory backtest only.

IMPORTANT: a today's-token pilot is survivorship-biased (not historically top200).
Fees, funding, slippage are ASSUMPTIONS. Historical order book is unavailable.
"""
import argparse, json, math, random, os, sys, time
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter
import requests
import numpy as np
import pandas as pd

TFMINS={"30m":30,"1H":60,"2H":120,"4H":240,"6H":360,"8H":480,"12H":720,"1D":1440}
UTC8MS=8*3600*1000
REQ_S=requests.Session()

def candles_history(inst_id, since_ms, until_ms, pause=.12):
    """Fetch OKX history-candles using 'after' = rows OLDER than cursor.

    'ts' is the OPEN timestamp, confirm=1 required. Fail closed on HTTP errors
    or nonprogressing page cursors. Resume via local CSV caching outside this fn.
    """
    after=None; records={}; pages=0
    while True:
        params={"instId":inst_id,"bar":"30m","limit":"300"}
        if after is not None:params['after']=str(after)
        exc=None
        for retry in range(4):
            try:
                r=REQ_S.get('https://www.okx.com/api/v5/market/history-candles',params=params,timeout=(8,25))
                if r.status_code in (429,500,502,503,504):raise RuntimeError(f'HTTP {r.status_code}')
                r.raise_for_status();p=r.json()
                if p.get('code')!='0':raise RuntimeError(str(p.get('code'))+' '+str(p.get('msg')))
                a=p['data'];break
            except (requests.RequestException,ValueError,RuntimeError,KeyError) as e:
                exc=e
                if retry==3:raise RuntimeError(f'OKX API error {inst_id}: {exc}') from exc
                time.sleep(1.0+2**retry)
        if not a:break
        pages+=1
        if pages>2000:raise RuntimeError('Pagination safety limit')
        for x in a:
            if len(x)<9 or x[8]!='1':continue
            try:
                v=(int(x[0]),float(x[1]),float(x[2]),float(x[3]),float(x[4]),float(x[5]))
                if v[0]>=since_ms and v[0]<until_ms and min(v[1:5])>0 and v[2]>=max(v[1],v[3],v[4]) and v[3]<=min(v[1],v[2],v[4]):records[v[0]]=v
            except (TypeError,ValueError):continue
        oldest=min(int(x[0]) for x in a)
        if oldest<since_ms:break
        if after is not None and oldest>=after:raise RuntimeError('Nonprogressing OKX history-candles pagination')
        after=oldest
        time.sleep(pause)
    if not records:raise RuntimeError('No historical candles')
    df=pd.DataFrame([records[k] for k in sorted(records)],columns=['ts','open','high','low','close','volume'])
    assert_continuous(df,30)
    return df

def assert_continuous(df,tf_minutes):
    if df.empty:raise ValueError('Empty candle data')
    if (df.ts.diff().iloc[1:] != tf_minutes*60_000).any():
        raise ValueError('Historical OHLCV contains missing/duplicate 30m candles; abort backtest')
    if not np.isfinite(df[['open','high','low','close','volume']].values).all():
        raise ValueError('Nonfinite prices/volume')

def resample(df,minutes):
    if minutes==30:return df.copy()
    interval=minutes*60_000
    # UTC+8 daily candles open at 00:00 Taipei time (16:00 UTC).
    shift=UTC8MS if minutes in (360,720,1440) else 0
    d=df.copy()
    d['bucket']=((d.ts.astype('int64')+shift)//interval)*interval-shift
    n=minutes//30
    output=[]
    for bucket,g in d.groupby('bucket',sort=True):
        if len(g)!=n or int(g.ts.iloc[0])!=bucket or int(g.ts.iloc[-1])!=bucket+(n-1)*1_800_000:continue
        output.append((bucket,float(g.open.iloc[0]),float(g.high.max()),float(g.low.min()),float(g.close.iloc[-1]),float(g.volume.sum())))
    return pd.DataFrame(output,columns=['ts','open','high','low','close','volume'])

def indicators(df):
    d=df.copy();c=d.close
    d['dif']=c.ewm(span=12,adjust=False).mean()-c.ewm(span=26,adjust=False).mean()
    d['dea']=d.dif.ewm(span=9,adjust=False).mean()
    d['hist']=d.dif-d.dea
    delta=c.diff()
    gain=delta.clip(lower=0).ewm(alpha=1/14,adjust=False).mean()
    loss=(-delta.clip(upper=0)).ewm(alpha=1/14,adjust=False).mean()
    d['rsi']=100-100/(1+gain/loss.replace(0,1e-12))
    tr=pd.concat([d.high-d.low,(d.high-c.shift(1)).abs(),(d.low-c.shift(1)).abs()],axis=1).max(axis=1)
    d['atr']=tr.ewm(alpha=1/14,adjust=False).mean()
    # Close timestamps based on the actual bar interval (never infer from a gap).
    d['close_ts']=d.ts+int((d.ts.diff().iloc[1] if len(d)>1 else 1800000))
    return d

def signal_a(d):
    day=d['1D'];h=d['1H'];h4=d['4H'];m=day.close.iloc[-25:].mean();sd=day.close.iloc[-25:].std()
    if not m or not sd or not np.isfinite(sd):return None
    bias=(day.close.iloc[-1]/m-1)*100
    z=(day.close.iloc[-1]-m)/sd
    cross=any(h.dif.iloc[i]>h.dea.iloc[i] and h.dif.iloc[i-1]<=h.dea.iloc[i-1] for i in (-1,-2))
    v20=h.volume.iloc[-21:-1].mean()
    breakout=h.close.iloc[-1]>h.high.iloc[-13:-1].max() and h.close.iloc[-2]<=h.high.iloc[-14:-2].max()
    if (bias<=-2.5 and z<=-1.5 and cross and h['hist'].iloc[-1]>h['hist'].iloc[-2]>h['hist'].iloc[-3]
        and breakout and v20>0 and h.volume.iloc[-1]>=1.2*v20 and h4['hist'].iloc[-1]>=h4['hist'].iloc[-2]
        and 25<=h.rsi.iloc[-1]<=75):return 'A_BNF_MACD'
    return None

def pivots_latest_confirmed(d,lookback=60):
    lows=d.low.to_numpy();p=[]
    for i in range(max(3,len(d)-lookback),len(d)-3):
        if all(lows[i]<v for v in lows[i-3:i]) and all(lows[i]<v for v in lows[i+1:i+4]):p.append(i)
    if len(p)<2:return None
    a,b=p[-2:]
    if b-a<6 or len(d)-1-b>12:return None
    if lows[b]<lows[a] and d.dif.iloc[b]>d.dif.iloc[a]:return (int(d.ts.iloc[a]),int(d.ts.iloc[b]))
    return None

def signal_b(d):
    matches={tf:p for tf in TFMINS if (p:=pivots_latest_confirmed(d[tf]))}
    if len(matches)<4 or '4H' not in matches or not ('12H' in matches or '1D' in matches):return None
    def breakout_volume(f,n):
        if f.close.iloc[-1]<=f.high.iloc[-n-1:-1].max() or f.close.iloc[-2]>f.high.iloc[-n-2:-2].max():return False
        avg=f.volume.iloc[-21:-1].mean()
        return avg>0 and f.volume.iloc[-1]>=1.2*avg
    if not (breakout_volume(d['30m'],8) or breakout_volume(d['1H'],12)):return None
    h=d['1H']
    if h['hist'].iloc[-1]<=h['hist'].iloc[-2] or not 25<=h.rsi.iloc[-1]<=75:return None
    return 'B_MTF_MACD_DIVERGENCE'

def plan(d,price,fee,slip,funding):
    """Use only historical levels observed before decision time. Return stop,targets,R:R."""
    h=d['1H'];h4=d['4H'];atr=float(h.atr.iloc[-1]);close=float(d['30m'].close.iloc[-1])
    if atr<=0 or abs(price-close)>0.7*atr:return None
    stop=float(h.low.iloc[-16:-1].min())-0.2*atr
    risk=price-stop
    if risk<=0 or not .002<=risk/price<=.07:return None
    highs=sorted({float(v) for v in pd.concat([h.high.iloc[-90:],h4.high.iloc[-80:]]) if float(v)>price+.25*atr})
    cost=price*(2*fee+2*slip+funding)
    rr=lambda tgt:(tgt-price-cost)/(risk+cost)
    targets=[x for x in highs if rr(x)>=2.0 and (x-price)/price<=.18]
    if len(targets)<2 or rr(targets[-1])<2.8:return None
    return stop,targets[0],targets[-1],rr(targets[0]),rr(targets[-1])

def simulate_after(df,entry_index,stop,tp1,tp2,fee,slip, funding_per_8h, max_hold_bars):
    """Long half TP1/half TP2; adverse gap stop; stop before TP on same bar.

    All tests assume one instrument, one long at a time. No liquidation modeling.
    The entry is next 30m open, slippage adversely applied. The trade can stop
    during its entry bar. Never examine future bars to decide WHETHER to enter.
    """
    if entry_index>=len(df):return None
    fill=float(df.open.iloc[entry_index])*(1+slip)
    if fill<=stop:return None
    shares=1.0; p1done=False; proceeds=0.0;hold=0; exit_reason='timeout'
    for i in range(entry_index,min(len(df),entry_index+max_hold_bars)):
        row=df.iloc[i];hold+=1
        if row.low<=stop:   # same-bar collision -> stop is first
            px=min(float(row.open),stop)*(1-slip)
            proceeds+=shares*px; shares=0;exit_reason='stop';break
        if not p1done and row.high>=tp1:
            proceeds+=.5*tp1*(1-slip);shares=.5;p1done=True;exit_reason='tp1'
        if row.high>=tp2:
            proceeds+=shares*tp2*(1-slip);shares=0;exit_reason='tp2';break
    if shares:
        px=float(df.close.iloc[min(len(df)-1,entry_index+max_hold_bars-1)])*(1-slip)
        proceeds+=shares*px;exit_reason='timeout' if not p1done else 'tp1_then_timeout'
    funded=math.ceil(hold/16)*funding_per_8h*fill
    net=(proceeds-fill-fee*(fill+proceeds)-funded)/fill
    return {'return_pct':net*100,'exit_reason':exit_reason,'hold_30m_bars':hold,
            'entry_actual':fill,'exit_time_ms':int(df.ts.iloc[min(len(df)-1,entry_index+hold-1)])+1800000}

def historical_backtest(df,fee,slip,funding_per_8h,max_hold_bars=96,warmup_days=75):
    assert_continuous(df,30)
    groups={tf:indicators(resample(df,mins)) for tf,mins in TFMINS.items()}
    for tf,f in groups.items():
        if f.empty:raise ValueError('No completed candles for '+tf)
    z={tf:f.close_ts.to_numpy() for tf,f in groups.items()}
    trades=[];last_exit=0; cooldown=0
    for t in range(warmup_days*48,len(df)-1):
        decision=int(df.ts.iloc[t])+1800000
        if decision < last_exit+cooldown*1800000:continue
        d={};valid=True
        for tf,f in groups.items():
            idx=np.searchsorted(z[tf],decision,side='right')
            if idx< (65 if tf!='1D' else 40):valid=False;break
            d[tf]=f.iloc[:idx]
        if not valid:continue
        hits=[f for f in (signal_a(d),signal_b(d)) if f]
        if not hits:continue
        # DECISION uses only the most recent fully closed bar; never next bar's open.
        signal_close=float(df.close.iloc[t]); entry_est=signal_close*(1+slip)
        plan_result=plan(d,entry_est,fee,slip,funding_per_8h)
        if not plan_result:continue
        stop,tp1,tp2,rr1,rr2=plan_result
        # At the next opening, a pre-defined bounded entry order is allowed to
        # remain unfilled if the market gaps away; do not reinterpret the signal.
        next_open=float(df.open.iloc[t+1]); actual_entry=next_open*(1+slip)
        atr=float(d['1H'].atr.iloc[-1])
        if actual_entry<entry_est-.12*atr or actual_entry>entry_est or actual_entry<=stop:continue
        # Fail closed if price movement degraded previously required risk/reward.
        if (tp1-actual_entry-(actual_entry*(2*fee+2*slip+funding_per_8h))) / (actual_entry-stop+(actual_entry*(2*fee+2*slip+funding_per_8h))) < 2.0:continue
        outcome=simulate_after(df,t+1,stop,tp1,tp2,fee,slip,funding_per_8h,max_hold_bars)
        if outcome is None:continue
        trades.append({'signal_time_utc':datetime.fromtimestamp(decision/1000,timezone.utc).isoformat(),
                       'signal_time_ms':decision,'strategy':'+'.join(hits),
                       'stop':stop,'tp1':tp1,'tp2':tp2,'rr1_est':rr1,'rr2_est':rr2,**outcome})
        last_exit=outcome['exit_time_ms']
        cooldown=2
    return trades

def stats(rows):
    if not rows:return {'trades':0,'win_rate':None,'net_pct_mean':None,'profit_factor':None,
                        'max_trade_drawdown':None,'note':'No trades; no evidence of profitability'}
    ar=np.array([x['return_pct'] for x in rows]);profit=ar[ar>0].sum();loss=-ar[ar<0].sum()
    # Flat risk sizing illustration: risk 0.5% initial equity per trade proxied as
    # 0.5% of equity / signal stop distance, limited nominal leverage to 2x.
    equity=1.0;peak=1.0;mdd=0.0
    for r in rows:
        dist=max(.002,(r['entry_actual']-r['stop'])/r['entry_actual'])
        exposure=min(2.0,.005/dist)
        equity=max(.000001,equity*(1+exposure*r['return_pct']/100))
        peak=max(peak,equity);mdd=max(mdd,1-equity/peak)
    return {'trades':int(len(ar)),'win_rate':round(float((ar>0).mean()),4),
            'net_pct_mean':round(float(ar.mean()),4),'net_pct_median':round(float(np.median(ar)),4),
            'profit_factor':round(float(profit/loss),3) if loss else None,
            'max_drawdown_0_5pct_risk':round(mdd,4),
            'equity_multiple_0_5pct_risk':round(equity,4),
            'stopped_out':sum(r['exit_reason']=='stop' for r in rows)}

def run(args):
    folder=Path(args.cache);folder.mkdir(parents=True,exist_ok=True)
    now=int(time.time()*1000);end=int(args.end_ms) if args.end_ms else now
    since=end-args.days*86400000
    outputs=[];failures={}
    for sym in args.symbols:
        iid=sym if sym.endswith('-USDT-SWAP') else sym.upper()+'-USDT-SWAP'
        dst=folder/(iid+'_'+str(since)+'_'+str(end)+'.csv')
        try:
            if dst.exists():d=pd.read_csv(dst)
            else:
                print('Downloading',iid,'days=',args.days,flush=True)
                d=candles_history(iid,since,end,args.pause);d.to_csv(dst,index=False)
            tr=historical_backtest(d,args.fee,args.slippage,args.funding,args.max_hold,args.warmup)
            for x in tr:x['instrument']=iid
            outputs+=tr
            print(iid,'candles',len(d),'trades',len(tr),flush=True)
        except Exception as e:
            failures[iid]=str(e);print('SKIP',iid,str(e),file=sys.stderr,flush=True)
    result={'timestamp_utc':datetime.now(timezone.utc).isoformat(),
      'status':'PILOT_SURVIVORSHIP_BIASED_NOT_VALIDATED',
      'data':'OKX history-candles confirmed 30m',
      'requested_symbols':args.symbols,'failed':failures,
      'assumptions':{'fee_per_side':args.fee,'slippage_per_side':args.slippage,
                     'funding_per_8h':args.funding,'max_hold_30m_bars':args.max_hold,
                     'entry':'NEXT_30m_OPEN','stop_tp_collision':'STOP_FIRST',
                     'not_modeled':['historical bid ask','liquidation','historical market-cap membership','order book depth']},
      'total':stats(outputs),'by_strategy':{s:stats([r for r in outputs if s in r['strategy']])
                           for s in ('A_BNF_MACD','B_MTF_MACD_DIVERGENCE')},
      'train_validation_test':{}}
    sorted_tr=sorted(outputs,key=lambda x:x['signal_time_ms'])
    for name,l,r in [('train',0,.6),('validation',.6,.8),('test',.8,1)]:
        lidx=math.floor(len(sorted_tr)*l);ridx=math.floor(len(sorted_tr)*r)
        result['train_validation_test'][name]=stats(sorted_tr[lidx:ridx])
    out=Path(args.output);out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    pd.DataFrame(sorted_tr).to_csv(out.with_suffix('.trades.csv'),index=False)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if not failures else 2

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--symbols',nargs='+',default=['BTC','ETH','SOL','XRP','ADA','DOGE','LINK','AVAX'])
    p.add_argument('--days',type=int,default=180)
    p.add_argument('--warmup',type=int,default=75)
    p.add_argument('--end-ms',type=int)
    p.add_argument('--fee',type=float,default=.0005)
    p.add_argument('--slippage',type=float,default=.0005)
    p.add_argument('--funding',type=float,default=.0001)
    p.add_argument('--max-hold',type=int,default=96)
    p.add_argument('--pause',type=float,default=.12)
    p.add_argument('--cache',default='backtest_data')
    p.add_argument('--output',default='backtest_report.json')
    args=p.parse_args()
    if args.days<=args.warmup+30:p.error('days must exceed warmup by at least 30 days')
    if not 0<=args.slippage<=.02 or not 0<=args.fee<=.02 or not 0<=args.funding<=.02:p.error('Invalid transaction costs')
    sys.exit(run(args))
