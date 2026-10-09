#!/usr/bin/env python3
"""Second-round OKX diagnostic backtest; records why research trades are rejected.

Requires historical_backtest.py from the first round in same directory.
NO parameter optimization, NO exchange orders, NO notifications.
Independent gate counts are not trade counts. Cumulative counts depend on gate order.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys
import time

import numpy as np
import pandas as pd
import historical_backtest as bt

A_KEYS = (
    "A1_ma25_bias_below_minus2_5pct", "A2_ma25_z_below_minus1_5",
    "A3_1h_macd_cross_last2", "A4_1h_hist_rising3",
    "A5_1h_price_breakout", "A6_1h_volume_1_2x",
    "A7_4h_hist_not_falling", "A8_1h_rsi_25to75",
)
B_KEYS = (
    "B1_divergence_at_least4_of8", "B2_includes_4h_divergence",
    "B3_includes_12h_or_1d_divergence", "B4_confirmed_breakout_AND_volume_same_tf",
    "B5_1h_hist_improving", "B6_1h_rsi_25to75",
)
PLAN_KEYS = (
    "P1_atr_positive", "P2_entry_near_last_closed_bar",
    "P3_stop_risk_in_0_2to7pct", "P4_historical_resistance_exists",
    "P5_at_least2_targets_net_rr_ge2", "P6_tp2_net_rr_ge2_8",
)
FILL_KEYS = ("E1_next_open_within_entry_range", "E2_next_open_above_stop", "E3_net_rr_still_ge2")


def vol_good(frame):
    average = float(frame.volume.iloc[-21:-1].mean())
    return bool(average > 0 and float(frame.volume.iloc[-1]) >= 1.2 * average)


def breaking(frame, lookback):
    return bool(float(frame.close.iloc[-1]) > float(frame.high.iloc[-lookback-1:-1].max())
                and float(frame.close.iloc[-2]) <= float(frame.high.iloc[-lookback-2:-2].max()))


def a_flags(d):
    daily, h, h4 = d["1D"], d["1H"], d["4H"]
    ma = float(daily.close.iloc[-25:].mean())
    sd = float(daily.close.iloc[-25:].std())
    px = float(daily.close.iloc[-1])
    finite = math.isfinite(ma) and math.isfinite(sd) and ma > 0 and sd > 0
    bias = (px / ma - 1) * 100 if finite else float("nan")
    z = (px-ma)/sd if finite else float("nan")
    out = (
        finite and bias <= -2.5,
        finite and z <= -1.5,
        any(float(h.dif.iloc[k]) > float(h.dea.iloc[k])
            and float(h.dif.iloc[k-1]) <= float(h.dea.iloc[k-1]) for k in (-1,-2)),
        bool(h["hist"].iloc[-1] > h["hist"].iloc[-2] > h["hist"].iloc[-3]),
        breaking(h,12),
        vol_good(h),
        bool(h4["hist"].iloc[-1] >= h4["hist"].iloc[-2]),
        bool(25 <= h.rsi.iloc[-1] <= 75),
    )
    return dict(zip(A_KEYS, map(bool,out)))


def b_flags(d):
    divergences = {tf: bt.pivots_latest_confirmed(d[tf]) is not None for tf in bt.TFMINS}
    br30,br1h = breaking(d["30m"],8),breaking(d["1H"],12)
    v30,v1h = vol_good(d["30m"]),vol_good(d["1H"])
    f = dict(zip(B_KEYS, (
        sum(divergences.values()) >= 4,
        divergences["4H"],
        divergences["12H"] or divergences["1D"],
        (br30 and v30) or (br1h and v1h),
        d["1H"]["hist"].iloc[-1] > d["1H"]["hist"].iloc[-2],
        25 <= d["1H"].rsi.iloc[-1] <= 75,
    )))
    extra={"div_"+k:v for k,v in divergences.items()}
    extra.update({"breakout_30m":br30,"volume_30m":v30,
                  "breakout_1h":br1h,"volume_1h":v1h,
                  "breakout_either_tf":br30 or br1h,
                  "volume_either_tf":v30 or v1h})
    return {k:bool(v) for k,v in f.items()},extra,int(sum(divergences.values()))


def plan_flags(d,price,fee,slip,funding):
    h,h4=d["1H"],d["4H"]
    atr=float(h.atr.iloc[-1]);close=float(d["30m"].close.iloc[-1])
    f={key:False for key in PLAN_KEYS}
    f[PLAN_KEYS[0]]=bool(math.isfinite(atr) and atr>0)
    if not f[PLAN_KEYS[0]]:return f,None
    f[PLAN_KEYS[1]]=abs(price-close)<=.7*atr
    stop=float(h.low.iloc[-16:-1].min())-.2*atr
    risk=price-stop
    f[PLAN_KEYS[2]]=bool(risk>0 and .002 <=risk/price<=.07)
    highs=sorted({float(v) for v in pd.concat([h.high.iloc[-90:],h4.high.iloc[-80:]])
                  if float(v)>price+.25*atr})
    f[PLAN_KEYS[3]]=len(highs)>0
    cost=price*(2*fee+2*slip+funding)
    rr=lambda target:(target-price-cost)/(risk+cost) if risk+cost>0 else float("-inf")
    targets=[v for v in highs if rr(v)>=2 and (v-price)/price<=.18]
    f[PLAN_KEYS[4]]=len(targets)>=2
    f[PLAN_KEYS[5]]=len(targets)>=2 and rr(targets[-1])>=2.8
    output=(stop,targets[0],targets[-1],rr(targets[0]),rr(targets[-1])) if all(f.values()) else None
    # Exact regression check against the first-round plan.
    baseline=bt.plan(d,price,fee,slip,funding)
    if (baseline is None)!=(output is None):
        raise AssertionError("Diagnostic trade-plan logic differs from original")
    if baseline is not None and not np.allclose(baseline,output,rtol=1e-12,atol=1e-12):
        raise AssertionError("Diagnostic trade-plan price levels differ from original")
    return f,output


def entry_flags(df,t,estimated,stop,tp1,fee,slip,funding,atr):
    real=float(df.open.iloc[t+1])*(1+slip)
    cost=real*(2*fee+2*slip+funding)
    ratio=(tp1-real-cost)/(real-stop+cost) if real-stop+cost>0 else float("-inf")
    return {FILL_KEYS[0]: bool(real>=estimated-.12*atr and real<=estimated),
            FILL_KEYS[1]: bool(real>stop),
            FILL_KEYS[2]: bool(ratio>=2)}


class GateCounts:
    def __init__(self,keys):
        self.keys=tuple(keys)
        self.total=0
        self.independent=Counter()
        self.cumulative=Counter()
        self.missing_hist=Counter()
        self.one_failure=Counter()
    def record(self, flags):
        if set(flags)!=set(self.keys):raise AssertionError("Gate key mismatch")
        self.total+=1
        success=True
        missing=[]
        for k in self.keys:
            if flags[k]:self.independent[k]+=1
            else:missing.append(k)
            success=success and flags[k]
            if success:self.cumulative[k]+=1
        self.missing_hist[str(len(missing))]+=1
        if len(missing)==1:self.one_failure[missing[0]]+=1
    def asdict(self):
        return {"evaluated":self.total,"individual_pass":{k:self.independent[k] for k in self.keys},
                "sequential_pass":{k:self.cumulative[k] for k in self.keys},
                "all_pass":self.missing_hist["0"],
                "failed_gate_count_histogram":dict(sorted(self.missing_hist.items(),key=lambda kv:int(kv[0]))),
                "only_one_failed_gate":{k:self.one_failure[k] for k in self.keys}}


def diagnosis(df,fee,slippage,funding,warmup):
    bt.assert_continuous(df,30)
    groups={tf:bt.indicators(bt.resample(df,mins)) for tf,mins in bt.TFMINS.items()}
    closes={tf:frame.close_ts.to_numpy() for tf,frame in groups.items()}
    a,b,ap,bp,ae,be=(GateCounts(A_KEYS),GateCounts(B_KEYS),
                      GateCounts(PLAN_KEYS),GateCounts(PLAN_KEYS),
                      GateCounts(FILL_KEYS),GateCounts(FILL_KEYS))
    extras=Counter();div_count=Counter();too_early=0;inspected=0
    time_start=None;time_end=None
    examples=[]
    for t in range(warmup*48,len(df)-1):
        decision=int(df.ts.iloc[t])+1800000
        d={}
        for tf,frame in groups.items():
            idx=np.searchsorted(closes[tf],decision,side="right")
            if idx < (40 if tf=="1D" else 65):
                d=None;break
            d[tf]=frame.iloc[:idx]
        if d is None:
            too_early+=1;continue
        inspected+=1
        if time_start is None:time_start=decision
        time_end=decision
        fa=a_flags(d);fb,extra,divn=b_flags(d)
        a.record(fa);b.record(fb)
        for k,v in extra.items():
            if v:extras[k]+=1
        div_count[str(divn)]+=1
        # Explicit assertion ensures audit gates haven't silently changed strategy definitions.
        sa=bt.signal_a(d) is not None
        sb=bt.signal_b(d) is not None
        if sa!=all(fa.values()) or sb!=all(fb.values()):
            raise AssertionError(f"Signal mismatch at {decision}: {fa=} {fb=}")
        if not (sa or sb):continue
        estimated=float(df.close.iloc[t])*(1+slippage)
        stages,plan=plan_flags(d,estimated,fee,slippage,funding)
        for label,sig,plan_counts,entry_counts in (("A",sa,ap,ae),("B",sb,bp,be)):
            if not sig:continue
            plan_counts.record(stages)
            if plan is not None:
                stop,tp1,tp2,rr1,rr2=plan
                fill=entry_flags(df,t,estimated,stop,tp1,fee,slippage,funding,
                                 float(d["1H"].atr.iloc[-1]))
                entry_counts.record(fill)
                if len(examples)<12:
                    examples.append({"time_utc":datetime.fromtimestamp(decision/1000,timezone.utc).isoformat(),
                                     "strategy":label, "plan_pass":True,
                                     "entry_pass":all(fill.values())})
    return {"evaluated_decisions":inspected,"missing_warmup_decisions":too_early,
            "effective_window_start_utc":datetime.fromtimestamp(time_start/1000,timezone.utc).isoformat() if time_start else None,
            "effective_window_end_utc":datetime.fromtimestamp(time_end/1000,timezone.utc).isoformat() if time_end else None,
            "strategy_A":a.asdict(),"strategy_B":b.asdict(),
            "strategy_B_independent_extra":dict(sorted(extras.items())),
            "strategy_B_number_of_divergent_timeframes":dict(sorted(div_count.items(),key=lambda kv:int(kv[0]))),
            "after_A_raw_signal_trade_plan":ap.asdict(),"after_B_raw_signal_trade_plan":bp.asdict(),
            "after_A_plan_entry_limit":ae.asdict(),"after_B_plan_entry_limit":be.asdict(),
            "debug_examples_not_orders":examples}


def flatten_csv(results,path):
    rows=[]
    for x in results:
        iid=x["instrument"]
        for group in ("strategy_A","strategy_B","after_A_raw_signal_trade_plan",
                      "after_B_raw_signal_trade_plan","after_A_plan_entry_limit","after_B_plan_entry_limit"):
            z=x[group]
            for condition in z["individual_pass"]:
                rows.append({"instrument":iid,"section":group,"condition":condition,
                             "eligible":z["evaluated"],"pass_independent":z["individual_pass"][condition],
                             "pass_cumulative":z["sequential_pass"][condition]})
        for name,num in x["strategy_B_independent_extra"].items():
            rows.append({"instrument":iid,"section":"strategy_B_extra","condition":name,
                         "eligible":x["evaluated_decisions"],"pass_independent":num,"pass_cumulative":None})
    pd.DataFrame(rows,columns=["instrument","section","condition","eligible","pass_independent","pass_cumulative"]).to_csv(path,index=False)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbols",nargs="+",default=["BTC","ETH","SOL","XRP","ADA","DOGE","LINK","AVAX"])
    p.add_argument("--days",type=int,default=180)
    p.add_argument("--warmup",type=int,default=75)
    p.add_argument("--end-ms",type=int)
    p.add_argument("--fee",type=float,default=.0005)
    p.add_argument("--slippage",type=float,default=.0005)
    p.add_argument("--funding",type=float,default=.0001)
    p.add_argument("--output",default="diagnostic_v02.json")
    p.add_argument("--cache",default="backtest_data")
    p.add_argument("--pause",type=float,default=.12)
    args=p.parse_args(argv)
    if args.days<=args.warmup+30:p.error("days must exceed warmup by at least 30 days")
    if any(not 0<=x<=.02 for x in (args.fee,args.slippage,args.funding)):
        p.error("invalid transaction-cost assumption")
    until=int(args.end_ms or time.time()*1000)
    since=until-args.days*86400000
    root=Path(args.cache);root.mkdir(exist_ok=True,parents=True)
    report={"status":"DIAGNOSTIC_NOT_STRATEGY_VALIDATION","source":"OKX confirmed 30m historical candles",
            "requested_symbols":args.symbols,"days_requested":args.days,"warmup_days":args.warmup,
            "notes":["Independent filters count on all evaluable 30m decisions, not independent trades",
                     "Sequential funnel depends on published gate order; never equate to causal contribution",
                     "The first 75 days are used for indicator warmup, reducing evaluable days",
                     "A fixed current-symbol pilot has survivorship bias; no historical order book or market-cap membership",
                     "Research-only: no notification or exchange order"],
            "results":[],"failures":{},"summary":{}}
    for sym in args.symbols:
        iid=sym if sym.endswith("-USDT-SWAP") else sym.upper()+"-USDT-SWAP"
        cache=root/f"{iid}_{since}_{until}.csv"
        try:
            if cache.exists():df=pd.read_csv(cache)
            else:
                print(f"Downloading {iid} {args.days} days",flush=True)
                df=bt.candles_history(iid,since,until,args.pause)
                df.to_csv(cache,index=False)
            observed=diagnosis(df,args.fee,args.slippage,args.funding,args.warmup)
            original=bt.historical_backtest(df,args.fee,args.slippage,args.funding,warmup_days=args.warmup)
            observed.update({"instrument":iid,"candles":len(df),"baseline_complete_trades":len(original)})
            report["results"].append(observed)
            print(f"DIAG {iid}: A_raw={observed['strategy_A']['all_pass']} B_raw={observed['strategy_B']['all_pass']} "
                  f"A_plans={observed['after_A_raw_signal_trade_plan']['all_pass']} "
                  f"B_plans={observed['after_B_raw_signal_trade_plan']['all_pass']} "
                  f"executed={len(original)}",flush=True)
        except Exception as e:
            report["failures"][iid]=str(e)
            print(f"DIAG ERROR {iid} {e}",file=sys.stderr,flush=True)
    aggregate={}
    for group in ("strategy_A","strategy_B","after_A_raw_signal_trade_plan", "after_B_raw_signal_trade_plan",
                  "after_A_plan_entry_limit","after_B_plan_entry_limit"):
        groups=[r[group] for r in report["results"]]
        names=groups[0]["individual_pass"].keys() if groups else []
        aggregate[group]={"evaluated":sum(a["evaluated"] for a in groups),
                          "all_pass":sum(a["all_pass"] for a in groups),
                          "independent_pass":{k:sum(a["individual_pass"][k] for a in groups) for k in names},
                          "sequential_pass":{k:sum(a["sequential_pass"][k] for a in groups) for k in names}}
    report["summary"]={"symbols_ok":len(report["results"]),"symbols_failed":len(report["failures"]),
                       "total_30m_candles":sum(a["candles"] for a in report["results"]),
                       "baseline_complete_trades":sum(a["baseline_complete_trades"] for a in report["results"]),
                       "sections":aggregate}
    out=Path(args.output);out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    flatten_csv(report["results"],out.with_suffix(".csv"))
    print("SUMMARY "+json.dumps(report["summary"],ensure_ascii=False),flush=True)
    if report["failures"]:return 2
    if len(report["results"])!=len(args.symbols):return 2
    return 0

if __name__=="__main__":sys.exit(main())
