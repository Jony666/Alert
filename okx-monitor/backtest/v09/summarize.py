"""Render every primary holdout result and attrition counts; no ranking/selection."""
import json
from pathlib import Path
import sys

def render(path):
    r=json.loads(Path(path).read_text())
    lines=['# v0.9 擴充研究結果','',f"區間：{r['interval']}；覆蓋 {len(r['coverage'])} 組；共 {len(r['results'])} 情境。",'',
           '**NO QUALIFIED SIGNAL：沒有策略通過全部可信度與全倉安全條件。**','',
           '下表為預先固定的主要保留測試（5倍、基準成本）；不是挑選最佳情境。全部情境及年度獨立帳戶另見 summary.json。', '',
           '|週期|策略|筆數|勝率%|計畫淨RR|實現淨盈虧比|平均淨R|最大回撤%|平均天數|',
           '|---|---|---:|---:|---:|---:|---:|---:|---:|']
    def fmt(x):return 'N/A' if x is None else f'{x:.3f}'
    for z in r['results']:
        if z['split']=='holdout' and z['leverage']==5 and z['cost_mult']==1 and z['execution']=='base':
            lines.append('|'+ '|'.join([z['bar'],z['strategy'],str(z['trades']),fmt(None if z['win_rate'] is None else z['win_rate']*100),fmt(z['avg_planned_net_rr']),fmt(z['realized_net_payoff']),fmt(z['avg_net_r']),fmt(z['max_drawdown_pct']),fmt(z['avg_hold_days'])])+'|')
    lines += ['', '## 樣本流失診斷（主訓練區間、5倍、基準成本）','', '|週期|策略|候選訊號|結構淨RR通過|實際紙上筆數|持續停機拒絕|首次停機|原因|', '|---|---|---:|---:|---:|---:|---|---|']
    for z in r['results']:
        if z['split']=='train' and z['leverage']==5 and z['cost_mult']==1 and z['execution']=='base':
            c=z['counts'];h=z['first_halt'] or {}
            lines.append('|'+ '|'.join([z['bar'],z['strategy'],str(c.get('raw_candidate_signals',0)),str(c.get('structural_net_rr_pass',0)),str(z['trades']),str(c.get('PERSISTENT_HALT',0)),h.get('time','未停機'),','.join(h.get('reasons',[]))])+'|')
    lines += ['', '年度帳戶與主分區彼此重疊，不能合併成獨立樣本。沒有可信前瞻驗證；當前合約名單及規格亦非完整歷史 point-in-time reconstruction。', '',
              '合格勝率門檻仍為95% Wilson下界至少60%、至少100筆、pristine與前瞻驗證；計畫加權淨RR至少3。無因樣本不足調低門檻。']
    Path(path).with_name('RESULTS.md').write_text('\n'.join(lines)+'\n')

if __name__=='__main__':render(sys.argv[1])
