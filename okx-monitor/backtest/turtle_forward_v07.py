#!/usr/bin/env python3
"""Forward-only, NO REAL ORDERS / NO PUSH, paper shadow study of Turtle S1/S2 SHORT.

No exchange authentication, no order endpoints, no Telegram/email, no repository writes.
State is carried between GitHub Actions runs through immutable run artifacts, never
through commits (commits could redeploy the separate Render service).

An arm event records a hypothetical resting stop SELL for an as-yet-unseen bar.
A later run may simulate execution using that *later* bar's OHLC. All fills
are theoretical OHLC estimates, not an exchange paper account or executable quotes.
"""
from __future__ import annotations
import argparse, csv, hashlib, io, json, math, os, sys, time, urllib.request, zipfile
from bisect import bisect_right
from datetime import datetime, timezone
from pathlib import Path

MS30 = 1_800_000
DAY = 86_400_000
H8 = 28_800_000
TPE = H8
START_CASH = 10_000.0
SYMBOLS = ('BTC','ETH','SOL','XRP','ADA','DOGE','LINK','AVAX','LTC','BCH','DOT','TRX')
SYSTEMS = {'S1_BTC_BEAR': {'entry':20,'exit':10},'S2_BTC_BEAR':{'entry':55,'exit':20}}
ARTIFACT = 'okx-turtle-forward-v07-state'
WORKFLOW = 'okx-turtle-forward-v07.yml'
LIMIT_CATCHUP_BARS = 100
VERSION = 7


