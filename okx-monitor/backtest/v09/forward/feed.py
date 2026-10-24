"""Confirmed public OKX data. No credentials and no private/order endpoints."""
from datetime import datetime,timezone
import math,time
import pandas as pd
import requests
from ..data import COLS,validate,iso
from ..config import STEPS,DAY
from .freeze import digest

ROOT='https://www.okx.com/api/v5/'
ALLOWED=('public/time','public/instruments','market/history-candles','public/funding-rate-history','public/mark-price')

def get(path,params=None):
    if path not in ALLOWED:raise ValueError('public read-only feed endpoint required')
    for attempt in range(4):
        try:
            r=requests.get(ROOT+path,params=params,timeout=(10,40));r.raise_for_status();p=r.json()
            if p.get('code')!='0':raise ValueError('public feed API code '+str(p.get('code')))
            return p['data']
        except (requests.RequestException,ValueError):
            if attempt==3:raise
            time.sleep(.5*2**attempt)

def server_time():return int(get('public/time')[0]['ts'])

def history(inst,bar,start,end,cached=None):
    step=STEPS[bar];records={int(r[0]):r for r in (cached or []) if start<=int(r[0])<end}
    cursor=end
    for _ in range(100):
        rows=get('market/history-candles',dict(instId=inst,bar='1Dutc' if bar=='1D' else bar,after=str(cursor),limit='300'))
        if not rows:break
        for r in rows:
            if len(r)<9 or str(r[8])!='1':continue
            t=int(r[0])
            if start<=t<end:
                value=[t,*map(float,r[1:6]),float(r[7])]
                if t in records and records[t]!=value:raise ValueError('exchange revised cached candle; preserve old state and review')
                records[t]=value
        oldest=min(int(r[0]) for r in rows)
        if oldest>=cursor:raise ValueError('feed pagination stalled')
        if records and min(records)==start and len(records)==(end-start)//step:break
        if oldest<=start:break
        cursor=oldest;time.sleep(.12)
    values=[records[t] for t in sorted(records)]
    validate(pd.DataFrame(values,columns=COLS),step,start,end)
    return values

def collect(freeze,previous=None,positions=None):
    cached=(previous or {}).get('frames',{});frames={}
    catalog={r['instId']:r for r in get('public/instruments',{'instType':'SWAP'})
             if r.get('settleCcy')=='USDT' and r.get('ctType')=='linear' and r.get('state')=='live'}
    for attempt in range(2):
        now=server_time();metadata=[]
        for bar,step in STEPS.items():
            end=now//step*step;start=end-freeze['warmup_days']*DAY
            for sym in freeze['symbols']:
                inst=sym+'-USDT-SWAP';key=inst+'|'+bar
                if inst not in catalog:raise ValueError('contract not currently tradable '+inst)
                rows=history(inst,bar,start,end,frames.get(key,cached.get(key)))
                frames[key]=rows
                metadata.append(dict(key=key,exchange_time_utc=iso(now),last_confirmed_open_utc=iso(rows[-1][0]),
                                     last_confirmed_close_utc=iso(end),source=ROOT+'market/history-candles',
                                     count=len(rows),checksum=digest(rows),calendar_complete=True,confirmed_only=True))
        finish=server_time()
        if all(now//s==finish//s for s in STEPS.values()):break
        if attempt==1:raise ValueError('data horizon changed during collection; retry without resetting state')
    host=int(datetime.now(timezone.utc).timestamp()*1000)
    if abs(finish-host)>300000:raise ValueError('exchange/runner clock divergence')
    for row in metadata:row['fetched_at_utc']=iso(finish);row['latency_ms']=finish-int(pd.Timestamp(row['last_confirmed_close_utc']).timestamp()*1000)
    funding={}
    for p in (positions or {}).values():
        inst=p['symbol']
        if inst in funding:continue
        rates=get('public/funding-rate-history',{'instId':inst,'limit':'100'})
        parsed=[]
        for r in rates:
            t=int(r['fundingTime']);rate=float(r.get('realizedRate') or r['fundingRate'])
            if not math.isfinite(rate):raise ValueError('invalid published funding')
            if t<=finish:parsed.append(dict(time=t,rate=rate,source=ROOT+'public/funding-rate-history'))
        if not parsed or min(r['time'] for r in parsed)>int(p.get('entry_ts',p.get('eligible_open_ts',finish))):raise ValueError('published funding coverage insufficient')
        funding[inst]=sorted(parsed,key=lambda r:r['time'])
    marks={r['instId']:dict(price=float(r['markPx']),time=int(r['ts'])) for r in get('public/mark-price',{'instType':'SWAP'})}
    for sym in freeze['symbols']:
        m=marks[sym+'-USDT-SWAP']
        if m['price']<=0 or abs(finish-m['time'])>300000:raise ValueError('stale/nonpositive public mark')
    return dict(origin='REAL_OKX_PUBLIC',observed_at_utc=iso(finish),frames=frames,metadata=metadata,
                marks={s+'-USDT-SWAP':marks[s+'-USDT-SWAP'] for s in freeze['symbols']},
                instruments={s+'-USDT-SWAP':catalog[s+'-USDT-SWAP'] for s in freeze['symbols']},funding=funding,
                news=dict(status='UNKNOWN_COVERAGE',events=[],source='No verified current comprehensive event feed',win_rate_adjustment=None))
