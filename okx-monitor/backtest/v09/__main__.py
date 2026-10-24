import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path
import gzip
import json
import sys
from .config import CONFIG, STEPS, STRATEGIES
from .data import timestamp, iso, instruments, download
from .strategies import features, signal
from .research import splits, simulate, stats, evidence
from .report import candidate
from .exits import Plan
from .events import assess

def parser():
    p = argparse.ArgumentParser(description='V09 public historical research only; no orders or notifications')
    p.add_argument('--start', default='2024-01-01T00:00:00Z')
    p.add_argument('--end', default='2026-01-01T00:00:00Z')
    p.add_argument('--symbols', nargs='+', default=['BTC','ETH','SOL','XRP','ADA','DOGE','LINK','AVAX','LTC','BCH','DOT','TRX'])
    p.add_argument('--bars', nargs='+', choices=list(STEPS), default=list(STEPS))
    p.add_argument('--cache', default='research_v09_cache')
    p.add_argument('--output', default='v09/results')
    p.add_argument('--events', help='Optional verified point-in-time event JSON list')
    p.add_argument('--instrument-snapshot', help='Reuse coverage.json contract metadata for exact reproduction')
    p.add_argument('--fetch-only', action='store_true')
    return p

def run(args):
    start, end = timestamp(args.start), timestamp(args.end)
    spans = splits(start, end)
    if len(set(args.symbols)) != len(args.symbols) or not args.symbols:
        raise ValueError('duplicate/empty symbols')
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    catalog = json.loads(Path(args.instrument_snapshot).read_text())['current_instruments'] if args.instrument_snapshot else instruments()
    requested = [s if s.endswith('-USDT-SWAP') else s+'-USDT-SWAP' for s in args.symbols]
    coverage, failures, datasets = [], {}, {}
    def load(sym, bar):
        if sym not in catalog:
            raise ValueError('not currently listed live USDT linear perpetual')
        return download(sym, bar, start, end, args.cache)
    with ThreadPoolExecutor(max_workers=3) as pool:
        tasks = {pool.submit(load,s,b):(s,b) for b in args.bars for s in requested}
        for task in as_completed(tasks):
            sym, bar = tasks[task]
            try:
                raw, sha = task.result()
                datasets[(sym,bar)] = raw
                coverage.append(dict(symbol=sym,bar=bar,candles=len(raw),sha256=sha))
                print('DATA',sym,bar,len(raw),flush=True)
            except Exception as e:
                failures[sym+'|'+bar] = type(e).__name__+': '+str(e)
                print('FAILED',sym,bar,failures[sym+'|'+bar],flush=True)
    coverage.sort(key=lambda r:(r['bar'],r['symbol']))
    (out/'coverage.json').write_text(json.dumps({'coverage':sorted(coverage,key=lambda r:(r['bar'],r['symbol'])), 'failures':failures,
                                               'current_instruments':{s:catalog[s] for s in requested if s in catalog}},indent=2)+'\n')
    if args.fetch_only:
        return 2 if failures else 0
    rows, all_trades, all_curves, grouped = [], [], [], []
    events = json.loads(Path(args.events).read_text()) if args.events else []
    for bar in args.bars:
        markets = {}
        for sym in requested:
            if (sym,bar) not in datasets:
                continue
            x = features(datasets[(sym,bar)],STEPS[bar])
            records = list(x.itertuples(index=False))
            signals = {s:[signal(records,i,s) for i in range(len(records))] for s in STRATEGIES}
            markets[sym] = {'frame':x,'records':records,'signals':signals,'instrument':catalog[sym]}
        if not markets:
            continue
        for strat in (*STRATEGIES,'ALL'):
            for mult, execution in ((1.,'base'),(2.,'base'),(1.,'missed_limits')):
                for split in ('train','validation','holdout'):
                    row,trades,curve = simulate(markets,strat,spans[split],STEPS[bar],mult,5,execution,events=events)
                    meta = dict(bar=bar,split=split,scenario_strategy=strat,cost_mult=mult,execution=execution,leverage=5)
                    row.update(meta)
                    row['walk_forward_block'] = {'validation':1,'holdout':2}.get(split)
                    row['evidence'] = evidence(trades) if split != 'train' else {'status':'TRAINING_NOT_EVIDENCE'}
                    rows.append(row)
                    all_trades.extend(dict(t,**meta) for t in trades)
                    all_curves.extend(dict(t,**meta) for t in curve)
                    groups = defaultdict(list)
                    for t in trades:
                        for dim in ('symbol','side','year','regime','strategy'):
                            groups[(dim,str(t[dim]))].append(t)
                    grouped.extend(dict(**meta,dimension=dim,value=value,**stats(ts)) for (dim,value),ts in sorted(groups.items()))
                    print('RESULT',bar,strat,mult,execution,split,row['trades'],round(row['return_pct'],3),flush=True)
        for lev in (10,20):
            for split in ('train','validation','holdout'):
                row,trades,curve = simulate(markets,'ALL',spans[split],STEPS[bar],1.,lev,events=events)
                meta = dict(bar=bar,split=split,scenario_strategy='ALL',cost_mult=1.,execution='base',leverage=lev)
                row.update(meta)
                rows.append(row)
                all_trades.extend(dict(t,**meta) for t in trades)
                all_curves.extend(dict(t,**meta) for t in curve)
    result = {'version':'0.9.0','status':'NO QUALIFIED SIGNAL','ready_for_live':False,
              'config':asdict(CONFIG),'config_sha256':CONFIG.digest(),'event_input':events,'interval':[args.start,args.end],
              'splits':{k:[iso(t) for t in v] for k,v in spans.items()},
              'coverage':coverage,'failures':failures,'complete':not failures and len(rows)==42*len(args.bars),
              'results':rows,'grouped_results':grouped,
              'limitations':['Static requested basket of currently listed instruments, NOT all-market or historical top-200; survivorship bias.',
              'Fixed predeclared parameters; temporal holdout reserved in this run but NOT certified pristine research history. No forward verification.',
              'No real cross liquidation or account synchronization; 100x rejected. No trade qualifies for user review.',
              'Fixed assumed 32 TWD/USDT dated 2023-01-01; NOT observed FX or actual account equity.',
              'Adverse funding assumption both sides; conservative full-bar funding even if intrabar exit precedes funding.',
              'Taker fees, adverse slippage, stop-first OHLC; no tick/mark/orderbook. All TP filled vs missed-limit stress brackets uncertainty.',
              'Partial exits fixed fractions; arbitrary partial entry fills and resting-order queues NOT simulated.',
              'No complete point-in-time news history or correlation matrix; joint 20% adverse gap is not cross liquidation safety.',
              'Unverified win-rate blocks real qualification. Exploratory portfolio admits structural net RR>=3, fixed risk/capital controls.',
              'Walk-forward blocks reuse validation/holdout; never double counts. Multiple comparisons disclosed, no best-case selection.',
              'Drawdown at candle marks; losses can exceed budget; paper fee reserve is not physical cross-margin isolation.']}
    (out/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    reports = []
    for t in all_trades:
        plan = Plan(t['entry'],t['stop'],t['tp1'],t['tp2'],t['side'],tuple(t['weights']))
        allocation = {k:t[k] for k in ('notional_twd','risk_twd','units','paper_leverage')}
        allocation.update(cross_verified=False,fx_twd_per_usdt=32,fx_time='2023-01-01T00:00:00Z',fx_source='RESEARCH_ASSUMPTION')
        r = candidate(t['symbol'],t['strategy'],plan,evidence([]),allocation,
                      assess(events,t['entry_time'],t['symbol']),'EXPLORATORY_PAPER_NOT_QUALIFIED',t['simultaneous_risk_twd'],t['cost_mult'])
        r.update(historical_entry_time=t['entry_time'],bar=t['bar'],split=t['split'],scenario_strategy=t['scenario_strategy'],
                 execution=t['execution'],retrospective_example=True)
        reports.append(r)
    for name,data in (('trades',all_trades),('equity',all_curves),('candidate_reports',reports)):

        with gzip.open(out/(name+'.jsonl.gz'),'wt',encoding='utf-8') as f:
            for r in data:
                f.write(json.dumps(r,ensure_ascii=False,allow_nan=False)+'\n')
    return 0 if result['complete'] else 2

if __name__ == '__main__':
    try:
        sys.exit(run(parser().parse_args()))
    except Exception as e:
        print('FAIL_CLOSED_V09',type(e).__name__,str(e),file=sys.stderr)
        sys.exit(2)
