import unittest
import pandas as pd
from historical_backtest import resample,assert_continuous,indicators,simulate_after,pivots_latest_confirmed

class IntegrityTest(unittest.TestCase):
    def setUp(self):
        self.base=pd.DataFrame([(i*1_800_000,100,105,95,100,10) for i in range(96)],columns=['ts','open','high','low','close','volume'])
    def test_daily_utc8_anchor(self):
        x=resample(self.base,1440)
        self.assertEqual(len(x),1)
        self.assertEqual(int(x.ts.iloc[0]),-28_800_000 if False else 57_600_000) # time 16:00 UTC
    def test_native_12hour_utc8_alignment(self):
        # HK 12H candle at 00:00 local opens at 16:00 UTC (ts=57600000).
        d=self.base.iloc[32:56]  # UTC 16:00 to UTC 04:00, exactly 24 30m bars
        y=resample(d,720)
        self.assertEqual(len(y),1)
        self.assertEqual(int(y.ts.iloc[0]),57_600_000)
    def test_pivot_unconfirmed_does_not_trigger(self):
        d=self.base.copy()
        d['low']=100
        d.loc[15,'low']=90
        d.loc[29,'low']=89
        d['dif']=-5.0
        d.loc[15,'dif']=-7.0
        d.loc[29,'dif']=-6.0
        self.assertIsNone(pivots_latest_confirmed(d.iloc[:32])) # not enough right bars for b=29
    def test_not_count_partial_candle(self):
        x=resample(self.base.iloc[:90],60)
        self.assertEqual(len(x),45)
    def test_no_gaps(self):
        b=self.base.drop(index=20)
        with self.assertRaises(ValueError):assert_continuous(b,30)
    def test_worst_case_stop_first(self):
        x=pd.DataFrame([(0,100,120,80,110,10)],columns=self.base.columns)
        y=simulate_after(x,0,90,105,115,.0005,.0005,.0001,20)
        self.assertEqual(y['exit_reason'],'stop')
        self.assertLess(y['return_pct'],0)
    def test_indicator_no_future(self):
        before=indicators(self.base.iloc[:80])
        later=self.base.copy();later.loc[95,'close']=110
        after=indicators(later)
        for v in ['dif','dea','hist','atr','rsi']:
            self.assertAlmostEqual(before[v].iloc[-1],after[v].iloc[79],places=9)

if __name__=='__main__':unittest.main()
