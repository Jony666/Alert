import unittest
from argparse import Namespace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import numpy as np
import pandas as pd
import trader_strategies_v04 as m


class IntegrityTests(unittest.TestCase):
    def candle_df(self,n=144,base=0):
        ts=np.arange(n,dtype=np.int64)*m.MS30+base
        close=100+.05*np.arange(n)
        op=np.r_[close[0],close[:-1]]
        return pd.DataFrame(dict(ts=ts,open=op,high=np.maximum(op,close)+1,
                                 low=np.minimum(op,close)-1,close=close,volume=10.))

    def test_gap_rejected(self):
        d=self.candle_df().drop(index=12)
        with self.assertRaises(ValueError):m.validate_30m(d)

    def test_daily_taipei_anchor(self):
        d=self.candle_df(96)
        out=m.aligned_bars(d,1440)
        self.assertEqual(len(out),1)
        self.assertEqual(int(out.ts.iloc[0]),16*3_600_000)

    def test_4h_from_complete_30m(self):
        d=self.candle_df(18)
        out=m.aligned_bars(d,240)
        self.assertEqual(len(out),2)
        self.assertEqual(float(out.volume.iloc[0]),80.)

    def test_no_partial_4h(self):
        self.assertEqual(len(m.aligned_bars(self.candle_df(7),240)),0)

    def test_rsi_no_future_leak(self):
        x=pd.Series([100.+np.sin(i*.7) for i in range(120)])
        early=m.rsi_wilder(x.iloc[:70]).iloc[-1]
        later=m.rsi_wilder(x).iloc[69]
        self.assertAlmostEqual(early,later,places=11)

    def test_adx_no_future_leak(self):
        x=self.candle_df(160)
        small=m.adx_wilder(x.iloc[:100]);full=m.adx_wilder(x)
        for k in ('adx','pdi','mdi','ema20','atr14'):
            self.assertAlmostEqual(float(small.iloc[-1][k]),float(full.iloc[99][k]),places=10)

    def test_adx_uses_direction(self):
        x=self.candle_df(150)
        a=m.adx_wilder(x)
        self.assertGreater(float(a.pdi.iloc[-1]),float(a.mdi.iloc[-1]))

    def test_connors_three_day_decline_and_above_sma(self):
        n=205
        d=pd.DataFrame({'ts':np.arange(n)*m.MSDAY,'close':np.full(n,102.),
                        'ma200':np.full(n,100.),'rsi2':np.full(n,80.)})
        d.loc[202:204,'rsi2']=[60.,40.,20.]
        self.assertTrue(m.connors_setup(d,204))
        d.loc[204,'close']=90
        self.assertFalse(m.connors_setup(d,204))

    def test_connors_strict_source_day1_threshold(self):
        n=205;d=pd.DataFrame({'ts':np.arange(n)*m.MSDAY,
            'close':np.full(n,102.),'ma200':np.full(n,100.),'rsi2':np.full(n,80.)})
        d.loc[202:204,'rsi2']=[65,40,20]
        self.assertFalse(m.connors_setup(d,204))

    def test_stop_before_target_on_same_entry_bar_long(self):
        f=self.candle_df(7);f.loc[1,['open','high','low','close']]=[101.,112.,85.,105.]
        args=Namespace(raschke_order_valid_bars=2,raschke_max_hold_4h=3,slippage=0)
        trade,reason=m.simulate_4h_fill(f,0,1,110.,90.,111.,args)
        self.assertIsNone(reason)
        self.assertEqual(trade[-1],'protective_stop')
        self.assertEqual(trade[1],90.)

    def test_stop_before_target_on_same_entry_bar_short(self):
        f=self.candle_df(7);f.loc[1,['open','high','low','close']]=[100.,120.,80.,105.]
        args=Namespace(raschke_order_valid_bars=2,raschke_max_hold_4h=3,slippage=0)
        trade,reason=m.simulate_4h_fill(f,0,-1,90.,110.,81.,args)
        self.assertIsNone(reason)
        self.assertEqual(trade[-1],'protective_stop')
        self.assertEqual(trade[1],110.)

    def test_target_fill_long(self):
        f=self.candle_df(7)
        f.loc[1,['open','high','low','close']]=[101.,115.,100.,111.]
        args=Namespace(raschke_order_valid_bars=2,raschke_max_hold_4h=3,slippage=0)
        trade,reason=m.simulate_4h_fill(f,0,1,110,90,114,args)
        self.assertIsNone(reason)
        self.assertEqual(trade[-1],'retest_impulse_extreme')
        self.assertEqual(trade[1],114.)

    def test_target_fill_short(self):
        f=self.candle_df(7)
        f.loc[1,['open','high','low','close']]=[100.,101.,84.,90.]
        args=Namespace(raschke_order_valid_bars=2,raschke_max_hold_4h=3,slippage=0)
        trade,reason=m.simulate_4h_fill(f,0,-1,90,110,85,args)
        self.assertIsNone(reason)
        self.assertEqual(trade[-1],'retest_impulse_extreme')
        self.assertEqual(trade[1],85.)

    def test_missing_entry_order_expires(self):
        f=self.candle_df(7)
        f.loc[1:2,'high']=105.
        args=Namespace(raschke_order_valid_bars=2,raschke_max_hold_4h=3,slippage=0)
        trade,reason=m.simulate_4h_fill(f,0,1,120,90,130,args)
        self.assertIsNone(trade)
        self.assertEqual(reason,'entry_order_unfilled')

    def test_price_gap_invalidates_plan(self):
        f=self.candle_df(6);f.loc[1,['open','high','low','close']]=[130.,135.,125.,130.]
        args=Namespace(raschke_order_valid_bars=2,raschke_max_hold_4h=2,slippage=0)
        out,why=m.simulate_4h_fill(f,0,1,110,90,120,args)
        self.assertIsNone(out)
        self.assertEqual(why,'gap_invalidates_plan')

    def test_raschke_first_touch_long(self):
        d=pd.DataFrame({'ts':np.arange(68)*4*3_600_000,'open':106.,'high':108.,'low':105.,
           'close':106.,'volume':10.,'adx':35.,'ema20':100.,'pdi':40.,'mdi':10.,'atr14':2.})
        d.loc[65,'adx']=34;d.loc[66,'adx']=35
        d.loc[65,'ema20']=100;d.loc[66,'ema20']=100.2;d.loc[67,'ema20']=100.4
        d.loc[67,['low','high']]=[100.,107.]
        self.assertTrue(m.raschke_setup(d,67,1,3))
        d.loc[65,'low']=98.
        self.assertFalse(m.raschke_setup(d,67,1,3))

    def test_funding_fees_reduce_pnl(self):
        p=m.net_trade(100,110,1,.0005,.0001,2,2)
        self.assertLess(p,10)
        self.assertGreater(p,9)

    def test_short_pnl(self):
        self.assertGreater(m.net_trade(100,90,-1,.0005,.0001,2,2),9)

    def test_split_nonoverlap(self):
        b=m.split_ranges(0,500*m.MSDAY,240)
        self.assertEqual(b['train'][1],b['validation'][0])
        self.assertEqual(b['validation'][1],b['test_not_pristine'][0])
        self.assertIsNone(m.which_split(b['train'][0]-1,b))

    def test_source_lineage_distinct(self):
        self.assertIn('CONNORS_IMPROVED_R2',m.SOURCE_LINKS)
        self.assertIn('RASCHKE_HOLY_GRAIL',m.SOURCE_LINKS)

    def test_reject_future_end(self):
        args=m.arg_parser().parse_args(['--end-utc','2100-01-01T00:00:00Z'])
        with self.assertRaises(ValueError):m.run(args)

    def test_report_with_synthetic_data_is_only_software_check(self):
        end=m.timestamp_utc('2026-10-01T00:00:00Z');days=400
        ts=end-days*m.MSDAY+np.arange(days*48,dtype=np.int64)*m.MS30
        # Artificial simulation checks ONLY output plumbing, not strategy performance.
        close=100+np.sin(np.arange(days*48)/42)*2+np.arange(days*48)*.001
        op=np.r_[close[0],close[:-1]]
        candles=pd.DataFrame(dict(ts=ts,open=op,high=np.maximum(op,close)+.25,
                                  low=np.minimum(op,close)-.25,close=close,volume=100.))
        with TemporaryDirectory() as tmp:
            args=m.arg_parser().parse_args(['--days','400','--warmup','230',
                    '--symbols','BTC','--output',str(Path(tmp)/'run.json'),
                    '--cache',str(Path(tmp)/'cache')])
            with patch.object(m,'download_30m',return_value=candles):
                self.assertEqual(m.run(args),0)
            report=Path(tmp)/'run.json'
            self.assertTrue(report.exists())
            self.assertIn('ready_for_live_signals',{'ready_for_live_signals':False})
            import json
            data=json.loads(report.read_text())
            self.assertFalse(data['ready_for_live_signals'])
            self.assertTrue((Path(tmp)/'run.summary.csv').exists())
            self.assertTrue((Path(tmp)/'run.trades.csv').exists())

if __name__=='__main__':unittest.main()
