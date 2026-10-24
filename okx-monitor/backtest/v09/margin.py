"""Auditable linear cross-margin stress, NOT OKX's official liquidation engine.
No assumed account cash or invented maintenance tiers. All live suggestions fail closed.
"""
import math
from .events import aware


def cross_account(snapshot, positions, tiers, at, max_age_seconds=300):
    missing = []
    required = ('cash_usdt', 'liabilities_usdt', 'other_collateral_haircut_usdt',
                'reserved_fees_usdt', 'funding_due_usdt', 'time', 'reconciled')
    if not snapshot or any(k not in snapshot for k in required):
        return {'status':'NOT VERIFIED', 'allow_recommendation':False, 'reasons':['ACCOUNT_SNAPSHOT_MISSING']}
    age = (aware(at)-aware(snapshot['time'])).total_seconds()
    if age < 0 or age > max_age_seconds or not snapshot['reconciled']:
        missing.append('ACCOUNT_STALE_OR_UNRECONCILED')
    for k in required[:5]:
        if not math.isfinite(snapshot[k]) or snapshot[k] < 0:
            missing.append('INVALID_ACCOUNT_VALUES')
    prepared = []
    for p in positions:
        try:
            if p['side'] not in (-1,1) or not all(math.isfinite(p[k]) and p[k]>0 for k in ('units','entry','mark','leverage')):
                raise ValueError('invalid position')
            age = (aware(at)-aware(p['mark_time'])).total_seconds()
            spec = tiers[p['symbol']]
            if age < 0 or age > max_age_seconds or aware(spec['effective_from']) > aware(at) or aware(spec['valid_until']) < aware(at):
                raise ValueError('stale/future mark or tier')
            # Explicit piecewise whole-position rates and deductions, never guessed.
            tier = next(t for t in spec['tiers'] if p['units'] <= t['max_units'])
            if not (0 < tier['mmr'] <= tier['imr'] <= 1) or tier['deduction_usdt'] < 0 or p['leverage'] > 1/tier['imr']:
                raise ValueError('invalid tier/leverage')
            prepared.append((p,tier))
        except (KeyError, ValueError, StopIteration, TypeError):
            missing.append('POSITION_MARK_TIER_OR_LEVERAGE_UNVERIFIED')
    if missing:
        return {'status':'NOT VERIFIED','allow_recommendation':False,'reasons':sorted(set(missing))}
    def evaluate(shock):
        # Every position moves against its side: correlated adversarial gap.
        upnl = initial = maintenance = close_fee = 0.
        for p,t in prepared:
            mark = p['mark']*(1-p['side']*shock)
            notional = p['units']*mark
            upnl += p['side']*(mark-p['entry'])*p['units']
            initial += notional/p['leverage']
            maintenance += max(0.,notional*t['mmr']-t['deduction_usdt'])
            close_fee += notional*.001  # disclosed adverse liquidation fee budget, not official fee
        equity = snapshot['cash_usdt']+snapshot['other_collateral_haircut_usdt']+upnl-snapshot['liabilities_usdt']-snapshot['reserved_fees_usdt']-snapshot['funding_due_usdt']
        required = maintenance+close_fee
        return dict(adverse_gap=shock,equity_usdt=equity,initial_margin_usdt=initial,
                    maintenance_plus_fee_usdt=required,margin_ratio=equity/required if required else None,
                    maintenance_breach=equity<=required,free_initial_margin_usdt=equity-initial)
    grid = [evaluate(s) for s in (0.,.01,.03,.05,.10,.20)]
    lo,hi=0.,.95
    boundary=None
    if evaluate(hi)['maintenance_breach']:
        if evaluate(0)['maintenance_breach']:
            boundary=0.
        else:
            for _ in range(60):
                mid=(lo+hi)/2
                if evaluate(mid)['maintenance_breach']:hi=mid
                else:lo=mid
            boundary=hi
    return {'status':'MODEL_ONLY_NOT_OFFICIAL_OKX','allow_recommendation':False,
            'reasons':['OFFICIAL_CROSS_MODEL_AND_ACCOUNT_MODE_NOT_VALIDATED'],
            'stress':grid,'joint_adverse_margin_breach_fraction':boundary,
            'note':'Linear USDT-only supplied-tier model; no ADL, portfolio margin, account collateral offsets or official liquidation certification.'}
