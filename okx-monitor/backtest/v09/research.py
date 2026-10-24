"""Chronological paper portfolios; exploratory fills never become actual records."""
import math
from collections import Counter
from dataclasses import asdict
import numpy as np
from .config import CONFIG, DAY, STRATEGIES
from .data import iso
from .strategies import signal
from .exits import structure_plan, costs, bar_exit, quantize_plan
from .risk import State, size, cross_stress
from .events import assess

def wilson(wins, n):
    if n <= 0:
        return [0., 1.]
    z = 1.959963984540054
    p = wins/n
    center = (p+z*z/(2*n))/(1+z*z/n)
    half = z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/(1+z*z/n)
    return [max(0., center-half), min(1., center+half)]

def evidence(trades, forward_verified=False, pristine=False, cfg=CONFIG):
    n = len(trades)
    wins = sum(t['net_twd'] > 0 for t in trades)
    interval = wilson(wins, n)
    qualified = n >= cfg.min_oos and interval[0] >= cfg.min_win_lower and forward_verified and pristine
    return {'n': n, 'wins': wins, 'win_rate': wins/n if n else None, 'wilson_95': interval,
            'forward_verified': forward_verified, 'pristine_holdout': pristine,
            'trusted_60_percent': qualified, 'status': 'VERIFIED' if qualified else 'NOT VERIFIED'}

def qualification(plan_cost, ev, cross_verified=False, event_review=False, risk_state='ALLOW_RESEARCH'):
    reasons = []
    if not math.isfinite(plan_cost['net_rr']) or plan_cost['net_rr'] < 3:
        reasons.append('NET_RR_BELOW_3')
    if not ev['trusted_60_percent']:
        reasons.append('WIN_RATE_NOT_VERIFIED')
    if not cross_verified:
        reasons.append('CROSS_MARGIN_NOT_VERIFIED')
    if event_review:
        reasons.append('MAJOR_EVENT_REVIEW')
    if risk_state != 'ALLOW_RESEARCH':
        reasons.append(risk_state)
    return {'conclusion': 'NO QUALIFIED SIGNAL' if reasons else 'QUALIFIED FOR USER REVIEW', 'reasons': reasons}

def splits(start, end):
    # 60 days warmup. 14d embargo between all measured partitions.
    available = end-(start+60*DAY)
    a = start+60*DAY
    b = a+int(available*.6)//DAY*DAY
    c = a+int(available*.8)//DAY*DAY
    if min(b-a, c-b, end-c) <= 30*DAY:
        raise ValueError('insufficient split history')
    return {'train': (a, b), 'validation': (b+14*DAY, c), 'holdout': (c+14*DAY, end),
            'walk_1': (b+14*DAY, c), 'walk_2': (c+14*DAY, end)}

