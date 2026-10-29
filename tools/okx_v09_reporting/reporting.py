"""Traditional Chinese exports from verified source records; no signal generation."""
import csv
from html import escape
import io
import json
import math
from pathlib import Path
from statistics import mean, median
import subprocess
import sys
from zoneinfo import ZoneInfo
from . import VERSION
from .common import at, atomic_directory, canonical, number, sha256

TPE = ZoneInfo('Asia/Taipei')
STRATEGIES = {'SUPPORT_RESISTANCE_REVERSAL':'支撐阻力反轉', 'VOLATILITY_CONTRACTION_BREAKOUT':'波動收縮突破'}
REASONS = {'stop':'停損／保護性停損','tp1':'第一段止盈','tp2':'第二段止盈',
           'TIME_EXIT_3D':'三日時間退出','STRUCTURE_INVALIDATION':'結構失效退出'}


def percentile(values, p):
    if not values: return None
    values=sorted(values); x=(len(values)-1)*p
    lo=math.floor(x); hi=math.ceil(x)
    return values[lo]+(values[hi]-values[lo])*(x-lo)


def realized_metrics(trades):
    net=[number(t['net_twd']) for t in trades]
    wins=[x for x in net if x>0];losses=[x for x in net if x<0]
    holding=[number(t['hold_days']) for t in trades]
    n=len(net); wr=len(wins)/n if n else None
    if n:
        z=1.96;den=1+z*z/n
        center=(wr+z*z/(2*n))/den
        half=z*math.sqrt(wr*(1-wr)/n+z*z/(4*n*n))/den
        ci=[max(0,center-half),min(1,center+half)]
    else:ci=None
    return dict(closed_trades=n,wins=len(wins),losses=len(losses),breakeven=n-len(wins)-len(losses),
        observed_win_rate=wr,win_ci_95=ci,independent_sample_n=None,
        avg_win_twd=mean(wins) if wins else None,avg_loss_twd=mean(losses) if losses else None,
        realized_payoff=mean(wins)/abs(mean(losses)) if wins and losses else None,
        profit_factor=sum(wins)/abs(sum(losses)) if losses else None,
        net_twd=sum(net),avg_net_r=mean([t['net_r'] for t in trades]) if n else None,
        avg_entry_planned_net_rr=mean([t['planned_net_rr'] for t in trades]) if n else None,
        avg_hold_days=mean(holding) if n else None,median_hold_days=median(holding) if n else None,
        p90_hold_days=percentile(holding,.9),p95_hold_days=percentile(holding,.95),
        max_hold_days=max(holding) if n else None,target_1_to_3_days_fraction=sum(1<=x<=3 for x in holding)/n if n else None,
        holding_over_14_days=sum(x>14 for x in holding),live_qualified=False)


def code_version():
    root=Path(__file__).parent
    inventory={str(p.relative_to(root)):sha256(p.read_bytes()) for p in sorted(root.rglob('*'))
               if p.is_file() and p.suffix in ('.py','.json') and '__pycache__' not in p.parts}
    try:
        commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True,stderr=subprocess.DEVNULL).strip()
        dirty=bool(subprocess.check_output(['git','status','--porcelain','--',str(root)],cwd=root,text=True).strip())
    except (OSError,subprocess.SubprocessError): commit=None;dirty=None
    return dict(report_version=VERSION,report_schema=1,code_commit=commit,code_dirty=dirty,
                code_inventory_sha256=sha256(canonical(inventory)),python=sys.version.split()[0])


def build_metrics(artifact):
    artifact.assert_unchanged()
    state=artifact.state;summary=artifact.summary
    by={sid:realized_metrics([t for t in state['closed'] if t['strategy_id']==sid])
        for sid in state['freeze']['strategy_ids'].values()}
    return dict(version=code_version(),source=artifact.provenance(),portfolio=realized_metrics(state['closed']),
                strategies=by,account=dict(capital_twd=10000,initial_fee_reserve_twd=1000,
                cash_twd=state['cash_twd'],equity_twd=summary['current_equity_twd'],unrealized_twd=summary['unrealized_twd'],
                realized_net_twd=summary['realized_closed_trade_net_twd'],max_drawdown_pct=summary['max_drawdown_pct'],
                fee_reserve_remaining_twd=summary['fee_budget_remaining_twd'],open_positions=len(state['positions']),
                pending_orders=len(state['pending'])),news_status=summary['news_status'],live_qualified=False)