def iso(ms): return datetime.fromtimestamp(ms/1000,timezone.utc).isoformat()
def bucket(ts): return ((ts+TPE)//DAY)*DAY-TPE


def valid_bar(b, interval):
    if not set(('ts','o','h','l','c')).issubset(b):raise ValueError('missing candle fields')
    if int(b['ts']) % interval != (bucket(0) % DAY if interval == DAY else 0):raise ValueError('unaligned candle')
    x=[float(b[k]) for k in ('o','h','l','c')]
    if not all(math.isfinite(v) and v>0 for v in x):raise ValueError('invalid price')
    if not (b['h'] >= max(b['o'],b['c'],b['l']) and b['l']<=min(b['o'],b['c'],b['h'])):
        raise ValueError('invalid OHLC')


def check_bars(bars, interval):
    if not bars:raise ValueError('no candles')
    for i,b in enumerate(bars):
        valid_bar(b,interval)
        if i and b['ts']-bars[i-1]['ts']!=interval:raise ValueError('missing or duplicated OHLCV timestamps')


def parse_ohlc(rows, interval):
    d={}
    for r in rows:
        if len(r)<9 or str(r[8])!='1':continue
        b={'ts':int(r[0]),'o':float(r[1]),'h':float(r[2]),'l':float(r[3]),'c':float(r[4])}
        if b['ts'] in d and d[b['ts']]!=b:raise ValueError('conflicting duplicate candles')
        d[b['ts']]=b
    seq=[d[t] for t in sorted(d)]
    check_bars(seq,interval)
    return seq


def okx_candles(inst, bar, cap, interval, opener=None):
    """Only public, confirmed OHLC; fail on 429, missing candles or API errors."""
    import urllib.parse
    url='https://www.okx.com/api/v5/market/history-candles?'+urllib.parse.urlencode({'instId':inst,'bar':bar,'limit':cap})
    if opener is None:opener=urllib.request.urlopen
    last=None
    for i in range(3):
        try:
            req=urllib.request.Request(url,headers={'User-Agent':'OKX-paper-research-v07/1.0'})
            with opener(req,timeout=25) as r: payload=json.load(r)
            if str(payload.get('code'))!='0':raise ValueError('OKX response '+str(payload.get('code')))
            return parse_ohlc(payload['data'],interval)
        except (OSError,ValueError,KeyError,AssertionError) as e:
            last=e
            if i<2:time.sleep(i+1)
    raise RuntimeError('PUBLIC DATA ERROR '+inst+' '+bar+': '+str(last))


def daily_features(bars, ts):
    """Point-in-time: use only fully closed UTC+8 daily candles before bar start."""
    ends=[b['ts']+DAY for b in bars]
    stop=bisect_right(ends,ts)  # an ending at exactly ts is known
    history=bars[:stop]
    if len(history)<225:raise ValueError('insufficient 225 completed daily candles')
    closes=[b['c'] for b in history]
    sma200=sum(closes[-200:])/200
    prev=None;N=None
    for b in history:
        tr=max(b['h']-b['l'],abs(b['h']-prev) if prev else 0,abs(b['l']-prev) if prev else 0)
        N=tr if N is None else N+(tr-N)/20
        prev=b['c']
    return {'sma200':sma200,'close':closes[-1], 'n20':N,
            'low20':min(x['l'] for x in history[-20:]),
            'low55':min(x['l'] for x in history[-55:]),
            'high10':max(x['h'] for x in history[-10:]),
            'high20':max(x['h'] for x in history[-20:]),
            'last_day_end':ends[stop-1]}


def state_digest(state):
    raw=json.dumps({k:v for k,v in state.items() if k!='sha256'},sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def seal(state):state['sha256']=state_digest(state);return state

def ensure_state(state,syms):
    if state.get('version')!=VERSION:raise ValueError('paper state version mismatch; DO NOT AUTO RESET')
    if state.get('symbols')!=list(syms):raise ValueError('coin universe changed; requires explicit new cohort')
    if state.get('sha256')!=state_digest(state):raise ValueError('paper state integrity failure')
    for label in SYSTEMS:
        if label not in state['accounts']:raise ValueError('missing account '+label)
    if not isinstance(state['last_bar_ts'],int):raise ValueError('invalid watermark')
    return state


def fresh_state(symbols,target,now):
    return seal({'version':VERSION,'symbols':list(symbols),'created_utc':iso(now),
       'last_bar_ts':target,'last_observed_utc':iso(now),'run_count':0,
       'accounts':{s:{'cash':START_CASH,'peak':START_CASH,'max_dd_pct':0.,'halted':False,'halt_next':False,
                       'positions':{},'trades':[],'equity':[],'events':[],'pending':{},'raw_signals':0,
                       'orders_armed':0,'missed_unarmed_breakouts':0} for s in SYSTEMS}})


def gross_equity(a,prices):
    return a['cash']+sum(p['qty']*(p['entry']-prices[k]) for k,p in a['positions'].items())


def close_short(a,symbol,px,ts,reason,cost):
    p=a['positions'].pop(symbol)
    exit_fee=p['qty']*px*cost['fee']
    a['cash']+=p['qty']*(p['entry']-px)-exit_fee
    net=p['qty']*(p['entry']-px)-p['entry_fee']-exit_fee-p['funding_paid']
    a['trades'].append({'symbol':symbol,'entry_ts':p['entry_ts'],'exit_ts':ts,'entry':p['entry'],'exit':px,
                        'notional':p['notional'],'net_pnl_usdt':round(net,8),
                        'net_return_pct_of_notional':round(100*net/p['notional'],5),
                        'funding_paid_usdt':round(p['funding_paid'],8),'reason':reason})
    a['events'].append({'ts':ts,'type':'EXIT','symbol':symbol,'why':reason,'price':round(px,8)})


def arm(a,name,syms,daily,now,target,config):
    """Only arm future bar, never backdate an order to an already opened bar."""
    future=(now//MS30+1)*MS30
    if future<=target:raise ValueError('future order is not future')
    for k in list(a['pending']):
        if int(k)<=target:del a['pending'][k]
    if a['halted'] or a['halt_next']:return
    feat={sym:daily_features(daily[sym],future) for sym in syms}
    btc=feat['BTC']['close']<feat['BTC']['sma200']
    orders={}
    if btc:
        for symbol in syms:
            if symbol in a['positions']:continue
            f=feat[symbol];n=f['n20']; trigger=f['low'+str(config['entry'])]
            if n>0 and trigger>0 and .003<=2*n/trigger<=.35:
                orders[symbol]={'trigger':trigger,'n20':n,'prepared_utc':iso(now),'prepared_for':future,
                                'reference_day_end':f['last_day_end'],'btc_bear':True}
    if orders and str(future) not in a['pending']:
        a['pending'][str(future)]=orders
        a['orders_armed']+=len(orders)
    # Preserve *earlier future* bar orders, even when newer orders are armed.
    # A scan at :37 has not seen the :30 bar close, which is not processed until :07.


def advance_bar(a,name,bar_ts,market,daily,syms,cost):
    cfg=SYSTEMS[name]
    bars={s:market[s][bar_ts] for s in syms}
    current={s:bars[s]['c'] for s in syms}
    exits=set()
    if a['halt_next']:
        for s in sorted(list(a['positions'])):
            close_short(a,s,bars[s]['o']*(1+cost['slip']),bar_ts,'drawdown_halt',cost)
            exits.add(s)
        a['halt_next']=False;a['halted']=True
    for s in sorted(list(a['positions'])):
        p=a['positions'][s]
        # Hypothetical conservative 8h funding charged as a positive cost to SHORT.
        while bar_ts+MS30>=p['next_funding_ts']:
            fee=p['notional']*cost['funding'];a['cash']-=fee;p['funding_paid']+=fee;p['next_funding_ts']+=H8
        channel=daily_features(daily[s],bar_ts)['high'+str(cfg['exit'])]
        hit=[level for level in (p['stop'],channel) if bars[s]['h']>=level]
        if hit:
            price=max(bars[s]['o'],*hit)*(1+cost['slip'])
            close_short(a,s,price,bar_ts+MS30,'stop_or_channel_worst_first',cost)
            exits.add(s)
    orders=a['pending'].pop(str(bar_ts),{})
    if not a['halted'] and not a['halt_next'] and not exits:
        for s in sorted(orders):
            if s in a['positions']:continue
            order=orders[s]
            prepared_ms=int(datetime.fromisoformat(order['prepared_utc']).timestamp()*1000)
            if order['prepared_for']!=bar_ts or prepared_ms>=bar_ts or not order['btc_bear']:
                raise ValueError('invalid future order provenance')
            if bars[s]['l']>order['trigger']:continue
            a['raw_signals']+=1
            if len(a['positions'])>=3:continue
            equity=gross_equity(a,{x:bars[x]['o'] for x in syms})
            if equity<=0:continue
            entry=min(bars[s]['o'],order['trigger'])*(1-cost['slip'])
            stop=entry+2*order['n20']
            riskdist=(stop-entry)/entry
            if not (.003<=riskdist<=.35):continue
            active_risk=sum(max(0,p['qty']*(p['stop']-p['entry'])) for p in a['positions'].values())
            gross=sum(p['notional'] for p in a['positions'].values())
            nom=min(equity*.005/riskdist,equity*.75,max(0,equity*.02-active_risk)/riskdist,
                    max(0,equity*2-gross))
            if nom<equity*.05:continue
            qty=nom/entry;fee=nom*cost['fee'];a['cash']-=fee
            a['positions'][s]={'entry':entry,'stop':stop,'qty':qty,'notional':nom,'entry_ts':bar_ts,
               'entry_fee':fee,'funding_paid':0.,'next_funding_ts':bar_ts+H8}
            a['events'].append({'ts':bar_ts,'type':'PAPER_ENTRY','symbol':s,'price':round(entry,8),'notional':round(nom,3)})
            # Entry stop may already have been touched within same 30m bar.
            if bars[s]['h']>=stop:
                close_short(a,s,max(bars[s]['o'],stop)*(1+cost['slip']),bar_ts+MS30,'same_bar_adverse_stop',cost)
    else:
        for s,order in orders.items():
            if bars[s]['l']<=order['trigger']:a['missed_unarmed_breakouts']+=1
    equity=gross_equity(a,current)
    a['peak']=max(a['peak'],equity)
    dd=max(0,1-equity/a['peak'])*100
    a['max_dd_pct']=max(a['max_dd_pct'],dd)
    if dd>=20 and not a['halted']:a['halt_next']=True
    a['equity'].append({'ts':bar_ts+MS30,'equity':round(equity,5),'drawdown_pct':round(dd,5),
                       'positions':len(a['positions'])})


def paper_step(state,market,daily,now,syms=SYMBOLS,cost=None):
    """In-memory pure state transition; market is {symbol:{bar_ts:ohlc}}."""
    if cost is None:cost={'fee':.0005,'slip':.0005,'funding':.0001}
    for v in cost.values():
        if not 0<=v<=.01:raise ValueError('unsafe/invalid cost assumption')
    target=(now//MS30)*MS30-MS30
    if target<0:raise ValueError('invalid clock')
    if 'BTC' not in syms:raise ValueError('BTC required for market filter')
    if state is None:
        state=fresh_state(syms,target,now)
        for label,cfg in SYSTEMS.items():
            arm(state['accounts'][label],label,syms,daily,now,target,cfg)
        state['run_count']=1
        return seal(state),{'bootstrap':True,'advanced_bars':0,'fresh_cohort':True,'target':target}
    ensure_state(state,syms)
    old=state['last_bar_ts']
    if target<old:raise RuntimeError('clock moved backwards: fail closed')
    steps=(target-old)//MS30
    if steps>LIMIT_CATCHUP_BARS:raise RuntimeError('too many missed bars; manual state audit required')
    if steps:
        for t in range(old+MS30,target+MS30,MS30):
            for sym in syms:
                if t not in market.get(sym,{}):raise RuntimeError('missing completed 30m bar '+sym+' '+iso(t))
            # same bar across scenarios is independent, NO combined-real-account claim
            for label in SYSTEMS:
                advance_bar(state['accounts'][label],label,t,market,daily,syms,cost)
    for label,cfg in SYSTEMS.items():
        arm(state['accounts'][label],label,syms,daily,now,target,cfg)
    state['last_bar_ts']=target;state['last_observed_utc']=iso(now);state['run_count']+=1
    return seal(state),{'bootstrap':False,'advanced_bars':steps,'fresh_cohort':False,'target':target}


def save_outputs(state,out):
    path=Path(out);path.mkdir(parents=True,exist_ok=True)
    ensure_state(state,state['symbols'])
    (path/'state.json').write_text(json.dumps(state,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    report={'status':'FORWARD_PAPER_ONLY_NO_ORDERS','started':state['created_utc'],
            'last_bar_utc':iso(state['last_bar_ts']), 'run_count':state['run_count'],
            'assumptions':{'per_side_fee':.0005,'per_side_slippage':.0005,'8h_funding_short_cost':.0001,
              'note':'Hypothetical resting orders with 30m OHLC; NOT real exchange fills or live quotations.'},
            'systems':{n:{'cash':a['cash'],'open_positions':len(a['positions']),
                     'closed_trades':len(a['trades']),'max_dd_pct':a['max_dd_pct'],
                     'halted':a['halted'],'raw_signals':a['raw_signals'],
                     'pending_windows':len(a['pending']), 'orders_armed':a['orders_armed']}
                      for n,a in state['accounts'].items()},
            'limitations':['No actual orders, accounts, secrets or notifications',
              'Synthetic passive orders are NOT submitted to any exchange',
              'OHLC cannot know exact fill queue, partial fills, or intrabar chronology',
              'Fixed assumed funding (always paid by shorts), fees and slippage',
              'Missing/scheduled-late scans are censored; future order must already be armed',
              'Static 12-symbol universe not historical top-200',
              'No mark-price liquidation, borrowing, margin or portfolio-combined equity']}
    (path/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    for filename,fields,key in [('trades.csv',['system','symbol','entry_ts','exit_ts','entry','exit','notional','net_pnl_usdt','net_return_pct_of_notional','funding_paid_usdt','reason'],'trades'),
                                ('equity.csv',['system','ts','equity','drawdown_pct','positions'],'equity'),
                                ('events.csv',['system','ts','type','symbol','why','price','notional'],'events')]:
        with (path/filename).open('w',newline='',encoding='utf-8') as f:
            w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');w.writeheader()
            for system,account in state['accounts'].items():
                for item in account[key]:w.writerow({'system':system,**item})


def restore_from_github(repository,workflow,current_run,token,destination):
    """Find newest earlier SUCCESS run; do not silently reset if artifact was lost."""
    if not token or not repository or not current_run:raise RuntimeError('missing GitHub read access')
    def get(url):
        req=urllib.request.Request(url,headers={'Authorization':'Bearer '+token,
              'Accept':'application/vnd.github+json','User-Agent':'okx-paper-v07-state-reader'})
        with urllib.request.urlopen(req,timeout=35) as res:return res.read()
    api='https://api.github.com/repos/'+repository
    import urllib.parse
    workflow=urllib.parse.quote(workflow,safe='')
    all_runs=json.loads(get(api+'/actions/workflows/'+workflow+'/runs?per_page=100'))['workflow_runs']
    prior=[r for r in all_runs if r['id']!=int(current_run) and r['status']=='completed']
    prior.sort(key=lambda r:r['created_at'],reverse=True)
    if not prior:
        print('FIRST BOOTSTRAP: no prior workflow runs; no historic trades will be invented.')
        return False
    for r in prior:
        if r['conclusion']!='success':continue
        artifacts=json.loads(get(api+'/actions/runs/'+str(r['id'])+'/artifacts?per_page=100'))['artifacts']
        matching=[a for a in artifacts if a['name']==ARTIFACT and not a['expired']]
        if not matching:raise RuntimeError('previous SUCCESS run lacks state artifact; REFUSE TO RESET')
        ident=matching[0]['id']; archive=get(api+'/actions/artifacts/'+str(ident)+'/zip')
        with zipfile.ZipFile(io.BytesIO(archive)) as z:
            if 'state.json' not in z.namelist():raise RuntimeError('state archive missing state.json')
            obj=json.loads(z.read('state.json'))
        ensure_state(obj,obj.get('symbols'))
        Path(destination).parent.mkdir(parents=True,exist_ok=True)
        Path(destination).write_text(json.dumps(obj,ensure_ascii=False,indent=2)+'\n')
        print('RESTORED from prior successful workflow',r['id'],'artifact',ident)
        return True
    # There have been previous runs, but none succeeded. Never silently bootstrap
    # unless first run (it might conceal earlier partially observed activity).
    raise RuntimeError('prior workflow runs exist but no successful state; manual review required')


def fetch_all(symbols,now,previous_ts=None):
    target=(now//MS30)*MS30-MS30
    market={};daily={}
    for sym in symbols:
        inst=sym+'-USDT-SWAP'
        bars=okx_candles(inst,'30m',LIMIT_CATCHUP_BARS,MS30)
        if not bars or bars[-1]['ts']<target:raise RuntimeError(inst+' recent 30m candles stale')
        time.sleep(.12)  # Shared-IP API rate-limit guard.
        d=okx_candles(inst,'1D',300,DAY)
        if not d or d[-1]['ts']+DAY>target+MS30:raise RuntimeError(inst+' daily close from future')
        if now-(d[-1]['ts']+DAY)>DAY:raise RuntimeError(inst+' daily candle feed stale')
        time.sleep(.12)
        # daily may contain last fully closed, never partial; last day may be < current.
        needed=target if previous_ts is None else previous_ts+MS30
        available={b['ts']:b for b in bars if needed<=b['ts']<=target}
        if previous_ts is not None:
            for t in range(needed,target+MS30,MS30):
                if t not in available:raise RuntimeError(inst+' gap at '+iso(t))
        market[sym]=available;daily[sym]=d
    return market,daily


def cli(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode',choices=['restore','scan'])
    p.add_argument('--state',default='paper_v07/state.json')
    p.add_argument('--out',default='paper_v07')
    p.add_argument('--repository',default=os.environ.get('GITHUB_REPOSITORY',''))
    p.add_argument('--run-id',default=os.environ.get('GITHUB_RUN_ID',''))
    a=p.parse_args(argv)
    if a.mode=='restore':
        restore_from_github(a.repository,WORKFLOW,a.run_id,os.environ.get('GH_TOKEN',''),a.state);return 0
    state=None
    if Path(a.state).exists():state=json.loads(Path(a.state).read_text())
    now=int(time.time()*1000)
    market,daily=fetch_all(SYMBOLS,now,None if state is None else state['last_bar_ts'])
    result,info=paper_step(state,market,daily,now)
    save_outputs(result,a.out)
    print('PAPER_ONLY NO_REAL_ORDERS NO_PUSH '+json.dumps({'info':info,'systems':{k:{'closed_trades':len(v['trades']),
           'positions':len(v['positions']),'max_dd_pct':v['max_dd_pct']} for k,v in result['accounts'].items()}}))
    return 0


if __name__=='__main__':
    try:raise SystemExit(cli())
    except Exception as e:
        print('FAIL_CLOSED_PAPER_V07:',type(e).__name__,str(e),file=sys.stderr);raise SystemExit(2)
