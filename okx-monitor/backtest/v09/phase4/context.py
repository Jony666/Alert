"""POST-TRADE attribution only. Never consulted by signals, ranking or execution.
Every explanatory value is from the preceding completed bar. Outcome labels are
explicit hindsight diagnostics; they cannot be fed back into this frozen run.
"""
from collections import defaultdict
from pathlib import Path
import argparse,gzip,json
import numpy as np
import pandas as pd
from ..config import STEPS
from ..data import timestamp
from ..strategies import features
from ..phase3.strategies import enrich

def explain(prior,side):
    # Fixed descriptive bins; not optimized thresholds and not trade filters.
    trend=abs(prior.hfast-prior.hslow)/prior.atr
    vol=prior.volume/prior.vol20
    reclaim=(prior.close-prior.lo20)/prior.atr if side==1 else (prior.hi20-prior.close)/prior.atr
    return dict(atr_fraction=prior.atr_fraction,volume_ratio=vol,
                width_ratio=prior.width20/prior.width_reference,
                higher_trend_strength_atr=trend,btc_move24h=prior.btc_move24h,
                sr_reclaim_depth_atr=reclaim,
                environment='TREND' if trend>=1 else 'RANGE',
                atr_bin='LOW' if prior.atr_fraction<.02 else 'MID' if prior.atr_fraction<.05 else 'HIGH',
                volume_bin='BELOW_1.25' if vol<1.25 else '1.25_TO_2' if vol<2 else 'AT_LEAST_2',
                reclaim_bin='SHALLOW' if reclaim<.5 else 'DEEP')

def run(folder,cache):
    out=Path(folder);trades=[json.loads(s) for s in gzip.open(out/'trades.jsonl.gz','rt')]
    by_market=defaultdict(list)
    for t in trades:by_market[(t['symbol'],t['bar'])].append(t)
    groups=defaultdict(list);counts=0
    with gzip.open(out/'entry_context.jsonl.gz','wt') as f:
        for bar,step in STEPS.items():
            btc=pd.read_csv(Path(cache)/f'BTC-USDT-SWAP_{bar}_1640995200000_1767225600000.csv')
            btc_context=features(btc,step)
            for (inst,b),ts in sorted(by_market.items()):
                if b!=bar:continue
                raw=pd.read_csv(Path(cache)/f'{inst}_{bar}_1640995200000_1767225600000.csv')
                x=enrich(raw,step,btc,btc_context);stamps=x.ts.to_numpy()
                for t in ts:
                    at=timestamp(t['entry_time']);j=int(np.searchsorted(stamps,at))
                    if j<=0 or j>=len(x) or int(stamps[j])!=at:raise ValueError('entry alignment failed')
                    prior=x.iloc[j-1]
                    assert int(prior.available)<=at
                    context=explain(prior,t['side'])
                    hindsight=dict(stopped_before_any_tp1=any(p['reason']=='stop' for p in t['fills']) and not any(p['reason']=='tp1' for p in t['fills']),
                                   not_proof_of_false_breakout=True)
                    f.write(json.dumps(dict(**{k:t[k] for k in ('bar','core','variant','exit','split','pressure','symbol','entry_time')},
                                            known_at_utc=t['entry_time'],context=context,hindsight_diagnostic_only=hindsight))+'\n');counts+=1
                    if t['variant']=='base' and t['exit']=='split_atr' and t['pressure']=='base' and t['split'].startswith('wf_'):
                        for dim in ('environment','atr_bin','volume_bin','reclaim_bin'):
                            groups[(t['core'],bar,dim,context[dim])].append(dict(net=t['net_twd'],r=t['net_r'],false_proxy=hindsight['stopped_before_any_tp1']))
    rows=[]
    for (core,bar,dim,value),items in sorted(groups.items()):
        rows.append(dict(core=core,bar=bar,dimension=dim,value=value,n=len(items),win_rate=sum(t['net']>0 for t in items)/len(items),
                         avg_net_r=float(np.mean([t['r'] for t in items])),stop_before_tp1_share=float(np.mean([t['false_proxy'] for t in items]))))
    result=dict(role='DESCRIPTIVE_POST_TRADE_NOT_CAUSAL_NOT_USED_FOR_SELECTION',entry_context_rows=counts,rows=rows,
                warning='Sparse correlated outcome-selected executed samples; not all raw patterns and not independent evidence. No threshold changes inferred.')
    (out/'context_associations.json').write_text(json.dumps(result,indent=2)+'\n')
    lines=['# 型態、趨勢/震盪與假突破代理（只供診斷）','', '固定基準退出、驗證區塊、已成交樣本的描述性關聯；沒有拿結果調參。無TP1先停損僅是假突破代理，不是事件因果證明。全部解釋值取前一已收盤棒，未來出場僅作明示事後標籤。','',
           '|核心|週期|維度|分組|N|勝率%|平均淨R|無TP1先停損%|','|---|---|---|---|---:|---:|---:|---:|']
    for r in rows:lines.append('|'+ '|'.join([r['core'],r['bar'],r['dimension'],r['value'],str(r['n']),f"{r['win_rate']*100:.2f}",f"{r['avg_net_r']:.3f}",f"{r['stop_before_tp1_share']*100:.2f}"])+'|')
    (out/'CONTEXT.md').write_text('\n'.join(lines)+'\n')
    print('POST_TRADE_CONTEXT_PASS',counts)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('folder');p.add_argument('--cache',required=True);a=p.parse_args();run(a.folder,a.cache)
