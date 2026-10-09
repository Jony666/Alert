#!/usr/bin/env python3
"""Predeclared OKX 30m sensitivity research. NO TRADING / NO PUSH.

Depends on repository's existing historical_backtest.py & diagnostic_backtest.py.
All candidate features are based exclusively on *closed* candles. Next-bar open
is the first permitted fill; forward OHLCV is used only to simulate exits.

The temporal test bucket is not pristine OOS: the preceding 180d pilot inspected
part of the same dates. All scenario comparisons are exploratory/multiple-tested.
"""
from __future__ import annotations
import argparse
from collections import defaultdict, Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys
import time

import numpy as np
import pandas as pd
import historical_backtest as bt
import diagnostic_backtest as dg

MS_30M = 1_800_000
MS_DAY = 86_400_000
COST_NOTE = 'Static assumed costs; historical funding, L2 book, fills and liquidation not reproduced'

@dataclass(frozen=True)
class Scenario:
    name: str
    strategy: str
    gate_change: str = ''
    rr1_min: float = 2.0
    max_entry_above_atr: float = 0.0
    cost_mult: float = 1.0


SCENARIOS = (
    Scenario('A_BASE', 'A'),
    Scenario('A_NO_1H_MACD_CROSS', 'A', 'A3'),
    Scenario('A_NO_1H_BREAKOUT', 'A', 'A5'),
    Scenario('A_NO_1H_VOLUME', 'A', 'A6'),
    Scenario('A_NO_4H_HIST_CONFIRM', 'A', 'A7'),
    Scenario('B_BASE', 'B'),
    Scenario('B_REQUIRE_3_OF_8', 'B', 'B1_3_OF_8'),
    Scenario('B_NO_4H_DIVERGENCE_REQUIREMENT', 'B', 'B2'),
    Scenario('B_NO_12H_1D_REQUIREMENT', 'B', 'B3'),
    Scenario('B_BREAKOUT_ONLY_NO_VOLUME', 'B', 'B4_BREAKOUT_ONLY'),
    Scenario('B_VOLUME_ONLY_NO_BREAKOUT', 'B', 'B4_VOLUME_ONLY'),
    Scenario('B_NO_BREAKOUT_VOLUME_CONFIRM', 'B', 'B4'),
    Scenario('B_MIN_TP1_RR_1_5', 'B', rr1_min=1.5),
    Scenario('B_ALLOW_ENTRY_UPPER_0_12ATR', 'B', max_entry_above_atr=.12),
    Scenario('B_NO_B4_DOUBLE_COSTS', 'B', 'B4', cost_mult=2.0),
    Scenario('A_NO_A5_DOUBLE_COSTS', 'A', 'A5', cost_mult=2.0),
)
SCENARIO_LOOKUP={s.name:s for s in SCENARIOS}


def matches(flags:dict[str,bool], extra:dict[str,bool], diverged:int, s:Scenario)->bool:
    f=dict(flags)
    if s.gate_change in ('A3','A5','A6','A7','B2','B3','B4'):
        key=next(k for k in f if k.startswith(s.gate_change+'_'))
        f[key]=True
    elif s.gate_change=='B1_3_OF_8':
        f[dg.B_KEYS[0]]=diverged>=3
    elif s.gate_change=='B4_BREAKOUT_ONLY':
        f[dg.B_KEYS[3]]=extra['breakout_either_tf']
    elif s.gate_change=='B4_VOLUME_ONLY':
        f[dg.B_KEYS[3]]=extra['volume_either_tf']
    elif s.gate_change:
        raise ValueError('Unknown scenario gate '+s.gate_change)
    return all(f.values())


