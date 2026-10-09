import unittest
import numpy as np
import pandas as pd
from diagnostic_backtest import GateCounts,a_flags,b_flags,plan_flags,entry_flags,diagnosis,A_KEYS,B_KEYS,PLAN_KEYS,FILL_KEYS
import historical_backtest as bt

class DiagnosticRegressionTests(unittest.TestCase):
    @staticmethod
    def fake_30m(days=90):
        n=days*48
        # Midnight-aligned at UTC 16:00, every bar has positive volume and consistent OHLC.
        ts=np.arange(n,dtype=np.int64)*1800000 + 57600000
        rng=np.random.default_rng(1002)
        steps=rng.normal(.0001,.002,n)
        close=100*np.exp(np.cumsum(steps))
        op=np.concatenate(([100.],close[:-1]))
        hi=np.maximum(close,op)*1.001
        low=np.minimum(close,op)*.999
        volume=30+rng.integers(0,20,n)
        return pd.DataFrame({'ts':ts,'open':op,'high':hi,'low':low,'close':close,'volume':volume})

    def test_independent_and_cumulative_counts(self):
        c=GateCounts(('a','b','c'))
        c.record({'a':True,'b':False,'c':True})
        c.record({'a':True,'b':True,'c':True})
        x=c.asdict()
        self.assertEqual(x['individual_pass'],{'a':2,'b':1,'c':2})
        self.assertEqual(x['sequential_pass'],{'a':2,'b':1,'c':1})
        self.assertEqual(x['all_pass'],1)
        self.assertEqual(x['only_one_failed_gate']['b'],1)

    def test_a_gates_equivalent_to_original_signal(self):
        df=self.fake_30m()
        d={tf:bt.indicators(bt.resample(df,minutes)) for tf,minutes in bt.TFMINS.items()}
        f=a_flags(d)
        self.assertEqual(list(f),list(A_KEYS))
        self.assertEqual(all(f.values()),bool(bt.signal_a(d)))

    def test_b_gates_equivalent_to_original_signal(self):
        df=self.fake_30m()
        d={tf:bt.indicators(bt.resample(df,minutes)) for tf,minutes in bt.TFMINS.items()}
        f,extra,count=b_flags(d)
        self.assertEqual(list(f),list(B_KEYS))
        self.assertEqual(count,sum(v for k,v in extra.items() if k.startswith('div_')))
        self.assertEqual(all(f.values()),bool(bt.signal_b(d)))

    def test_trade_plan_equivalent_to_original(self):
        df=self.fake_30m()
        d={tf:bt.indicators(bt.resample(df,minutes)) for tf,minutes in bt.TFMINS.items()}
        px=float(d['30m'].close.iloc[-1])*(1+.0005)
        f,plan=plan_flags(d,px,.0005,.0005,.0001)
        self.assertEqual(list(f),list(PLAN_KEYS))
        self.assertEqual(plan is not None,bt.plan(d,px,.0005,.0005,.0001) is not None)

    def test_entry_gates_no_future_in_raw_signal(self):
        df=pd.DataFrame({'open':[100,90],'ts':[0,1800000]})
        c=entry_flags(df,0,100,95,112,.0005,.0005,.0001,10)
        self.assertEqual(list(c),list(FILL_KEYS))
        self.assertFalse(c[FILL_KEYS[0]])
        self.assertFalse(c[FILL_KEYS[1]])

    def test_warmup_reduces_counted_window(self):
        df=self.fake_30m(days=90)
        result=diagnosis(df,.0005,.0005,.0001,75)
        self.assertGreater(result['evaluated_decisions'],0)
        self.assertLess(result['evaluated_decisions'],len(df))
        self.assertEqual(result['strategy_A']['evaluated'],result['strategy_B']['evaluated'])
        self.assertEqual(result['strategy_A']['all_pass'],
                         result['strategy_A']['failed_gate_count_histogram'].get('0',0))

    def test_bad_gap_in_history_rejected(self):
        df=self.fake_30m(days=90).drop(index=2)
        with self.assertRaises(ValueError):diagnosis(df,.0005,.0005,.0001,75)

if __name__=='__main__':unittest.main()