def fmt(value, digits=2, percent=False):
    if value is None: return '未驗證'
    if percent:return f'{100*value:.2f}%'
    return f'{value:,.{digits}f}'


def local_time(value):
    return at(value).astimezone(TPE).strftime('%Y-%m-%d %H:%M:%S +08:00')


def equity_svg(points):
    values=[number(p['equity_twd']) for p in points]
    if not values:
        return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 900 280"><text x="30" y="80">沒有資金曲線資料</text></svg>'
    times=[at(p['time']).timestamp() for p in points];t0,t1=min(times),max(times)
    low,high=min(values),max(values);padding=max((high-low)*.1,abs(high)*.005,1)
    low-=padding;high+=padding
    coords=[(75+775*(t-t0)/max(1,t1-t0),215-165*(v-low)/(high-low)) for t,v in zip(times,values)]
    path=' '.join(('M' if i==0 else 'L')+f'{x:.2f},{y:.2f}' for i,(x,y) in enumerate(coords))
    labels=''.join(f'<text x="65" y="{y+4:.1f}" text-anchor="end">{value:,.0f}</text><line x1="75" y1="{y}" x2="850" y2="{y}" stroke="#dde3ed"/>'
                   for y,value in [(50,high),(132.5,(high+low)/2),(215,low)])
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 900 280" role="img" aria-label="模擬帳本資金曲線">'
            '<title>前瞻紙上模擬資金曲線</title><desc>僅使用已驗證帳本的 equity 快照；不是盤中風險或強平保證。</desc>'
            '<rect width="900" height="280" fill="white"/><g font-family="system-ui,sans-serif" font-size="12" fill="#25334c">'
            '<text x="75" y="25">資金曲線（NT$，帳本快照）</text>'+labels+
            f'<path d="{path}" fill="none" stroke="#1768ac" stroke-width="2.5"/>'+
            f'<circle cx="{coords[-1][0]:.2f}" cy="{coords[-1][1]:.2f}" r="3" fill="#1768ac"/>'+
            f'<text x="75" y="245">{escape(local_time(points[0]["time"]))}</text>'+
            f'<text x="850" y="245" text-anchor="end">{escape(local_time(points[-1]["time"]))}</text>'+
            '<text x="450" y="270" text-anchor="middle">台灣時間；未呈現每一盤中價格</text></g></svg>')


def csv_export(columns, rows):
    stream=io.StringIO(newline='');writer=csv.DictWriter(stream,fieldnames=columns,extrasaction='ignore');writer.writeheader()
    for row in rows:
        clean={}
        for key in columns:
            value=row.get(key)
            if isinstance(value,(dict,list)): value=json.dumps(value,ensure_ascii=False,sort_keys=True)
            if isinstance(value,str) and value.lstrip().startswith(('=','+','-','@')):value="'"+value
            clean[key]=value
        writer.writerow(clean)
    return stream.getvalue().encode('utf-8-sig')


def _md(value):
    return escape(str(value),quote=False).replace('|','\\|').replace('\n',' ').replace('`','\\`').replace('[','\\[').replace(']','\\]')


def table(headers, rows, html=False):
    if not rows:return '<p>目前沒有資料。</p>' if html else '目前沒有資料。\n'
    if html:
        return '<div class="table-scroll"><table><thead><tr>'+''.join('<th>'+escape(str(x))+'</th>' for x in headers)+'</tr></thead><tbody>'+''.join('<tr>'+''.join('<td>'+escape(str(x))+'</td>' for x in row)+'</tr>' for row in rows)+'</tbody></table></div>'
    return '| '+' | '.join(_md(x) for x in headers)+' |\n| '+' | '.join('---' for x in headers)+' |\n'+''.join('| '+' | '.join(_md(x) for x in row)+' |\n' for row in rows)


