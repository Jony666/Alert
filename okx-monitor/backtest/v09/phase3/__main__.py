import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor,as_completed
from dataclasses import asdict
from datetime import datetime,timezone
from pathlib import Path
import gzip,hashlib,json,sys
from ..config import CONFIG,STEPS,DAY
from ..data import download,instruments,timestamp,iso
from ..research import simulate
from ..strategies import features
from ..events import assess
from .strategies import STRATEGIES,NEW,RULES,rules_hash,enrich,signal,plan
from .evaluation import summarize,rank

SYMBOLS='BTC ETH SOL XRP ADA DOGE LINK AVAX LTC BCH DOT TRX ETC FIL UNI AAVE ATOM NEAR ALGO XLM SAND MANA ICP CRV SUSHI COMP GRT ZIL BAT SNX'.split()
SPLITS={'train_2022':('2022-03-02','2023-01-01'),
        'wf_2023H1':('2023-01-15','2023-07-01'),'wf_2023H2':('2023-07-15','2024-01-01'),
        'wf_2024H1':('2024-01-15','2024-07-01'),'wf_2024H2':('2024-07-15','2025-01-01'),
        'test_2025_contaminated':('2025-01-15','2026-01-01')}
# A 14d embargo precedes each next evaluation block. Fixed rules, no fitting.
# Train is inspected only diagnostically; ranking reads wf_* validation only.

