#!/usr/bin/env python3
"""Independent OKX Turtle SHORT research v0.6. NO orders, secrets, or notifications.

Reference: Original Turtle Rules (Curtis Faith), https://tradingblox.com/originalturtles/originalturtlerules.htm
Research adaptation: simplified single-unit breakout with original-style 20/55-day
short entry, 10/20-day channel cover, 2N stop; no original S1 winner-skip,
55-day S1 failsafe, pyramiding, original futures dollar-volatility sizing or
correlated-market portfolio unit limits. Explicit *alternative* risk scenarios.

Information chronology: fixed UTC+8 trading day; prior COMPLETED daily bands,
prior completed N and market regime; 30-minute breakout execution. OHLC bars
cannot resolve all intrabar order sequencing: adversarial same-bar stop is used.
Costs are assumed and do not reproduce funding/impact/liquidations.
"""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import time
from typing import Callable

import numpy as np
import pandas as pd
import trader_strategies_v04 as base

MS30=base.MS30
DAY=base.MSDAY
H8=8*3_600_000
TPE_OFFSET=base.TPE_OFFSET
SOURCE='https://tradingblox.com/originalturtles/originalturtlerules.htm'
SYMBOLS=('BTC','ETH','SOL','XRP','ADA','DOGE','LINK','AVAX','LTC','BCH','DOT','TRX')


@dataclass(frozen=True)
class Scenario:
    name:str
    entry_days:int=20
    exit_days:int=10
    stop_n:float=2.0
    risk_fraction:float=.005
    port_risk_cap:float=.02
    symbol_notional_cap:float=.75
    portfolio_gross_cap:float=2.0
    max_positions:int=3
    market_filter:str='none'
    atr_filter:str='none'
    cost_mult:float=1.0
    drawdown_halt:float=.20


SCENARIOS=(
    Scenario('S1_SHORT_BASE'),
    Scenario('S2_SHORT_BASE',55,20),
    Scenario('S1_SHORT_RISK_025',risk_fraction=.0025),
    Scenario('S1_SHORT_RISK_100',risk_fraction=.0100),
    Scenario('S1_SHORT_STOP_2_5N',stop_n=2.5),
    Scenario('S1_SHORT_BTC_BEAR',market_filter='btc_below_sma200'),
    Scenario('S1_SHORT_ATR_1_TO_10PCT',atr_filter='one_to_ten_pct'),
    Scenario('S1_SHORT_DOUBLE_COSTS',cost_mult=2.0),
    Scenario('S1_SHORT_MAX_2_POSITIONS',max_positions=2),
    Scenario('S1_SHORT_DD_HALT_10PCT',drawdown_halt=.10),
    Scenario('S2_SHORT_BTC_BEAR',55,20,market_filter='btc_below_sma200'),
)
SCENARIO_BY_NAME={s.name:s for s in SCENARIOS}


@dataclass
class Position:
    symbol:str
    entry:float
    entry_ts:int
    entry_day:int
    units:float
    notional:float
    initial_stop:float
    n_at_entry:float
    fee:float
    slippage:float
    funding_rate:float
    entry_fee:float
    next_fund_ts:int
    funding_paid:float=0.0


def utc(ms:int)->str:
    return datetime.fromtimestamp(ms/1000,timezone.utc).isoformat()


def bucket_of(ts:int)->int:
    """Exact UTC+8 midnight anchor; OKX 1D candle buckets correspond to this."""
    return (int(ts)+TPE_OFFSET)//DAY*DAY-TPE_OFFSET