def trade_plan(d, estimated:float, fee:float, slip:float, funding:float, min_rr:float):
    """Exact original plan for min_rr=2.0; single sensitivity knob for others."""
    if min_rr==2.0:
        return bt.plan(d,estimated,fee,slip,funding), None
    h,h4=d['1H'],d['4H']
    atr=float(h.atr.iloc[-1]);last_close=float(d['30m'].close.iloc[-1])
    if not math.isfinite(atr) or atr<=0:return None,'invalid_atr'
    if abs(estimated-last_close)>.7*atr:return None,'entry_too_far_at_plan'
    stop=float(h.low.iloc[-16:-1].min())-.2*atr
    risk=estimated-stop
    if risk<=0 or not .002<=risk/estimated<=.07:return None,'stop_risk_out_of_range'
    highs=sorted({float(v) for v in pd.concat([h.high.iloc[-90:],h4.high.iloc[-80:]])
                  if float(v)>estimated+.25*atr})
    if not highs:return None,'no_resistance'
    cost=estimated*(2*fee+2*slip+funding)
    rr=lambda tgt:(tgt-estimated-cost)/(risk+cost)
    targets=[x for x in highs if rr(x)>=min_rr and (x-estimated)/estimated<=.18]
    if len(targets)<2:return None,'not_two_targets_at_min_rr'
    if rr(targets[-1])<2.8:return None,'tp2_rr_below_2_8'
    return (stop,targets[0],targets[-1],rr(targets[0]),rr(targets[-1])),None


def baseline_plan_reason(d,estimated,fee,slip,funding):
    """Only invoked after bt.plan rejected a signal, for explanatory counters."""
    f,_=dg.plan_flags(d,estimated,fee,slip,funding)
    return next((k for k in dg.PLAN_KEYS if not f[k]), 'unknown_plan_rejection')


def entry_reason(real,estimated,atr,stop,tp1,fee,slip,funding,rr1_min,max_upper_atr):
    if real < estimated-.12*atr or real>estimated+max_upper_atr*atr:
        return 'next_open_outside_entry_band'
    if real<=stop:return 'next_open_below_stop'
    cost=real*(2*fee+2*slip+funding)
    ratio=(tp1-real-cost)/(real-stop+cost) if real-stop+cost>0 else -math.inf
    if ratio<rr1_min:return 'net_rr_degraded_after_fill'
    return None


def split_bounds(start_ms:int,end_ms:int):
    if start_ms>=end_ms:raise ValueError('invalid split range')
    return {
        'train':(start_ms,start_ms+int(.6*(end_ms-start_ms))),
        'validation':(start_ms+int(.6*(end_ms-start_ms)),start_ms+int(.8*(end_ms-start_ms))),
        'temporal_test_not_pristine_oos':(start_ms+int(.8*(end_ms-start_ms)),end_ms),
    }


def locate_split(ts:int,bounds):
    for label,(low,high) in bounds.items():
        if low<=ts<high:return label
    return None


def stats(rows):
    """Portfolio-account drawdown is NOT modeled; trade-sequence DD is illustrative."""
    if not rows:return {'trades':0,'win_rate':None,'mean_net_return_pct':None,'profit_factor':None,
                        'illustrative_sequential_dd':None,'losing_streak_max':None,
                        'sample_sufficient_for_validation':False}
    rows=sorted(rows,key=lambda x:(x['exit_time_ms'],x['instrument'],x['scenario']))
    x=np.array([r['return_pct'] for r in rows],dtype=float)
    wins=x[x>0].sum();loss=-x[x<0].sum();equity=peak=1.0;dd=0.;streak=max_streak=0
    for t in rows:
        if t['return_pct']<0:
            streak+=1;max_streak=max(max_streak,streak)
        else:streak=0
        # Illustrative compounded *sequential* 0.5% risk sizing, 2x capped;
        # concurrent holdings across instruments NOT represented.
        stop_distance=max(.002,(t['entry_actual']-t['stop'])/t['entry_actual'])
        exposure=min(2.0,.005/stop_distance)
        equity=max(1e-9,equity*(1.0+exposure*t['return_pct']/100.0))
        peak=max(peak,equity);dd=max(dd,1-equity/peak)
    return {'trades':len(rows),'win_rate':round(float(np.mean(x>0)),4),
            'mean_net_return_pct':round(float(x.mean()),4),
            'median_net_return_pct':round(float(np.median(x)),4),
            'profit_factor':round(float(wins/loss),4) if loss else None,
            'illustrative_sequential_dd':round(dd,4),'losing_streak_max':max_streak,
            'avg_hold_hours':round(float(np.mean([r['hold_30m_bars']*.5 for r in rows])),2),
            'sample_sufficient_for_validation':len(rows)>=100,
            'illustrative_equity_multiple':round(equity,4)}


