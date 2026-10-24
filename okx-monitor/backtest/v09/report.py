import json
from .exits import costs
from .research import qualification
from .margin import cross_account

def candidate(symbol, strategy, plan, evidence, allocation, event_state, risk_state, simultaneous_risk, cost_mult=1.):
    c = costs(plan,cost_mult)
    cross = allocation.get('cross_assessment') or cross_account(None,[],{},allocation.get('fx_time','2025-01-01T00:00:00Z'))
    q = qualification(c, evidence, cross.get('allow_recommendation',False), event_state['requires_review'], risk_state)
    return {'example_only': True, 'strategy': strategy, 'strategy_selected_symbol': symbol,
            'direction': 'LONG' if plan.side == 1 else 'SHORT', 'entry': plan.entry, 'stop': plan.stop,
            'tp1': {'price': plan.tp1, 'fraction': plan.weights[0], 'reason': 'prior 55-bar structure'},
            'tp2': {'price': plan.tp2, 'fraction': plan.weights[1], 'reason': '1.618 range extension'},
            'actual_exit': None, 'exit_conditions': 'stop first; 7-day time exit; maximum 14 days; invalidation at next open',
            'trailing': 'after TP1, next bar: entry protection then 2 ATR from completed close; never loosen',
            'logic': 'Frozen strategy before ranking by signal strength and trailing liquidity; no symbol tuning.',
            'why_now': 'EXAMPLE historical completed-bar condition; NOT a current market signal',
            'evidence': evidence, 'costs': c, 'ideal_hold_days': [1,3], 'general_hold_days': [1,7], 'max_hold_days':14,
            'allocation': allocation, 'cross_margin_assessment':cross, 'simultaneous_risk_twd': simultaneous_risk,
            'other_positions_allowed': risk_state == 'ALLOW_RESEARCH' and simultaneous_risk < 2000,
            'events': event_state, 'risk_state': risk_state, **q,
            'decision': '最終是否下單由使用者決定；此報告不是訂單。',
            'uncertainty': '勝率、全倉強平及實際成交均未驗證；跳空損失可能超過預算。'}

def markdown(report):
    return '# v0.9 模擬交易報告（範例，非當下訊號）\n\n```json\n'+json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)+'\n```\n'