def dump(path,obj):path.write_text(json.dumps(obj,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
def parser():
    p=argparse.ArgumentParser(description='Phase3 paper-only frozen multi-market research')
    p.add_argument('--cache',default='phase3_cache')
    p.add_argument('--output',default='v09/phase3_results')
    p.add_argument('--snapshot',required=True)
    p.add_argument('--events',default='v09/verified_events.json')
    return p

def run(args):
    out=Path(args.output);out.mkdir(parents=True,exist_ok=True)
    catalog=json.loads(Path(args.snapshot).read_text())['current_instruments']
    events=json.loads(Path(args.events).read_text())
    start=timestamp('2022-01-01T00:00:00Z');end=timestamp('2026-01-01T00:00:00Z')
    spans={k:tuple(timestamp(v+'T00:00:00Z') for v in pair) for k,pair in SPLITS.items()}
    freeze={'version':RULES.version,'created_utc':datetime.now(timezone.utc).isoformat(),
            'strategy_rules':asdict(RULES),'rules_sha256':rules_hash(),'financial_controls':asdict(CONFIG),
            'strategies':STRATEGIES,'symbols':SYMBOLS,'splits':SPLITS,'family_size':24,
            'prospective_after_freeze_only':'No 2022–2025 interval is pristine. New forward observation must start after pinned code commit; no tuning/retraining on it.',
            'ranking':'Validation-only deterministic lexicographic rubric; see phase3/evaluation.py; never selects from 2025 test.',
            'ranking_rubric':{'validation_n':100,'min_proxy_per_half_year':8,'min_positive_blocks':3,'max_dd_lt':20,'min_win_lower':.60,'all_double_cost_returns_nonnegative':True},
            'new_time_exit_days':3,'baseline_time_exit_days':7}
    dump(out/'freeze.json',freeze)
    source_root=Path(__file__).resolve().parents[1]
    source_paths=list(source_root.glob('*.py'))+list((source_root/'phase3').glob('*.py'))
    dump(out/'sources.json',{str(p.relative_to(source_root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(source_paths)})
    datasets={};coverage=[];failures={}
    def load(sym,bar):
        inst=sym+'-USDT-SWAP'
        spec=catalog[inst]
        if int(spec['listTime'])>start:raise ValueError('listed after requested start; cannot claim complete interval')
        return download(inst,bar,start,end,args.cache)
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures={pool.submit(load,s,b):(s,b) for b in STEPS for s in SYMBOLS}
        for task in as_completed(futures):
            sym,bar=futures[task]
            try:
                raw,sha=task.result();datasets[(sym,bar)]=raw
                coverage.append(dict(symbol=sym,bar=bar,candles=len(raw),sha256=sha,source='OKX public history-candles confirmed=1',open_interval=[iso(start),iso(end)]))
            except Exception as e:failures[sym+'|'+bar]=type(e).__name__+': '+str(e)
    coverage.sort(key=lambda r:(r['bar'],r['symbol']))
    dump(out/'coverage.json',dict(coverage=coverage,failures=failures,current_instruments={s+'-USDT-SWAP':catalog[s+'-USDT-SWAP'] for s in SYMBOLS if s+'-USDT-SWAP' in catalog}))
    if failures or len(coverage)!=90:
        dump(out/'summary.json',dict(complete=False,failures=failures,status='NO QUALIFIED SIGNAL'))
        return 2
    rows=[];groups=[]
    trade_stream=gzip.open(out/'trades.jsonl.gz','wt')
    equity_stream=gzip.open(out/'equity.jsonl.gz','wt')
    try:
        for bar,step in STEPS.items():
            btc_context=features(datasets[('BTC',bar)],step)
            markets={};schedule={s:defaultdict(list) for s in STRATEGIES}
            for sym in SYMBOLS:
                raw=datasets[(sym,bar)]
                x=enrich(raw,step,datasets[('BTC',bar)],btc_context);records=list(x.itertuples(index=False))
                inst=sym+'-USDT-SWAP'
                markets[inst]=dict(frame=x,records=records,instrument=catalog[inst],signals={})
                for strat in STRATEGIES:
                    for i in range(len(records)-1):
                        side,strength=signal(records,i,strat)
                        if side:schedule[strat][i+1].append((-strength,-float(records[i].liquidity),inst,strat,side))
            for strat in (*STRATEGIES,'ALL'):
                selected_splits=spans if strat!='ALL' else {k:v for k,v in spans.items() if k in ('train_2022','wf_2024H2','test_2025_contaminated')}
                for mult,execution in ((1.,'base'),(2.,'base'),(1.,'missed_limits')):
                    for split,span in selected_splits.items():
                        row,trades,curve=simulate(markets,strat,span,step,mult,5,execution,
                            events=events,strategy_names=STRATEGIES,plan_factory=plan,candidate_schedule=schedule,
                            hold_days_by_strategy={s:RULES.new_time_exit_days for s in NEW})
                        meta=dict(bar=bar,split=split,scenario_strategy=strat,cost_mult=mult,execution=execution,leverage=5)
                        row.update(summarize(trades));row.update(meta)
                        row['regime_count']=len({t['regime'] for t in trades});rows.append(row)
                        for t in trades:trade_stream.write(json.dumps(dict(t,**meta),allow_nan=False)+'\n')
                        for t in curve:equity_stream.write(json.dumps(dict(t,**meta),allow_nan=False)+'\n')
                        grouped=defaultdict(list)
                        for t in trades:
                            for dim in ('symbol','side','year','regime'):grouped[(dim,str(t[dim]))].append(t)
                        for (dim,value),ts in sorted(grouped.items()):groups.append(dict(**meta,dimension=dim,value=value,**summarize(ts)))
                        print('RESULT',bar,strat,mult,execution,split,row['trades'],round(row['return_pct'],4),flush=True)
    finally:trade_stream.close();equity_stream.close()
    expected=(8*6*3+1*3*3)*3
    result=dict(version=RULES.version,complete=len(rows)==expected,scenario_count=len(rows),
        status='NO QUALIFIED SIGNAL',qualified_strategy_count=0,coverage=coverage,failures=failures,
        results=rows,grouped_results=groups,rankings=rank(rows),rules_sha256=rules_hash(),
        prior_results_preserved='v09/results and v09/round2_results; no old result files rewritten',
        limitations=['2022–2025 has been repeatedly inspected, NOT pristine; no actual forward verification.',
        '30 current survivors, not historical all-market/top200/delisting reconstruction. Historical contract specifications unavailable.',
        'Fixed 32 TWD/USDT is model assumption, not observed historical FX.',
        'Fees/slippage/adverse funding assumed, not historical ticks/orderbook/funding. Missed-limit and 2x costs are explicit separate cases.',
        'Repeated strategy/asset/regime comparisons and correlated trades remain; family24 interval and thinning do not prove IID.',
        '4 verified Fed statements only; no complete election/macro/news corpus. No quantified news win-rate adjustment.',
        'Cross liquidation not officially verified. All 5x results are paper-only; 100x refused; no live recommendations.',
        'Each measured split is an independent hypothetical account, no reset of a real halted account.',
        '1D intrabar exit timestamps unknown; daily loss gates use conservative candle-end timestamps.'])
    dump(out/'summary.json',result)
    write_report(out,result)
    return 0 if result['complete'] else 2

def write_report(out,r):
    def f(v):return 'N/A' if v is None else f'{v:.3f}'
    lines=['# 第三階段完整結果','',f"共{r['scenario_count']}情境；30種固定合約、2022–2025。所有新舊策略均未取得實盤資格。",'',
    '下表是5倍、基準成本、2025受污染測試；不是乾淨樣本外證據。實現盈虧比=平均淨獲利/平均淨虧損絕對值，計畫淨RR與平均淨R另列。', '',
    '|週期|策略|筆數|勝率%|95%CI|計畫淨RR|實現盈虧比|平均淨R|淨報酬%|DD%|均/中位/最長天數|',
    '|---|---|---:|---:|---|---:|---:|---:|---:|---:|---|']
    for z in r['results']:
        if z['split']=='test_2025_contaminated' and z['cost_mult']==1 and z['execution']=='base':
            ci=z['win_ci_95'];hold='/'.join(f(z[k]) for k in ('avg_hold_days','median_hold_days','max_hold_days'))
            lines.append('|'+ '|'.join([z['bar'],z['strategy'],str(z['trades']),f(None if z['win_rate'] is None else z['win_rate']*100),f'{ci[0]:.3f}–{ci[1]:.3f}',f(z['avg_planned_net_rr']),f(z['realized_net_payoff']),f(z['avg_net_r']),f(z['return_pct']),f(z['max_drawdown_pct']),hold])+'|')
    lines+=['','## 預定驗證規則排名（不讀2025測試）','', '|序|週期|策略|分類|理由|','|---:|---|---|---|---|']
    for i,z in enumerate(r['rankings'],1):lines.append('|'+ '|'.join([str(i),z['bar'],z['strategy'],z['classification'],'；'.join(z['reasons'])])+'|')
    lines+=['','完整每筆淨R、獲利/虧損、PF、信賴區間、同時校正區間、14日稀疏樣本、各幣/方向/年/市況、成本壓力及所有失敗情境均在summary.json及trades.jsonl.gz。','',
            '**結論：沒有足夠可信證據同時符合60%勝率、淨RR≥3、主要1–3天及全部全倉風控條件。**']
    (out/'RESULTS.md').write_text('\n'.join(lines)+'\n')

if __name__=='__main__':
    try:sys.exit(run(parser().parse_args()))
    except Exception as e:
        print('FAIL_CLOSED_PHASE3',type(e).__name__,str(e),file=sys.stderr);sys.exit(2)
