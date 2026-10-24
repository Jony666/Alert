"""Independent account controls. Persist State JSON between manual sessions."""
from dataclasses import dataclass, field, asdict
from datetime import datetime
from zoneinfo import ZoneInfo
import math
import json
from pathlib import Path
from .config import CONFIG
from .events import aware

def day(ts):
    d = datetime.fromisoformat(ts.replace('Z', '+00:00'))
    if d.tzinfo is None:
        raise ValueError('timezone required')
    return d.astimezone(ZoneInfo('Asia/Taipei')).date().isoformat()

def single_cap(verified_capital=None):
    if verified_capital is None:
        return 1000
    if not math.isfinite(verified_capital) or verified_capital < 0:
        raise ValueError('invalid capital')
    return min(2000, 1000+375*math.floor(max(0, verified_capital-10000)/5000))

@dataclass
class State:
    capital: float = 10000
    verified_capital: float | None = None
    reserve: float = 1000
    peak: float = 10000
    halted: bool = False
    halt_reasons: list = field(default_factory=list)
    positions: list = field(default_factory=list)
    closes: list = field(default_factory=list)
    reconciliation_pending: bool = False

    def observe(self):
        if not all(math.isfinite(v) and v >= 0 for v in (self.capital, self.reserve, self.peak)):
            raise ValueError('invalid balances')
        self.peak = max(self.peak, self.capital)
        reasons = []
        if self.capital <= 8000 or self.capital <= .8*self.peak:
            reasons.append('20_PERCENT_DRAWDOWN_REVIEW_REQUIRED')
        if self.reserve <= 500:
            reasons.append('FEE_RESERVE_RECONCILIATION_REQUIRED')
        if reasons:
            self.halted = True
            self.halt_reasons = sorted(set(self.halt_reasons+reasons))

    def save(self, path):
        self.observe()
        Path(path).write_text(json.dumps(asdict(self), allow_nan=False, indent=2)+'\n')

    @classmethod
    def load(cls, path):
        s = cls(**json.loads(Path(path).read_text()))
        s.observe()
        return s

    def gate(self, strategy, risk, when, exceptional_research=False):
        self.observe()
        if not math.isfinite(risk) or risk <= 0:
            return 'INVALID_RISK'
        if self.halted:
            return 'PERSISTENT_HALT'
        if self.reconciliation_pending:
            return 'RECONCILIATION_REQUIRED'
        if risk > single_cap(self.verified_capital):
            return 'SINGLE_RISK_CAP'
        if len(self.positions) >= 3:
            return 'ACCOUNT_POSITION_CAP'
        if sum(p['strategy'] == strategy for p in self.positions) >= 2:
            return 'STRATEGY_POSITION_CAP'
        if sum(p['risk_twd'] for p in self.positions)+risk > 2000+1e-8:
            return 'TOTAL_RISK_CAP'
        today = sorted([c for c in self.closes if day(c['time']) == day(when) and aware(c['time']) <= aware(when)], key=lambda c: aware(c['time']))
        if len(today) >= 3 and all(c['net'] < 0 for c in today[-3:]):
            return 'DAILY_STOP_NO_FOURTH'
        if len(today) >= 2 and all(c['net'] < 0 for c in today[-2:]):
            previous = {c['strategy'] for c in today[-2:]}
            if strategy not in previous and exceptional_research:
                return 'EXCEPTION_RESEARCH_ONLY_REQUIRES_SEPARATE_USER_DECISION'
            return 'DAILY_TWO_LOSSES'
        return 'ALLOW_RESEARCH'

def cross_stress(positions, capital, shock=.20):
    """Joint adverse mark gap; never an OKX liquidation price/model."""
    loss = sum(p['notional_twd']*(shock+.002) for p in positions)
    return {'joint_gap_loss_twd': loss, 'project_equity_after_gap': capital-loss,
            'official_cross_liquidation_verified': False,
            'status': 'NOT VERIFIED',
            'limitations': 'Shared entire account equity, tiers, mark prices, other assets, ADL unknown.'}

def size(plan, quote_risk, fx, fx_time, fx_source, inst, available_risk=1000, leverage=5, cfg=CONFIG):
    if not math.isfinite(fx) or fx <= 0 or not fx_source or not fx_time:
        raise ValueError('timestamped sourced TWD/USDT FX required')
    day(fx_time)
    if leverage not in cfg.leverages or leverage > float(inst.get('lever', cfg.leverages[-1])):
        raise ValueError('high leverage rejected: cross margin NOT VERIFIED')
    # Paper allocation only; never a real-account liquidation safety approval.
    if not math.isfinite(quote_risk) or quote_risk <= 0 or not math.isfinite(available_risk) or available_risk <= 0:
        raise ValueError('invalid risk budget')
    ct = float(inst['ctVal'])
    lot = float(inst['lotSz'])
    minimum = float(inst['minSz'])
    if inst.get('ctValCcy') != inst['instId'].split('-')[0] or min(ct, lot, minimum) <= 0:
        raise ValueError('unsupported contract units')
    units = min(available_risk/(quote_risk*fx), cfg.initial*cfg.margin_fraction*leverage/(plan.entry*fx))
    contracts = math.floor(units/ct/lot+1e-12)*lot
    if contracts < minimum:
        return None
    units = contracts*ct
    return {'contracts': contracts, 'units': units, 'risk_twd': units*quote_risk*fx,
            'notional_twd': units*plan.entry*fx, 'paper_leverage': leverage,
            'fx_twd_per_usdt': fx, 'fx_time': fx_time, 'fx_source': fx_source,
            'cross_verified': False}
