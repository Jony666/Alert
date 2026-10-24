import argparse
from collections import defaultdict,Counter
from dataclasses import asdict,replace
from datetime import datetime,timezone
from pathlib import Path
import gzip,hashlib,json,sys
import numpy as np
import pandas as pd
from ..config import CONFIG,STEPS,DAY
from ..data import timestamp,validate
from ..strategies import features
from ..phase3.strategies import enrich,signal as old_signal
from ..phase3.__main__ import SPLITS
from ..research import simulate
from .design import CORES,VARIANTS,EXITS,DESIGN,experiments,staged_signal,make_plan,mode,digest
from .evaluation import summarize,classify

def dump(path,obj):path.write_text(json.dumps(obj,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
def parser():
    p=argparse.ArgumentParser(description='Two-core-only pre-registered Phase4 paper research')
    p.add_argument('--cache',required=True);p.add_argument('--output',default='v09/phase4_results')
    p.add_argument('--snapshot',default='v09/phase3_results/coverage.json')
    p.add_argument('--expanded-events',default='v09/phase4_results/events_expanded.json')
    return p

def run(args):
    out=Path(args.output);out.mkdir(parents=True,exist_ok=True)
    root=Path(__file__).resolve().parents[1]
    base=json.loads(Path(args.snapshot).read_text());catalog=base['current_instruments']
    old_events=json.loads((root/'verified_events.json').read_text());expanded=json.loads(Path(args.expanded_events).read_text())
    start=timestamp('2022-01-01T00:00:00Z');end=timestamp('2026-01-01T00:00:00Z')
    spans={k:tuple(timestamp(v+'T00:00:00Z') for v in pair) for k,pair in SPLITS.items()}
    cases=experiments()
    freeze=dict(design=asdict(DESIGN),design_sha256=digest(),cases=cases,case_count=len(cases)*3,
                created_utc=datetime.now(timezone.utc).isoformat(),financial_controls=asdict(CONFIG),
                splits=SPLITS,all_2022_2025_is_reused_research_data=True,forward_status='尚待前瞻驗證',
                baseline_events=old_events,expanded_events=expanded,
                exit_selection='four validation folds only; never highest test return',
                ranking='A n>=100/proxy>=8 each fold/3 positive blocks/CI lower>=.60/DD<20/3 hold-compliant/all double costs nonnegative; C only credible n/proxy and >=3 nonpositive blocks; otherwise B')
    dump(out/'freeze.json',freeze)
    sources=list(root.glob('*.py'))+list((root/'phase3').glob('*.py'))+list((root/'phase4').glob('*.py'))
    dump(out/'sources.json',{str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(sources)})
    data={};coverage=[]
    for c in base['coverage']:
        sym=c['symbol'];bar=c['bar'];inst=sym+'-USDT-SWAP'
        path=Path(args.cache)/f'{inst}_{bar}_{start}_{end}.csv'
        sha=hashlib.sha256(path.read_bytes()).hexdigest()
        if sha!=c['sha256']:raise ValueError('source hash changed '+str(path))
        raw=pd.read_csv(path);validate(raw,STEPS[bar],start,end)
        if int(catalog[inst]['listTime'])>start:raise ValueError('not listed before interval')
        data[(sym,bar)]=raw;coverage.append(dict(c,verified_sha256=sha,verified_candles=len(raw),complete=True))
    if len(coverage)!=90:raise ValueError('Phase3 coverage is not 90 complete histories')
    dump(out/'coverage.json',dict(coverage=coverage,current_instruments=catalog,coverage_fraction=1.,historical_universe_verified=False))
    prior=json.loads((root/'phase3_results/summary.json').read_text())
    reference={(r['bar'],r['strategy'],r['split'],r['cost_mult'],r['execution']):r for r in prior['results'] if r['strategy'] in CORES}
    reference_trades=defaultdict(list)
    with gzip.open(root/'phase3_results/trades.jsonl.gz','rt') as f:
        for line in f:
            t=json.loads(line)
            if t['scenario_strategy'] in CORES:reference_trades[(t['bar'],t['scenario_strategy'],t['split'],t['cost_mult'],t['execution'])].append(t)
    rows=[];groups=[];replays=[];technical=[]
    with gzip.open(out/'trades.jsonl.gz','wt') as tf,gzip.open(out/'equity.jsonl.gz','wt') as ef:
        for bar,step in STEPS.items():
            btc=features(data[('BTC',bar)],step);markets={}
            schedules={(s,v):{s:defaultdict(list)} for s in CORES for v in VARIANTS}
            stages={(s,v):defaultdict(Counter) for s in CORES for v in VARIANTS}
            for inst in sorted(catalog):
                sym=inst.split('-')[0];x=enrich(data[(sym,bar)],step,data[('BTC',bar)],btc)
                records=list(x.itertuples(index=False));markets[inst]=dict(frame=x,records=records,instrument=catalog[inst],signals={})
                for core in CORES:
                    for variant in VARIANTS:
                        for i in range(len(records)-1):
                            side,score,stage=staged_signal(records,i,core,variant)
                            if variant=='base' and (side,score)!=old_signal(records,i,core):raise AssertionError('technical baseline drift')
                            stages[(core,variant)][i+1][stage]+=1
                            if side:schedules[(core,variant)][core][i+1].append((-score,-float(records[i].liquidity),inst,core,side))
            ts=next(iter(markets.values()))['frame'].ts.to_numpy()
            for core,variant,ex,split,pressure in cases:
                span=spans[split];a,b=map(int,np.searchsorted(ts,span))
                funnel=Counter()
                for j in range(a,b):funnel.update(stages[(core,variant)].get(j,{}))
                cfg=CONFIG;mult=2. if pressure=='double_cost' else 1.;execution='missed_limits' if pressure=='missed_limits' else 'base'
                if pressure=='slip2pct':cfg=replace(CONFIG,slippage=.02)
                if pressure=='liquidity_1pct':cfg=replace(CONFIG,participation=CONFIG.participation*.01)
                events=expanded if pressure=='expanded_events' else old_events
                factory=lambda s,p,e,d:make_plan(s,variant,ex,p,e,d)
                row,trades,curve=simulate(markets,core,span,step,mult,5,execution,cfg,events,
                    strategy_names=CORES,plan_factory=factory,candidate_schedule=schedules[(core,variant)],
                    hold_days_by_strategy={s:3 for s in CORES},exit_mode=mode(ex),gap_fraction=.10 if pressure=='gap10pct' else 0.)
                meta=dict(bar=bar,core=core,variant=variant,exit=ex,split=split,pressure=pressure)
                row.update(summarize(trades));row.update(meta)
                row['funnel']=dict(raw_patterns=sum(n for k,n in funnel.items() if k!='no_raw_pattern'),technical_pass=funnel.get('technical_pass',0),
                                   technical_first_rejections=dict(funnel),held_symbol_exclusions=funnel.get('technical_pass',0)-row['counts'].get('raw_candidate_signals',0),
                                   planned_rr_pass=row['counts'].get('structural_net_rr_pass',0),paper_account_gate_pass=row['counts'].get('account_gate_pass',0),
                                   all_paper_risk_gates_pass=row['counts'].get('all_risk_gates_pass',0),simulated_entries=row['counts'].get('entries',0),rejections=row['counts'])
                assert row['funnel']['all_paper_risk_gates_pass']==row['funnel']['simulated_entries']
                if variant=='base' and ex=='split_atr' and pressure in ('base','double_cost','missed_limits'):
                    key=(bar,core,split,mult,execution);old=reference[key]
                    metrics=('trades','win_rate','net_twd','return_pct','max_drawdown_pct','profit_factor','avg_net_r','avg_hold_days')
                    assert all(row[k]==old[k] for k in metrics),(key,'baseline metric drift')
                    ot=reference_trades[key]
                    assert len(ot)==len(trades)
                    for t,u in zip(trades,ot):
                        for k in ('symbol','entry_time','exit_time','entry','stop','tp1','tp2','weights','fills','net_twd','risk_twd','planned_net_rr'):
                            value=list(t[k]) if k=='weights' else t[k]
                            assert value==u[k],(key,k)
                    replays.append(dict(bar=bar,core=core,split=split,pressure=pressure,exact_metric_and_trade_match=True))
                rows.append(row)
                for t in trades:tf.write(json.dumps(dict(t,**meta),allow_nan=False)+'\n')
                for t in curve:ef.write(json.dumps(dict(t,**meta),allow_nan=False)+'\n')
                g=defaultdict(list)
                for t in trades:
                    for dim in ('symbol','side','year','regime'):g[(dim,str(t[dim]))].append(t)
                groups.extend(dict(**meta,dimension=d,value=v,**summarize(items)) for (d,v),items in sorted(g.items()))
                print('RESULT',bar,core,variant,ex,split,pressure,len(trades),round(row['return_pct'],3),flush=True)
    result=dict(complete=len(rows)==len(cases)*3,scenario_count=len(rows),version=DESIGN.version,
                status='NO QUALIFIED SIGNAL',qualified_strategy_count=0,rows=rows,groups=groups,
                ranking=classify(rows),phase3_baseline_comparisons=replays,
                limitations=['All historical windows inspected, not pristine. Forward data pending; no new clean test manufactured.',
                '30 current survivors; historical delistings, membership, specs, and fees not reconstructed.',
                'Public CURRENT tiers/marks reviewed separately; no official account cross liquidation certification.',
                'Partial official event corpus; no news probability adjustment. Invalid publication timestamps excluded.',
                'N/CI/thinning/block bootstrap do not establish independent effective N; all candidates unverified.',
                'Gap/slippage/liquidity stresses are labelled hypothetical perturbations, NOT real market observations.',
                'Each split is independent paper capital, not reset authorization for an actual halted account.'])
    dump(out/'summary.json',result);write_report(out,result)
    return 0 if result['complete'] else 2

def write_report(out,r):
    def f(v):return 'N/A' if v is None else f'{v:.3f}'
    for core in CORES:
        lines=['# '+core+' 深度研究','', '**沒有合格策略；2025是已受研究影響的歷史測試，尚待前瞻驗證。**','',
        '|週期|退出|壓力|N|勝率%/95%CI|計畫RR|實現盈虧比|平均淨R|PF|淨報酬%|DD%|均/中/p95/最長天數|','|---|---|---|---:|---|---:|---:|---:|---:|---:|---:|---|']
        for z in r['rows']:
            if z['core']==core and z['variant']=='base' and z['split']=='test_2025_contaminated':
                ci=z['win_ci_95'];win=f(None if z['win_rate'] is None else z['win_rate']*100)
                holds='/'.join(f(v) for v in (z['avg_hold_days'],z['median_hold_days'],z['hold_percentiles_days']['95'],z['max_hold_days']))
                vals=[z['bar'],z['exit'],z['pressure'],str(z['trades']),f'{win} / {ci[0]:.3f}–{ci[1]:.3f}']
                vals += [f(z[k]) for k in ('avg_planned_net_rr','realized_net_payoff','avg_net_r','profit_factor','return_pct','max_drawdown_pct')];vals.append(holds)
                lines.append('|'+'|'.join(vals)+'|')
        dec=next(z for z in r['ranking']['core_decisions'] if z['core']==core)
        lines+=['','## 分類與退出選擇','',json.dumps(dec,ensure_ascii=False,indent=2),'',
                '所有鄰近參數、驗證區塊、逐層拒絕、市況/幣種、尾部R、期望值、平均獲利/虧損、獨立樣本限制及完整A/B/C理由在summary.json。不得用最高測試報酬選退出。']
        (out/(core+'.md')).write_text('\n'.join(lines)+'\n')
    lines=['# 第四階段A/B/C評估（只讀驗證區塊）','','|核心|週期|退出|分類|驗證N|最少14日代理|理由|','|---|---|---|---|---:|---:|---|']
    for p in r['ranking']['profiles']:lines.append('|'+ '|'.join([p['core'],p['bar'],p['exit'],p['category'],str(p['validation_n']),str(p['proxy_min']),'；'.join(p['reasons'])])+'|')
    (out/'RANKING.md').write_text('\n'.join(lines)+'\n')

if __name__=='__main__':
    try:sys.exit(run(parser().parse_args()))
    except Exception as e:print('FAIL_CLOSED_PHASE4',type(e).__name__,str(e),file=sys.stderr);sys.exit(2)
