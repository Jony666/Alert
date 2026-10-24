# v0.9 模擬交易報告（範例，非當下訊號）

```json
{
  "example_only": true,
  "strategy": "MACD_TREND",
  "strategy_selected_symbol": "BTC-USDT-SWAP",
  "direction": "LONG",
  "entry": 100,
  "stop": 98,
  "tp1": {
    "price": 120,
    "fraction": 0.5,
    "reason": "prior 55-bar structure"
  },
  "tp2": {
    "price": 140,
    "fraction": 0.5,
    "reason": "1.618 range extension"
  },
  "actual_exit": null,
  "exit_conditions": "stop first; 7-day time exit; maximum 14 days; invalidation at next open",
  "trailing": "after TP1, next bar: entry protection then 2 ATR from completed close; never loosen",
  "logic": "Frozen strategy before ranking by signal strength and trailing liquidity; no symbol tuning.",
  "why_now": "EXAMPLE historical completed-bar condition; NOT a current market signal",
  "evidence": {
    "n": 0,
    "wins": 0,
    "win_rate": null,
    "wilson_95": [
      0.0,
      1.0
    ],
    "forward_verified": false,
    "pristine_holdout": false,
    "trusted_60_percent": false,
    "status": "NOT VERIFIED"
  },
  "costs": {
    "risk_per_unit": 3.407975499999992,
    "reward_per_unit": 28.560032500000005,
    "net_rr": 8.38035147259717,
    "tp1_then_protection_net": 8.580000000000005,
    "funding_bound_per_unit": 1.26
  },
  "ideal_hold_days": [
    1,
    3
  ],
  "general_hold_days": [
    1,
    7
  ],
  "max_hold_days": 14,
  "allocation": {
    "contracts": 641.87,
    "units": 6.4187,
    "risk_twd": 699.9927149391984,
    "notional_twd": 20539.84,
    "paper_leverage": 5,
    "fx_twd_per_usdt": 32,
    "fx_time": "2022-01-01T00:00:00Z",
    "fx_source": "EXAMPLE_ASSUMED_FX",
    "cross_verified": false
  },
  "cross_margin_assessment": {
    "status": "NOT VERIFIED",
    "allow_recommendation": false,
    "reasons": [
      "ACCOUNT_SNAPSHOT_MISSING"
    ]
  },
  "simultaneous_risk_twd": 699.9927149391984,
  "other_positions_allowed": true,
  "events": {
    "events": [],
    "win_rate_adjustment": null,
    "technical_context": null,
    "technical_interaction": "No verified major event in supplied coverage; absence is not proof of no risk",
    "coverage": "USER_SUPPLIED_VERIFIED_EVENTS_ONLY; NO COMPLETE HISTORICAL NEWS FEED",
    "requires_review": false
  },
  "risk_state": "ALLOW_RESEARCH",
  "conclusion": "NO QUALIFIED SIGNAL",
  "reasons": [
    "WIN_RATE_NOT_VERIFIED",
    "CROSS_MARGIN_NOT_VERIFIED"
  ],
  "decision": "最終是否下單由使用者決定；此報告不是訂單。",
  "uncertainty": "勝率、全倉強平及實際成交均未驗證；跳空損失可能超過預算。"
}
```