def documents(artifact, metrics):
    state=artifact.state;account=metrics['account'];p=metrics['portfolio']
    overview=[('帳本版本／來源 Actions',f'{state["revision"]}／{artifact.manifest["run_id"]}'),
              ('有效行情時間（UTC）',state['last_processed_at']),('有效行情時間（台灣）',local_time(state['last_processed_at'])),
              ('已平倉／未平倉／待成交',f'{p["closed_trades"]}／{account["open_positions"]}／{account["pending_orders"]}'),
              ('研究本金／目前權益／費用池',f'NT${fmt(10000)}／NT${fmt(account["equity_twd"])}／NT${fmt(account["fee_reserve_remaining_twd"])}'),
              ('已實現淨損益／帳本最大回撤',f'NT${fmt(account["realized_net_twd"])}／{fmt(account["max_drawdown_pct"])}%'),
              ('觀察勝率／平均實現獲利虧損比',fmt(p['observed_win_rate'],percent=True)+'／'+fmt(p['realized_payoff'])),
              ('新聞資料／實盤資格',metrics['news_status']+'／未通過（報表不授權交易）')]
    sections=[]
    rows=[]
    for sid,m in metrics['strategies'].items():
        ci='未驗證' if m['win_ci_95'] is None else ' ～ '.join(fmt(x,percent=True) for x in m['win_ci_95'])
        rows.append([STRATEGIES.get(sid,sid),sid,m['closed_trades'],fmt(m['observed_win_rate'],percent=True),ci,
                     fmt(m['avg_entry_planned_net_rr']),fmt(m['realized_payoff']),fmt(m['avg_net_r']),fmt(m['profit_factor']),
                     fmt(m['avg_win_twd']),fmt(m['avg_loss_twd']),fmt(m['net_twd'])])
    sections.append(('策略績效（非獨立樣本數）',['策略','凍結 ID','筆數','觀察勝率','95% Wilson 區間','入場計畫淨 RR 平均','實現獲利／虧損比','平均淨 R','PF','平均獲利 NT$','平均虧損 NT$','淨損益 NT$'],rows))
    sections.append(('實際持倉分布',['策略','平均天數','中位數','P90','P95','最長','1～3 天比例','超過 14 天筆數'],[
        [STRATEGIES.get(sid,sid),fmt(m['avg_hold_days']),fmt(m['median_hold_days']),fmt(m['p90_hold_days']),fmt(m['p95_hold_days']),fmt(m['max_hold_days']),fmt(m['target_1_to_3_days_fraction'],percent=True),m['holding_over_14_days']] for sid,m in metrics['strategies'].items()]))
    trade_rows=[];entry_rows=[];exit_rows=[];open_rows=[];pending_rows=[]
    alltrades=state['closed']+list(state['positions'].values())
    for t in alltrades:
        common=[t['id'],t['symbol'],STRATEGIES.get(t['strategy_id'],t['strategy_id'])]
        for e in t['entries']:entry_rows.append(common+[local_time(e['time']),fmt(e['price'],8),fmt(e['units'],8),fmt(e['fee_twd'])])
        for e in t['exits']:exit_rows.append(common+[local_time(e['time']),fmt(e['price'],8),fmt(e['units'],8),REASONS.get(e['reason'],e['reason']),fmt(e['fee_twd'])])
        values=common+['多' if t['side']==1 else '空',local_time(t['entry_time']),fmt(t['entry'],8),
                       fmt(t['initial_stop'],8),fmt(t['stop'],8),fmt(t['tp1'],8),fmt(t['tp2'],8),fmt(t['planned_net_rr'])]
        if t in state['closed']:
            trade_rows.append(values+[local_time(t['exit_time']),fmt(t['net_twd']),fmt(t['net_r']),fmt(t['hold_days']),fmt(t['fees_twd']),fmt(t['funding_twd']),fmt(t['slippage_twd'])])
        else:
            holding=(at(state['last_processed_at'])-at(t['entry_time'])).total_seconds()/86400
            open_rows.append(values+[fmt(t['remaining_units'],8),fmt(holding),fmt(t['net_twd'])])
    base_headers=['交易 ID','合約','策略','方向','入場時間（台灣）','平均入場價','失效停損','目前保護停損','TP1','TP2','入場記錄淨 RR']
    sections.append(('逐筆已平倉交易',base_headers+['出場時間（台灣）','扣成本淨損益 NT$','實現淨 R','持倉天數','手續費 NT$','資金費 NT$','內含滑價模型 NT$'],trade_rows))
    sections.append(('逐筆入場成交',['交易 ID','合約','策略','時間（台灣）','成交價','數量','手續費 NT$'],entry_rows))
    sections.append(('逐筆止盈／止損與其他出場',['交易 ID','合約','策略','時間（台灣）','成交價','數量','退出原因','手續費 NT$'],exit_rows))
    sections.append(('未平倉部位',base_headers+['剩餘數量','截至資料時間持倉天數','已實現部分淨損益 NT$'],open_rows))
    for order in state['pending'].values():
        pending_rows.append([order['id'],order['symbol'],order['strategy_id'],fmt(order['expected_entry'],8),fmt(order['stop'],8),fmt(order['tp1'],8),fmt(order['tp2'],8),order['state'] if 'state' in order else '待成交'])
    sections.append(('待成交候選',['ID','合約','策略','條件入場價','停損','TP1','TP2','狀態'],pending_rows))
    reasons={}
    for c in state['candidates'].values():
        for reason in c.get('reasons',[]):reasons[reason]=reasons.get(reason,0)+1
    sections.append(('候選拒絕原因（可同時多項）',['原始原因代碼','候選筆數'],[[k,v] for k,v in sorted(reasons.items())]))
    versions=[]
    for e in state['journal']:
        if e['kind'] in ('INITIALIZE','RUN_COMPLETE','CONTRACT_SPEC_REVIEW_APPLIED'):
            payload=e['payload'];stamp=payload.get('time') or payload.get('applied_at_utc') or payload.get('research_start')
            versions.append([e['seq'],e['kind'],e['key'],local_time(stamp) if stamp else '原帳本未提供',str(payload.get('duplicate_input','不適用'))])
    sections.append(('累計帳本版本事件',['事件序號','事件類型','來源鍵／Run ID','時間（台灣）','相同輸入重播'],versions))
    provenance=metrics['source']
    sections.append(('完整性與版本紀錄',['項目','值'],[
        ['報表版本',VERSION],['報表程式指紋',metrics['version']['code_inventory_sha256']],['來源 Artifact ID',provenance['artifact_id']],
        ['完整 ZIP SHA-256',artifact.digest],['狀態 SHA-256',artifact.manifest['state_hash']],
        ['策略／風控 config hash',state['config_hash']],['凍結程式 SHA',state['freeze']['code_commit_sha']],
        ['排程 commit',artifact.context['run']['head_sha']],['規格操作 payload SHA',json.loads(artifact.files['operations_receipt.json'])['spec_review_payload_commit']],['直接 parent Artifact 核對','已核對' if artifact.parent_verified else '未提供（不宣稱完整 Artifact 祖先鏈已驗證）'],
        ['原始根節點',state['root_run_id']],['原研究開始（台灣）',local_time(state['research_start_timestamp_utc'])],
        ['資料超過正式引擎新鮮度門檻','是，僅可離線檢視' if provenance['exceeds_production_age_limit'] else '否；仍不構成正式恢復授權']]))
    notes=[
        '零交易或缺少獲利／虧損樣本時，勝率、PF、盈虧比及持倉統計顯示未驗證，不產生示範成交。',
        '入場計畫淨 RR、歷史平均實現獲利／虧損比、逐筆淨 R 與 PF 是不同量，不能互相替代。計畫值照原帳本記錄，不重新最佳化。',
        '觀察勝率及信賴區間不代表單筆真實成功機率；相對獨立及有效樣本數尚未驗證。報表不判定策略合格，也不調整 60%／淨 RR≥3 門檻。',
        '滑價已反映於模擬成交價，不再重複扣除；手續費、資金費及固定匯率均為凍結研究模型，不代表真實 OKX 成交或費率。',
        '資金曲線來自帳本快照，不能代表完整盤中尾部風險。計畫停損預算不是全倉實際虧損保證上限。',
        '一般新聞不產生勝率加成；UNKNOWN_COVERAGE 不是沒有重大事件。策略與全倉實盤資格維持未通過。',
        '報表是離線唯讀輸出，不執行策略、不恢復正式帳本、不傳送通知、不下單。原 7 天等正式停機規則沒有修改。',
        'SHA-256 針對提供的 GitHub 來源元資料驗證位元完整性，不是 GitHub 數位簽章；來源元資料仍需由可信 API／封存紀錄取得。',
        'Artifact 保留 90 天不是永久備份。外部備份傳輸未實作、未啟用；儲存目的地待使用者確認。',
    ]
    svg=equity_svg(state['equity'])
    md='# OKX v0.9｜前瞻紙上模擬績效報告\n\n'+''.join('- **'+_md(k)+'：**'+_md(v)+'\n' for k,v in overview)+'\n'
    body='<h1>OKX v0.9｜前瞻紙上模擬績效報告</h1><dl>'+''.join('<dt>'+escape(k)+'</dt><dd>'+escape(str(v))+'</dd>' for k,v in overview)+'</dl>'
    md+='## 資金曲線\n\n![帳本快照資金曲線](equity.svg)\n\n';body+='<h2>資金曲線</h2>'+svg
    for title,headers,rows in sections:
        md+='## '+title+'\n\n'+table(headers,rows)+'\n';body+='<h2>'+escape(title)+'</h2>'+table(headers,rows,True)
    md+='## 資料品質與限制\n\n'+''.join('- '+_md(n)+'\n' for n in notes)
    body+='<h2>資料品質與限制</h2><ul>'+''.join('<li>'+escape(n)+'</li>' for n in notes)+'</ul>'
    html='<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src &#39;none&#39;; style-src &#39;unsafe-inline&#39;; img-src data:; base-uri &#39;none&#39;; form-action &#39;none&#39;"><title>OKX v0.9 前瞻紙上模擬績效報告</title><style>body{font-family:system-ui,sans-serif;max-width:1250px;margin:2rem auto;padding:0 1rem;color:#20304a;line-height:1.65}dt{font-weight:bold}dd{margin:0 0 .65rem}table{border-collapse:collapse;font-size:.86rem}th,td{border:1px solid #d7dfeb;padding:.45rem;vertical-align:top;word-break:break-word}th{background:#edf3fa}.table-scroll{overflow-x:auto}svg{max-width:100%;height:auto}h2{margin-top:2.2rem}</style></head><body>'+body+'</body></html>'
    return md,html,svg