def prepare_market(df:pd.DataFrame):
    """OHLCV, fully closed historical 1D indicators, and one lookup per 30m bar."""
    base.validate_30m(df)
    day=base.aligned_bars(df,1440).reset_index(drop=True)
    if len(day)<320:raise ValueError('Not enough completed 1D candles')
    close=day.close
    prev_close=close.shift(1)
    tr=pd.concat([day.high-day.low,(day.high-prev_close).abs(),(day.low-prev_close).abs()],axis=1).max(axis=1)
    # N is a 20-day Wilder moving average, lagged one full day.
    day['n20_prev']=tr.ewm(alpha=1/20,adjust=False,min_periods=20).mean().shift(1)
    for n in (10,20,55):
        day[f'high{n}_prev']=day.high.shift(1).rolling(n,min_periods=n).max()
        day[f'low{n}_prev']=day.low.shift(1).rolling(n,min_periods=n).min()
    day['close_prev']=prev_close
    day['sma200_prev']=close.rolling(200,min_periods=200).mean().shift(1)
    day['atr_ratio_prev']=day.n20_prev/day.close_prev
    mapped={int(r.ts):r for _,r in day.iterrows()}
    timestamps=df.ts.to_numpy(dtype=np.int64)
    idxs=np.array([bucket_of(int(t)) for t in timestamps],dtype=np.int64)
    # At start/end, one partial daily bucket may have been properly discarded.
    day_by_bar=[mapped.get(int(k)) for k in idxs]
    return {'raw':df.reset_index(drop=True),'day':day,'day_by_bar':day_by_bar,'ts':timestamps,
            'open':df.open.to_numpy(dtype=float),'high':df.high.to_numpy(dtype=float),
            'low':df.low.to_numpy(dtype=float),'close':df.close.to_numpy(dtype=float),
            'bar_day':idxs}


def entry_quote(open_px:float,low_px:float,trigger:float,slip:float):
    if low_px>trigger:return None
    return min(open_px,trigger)*(1-slip)


def cover_quote(open_px:float,high_px:float,stop:float,channel:float,slip:float):
    """Short buy-stop / channel exit; if both hit, choose worse cover level."""
    hit_stop=high_px>=stop
    hit_channel=high_px>=channel
    if not (hit_stop or hit_channel):return None
    level=max([x for x,hit in ((stop,hit_stop),(channel,hit_channel)) if hit])
    return max(open_px,level)*(1+slip), ('stop_first_conservative' if hit_stop else 'channel_cover')


def market_ok(s:Scenario,market:dict,idx:int,regime:bool):
    d=market['day_by_bar'][idx]
    if d is None:return False,'incomplete_daily_bucket'
    fields=(f'low{s.entry_days}_prev',f'high{s.exit_days}_prev','n20_prev','close_prev','atr_ratio_prev')
    if any(not np.isfinite(float(d[k])) for k in fields):return False,'indicator_unavailable'
    if s.market_filter=='btc_below_sma200' and not regime:return False,'btc_regime_rejected'
    if s.atr_filter=='one_to_ten_pct' and not (.01<=float(d.atr_ratio_prev)<=.10):return False,'atr_band_rejected'
    return True,None


def mark_equity(cash:float,positions:dict,markets:dict,idx:int,using_open:bool=False):
    field='open' if using_open else 'close'
    return cash+sum(p.units*(p.entry-float(markets[p.symbol][field][idx])) for p in positions.values())