def simulate(markets, strategy, span, step, mult=1., leverage=5, execution='base', cfg=CONFIG, events=None, strategy_names=None, plan_factory=None, candidate_schedule=None, hold_days_by_strategy=None):
    # 'ALL' is a shared portfolio; other scenarios isolate each baseline.
    chosen = (strategy_names or STRATEGIES) if strategy == 'ALL' else (strategy,)
    symbols = sorted(markets)
    if not symbols:
        raise ValueError('no covered assets')
    ts = markets[symbols[0]]['frame'].ts.to_numpy()
    if any(not np.array_equal(ts, markets[s]['frame'].ts.to_numpy()) for s in symbols):
        raise ValueError('misaligned markets')
    start, end = span
    a, b = np.searchsorted(ts, [start, end])
    state = State()  # paper-only state, reset per independent scenario
    wallet = cfg.initial
    peak = wallet
    maxdd = 0.
    positions = {}
    trades, curve = [], []
    counts = Counter()
    first_halt = None
    expense_budget_used = 0.
    maximum = {'open': 0, 'risk': 0., 'joint_gap_loss': 0.}
    fee, slip = cfg.fee*mult, cfg.slippage*mult

    def close_piece(key, fraction, px, t, reason):
        nonlocal wallet, expense_budget_used
        p = positions[key]
        qty = p['units']*fraction
        proceeds = p['plan'].side*(px-p['plan'].entry)*qty*32
        exitfee = qty*px*fee*32
        wallet += proceeds-exitfee
        expense_budget_used += exitfee
        p['net'] += proceeds-exitfee
        p['remaining'] -= fraction
        p['fills'].append(dict(fraction=fraction, price=px, time=iso(t), reason=reason))
        if p['remaining'] < 1e-9:
            trade = {'strategy': p['strategy'], 'symbol': p['symbol'], 'side': p['plan'].side,
                     'entry_time': iso(p['entry_ts']), 'exit_time': iso(t), 'net_twd': p['net'],
                     'net_r': p['net']/p['risk_twd'], 'hold_days': (t-p['entry_ts'])/DAY,
                     'risk_twd': p['risk_twd'], 'notional_twd': p['notional_twd'], 'units': p['units'],
                     'simultaneous_risk_twd':p['simultaneous_risk_twd'], 'paper_leverage':p['paper_leverage'], 'entry': p['plan'].entry, 'stop': p['plan'].stop,
                     'tp1': p['plan'].tp1, 'tp2': p['plan'].tp2, 'weights': p['plan'].weights,
                     'planned_net_rr': p['rr'], 'fills': p['fills'], 'funding_twd': p['funding'],
                     'cost_twd': p['fees']+sum(f['price']*p['units']*f['fraction']*fee*32 for f in p['fills'])+p['funding'],
                     'regime': p['regime'], 'year': iso(p['entry_ts'])[:4], 'book': 'paper_backtest'}
            trades.append(trade)
            state.closes.append({'time': iso(t), 'strategy': p['strategy'], 'net': p['net']})
            del positions[key]

    for j in range(int(a), int(b)):
        t = int(ts[j])
        # Capacity cannot be reused from intrabar exits. Entries at next open
        # use only preceding close and account state available at that open.
        state.reserve = max(0., cfg.reserve-expense_budget_used)
        state.capital = wallet+sum(p['remaining']*p['units']*p['plan'].side*(markets[p['symbol']]['records'][j].open-p['plan'].entry)*32 for p in positions.values())
        state.observe()
        state.positions = [dict(strategy=p['strategy'], risk_twd=p['risk_twd'], notional_twd=p['notional_twd']) for p in positions.values()]
        candidates = []
        if candidate_schedule is not None:
            for strat in chosen:
                for item in candidate_schedule[strat].get(j, ()):
                    if not any(p['symbol']==item[2] for p in positions.values()):
                        candidates.append(item)
                        counts['raw_candidate_signals'] += 1
        else:
            if j > 0:
                for strat in chosen:
                    for sym in symbols:
                        key = (strat, sym)
                        if key in positions or any(p['symbol'] == sym for p in positions.values()):
                            continue
                        x = markets[sym]['frame']
                        side, strength = markets[sym]['signals'][strat][j-1]
                        if side:
                            # Deterministic signal strength, liquidity, symbol tie break.
                            counts['raw_candidate_signals'] += 1
                            candidates.append((-strength, -float(markets[sym]['records'][j-1].liquidity), sym, strat, side))
        for _, negliq, sym, strat, side in sorted(candidates):
            if any(p['symbol'] == sym for p in positions.values()):
                continue
            prior = markets[sym]['records'][j-1]
            if assess(events or [], iso(t), sym,technical={'atr_fraction':prior.atr/prior.close,'trailing_daily_quote_volume':prior.liquidity})['requires_review']:
                counts['MAJOR_EVENT_REVIEW'] += 1
                continue
            prior = markets[sym]['records'][j-1]
            if -negliq < cfg.min_quote_volume:
                counts['liquidity_rejected'] += 1
                continue
            opening = float(markets[sym]['records'][j].open)
            plan = (plan_factory(strat, prior, opening*(1+side*slip), side) if plan_factory else structure_plan(prior, opening*(1+side*slip), side))
            if plan is None:
                counts['structure_rejected'] += 1
                continue
            try:
                plan = quantize_plan(plan, markets[sym]['instrument']['tickSz'])
            except ValueError:
                counts['tick_rejected'] += 1
                continue
            c = costs(plan, mult, cfg)
            if not math.isfinite(c['net_rr']) or c['net_rr'] < 3:
                counts['net_rr_rejected'] += 1
                continue
            counts['structural_net_rr_pass'] += 1
            budget = min(1000., 2000-sum(p['risk_twd'] for p in positions.values()))
            if budget <= 0:
                counts['total_risk_rejected'] += 1
                continue
            allocated = size(plan, c['risk_per_unit'], 32., '2022-01-01T00:00:00Z',
                             'RESEARCH_ASSUMPTION_FIXED_32_NOT_OBSERVED_FX', markets[sym]['instrument'], budget, leverage)
            if not allocated:
                counts['minimum_size_rejected'] += 1
                continue
            verdict = state.gate(strat, allocated['risk_twd'], iso(t))
            if verdict != 'ALLOW_RESEARCH':
                counts[verdict] += 1
                continue
            # Combined margin and trailing daily volume participation cap.
            gross = sum(p['notional_twd'] for p in positions.values())
            if gross+allocated['notional_twd'] > max(0, state.capital)*cfg.margin_fraction*leverage:
                counts['margin_rejected'] += 1
                continue
            if allocated['notional_twd']/32 > -negliq*cfg.participation:
                counts['participation_rejected'] += 1
                continue
            entryfee = allocated['units']*plan.entry*fee*32
            wallet -= entryfee
            expense_budget_used += entryfee
            state.reserve = max(0.,cfg.reserve-expense_budget_used)
            state.capital = max(0.,state.capital-entryfee)
            key = (strat, sym)
            positions[key] = dict(**allocated, plan=plan, strategy=strat, symbol=sym, entry_ts=t,
                                  remaining=1., net=-entryfee, fees=entryfee, funding=0., fills=[],
                                  next_funding=(t//28800000+1)*28800000,
                                  stop=plan.stop, tp1_done=False, rr=c['net_rr'],
                                  simultaneous_risk_twd=sum(p['risk_twd'] for p in positions.values())+allocated['risk_twd'],
                                  regime=getattr(prior,'phase3_regime','trend_up' if prior.close > prior.ema50 else 'trend_down'))
            state.positions.append(dict(strategy=strat, risk_twd=allocated['risk_twd'], notional_twd=allocated['notional_twd']))
            counts['entries'] += 1
        maximum['open'] = max(maximum['open'], len(positions))
        maximum['risk'] = max(maximum['risk'], sum(p['risk_twd'] for p in positions.values()))
        maximum['joint_gap_loss'] = max(maximum['joint_gap_loss'], cross_stress(list(positions.values()), max(0,state.capital))['joint_gap_loss_twd'])
        for key in list(positions):
            p = positions[key]
            q = markets[p['symbol']]['records'][j]
            held = t-p['entry_ts']
            # Time exit known at open, before this bar's funding/high/low.
            time_days=min(cfg.max_days,(hold_days_by_strategy or {}).get(p['strategy'],cfg.time_exit_days))
            if held >= time_days*DAY:
                close_piece(key, p['remaining'], q.open*(1-p['plan'].side*slip), t, f'time_exit_{time_days}d')
                continue
            # Invalidation uses previous completed close; execution is this open.
            if held >= DAY and ((prior_close := markets[p['symbol']]['records'][j-1].close)-p['plan'].stop)*p['plan'].side < 0:
                close_piece(key, p['remaining'], q.open*(1-p['plan'].side*slip), t, 'signal_invalidation')
                continue
            while p['next_funding'] <= t+step:
                charge = p['notional_twd']*p['remaining']*cfg.funding_per_8h*mult
                wallet -= charge
                expense_budget_used += charge
                p['net'] -= charge
                p['funding'] += charge
                p['next_funding'] += 28800000
            exits = bar_exit(p['plan'], q.open, q.high, q.low, p['stop'], p['remaining'], p['tp1_done'], slip)
            # Alternative scenario: no limit TP fills; adverse stop still executes.
            if execution == 'missed_limits':
                exits = [e for e in exits if e[2] == 'stop']
            for fraction, px, reason in exits:
                close_piece(key, fraction, px, t+step, reason)
                if key not in positions:
                    break
                if reason == 'tp1':
                    p['tp1_done'] = True
            if key in positions and p['tp1_done']:
                # Completed close ATR trailing, applies NEXT bar only. Never loosen.
                trail = q.close-p['plan'].side*2*q.atr
                p['stop'] = max(p['stop'], p['plan'].entry, trail) if p['plan'].side == 1 else min(p['stop'], p['plan'].entry, trail)
        if j == b-1:
            for key in list(positions):
                p = positions[key]
                close_piece(key, p['remaining'], markets[p['symbol']]['records'][j].close*(1-p['plan'].side*slip), t+step, 'split_end')
        equity = wallet+sum(p['remaining']*p['units']*p['plan'].side*(markets[p['symbol']]['records'][j].close-p['plan'].entry)*32 for p in positions.values())
        peak = max(peak, equity)
        maxdd = max(maxdd, 1-equity/peak)
        state.reserve = max(0., cfg.reserve-expense_budget_used)
        state.capital = max(0, equity)
        state.observe()
        if state.halted and first_halt is None:
            first_halt = dict(time=iso(t+step),reasons=list(state.halt_reasons),capital_twd=state.capital,reserve_twd=state.reserve)
        curve.append(dict(time=iso(t+step), equity_twd=equity, positions=len(positions)))
    return {'strategy': strategy, 'cost_mult': mult, 'leverage': leverage, 'execution': execution,
            **stats(trades), 'return_pct': 100*(wallet/cfg.initial-1), 'max_drawdown_pct': 100*maxdd,
            'max_open': maximum['open'], 'max_planned_risk_twd': maximum['risk'],
            'max_joint_20pct_gap_loss_twd': maximum['joint_gap_loss'],
            'halted': state.halted, 'first_halt':first_halt, 'counts': dict(counts)}, trades, curve

def stats(trades):
    net = np.array([t['net_twd'] for t in trades])
    gains = net[net > 0].sum()
    losses = -net[net < 0].sum()
    return {'trades': len(trades), 'win_rate': float(np.mean(net > 0)) if len(net) else None,
            'net_twd': float(net.sum()), 'profit_factor': float(gains/losses) if losses else None,
            'realized_net_payoff': float(net[net>0].mean()/(-net[net<0].mean())) if len(net[net>0]) and len(net[net<0]) else (0. if losses else None),
            'avg_hold_days': float(np.mean([t['hold_days'] for t in trades])) if trades else None,
            'avg_planned_net_rr': float(np.mean([t['planned_net_rr'] for t in trades])) if trades else None,
            'avg_net_r': float(np.mean([t['net_r'] for t in trades])) if trades else None,
            'cost_twd': sum(t['cost_twd'] for t in trades)}
