"""Manual fill evidence only; pure local records, no exchange calls."""
from dataclasses import dataclass, field
import math
from .events import aware

BOOKS = ('actual_confirmed', 'estimated_pending_reconciliation', 'paper_backtest')

@dataclass
class Ledger:
    records: dict = field(default_factory=dict)

    def propose(self, key, symbol, strategy, side, book='actual_confirmed'):
        if key in self.records or book not in BOOKS or side not in (-1, 1):
            raise ValueError('invalid candidate')
        self.records[key] = dict(symbol=symbol, strategy=strategy, side=side, book=book,
                                 state='proposed', currency='USDT', fills=[], exits=[], realized=0., fees=0., funding=0., transfers=[])

    def response(self, key, placed, details=None):
        r = self.records[key]
        if r['state'] not in ('proposed', 'pending_reconciliation'):
            raise ValueError('response already recorded')
        if not placed:
            r['state'] = 'not_placed'
            return
        r['state'] = 'pending_reconciliation'
        required = {'time', 'price', 'units', 'fee', 'leverage', 'stop', 'tp1', 'tp2', 'fx', 'fx_time', 'fx_source'}
        if not details or not required <= details.keys():
            return
        aware(details['time']); aware(details['fx_time'])
        numeric = ('price', 'units', 'fee', 'leverage', 'stop', 'tp1', 'tp2', 'fx')
        if not all(math.isfinite(details[k]) for k in numeric) or min(details[k] for k in numeric if k != 'fee') <= 0 or details['fee'] < 0 or not details['fx_source']:
            raise ValueError('invalid manual fill')
        r['fills'].append(details.copy())
        r['fees'] += details['fee']
        r['remaining'] = details['units']
        r['state'] = 'open'

    def exit(self, key, time, price, units, fee, funding=0.):
        r = self.records[key]
        aware(time)
        if r['state'] not in ('open', 'partially_closed') or not all(math.isfinite(v) for v in (price, units, fee, funding)) or price <= 0 or units <= 0 or units > r['remaining']+1e-10 or fee < 0:
            raise ValueError('invalid close')
        fill = r['fills'][0]
        if aware(time) < aware(fill['time']):
            raise ValueError('exit before entry')
        allocated_entry_fee = fill['fee']*units/fill['units']
        net = r['side']*(price-fill['price'])*units-fee-funding-allocated_entry_fee
        r['exits'].append(dict(time=time, price=price, units=units, fee=fee, funding=funding, net=net))
        r['realized'] += net
        r['fees'] += fee
        r['funding'] += funding
        r['remaining'] = max(0, r['remaining']-units)
        r['state'] = 'closed' if r['remaining'] < 1e-10 else 'partially_closed'
        return net

    def cancel(self, key):
        r = self.records[key]
        if r['state'] not in ('proposed', 'pending_reconciliation', 'not_placed'):
            raise ValueError('cannot cancel confirmed open trade')
        r['state'] = 'cancelled'

    def transfer(self, key, amount, time, source):
        aware(time)
        if not math.isfinite(amount) or not source:
            raise ValueError('invalid transfer')
        self.records[key]['transfers'].append(dict(amount=amount, time=time, source=source))

    def performance(self, book):
        if book not in BOOKS:
            raise ValueError('invalid book')
        return sum(r['realized'] for r in self.records.values() if r['book'] == book)

    def save(self, path):
        import json
        from pathlib import Path
        Path(path).write_text(json.dumps(self.records, allow_nan=False, indent=2)+'\n')

    @classmethod
    def load(cls, path):
        import json
        from pathlib import Path
        return cls(json.loads(Path(path).read_text()))

    def estimated_balance(self, verified_capital, book, as_of, source):
        aware(as_of)
        if book not in BOOKS or not math.isfinite(verified_capital) or not source:
            raise ValueError('verified balance basis/source required')
        return dict(estimated_capital=verified_capital+self.performance(book), as_of=as_of,
                    source=source, currency='USDT', confidence='estimated_pending_reconciliation', book=book,
                    note='Input verified_capital must be USDT. Transfers tracked separately; never add this estimate to TWD capital or authorize compounding.')

    def add_entry_fill(self, key, details):
        # Multiple manual partial entries before any exit; aggregate cost basis.
        r = self.records[key]
        if r['state'] != 'open' or r['exits']:
            raise ValueError('additional entries only before first exit')
        trial = Ledger()
        trial.propose('check', r['symbol'], r['strategy'], r['side'], r['book'])
        trial.response('check', True, details)
        if trial.records['check']['state'] != 'open':
            raise ValueError('complete manual fill evidence required')
        old = r['fills'][0]
        if aware(details['time']) < aware(old['time']):
            raise ValueError('entry chronology')
        if any(details[k] != old[k] for k in ('fx','fx_source','fx_time','leverage','stop','tp1','tp2')):
            raise ValueError('reconcile changed FX or plan separately')
        if 'entry_evidence' not in r:
            r['entry_evidence'] = [old.copy()]
        r['entry_evidence'].append(details.copy())
        total = old['units']+details['units']
        old['price'] = (old['price']*old['units']+details['price']*details['units'])/total
        old['units'] = total
        old['fee'] += details['fee']
        r['fees'] += details['fee']
        r['remaining'] += details['units']