def simulate_portfolio(markets:dict,scenario:Scenario,bounds:tuple[int,int],args):
    """One simulated 1x-equity account per scenario/time split; real concurrency.

    Same-time exits run before entries, positions sized by cash+unrealized P&L,
    never using future candles. At split end, remaining positions exit on the
    last IN-SPLIT closed price to avoid cross-split outcome leakage.
    """
    symbols=sorted(markets)
    ts=markets[symbols[0]]['ts']
    a=int(np.searchsorted(ts,bounds[0]))
    b=int(np.searchsorted(ts,bounds[1]))
    if b<=a:raise ValueError('Empty evaluation split')
    for name in symbols[1:]:
        if not np.array_equal(ts,markets[name]['ts']):raise ValueError('Misaligned 30m calendar: '+name)
    if scenario.entry_days not in (20,55):raise ValueError('Unapproved entry window')
    if scenario.exit_days not in (10,20):raise ValueError('Unapproved exit window')
    fee=args.fee*scenario.cost_mult
    slip=args.slippage*scenario.cost_mult
    fund=args.funding
    cash=1.0;peak=1.0;max_dd=0.0
    positions={};last_entry_day={};trades=[];ctr=Counter()
    halted=False; halt_next=False
    equity_path=[];max_open=0;max_gross=0.;max_open_risk=0.;max_lev=0.

    def close_pos(symbol,j,price,reason,exit_ts):
        nonlocal cash
        p=positions.pop(symbol)
        # Conservative ceil charge (at least one 8h period); avoid charging
        # already-accrued funding more than once.
        total_fund=p.notional*fund*max(1,math.ceil((exit_ts-p.entry_ts)/H8))
        residual=max(0.,total_fund-p.funding_paid)
        pnl=p.units*(p.entry-price)
        ex_fee=p.units*price*fee
        cash+=pnl-ex_fee-residual
        net=pnl-p.entry_fee-ex_fee-total_fund
        trades.append({'scenario':scenario.name,'symbol':symbol,'side':'SHORT',
                       'entry_ts':p.entry_ts,'exit_ts':exit_ts,
                       'entry':p.entry,'exit':price,'initial_stop':p.initial_stop,
                       'notional_pct_of_equity_at_start':p.notional, # units: starting equity = 1
                       'entry_N':p.n_at_entry,'pnl_equity_units':net,
                       'net_return_pct_of_notional':100*net/p.notional,
                       'hold_hours':(exit_ts-p.entry_ts)/3_600_000,
                       'exit_reason':reason})
        ctr['exits_'+reason]+=1

    for j in range(a,b):
        t=int(ts[j]);current_day=bucket_of(t)
        if halt_next:
            for sym in sorted(list(positions)):
                px=float(markets[sym]['open'][j])*(1+slip)
                close_pos(sym,j,px,'portfolio_drawdown_halt',t)
            halted=True;halt_next=False
            ctr['portfolio_halt_triggered']+=1

        # 1. Conservative funding accrual on fixed 8h interval (across held positions).
        for sym,p in list(positions.items()):
            while t>=p.next_fund_ts:
                cash-=p.notional*fund
                p.funding_paid+=p.notional*fund
                p.next_fund_ts+=H8

        # 2. Cover short when prior-completed-day exit channel OR initial stop hit.
        exited_this_bar=set()
        for sym,p in sorted(list(positions.items())):
            m=markets[sym];d=m['day_by_bar'][j]
            channel=float(d[f'high{scenario.exit_days}_prev']) if d is not None else math.inf
            if not math.isfinite(channel):
                ctr['missing_exit_channel']+=1
                continue
            event=cover_quote(m['open'][j],m['high'][j],p.initial_stop,channel,p.slippage)
            if event:
                px,reason=event
                close_pos(sym,j,px,reason,t)
                exited_this_bar.add(sym)
            elif t-p.entry_ts>=args.max_hold_days*DAY:
                px=float(m['open'][j])*(1+slip)
                close_pos(sym,j,px,'max_hold_exit',t)
                exited_this_bar.add(sym)

        # 3. Entries only after closed previous day info (not current day OHLC).
        # With only OHLC bars, we cannot know whether another instrument's
        # entry occurred before a same-bar exit freed capacity. Conservatively
        # disallow NEW entries across all instruments on bars containing exits.
        if exited_this_bar:ctr['ambiguous_same_bar_cash_reuse_prevented']+=1
        if not halted and not halt_next and not exited_this_bar:
            eq=max(0.0,mark_equity(cash,positions,markets,j,using_open=True))
            if eq>0:
                # All symbols have the same timestamps and completed-day anchor.
                bm=markets.get('BTC-USDT-SWAP')
                bd=bm['day_by_bar'][j] if bm is not None else None
                btc_bear=bool(bd is not None and np.isfinite(float(bd.sma200_prev)) and float(bd.close_prev)<float(bd.sma200_prev))
                entries=[]
                for sym in symbols:
                    if sym in positions or sym in exited_this_bar:continue
                    m=markets[sym]
                    if last_entry_day.get(sym)==current_day:continue
                    valid,reason=market_ok(scenario,m,j,btc_bear)
                    if not valid:
                        ctr[reason]+=1;continue
                    d=m['day_by_bar'][j]
                    trigger=float(d[f'low{scenario.entry_days}_prev'])
                    if m['low'][j]<=trigger:
                        ctr['raw_short_breakouts']+=1
                        entries.append((sym,trigger,float(d.n20_prev)))
                # Deterministic selection across assets; avoid cherry picking.
                entries.sort()
                for sym,threshold,N in entries:
                    if len(positions)>=scenario.max_positions:
                        ctr['max_positions_rejected']+=1;continue
                    m=markets[sym]
                    eq=mark_equity(cash,positions,markets,j,using_open=True)
                    if eq<=0:
                        ctr['insolvent_rejected']+=1;continue
                    ent=entry_quote(m['open'][j],m['low'][j],threshold,slip)
                    if ent is None:continue
                    stop=ent+scenario.stop_n*N
                    stop_dist=(stop-ent)/ent
                    if not .003<=stop_dist<=.35 or ent<=0:
                        ctr['stop_out_of_range']+=1;continue
                    current_risk=sum(max(0,p.units*(p.initial_stop-p.entry)) for p in positions.values())
                    current_gross=sum(p.notional for p in positions.values())
                    requested=eq*scenario.risk_fraction/stop_dist
                    available_risk=max(0,eq*scenario.port_risk_cap-current_risk)/stop_dist
                    available_gross=max(0,eq*scenario.portfolio_gross_cap-current_gross)
                    notional=min(requested,eq*scenario.symbol_notional_cap,available_risk,available_gross)
                    if notional<eq*.05:
                        ctr['size_or_portfolio_cap_rejected']+=1;continue
                    unit=notional/ent
                    entry_fee=unit*ent*fee
                    cash-=entry_fee
                    positions[sym]=Position(sym,ent,t,current_day,unit,notional,stop,N,fee,slip,fund,entry_fee,t+H8)
                    last_entry_day[sym]=current_day
                    ctr['filled_short_entries']+=1
                    # Ambiguous order within the first 30m entry bar: if price
                    # could touch the new stop, assume entry-then-stop (loss).
                    if float(m['high'][j])>=stop:
                        ex=max(float(m['open'][j]),stop)*(1+slip)
                        close_pos(sym,j,ex,'same_bar_adverse_stop',t+MS30)

        # 4. At split boundary, liquidate at last completed 30m close in split.
        if j==b-1:
            for sym in sorted(list(positions)):
                px=float(markets[sym]['close'][j])*(1+slip)
                close_pos(sym,j,px,'research_split_end_close',t+MS30)

        eq=mark_equity(cash,positions,markets,j)
        peak=max(peak,eq)
        dd=1-eq/peak if peak>0 else 1.
        max_dd=max(max_dd,dd)
        if not halted and not halt_next and dd>=scenario.drawdown_halt and j<b-1:
            halt_next=True
        gross=sum(p.notional for p in positions.values())
        risk=sum(max(0,p.units*(p.initial_stop-p.entry)) for p in positions.values())
        max_open=max(max_open,len(positions))
        max_gross=max(max_gross,gross)
        max_open_risk=max(max_open_risk,risk)
        max_lev=max(max_lev,gross/max(eq,1e-9))
        equity_path.append((t+MS30,eq,dd,len(positions)))

    if positions:raise AssertionError('Positions leaked across split boundary')
    return {'trades':trades,'counts':dict(ctr),'starting_equity':1.0,'ending_equity':cash,
            'net_account_return_pct':100*(cash-1),'mark_to_market_max_dd_pct':100*max_dd,
            'max_simultaneous_positions':max_open,'max_notional_units':max_gross,
            'max_simultaneous_stop_risk_units':max_open_risk,'max_gross_leverage_of_equity':max_lev,
            'portfolio_halted':halted,'equity_points':equity_path}


