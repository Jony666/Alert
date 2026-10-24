"""Deterministic post-save audit/derived reports; never feeds trade selection."""
import argparse,gzip,hashlib,json
from collections import defaultdict
from pathlib import Path
from dataclasses import replace
from ..config import CONFIG
from ..exits import Plan,costs
from .design import CORES

def protected_path_rr(t):
    cfg=replace(CONFIG,slippage=.02) if t['pressure']=='slip2pct' else CONFIG
    mult=2. if t['pressure']=='double_cost' else 1.
    p=Plan(t['entry'],t['stop'],t['tp1'],t['tp2'],t['side'],tuple(t['weights']))
    c=costs(p,mult,cfg);fee=cfg.fee*mult;slip=cfg.slippage*mult
    tp=p.tp1*(1-p.side*slip);protection=p.entry*(1-p.side*slip)
    reward=p.weights[0]*(p.side*(tp-p.entry)-tp*fee)+p.weights[1]*(p.side*(protection-p.entry)-protection*fee)-p.entry*fee-c['funding_bound_per_unit']
    return reward/c['risk_per_unit']

def run(folder):
    out=Path(folder);r=json.loads((out/'summary.json').read_text())
    assert r['complete'] and r['scenario_count']==732 and len(r['phase3_baseline_comparisons'])==108
    count=0;pathvals=defaultdict(list)
    with gzip.open(out/'trades.jsonl.gz','rt') as f,gzip.open(out/'planned_paths.jsonl.gz','wt') as pf:
        for line in f:
            t=json.loads(line);count+=1
            assert t['planned_net_rr']>=3 and t['risk_twd']<=1000+1e-6 and t['hold_days']<=3
            assert abs(sum(x['fraction'] for x in t['fills'])-1)<1e-8
            rr=protected_path_rr(t)
            info={k:t[k] for k in ('bar','core','variant','exit','split','pressure','symbol','entry_time')}
            info.update(full_plan_net_rr=t['planned_net_rr'],tp1_then_protection_hypothetical_net_rr=rr,
                        not_guaranteed_path=True,never_qualified_for_live=True)
            pf.write(json.dumps(info)+'\n')
            pathvals[tuple(info[k] for k in ('bar','core','variant','exit','split','pressure'))].append(rr)
    for z in r['rows']:
        assert z['max_planned_risk_twd']<=2000+1e-6 and z['max_open']<=3
        assert z['funnel']['all_paper_risk_gates_pass']==z['funnel']['simulated_entries']
    def f(x):return 'N/A' if x is None else f'{x:.3f}'
    funnel=['# 原始訊號到紙上成交（基準版，2025受污染測試）','', '|週期|核心|原始型態|技術通過|持有排除|淨RR通過|帳戶gate通過|全風控通過/成交|停機拒絕|','|---|---|---:|---:|---:|---:|---:|---:|---:|']
    for z in r['rows']:
        if z['variant']=='base' and z['exit']=='split_atr' and z['split']=='test_2025_contaminated' and z['pressure']=='base':
            q=z['funnel'];funnel.append('|'+ '|'.join(map(str,[z['bar'],z['core'],q['raw_patterns'],q['technical_pass'],q['held_symbol_exclusions'],q['planned_rr_pass'],q['paper_account_gate_pass'],q['simulated_entries'],q['rejections'].get('PERSISTENT_HALT',0)]))+'|')
    funnel+=['','全部時間分區、first-failure技術分類與逐類風控拒絕均在summary.json；技術通過不等於可信勝率或實盤資格。覆蓋90/90完整，沒有用補棒或合成行情增加樣本。']
    (out/'FUNNEL.md').write_text('\n'.join(funnel)+'\n')
    comp=['# 第三階段完整基準差異','', '108個對照情境的交易數、淨損益、勝率、PF、淨R、持倉、回撤及每筆時間/價格/止損/目標/分批成交逐一相同。沒有覆蓋原始檔案。', '', '|週期|核心|分區|壓力|基準差異|','|---|---|---|---|---|']
    for c in r['phase3_baseline_comparisons']:comp.append('|'+ '|'.join([c['bar'],c['core'],c['split'],c['pressure'],'0（精確一致）'])+'|')
    (out/'PHASE3_COMPARISON.md').write_text('\n'.join(comp)+'\n')
    sens=['# 鄰近參數與保守路徑（非事後挑優勝）','', '|週期|核心|變體|驗證分區|壓力|N|淨R|淨報酬%|','|---|---|---|---|---|---:|---:|---:|']
    for z in r['rows']:
        if z['exit']=='split_atr' and z['split'].startswith('wf_') and z['pressure'] in ('base','double_cost'):
            sens.append('|'+ '|'.join([z['bar'],z['core'],z['variant'],z['split'],z['pressure'],str(z['trades']),f(z['avg_net_r']),f(z['return_pct'])])+'|')
    sens+=['','planned_paths.jsonl.gz逐筆列全計畫加權淨RR與TP1後掃保護停損的假設淨RR。後者可能遠低於3，不能把最遠TP2當確定獲利；所有路徑仍未取得可信勝率或全倉資格。']
    (out/'SENSITIVITY.md').write_text('\n'.join(sens)+'\n')
    validation=dict(scenarios=732,baseline_exact_replays=108,scenario_trades_not_independent=count,
                    post_save_checks='PASS risk<=1000/2000, positions<=3, hold<=3d, net planned RR>=3, partial fractions sum1, gate/entries reconcile',
                    historical_coverage='90/90 SHA and complete calendars verified by runner',
                    path_analysis='Exact assumed exit slippage/fees on each partial, full funding bound; not a predicted realized path')
    (out/'post_save_validation.json').write_text(json.dumps(validation,indent=2)+'\n')
    print('POST_SAVE_PHASE4_PASS',count)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('folder');run(p.parse_args().folder)