def research(df,inst:str,args):
    bt.assert_continuous(df,30)
    groups={tf:bt.indicators(bt.resample(df,mins)) for tf,mins in bt.TFMINS.items()}
    if any(f.empty for f in groups.values()):raise ValueError('No completed higher timeframe')
    closes={tf:f.close_ts.to_numpy() for tf,f in groups.items()}
    # Same range for every instrument (fixed --end-utc, duration and warmup).
    effective_start=int(args.since_ms)+args.warmup*MS_DAY
    # Last 48h are removed from decisions to avoid right-censored trades.
    bounds=split_bounds(effective_start,int(args.until_ms))
    outcome=[]
    counters=defaultdict(Counter)
    last_exit=defaultdict(int)
    audits={'evaluated_decisions':0,'earliest_eligible_signal_utc':None,
            'final_eligible_signal_utc':None,'baseline_A_raw':0,'baseline_B_raw':0}
    for t in range(args.warmup*48,len(df)-1):
        decision=int(df.ts.iloc[t])+MS_30M
        split=locate_split(decision,bounds)
        if split is None:continue
        d={};valid=True
        for tf,f in groups.items():
            k=int(np.searchsorted(closes[tf],decision,side='right'))
            if k<(40 if tf=='1D' else 65):valid=False;break
            d[tf]=f.iloc[:k]
        if not valid:continue
        audits['evaluated_decisions']+=1
        iso=datetime.fromtimestamp(decision/1000,timezone.utc).isoformat()
        audits['earliest_eligible_signal_utc']=audits['earliest_eligible_signal_utc'] or iso
        audits['final_eligible_signal_utc']=iso
        af=dg.a_flags(d)
        bf,extra,divergent=dg.b_flags(d)
        if all(af.values()):audits['baseline_A_raw']+=1
        if all(bf.values()):audits['baseline_B_raw']+=1
        # Same one-step-past features as previous diagnostic/backtest.
        if (all(af.values())!=(bt.signal_a(d) is not None) or
            all(bf.values())!=(bt.signal_b(d) is not None)):
            raise AssertionError('Baseline raw signal disagrees with repository strategy')
        for scenario in SCENARIOS:
            flags=af if scenario.strategy=='A' else bf
            if not matches(flags,extra,divergent,scenario):continue
            ctr=counters[(scenario.name,split)]
            ctr['raw_signal_decisions']+=1
            # Fully purge last 48 hours of EACH segment. No trade in train can
            # use future validation prices for its exit simulation.
            max_exit=int(df.ts.iloc[t+1])+args.max_hold*MS_30M
            if max_exit>bounds[split][1] or t+1+args.max_hold>len(df):
                ctr['purged_boundary_or_tail']+=1
                continue
            cooldown_key=(scenario.name,split)
            if decision<last_exit[cooldown_key]+2*MS_30M:
                ctr['cooldown_overlap_rejected']+=1
                continue
            fee=args.fee*scenario.cost_mult
            slip=args.slippage*scenario.cost_mult
            estimate=float(df.close.iloc[t])*(1+slip)
            p,reason=trade_plan(d,estimate,fee,slip,args.funding,scenario.rr1_min)
            if p is None:
                if reason is None:reason=baseline_plan_reason(d,estimate,fee,slip,args.funding)
                ctr['plan_reject_'+reason]+=1
                continue
            ctr['plan_pass']+=1
            stop,tp1,tp2,rr1,rr2=p
            real=float(df.open.iloc[t+1])*(1+slip)
            atr=float(d['1H'].atr.iloc[-1])
            reason=entry_reason(real,estimate,atr,stop,tp1,fee,slip,args.funding,
                                scenario.rr1_min,scenario.max_entry_above_atr)
            if reason:
                ctr['entry_reject_'+reason]+=1
                continue
            ctr['entry_pass']+=1
            trade=bt.simulate_after(df,t+1,stop,tp1,tp2,fee,slip,args.funding,args.max_hold)
            if trade is None:raise AssertionError('Unfillable trade passed price-entry verification')
            if trade['exit_time_ms']>bounds[split][1]:
                raise AssertionError('Right-censored trade escaped temporal purge')
            outcome.append({'instrument':inst,'scenario':scenario.name,'strategy':scenario.strategy,
                            'split':split,'signal_time_ms':decision,'signal_time_utc':iso,
                            'stop':float(stop),'tp1':float(tp1),'tp2':float(tp2),
                            'entry_estimate':estimate,'rr1_est':float(rr1),'rr2_est':float(rr2),
                            **trade})
            ctr['filled_trades']+=1
            last_exit[cooldown_key]=trade['exit_time_ms']
    return outcome,counters,audits,bounds