def trade_stats(trades):
    ret=np.asarray([t['net_return_pct_of_notional'] for t in trades],dtype=float)
    if not len(ret):
        return {'trades':0,'wins':0,'win_rate':None,'avg_net_trade_pct':None,'profit_factor':None,
                'median_net_trade_pct':None,'has_100_trades':False}
    profits=float(np.sum(ret[ret>0]));loss=-float(np.sum(ret[ret<0]))
    return {'trades':len(trades),'wins':int(np.sum(ret>0)),'win_rate':round(float(np.mean(ret>0)),4),
            'avg_net_trade_pct':round(float(np.mean(ret)),4),'median_net_trade_pct':round(float(np.median(ret)),4),
            'profit_factor':round(profits/loss,4) if loss>0 else None,'has_100_trades':len(trades)>=100}


def load_data(args,loader:Callable):
    since=base.timestamp_utc(args.end_utc)-args.days*DAY
    end=base.timestamp_utc(args.end_utc)
    cache=Path(args.cache);cache.mkdir(parents=True,exist_ok=True)
    expected=np.arange(since,end,MS30,dtype=np.int64)
    markets={};failures={};coverage=[]
    for symbol in args.symbols:
        inst=symbol if symbol.endswith('-USDT-SWAP') else symbol+'-USDT-SWAP'
        fname=cache/f'{inst}_{since}_{end}_30m.csv'
        try:
            if fname.exists():
                raw=pd.read_csv(fname)
                print('CACHE',inst,flush=True)
            else:
                print('DOWNLOAD',inst,args.days,'days',flush=True)
                raw=loader(inst,since,end,args.pause)
                # Cache only fully verified coverage; never reuse partial OHLCV.
            base.validate_30m(raw)
            if not np.array_equal(raw.ts.to_numpy(dtype=np.int64),expected):
                raise ValueError(f'{inst}: incomplete 30m calendar, expected={len(expected)} received={len(raw)}')
            m=prepare_market(raw)
            markets[inst]=m
            if not fname.exists():raw.to_csv(fname,index=False)
            coverage.append({'symbol':inst,'bars30m':len(raw),'days':len(m['day']),
                             'first':utc(int(raw.ts.iloc[0])),'last':utc(int(raw.ts.iloc[-1]))})
            print('VERIFIED',inst,len(raw),'confirmed 30m bars',flush=True)
        except Exception as err:
            failures[inst]=type(err).__name__+': '+str(err)
            print('FAIL',inst,failures[inst],flush=True)
    return markets,failures,coverage