def write_report(artifact, destination):
    metrics=build_metrics(artifact);md,html,svg=documents(artifact,metrics)
    state=artifact.state
    detail_columns=['id','book','symbol','bar','side','strategy_id','risk_twd','entry_time','exit_time','entry','units_total','initial_stop','stop','tp1','tp2','planned_net_rr','net_twd','net_r','hold_days','fees_twd','funding_twd','slippage_twd','entries','exits']
    fills=[]
    for t in state['closed']+list(state['positions'].values()):
        for side,key in [('ENTRY','entries'),('EXIT','exits')]:
            for fill in t[key]:fills.append(dict(trade_id=t['id'],symbol=t['symbol'],strategy_id=t['strategy_id'],fill_type=side,**fill))
    files={'report.md':md.encode(),'report.html':html.encode(),'equity.svg':svg.encode(),
           'metrics.json':canonical(metrics),'verified-input-inventory.json':canonical(artifact.inventory),
           'source-context.json':canonical(artifact.context),
           'trades.csv':csv_export(detail_columns,state['closed']),
           'fills.csv':csv_export(['trade_id','symbol','strategy_id','fill_type','time','price','units','fee_twd','reason'],fills),
           'equity.csv':artifact.files['equity.csv'],'source-trades.csv':artifact.files['trades.csv'],
           'positions.json':artifact.files['positions.json'],'pending-orders.json':canonical(state['pending'])}
    seal=dict(schema_version=1,report_version=VERSION,source_zip_sha256=artifact.digest,
              source_state_hash=artifact.manifest['state_hash'],files={n:sha256(b) for n,b in files.items()})
    files['report-manifest.json']=canonical(seal)
    artifact.assert_unchanged()
    return atomic_directory(destination,files)
