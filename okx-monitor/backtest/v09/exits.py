from dataclasses import dataclass
import math
from .config import CONFIG

@dataclass(frozen=True)
class Plan:
    entry: float
    stop: float
    tp1: float
    tp2: float
    side: int
    weights: tuple = (.5, .5)

    def validate(self):
        if self.side not in (-1, 1) or not all(math.isfinite(v) and v > 0 for v in (self.entry, self.stop, self.tp1, self.tp2)):
            raise ValueError('invalid plan prices')
        if len(self.weights) != 2 or min(self.weights) <= 0 or not math.isclose(sum(self.weights), 1):
            raise ValueError('invalid exit weights')
        if not (self.side*(self.entry-self.stop) > 0 and self.side*(self.tp1-self.entry) > 0 and self.side*(self.tp2-self.tp1) > 0):
            raise ValueError('invalid directional levels')
        return self

def structure_plan(prior, entry, side):
    # Prior 20-bar structure + ATR stop; 55-bar range/Fibonacci extension targets.
    stop = min(prior.lo20, entry-1.5*prior.atr) if side == 1 else max(prior.hi20, entry+1.5*prior.atr)
    width = prior.hi55-prior.lo55
    tp1 = prior.hi55 if side == 1 else prior.lo55
    tp2 = tp1+side*1.618*width
    try:
        return Plan(entry, stop, tp1, tp2, side).validate()
    except ValueError:
        return None

def costs(plan, mult=1., cfg=CONFIG):
    """Per base unit; adverse slippage on each exit, both fees, 14d funding."""
    plan.validate()
    fee, slip = cfg.fee*mult, cfg.slippage*mult
    entry_cost = plan.entry*fee
    funding = plan.entry*cfg.funding_per_8h*3*cfg.max_days*mult
    stop_fill = plan.stop*(1-plan.side*slip)
    risk = plan.side*(plan.entry-stop_fill)+entry_cost+stop_fill*fee+funding
    reward = sum(w*(plan.side*(tp*(1-plan.side*slip)-plan.entry)-tp*(1-plan.side*slip)*fee)
                 for w, tp in zip(plan.weights, (plan.tp1, plan.tp2)))-entry_cost-funding
    return {'risk_per_unit': risk, 'reward_per_unit': reward, 'net_rr': reward/risk,
            'tp1_then_protection_net': plan.weights[0]*(plan.side*(plan.tp1*(1-plan.side*slip)-plan.entry)-plan.tp1*fee)
                -entry_cost-funding-plan.weights[1]*plan.entry*(slip+fee),
            'funding_bound_per_unit': funding}

def bar_exit(plan, opening, high, low, current_stop, remaining, tp1_done, slip):
    """Worst touched stop first. TP fills at limit (no favorable gap improvement).
    Stop moves only from NEXT bar. Orders assumed fully filled; capacity stress
    and missed-limit scenario are separate research cases, not guaranteed fills.
    """
    side = plan.side
    stopped = low <= current_stop if side == 1 else high >= current_stop
    if stopped:
        px = min(opening, current_stop) if side == 1 else max(opening, current_stop)
        return [(remaining, px*(1-side*slip), 'stop')]
    result = []
    if not tp1_done and (high >= plan.tp1 if side == 1 else low <= plan.tp1):
        result.append((plan.weights[0], plan.tp1*(1-side*slip), 'tp1'))
    if high >= plan.tp2 if side == 1 else low <= plan.tp2:
        rest = remaining-sum(v[0] for v in result)
        if rest > 0:
            result.append((rest, plan.tp2*(1-side*slip), 'tp2'))
    return result

def quantize_plan(plan, tick):
    """Conservative exchange tick rounding: worsen entry/stop, shorten rewards."""
    from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
    tick = Decimal(str(tick))
    if not tick.is_finite() or tick <= 0:
        raise ValueError('invalid tick size')
    def rounded(v, up):
        return float((Decimal(str(v))/tick).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR)*tick)
    long = plan.side == 1
    return Plan(rounded(plan.entry,long),rounded(plan.stop,not long),rounded(plan.tp1,not long),
                rounded(plan.tp2,not long),plan.side,plan.weights).validate()