def run(args,loader=None):
    end=base.timestamp_utc(args.end_utc)
    since=end-args.days*DAY
    if end>int(time.time()*1000):raise ValueError('Future end timestamp')
    if args.days<=args.warmup_days+200 or args.warmup_days<320:raise ValueError('Insufficient warmup or evaluation period')
    if len(set(args.symbols))!=len(args.symbols):raise ValueError('Repeated symbols')
    if args.fee<0 or args.slippage<0 or args.funding<0 or args.fee>.01 or args.slippage>.01 or args.funding>.01:
        raise ValueError('Invalid assumed costs')
    if args.max_hold_days<20:raise ValueError('Max hold too short')
    if not 0<=args.pause<=3:raise ValueError('Invalid pause')
    out=Path(args.output);out.parent.mkdir(parents=True,exist_ok=True)
    if loader is None:loader=base.download_30m
    markets,errors,coverage=load_data(args,loader)
    required=args.min_covered
    if len(markets)<required or 'BTC-USDT-SWAP' not in markets:
        print('INSUFFICIENT VALID SYMBOLS',len(markets),'required',required,'BTC required',flush=True)
    # Exact range and fixed window; each scenario starts with a clean portfolio.
    bounds=split_bounds(since,end,args.warmup_days)
    trades=[];rows=[];diag=[];paths=[];failed_scenarios={}
    if len(markets)>=required and 'BTC-USDT-SWAP' in markets:
        for s in SCENARIOS:
            for label,bound in bounds.items():
                try:
                    outcome=simulate_portfolio(markets,s,bound,args)
                    for trade in outcome['trades']:
                        trade['split']=label
                        trades.append(trade)
                    stats=trade_stats(outcome['trades'])
                    row={'scenario':s.name,'split':label,**stats,**{k:v for k,v in outcome.items() if k not in ('trades','counts','equity_points')}}
                    rows.append(row)
                    diag.append({'scenario':s.name,'split':label,**outcome['counts']})
                    paths.extend({'scenario':s.name,'split':label,'ts':t,'equity':eq,'drawdown':dd,'positions':p}
                                 for t,eq,dd,p in outcome['equity_points'])
                    print('RESULT',s.name,label,'trades',stats['trades'],'return%',round(row['net_account_return_pct'],3),
                          'mtm_dd%',round(row['mark_to_market_max_dd_pct'],3),flush=True)
                except Exception as err:
                    failed_scenarios[f'{s.name}/{label}']=type(err).__name__+': '+str(err)
                    print('SCENARIO_FAIL',s.name,label,str(err),flush=True)
    pd.DataFrame(trades,columns=['scenario','split','symbol','side','entry_ts','exit_ts','entry','exit','initial_stop','notional_pct_of_equity_at_start','entry_N','pnl_equity_units','net_return_pct_of_notional','hold_hours','exit_reason']).to_csv(out.with_suffix('.trades.csv'),index=False)
    pd.DataFrame(rows).to_csv(out.with_suffix('.summary.csv'),index=False)
    pd.DataFrame(diag).to_csv(out.with_suffix('.diagnostic.csv'),index=False)
    pd.DataFrame(paths,columns=['scenario','split','ts','equity','drawdown','positions']).to_csv(out.with_suffix('.equity.csv'),index=False)
    success=len(markets)>=required and 'BTC-USDT-SWAP' in markets and not failed_scenarios and len(rows)==3*len(SCENARIOS)
    # Never label a profit-seeking strategy validated from observed, overlapping history.
    report={'status':'RESEARCH_ONLY_NOT_LIVE_VALIDATED','source':SOURCE,
            'reproducible':{'end_utc':args.end_utc,'days':args.days,'warmup_days':args.warmup_days,
                            'symbols':args.symbols,'min_covered':args.min_covered,'fee':args.fee,
                            'slippage':args.slippage,'funding_per_8h':args.funding,
                            'max_hold_days':args.max_hold_days},
            'interval_bounds':{label:[utc(int(x)) for x in b] for label,b in bounds.items()},
            'scenario_configs':[vars(s) for s in SCENARIOS], 'coverage':coverage,
            'missing_symbols':errors,'failed_scenarios':failed_scenarios,'results':rows,
            'complete_required_portfolios':success,'ready_for_live':False,
            'limitations':['Shorts only. S1 last-winning-breakout skip, S1 55d failsafe, pyramiding not reproduced.',
                           'Contemporaneous static 12 contracts are NOT historical top-200 membership; survivorship bias.',
                           'A fixed historical 1095/730d backtest can overlap past research. Holdout NOT pristine unseen OOS.',
                           'Costs are fixed conservative proxies, not true historical funding/liquidity/borrow/fees.',
                           'Intrabar OHLC ordering unknown; same-bar adverse stop chosen by design.',
                           'Portfolio is 1x initial-equity denomination, not exchange margin/liquidation simulator.',
                           'Drawdown guard queues liquidation at NEXT 30m open, NOT retrospectively at signal close.',
                           'Every train/validation/test interval has a separate cash ledger; open positions forcibly closed at split end.',
                           'Market filter uses only prior fully closed BTC UTC+8 daily close/SMA200.',
                           'All scenario comparisons exploratory/multiple-tested; independent forward paper validation still needed.']}
    out.write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    print('FINAL '+json.dumps({'valid_symbols':len(markets),'failed_symbols':len(errors),'scenarios':len(SCENARIOS),
                                'completed_buckets':len(rows),'trades':len(trades),'complete':success,
                                'ready_for_live':False}),flush=True)
    return 0 if success else 2


def split_bounds(start,end,warmup_days):
    return base.split_ranges(start,end,warmup_days)


def parser():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--symbols',nargs='+',default=list(SYMBOLS))
    p.add_argument('--days',type=int,choices=[730,1095],default=1095)
    p.add_argument('--warmup-days',type=int,default=320)
    p.add_argument('--end-utc',default='2026-10-01T00:00:00Z')
    p.add_argument('--min-covered',type=int,default=8)
    p.add_argument('--fee',type=float,default=.0005)
    p.add_argument('--slippage',type=float,default=.0005)
    p.add_argument('--funding',type=float,default=.0001)
    p.add_argument('--max-hold-days',type=int,default=90)
    p.add_argument('--pause',type=float,default=.17)
    p.add_argument('--cache',default='research_v06_cache')
    p.add_argument('--output',default='turtle_v06.json')
    return p


if __name__=='__main__':
    try:raise SystemExit(run(parser().parse_args()))
    except Exception as e:
        print('FATAL:',type(e).__name__,str(e),flush=True)
        raise SystemExit(2)