def ensure_datetime(text):
    if text.endswith('Z'):text=text[:-1]+'+00:00'
    dt=datetime.fromisoformat(text)
    if dt.tzinfo is None or dt.utcoffset().total_seconds()!=0:
        raise ValueError('End time must include UTC Z or +00:00')
    return int(dt.timestamp()*1000)


def run(args):
    args.until_ms=ensure_datetime(args.end_utc)
    if args.until_ms>int(time.time()*1000):raise ValueError('End time cannot be in the future')
    args.since_ms=args.until_ms-args.days*MS_DAY
    if args.days<=args.warmup+30:raise ValueError('Insufficient effective testing period')
    if args.max_hold<=0 or args.max_hold>500:raise ValueError('Invalid maximum hold')
    if any(not 0<=x<=.01 for x in (args.fee,args.slippage,args.funding)):
        raise ValueError('Invalid cost assumptions')
    if args.pause<0:raise ValueError('Invalid pause')
    output=Path(args.output)
    output.parent.mkdir(parents=True,exist_ok=True)
    cache=Path(args.cache);cache.mkdir(parents=True,exist_ok=True)
    trades=[];merged=defaultdict(Counter);audit_list=[];failed={};bounds=None
    for symbol in args.symbols:
        inst=symbol.upper() if symbol.upper().endswith('-USDT-SWAP') else symbol.upper()+'-USDT-SWAP'
        path=cache/f'{inst}_{args.since_ms}_{args.until_ms}.csv'
        try:
            if path.exists():
                df=pd.read_csv(path)
                print('Cache hit',inst,flush=True)
            else:
                print('Downloading confirmed 30m OKX history',inst,args.days,'days',flush=True)
                df=bt.candles_history(inst,args.since_ms,args.until_ms,args.pause)
                df.to_csv(path,index=False)
            bt.assert_continuous(df,30)
            # Strictly require enough time coverage; never silently accept
            # early delistings / API truncation as a full 1/2-year backtest.
            initial_lag=int(df.ts.iloc[0])-args.since_ms
            ending_lag=args.until_ms-(int(df.ts.iloc[-1])+MS_30M)
            if initial_lag>MS_DAY or ending_lag>MS_DAY:
                raise ValueError(f'Historical coverage incomplete: missing-start={initial_lag/MS_DAY:.2f}d, missing-end={ending_lag/MS_DAY:.2f}d')
            rows,counts,audit,bounds=research(df,inst,args)
            trades.extend(rows)
            for k,v in counts.items():merged[k].update(v)
            audit_list.append({'instrument':inst,'candles':len(df),**audit})
            print('DONE',inst,'candles',len(df),'A raw',audit['baseline_A_raw'],
                  'B raw',audit['baseline_B_raw'],'completed scenario trades',len(rows),flush=True)
        except Exception as exc:
            failed[inst]=f'{type(exc).__name__}: {exc}'
            print('FAIL',inst,failed[inst],file=sys.stderr,flush=True)
    reports=[]
    by_market=[]
    for inst in [a['instrument'] for a in audit_list]:
        for sc in SCENARIOS:
            for split in ('train','validation','temporal_test_not_pristine_oos'):
                mtr=[t for t in trades if t['instrument']==inst and t['scenario']==sc.name and t['split']==split]
                by_market.append({'instrument':inst,'scenario':sc.name,'split':split,**stats(mtr)})
    for s in SCENARIOS:
        for split in ('train','validation','temporal_test_not_pristine_oos'):
            rows=[t for t in trades if t['scenario']==s.name and t['split']==split]
            count=merged[(s.name,split)]
            reports.append({'scenario':s.name,'strategy':s.strategy,'split':split,
                            'gate_change':s.gate_change or None,'min_rr_tp1':s.rr1_min,
                            'entry_upper_atr':s.max_entry_above_atr,'cost_multiplier':s.cost_mult,
                            'counts':dict(count),'metrics':stats(rows)})
    # Eligibility for discussing any scenario as potentially promising is
    # PREDECLARED and uses TRAIN & VALIDATION exclusively, not test metrics.
    eligible=[]
    for s in SCENARIOS:
        def r(split):return next(x for x in reports if x['scenario']==s.name and x['split']==split)['metrics']
        train=r('train');val=r('validation')
        if (train['trades']>=40 and val['trades']>=25 and
            (train['mean_net_return_pct'] or 0)>0 and (val['mean_net_return_pct'] or 0)>0 and
            (val['profit_factor'] or 0)>1.1 and
            (val['illustrative_sequential_dd'] or 0)<.25):
            eligible.append(s.name)
    # csv contains time-split metrics and gate/transaction counts, plus raw trade ledger.
    csvpath=output.with_suffix('.scenarios.csv')
    flat=[]
    for item in reports:
        flat.append({k:v for k,v in item.items() if k not in ('counts','metrics')} |
                    {'count_'+k:v for k,v in item['counts'].items()} |
                    {'metric_'+k:v for k,v in item['metrics'].items()})
    pd.DataFrame(flat).to_csv(csvpath,index=False)
    markets_csv=output.with_suffix('.markets.csv')
    pd.DataFrame(by_market).to_csv(markets_csv,index=False)
    ledger=output.with_suffix('.trades.csv')
    pd.DataFrame(trades,columns=['instrument','scenario','strategy','split','signal_time_ms','signal_time_utc',
                                  'stop','tp1','tp2','entry_estimate','rr1_est','rr2_est','return_pct',
                                  'exit_reason','hold_30m_bars','entry_actual','exit_time_ms']).to_csv(ledger,index=False)
    report={'status':'EXPLORATORY_NOT_LIVE_VALIDATED','reproducible_parameters':{
                'end_utc':args.end_utc,'days':args.days,'warmup_days':args.warmup,
                'symbols':args.symbols,'max_hold_30m_bars':args.max_hold,
                'fee_per_side':args.fee,'slippage_per_side':args.slippage,
                'funding_per_8h':args.funding},
            'notes':[COST_NOTE,
                     'Current selected contracts: historical top-200 membership and liquidity are not reproduced',
                     'Independent scenario tests are exploratory: multiple comparisons risk overfitting',
                     'Temporal test uses overlapping dates from earlier 180d pilot: NOT pristine unseen OOS',
                     'Time splits are fixed by decision timestamp; last max-hold interval per split purged',
                     'Illustrative trade-sequence DD is NOT concurrent-portfolio DD',
                     'No auto selection or push; sample threshold 100 completed trades per temporal test'],
            'split_bounds_utc':{k:[datetime.fromtimestamp(v/1000,timezone.utc).isoformat() for v in bounds[k]] for k in bounds} if bounds else None,
            'downloaded_instruments':audit_list,'failed_instruments':failed,
            'all_instruments_succeeded':len(audit_list)==len(args.symbols) and not failed,
            'scenario_predeclared':[s.__dict__ for s in SCENARIOS],
            'scenario_results':reports,
            'market_split_results':by_market,
            'train_validation_only_eligible_exploratory':eligible,
            'any_scenario_ready_for_live_use':False}
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    print('SUMMARY '+json.dumps({'symbols_ok':len(audit_list),'symbols_failed':len(failed),
                                   'test_buckets':len(reports),'eligible_prelim':eligible,
                                   'sample_tests':[(s, next(x['metrics']['trades'] for x in reports if x['scenario']==s and x['split']=='temporal_test_not_pristine_oos')) for s in ('A_BASE','B_BASE','B_NO_BREAKOUT_VOLUME_CONFIRM')]},ensure_ascii=False),flush=True)
    return 0 if report['all_instruments_succeeded'] else 2


def parser():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--symbols',nargs='+',default=['BTC','ETH','SOL','XRP','ADA','DOGE','LINK','AVAX'])
    p.add_argument('--days',type=int,choices=(365,730),default=365)
    p.add_argument('--warmup',type=int,default=90)
    p.add_argument('--end-utc',default='2026-10-01T00:00:00Z')
    p.add_argument('--fee',type=float,default=.0005)
    p.add_argument('--slippage',type=float,default=.0005)
    p.add_argument('--funding',type=float,default=.0001)
    p.add_argument('--max-hold',type=int,default=96)
    p.add_argument('--pause',type=float,default=.14)
    p.add_argument('--cache',default='backtest_data')
    p.add_argument('--output',default='sensitivity_v03.json')
    return p

if __name__=='__main__':
    try:sys.exit(run(parser().parse_args()))
    except (ValueError,AssertionError) as e:
        print(f'FATAL RESEARCH INTEGRITY: {e}',file=sys.stderr)
        sys.exit(2)
