import csv,io,json
from pathlib import Path
from statistics import mean,median
from .freeze import canonical,digest
from .state import atomic_bundle,digest_bytes,financial_hash
from ..research import wilson
from ..risk import day


def metrics(trades):
    n=len(trades);wins=[t['net_twd'] for t in trades if t['net_twd']>0];losses=[t['net_twd'] for t in trades if t['net_twd']<0]
    holds=[t['hold_days'] for t in trades];net=sum(t['net_twd'] for t in trades)
    streak=maximum=0
    for t in sorted(trades,key=lambda t:t['exit_time']):
        streak=streak+1 if t['net_twd']<0 else 0;maximum=max(maximum,streak)
    return dict(closed_trades=n,win_rate=len(wins)/n if n else None,win_ci_95=wilson(len(wins),n),
                avg_win_twd=mean(wins) if wins else None,avg_loss_twd=mean(losses) if losses else None,
                realized_payoff=mean(wins)/abs(mean(losses)) if wins and losses else None,
                profit_factor=sum(wins)/abs(sum(losses)) if losses else None,realized_net_twd=net,
                avg_net_r=mean([t['net_r'] for t in trades]) if n else None,
                avg_planned_net_rr=mean([t['planned_net_rr'] for t in trades]) if n else None,
                avg_hold_days=mean(holds) if holds else None,median_hold_days=median(holds) if holds else None,
                holding_distribution={k:sum(lo<=h<hi for h in holds) for k,lo,hi in (('<1',0,1),('1–3',1,3.0000001),('>3–7',3.0000001,7.0000001),('>7–14',7.0000001,14.0000001))},
                max_consecutive_losses=maximum,independent_sample_n=None,quality='NOT VERIFIED')

def summary(s,snap):
    from .engine import exposures
    u=sum(p['remaining_units']*p['side']*(s['marks'][p['symbol']]['price']-p['entry'])*32 for p in s['positions'].values())
    by={}
    for core,sid in s['freeze']['strategy_ids'].items():
        trades=[t for t in s['closed'] if t['core']==core];c=[c for c in s['candidates'].values() if c['core']==core]
        by[sid]=dict(**metrics(trades),candidate_signals=len(c),paper_review_pass=sum(not x['reasons'] for x in c),
                     live_qualified=0,simulated_entries=sum(p['core']==core for p in s['positions'].values())+len(trades),
                     rejected=sum(bool(x['reasons']) for x in c),open_positions=sum(p['core']==core for p in s['positions'].values()),
                     pending_orders=sum(p['core']==core for p in s['pending'].values()))
    positions=list(s['positions'].values());alltrades=s['closed']+positions
    exp=exposures(s)
    return dict(book='PAPER_FORWARD',study_id=s['study_id'],strategy_version=s['freeze']['strategy_version'],
                config_hash=s['config_hash'],code_commit_sha=s['freeze']['code_commit_sha'],
                freeze_timestamp_utc=s['freeze']['freeze_timestamp_utc'],research_start_timestamp_utc=s['research_start_timestamp_utc'],
                data_through_utc=s['last_processed_at'],latest_run_id=s['last_success_run_id'],revision=s['revision'],
                strategies=by,portfolio=metrics(s['closed']),initial_capital_twd=10000,initial_fee_budget_twd=1000,
                cash_twd=s['cash_twd'],unrealized_twd=u,current_equity_twd=s['cash_twd']+u,
                project_net_return_pct=100*((s['cash_twd']+u)/10000-1),max_drawdown_pct=100*s['max_drawdown'],
                realized_closed_trade_net_twd=sum(t['net_twd'] for t in s['closed']),
                partial_exit_net_before_unallocated_entry_cost_twd=sum(p['net_twd'] for p in positions),
                occupied_planned_risk_twd=sum(e['risk_twd'] for e in exp),available_planned_risk_twd=max(0,2000-sum(e['risk_twd'] for e in exp)),
                occupied_notional_twd=sum(e['notional_twd'] for e in exp),
                fee_budget_remaining_twd=max(0,1000-s['expense_used_twd']),fees_paid_twd=sum(t['fees_twd'] for t in alltrades),
                funding_paid_twd=sum(t['funding_twd'] for t in alltrades),slippage_model_twd=sum(t['slippage_twd'] for t in alltrades),
                open_positions=len(positions),pending_orders=len(s['pending']),daily_halts=s['daily_halts'],
                global_halt=s['risk']['halted'],halt_reasons=s['risk']['halt_reasons'],warnings=s['warnings'],
                news_status=snap['news']['status'],market_groups=len(snap['frames']),
                market_regimes={reg:metrics([t for t in s['closed'] if t['regime']==reg]) for reg in sorted({t['regime'] for t in s['closed']})},
                financial_hash=financial_hash(s),live_qualified=False,
                notes=['No historical trade is imported. PAPER does not alter actual user capital.',
                       'Current strategies lack credible 60% and official cross evidence; candidates remain refused.',
                       'Fees/FX/slippage/OHLC execution/funding notional are frozen research assumptions, not real account fills.',
                       'A zero closed-trade count has no win rate, payoff or proof of profitability.'])

def csv_bytes(rows,columns):
    stream=io.StringIO();w=csv.DictWriter(stream,fieldnames=columns,extrasaction='ignore');w.writeheader()
    for row in rows:w.writerow({k:json.dumps(row.get(k),ensure_ascii=False) if isinstance(row.get(k),(list,dict)) else row.get(k) for k in columns})
    return stream.getvalue().encode()

def write_bundle(out,state,snap,run_id,parent=None,diagnostics=None,fail=False):
    report=summary(state,snap)
    files={'state.json':canonical(state),'snapshot.json':canonical(snap),'summary.json':canonical(report),
           'positions.json':canonical(state['positions']),
           'trades.csv':csv_bytes(state['closed'],['id','book','strategy_id','strategy_version','config_hash','symbol','bar','side','entry_time','exit_time','entry','units_total','net_twd','net_r','planned_net_rr','fees_twd','funding_twd','hold_days','entries','exits']),
           'equity.csv':csv_bytes(state['equity'],['time','cash_twd','unrealized_twd','equity_twd','drawdown']),
           'events.csv':csv_bytes(state['journal'],['seq','id','kind','key','payload','prev','hash'])}
    manifest=dict(schema_version=1,study_id=state['study_id'],run_id=str(run_id),state_hash=digest(state),
                  revision=state['revision'],root_run_id=state['root_run_id'],genesis_hash=state['journal'][0]['hash'],
                  parent=parent,diagnostics=diagnostics or {},
                  files={k:digest_bytes(v) for k,v in files.items()},artifact_role='COMPLETE_CUMULATIVE_STATE_NOT_PERMANENT_STORAGE')
    files['run_manifest.json']=canonical(manifest);atomic_bundle(out,files,fail_before_commit=fail)
    return manifest
