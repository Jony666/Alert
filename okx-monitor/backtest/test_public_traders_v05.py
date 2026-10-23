import unittest
import json
from argparse import Namespace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import numpy as np
import pandas as pd
import public_traders_v05 as m
import trader_strategies_v04 as base


class TraderV05Tests(unittest.TestCase):
    def daily(self,n=310):
        i=np.arange(n,dtype=float)
        op=100+i*.20
        cl=op+.04
        return pd.DataFrame({'ts':np.arange(n,dtype=np.int64)*m.DAY+16*3600*1000,
                             'open':op,'high':op+2.,'low':op-2.,'close':cl,
                             'volume':np.ones(n)*100})

    def minute(self,days=10,start=16*3600*1000):
        n=days*48
        op=100+np.sin(np.arange(n)/20.)*.1
        return pd.DataFrame({'ts':np.arange(n,dtype=np.int64)*m.MS30+start,
                             'open':op,'high':op+.25,'low':op-.25,
                             'close':op+.01,'volume':100.0})

    def test_five_predeclared_unique_scenarios(self):
        self.assertEqual(len(m.STRATEGIES),5)
        self.assertEqual(len(set(m.STRATEGIES)),5)

    def test_no_bnf_scanner_dependency(self):
        src=Path(m.__file__).read_text()
        self.assertNotIn('import scanner',src)
        self.assertNotIn('requests.post',src)
        self.assertNotIn('ENABLE_PUSH',src)

    def test_funding_deducted_8h(self):
        r=m.net_return(100,100,1,.0005,.0001,24*3600*1000)
        self.assertAlmostEqual(r,-.13)

    def test_long_and_short_consistent(self):
        a=m.net_return(100,104,1,.001,0,1000)
        b=m.net_return(104,100,-1,.001,0,1000)
        self.assertGreater(a,0);self.assertGreater(b,0)

    def test_invalid_prices_rejected(self):
        with self.assertRaises(ValueError):m.net_return(100,-1,-1,.001,0,0)
        with self.assertRaises(ValueError):m.net_return(100,100,3,.001,0,0)

    def test_time_splits_contiguous(self):
        x=m.split_bounds(0,730*m.DAY,320)
        self.assertEqual(x['train'][1],x['validation'][0])
        self.assertEqual(x['validation'][1],x['test_not_pristine'][0])
        self.assertEqual(m.mark_split(x['train'][1],x),'validation')

    def test_crabel_level_uses_previous_only(self):
        d=self.daily(50)
        result=m.crabel_levels(d,20,'CRABEL_2026_ORB')
        d.loc[20,'high']=1000000
        self.assertEqual(m.crabel_levels(d,20,'CRABEL_2026_ORB'),result)
        self.assertGreater(result[0],result[1])

    def test_crabel_2026_10day_range_exact(self):
        d=self.daily(50)
        i=20;w=d.iloc[i-10:i]
        self.assertAlmostEqual(m.crabel_levels(d,i,'CRABEL_2026_ORB')[0],float(d.open.iloc[i])+.8*(w.high.max()-w.low.min()))

    def test_crabel_nr7_drops_wider_setup(self):
        d=self.daily(50);d.loc[19,'high']+=100
        self.assertIsNone(m.crabel_levels(d,20,'CRABEL_NR7_STRETCH'))

    def test_crabel_nr7_pass_with_small_range(self):
        d=self.daily(50);d.loc[19,'high']=d.loc[19,'open']+.01;d.loc[19,'low']=d.loc[19,'open']-.01
        self.assertIsNotNone(m.crabel_levels(d,20,'CRABEL_NR7_STRETCH'))

    def test_ambiguous_same_30m_bar_never_guess_direction(self):
        a=self.minute(1).iloc[:2].copy()
        a.loc[a.index[0],'high']=200;a.loc[a.index[0],'low']=1
        side,_,reason=m.first_touch(a,110,90)
        self.assertIsNone(side);self.assertEqual(reason,'ambiguous_both_directions')

    def test_first_touch_direction_order(self):
        a=self.minute(1).iloc[:3].copy()
        a.loc[a.index[1],'high']=110.1
        side,where,reason=m.first_touch(a,110,90)
        self.assertEqual(side,1);self.assertEqual(where,a.index[1]);self.assertIsNone(reason)

    def test_gaps_valued_at_real_open(self):
        self.assertAlmostEqual(m.clipped_market_order(120,110,1,0),120)
        self.assertAlmostEqual(m.clipped_market_order(80,90,-1,0),80)
        self.assertAlmostEqual(m.exit_on_level(85,90,1,0),85)
        self.assertAlmostEqual(m.exit_on_level(115,110,-1,0),115)

    def test_turtle_same_bar_ambiguous(self):
        self.assertEqual(m.turtle_signal_from_bar(120,80,110,90),(0,True))
        self.assertEqual(m.turtle_signal_from_bar(111,97,110,90),(1,False))
        self.assertEqual(m.turtle_signal_from_bar(100,87,110,90),(-1,False))

    def test_turtle_prior_extreme_cannot_see_current(self):
        d=m.prep_daily(self.daily(310));level=d.hi20.iloc[250]
        d.loc[250,'high']=100000
        self.assertEqual(d.hi20.iloc[250],level)

    def test_turtle_stop_is_prior_to_channel(self):
        raw=self.minute(3)
        d=m.prep_daily(base.aligned_bars(raw,1440))
        slots=m.day_bars(raw,d)
        # Inject manual channel (uses previously completed day) and trigger stop on entry bar.
        d.loc[1,'lo10']=90.
        j=slots[1][0]
        raw.loc[j,'low']=80
        res=m.simulate_turtle(raw,d,slots,1,j,1,100,95,1,10,30,Namespace(slippage=0))
        self.assertEqual(res[3],'protective_2N_stop')

    def test_trend_template_requires_200rising(self):
        d=m.prep_daily(self.daily(320))
        # With synthetic rising close it should pass all thresholds except 25% from 52w low;
        # explicitly inject derived values for fixed gating.
        i=300;d.loc[i,['sma50','sma150','sma200','low252','high252']]=[149,145,140,100,180]
        d.loc[i-20,'sma200']=139
        self.assertTrue(m.trend_template_7of8(d,i))
        d.loc[i-20,'sma200']=141
        self.assertFalse(m.trend_template_7of8(d,i))

    def test_vcp_no_current_bar_lookahead(self):
        d=self.daily(100);i=90
        x=m.vcp_research_proxy(d,i)
        d.loc[i,'high']=9999999
        self.assertEqual(m.vcp_research_proxy(d,i),x)

    def test_vcp_requires_range_contraction_and_volume(self):
        d=self.daily(100);i=90
        d.loc[60:74,'high']=d.loc[60:74,'open']+10
        d.loc[75:84,'high']=d.loc[75:84,'open']+4
        d.loc[85:89,'high']=d.loc[85:89,'open']+1
        d.loc[60:74,'volume']=400
        d.loc[75:84,'volume']=200
        d.loc[85:89,'volume']=60
        d.loc[i,'close']=d.high.iloc[i-15:i].max()+1
        d.loc[i,'volume']=300
        self.assertTrue(m.vcp_research_proxy(d,i))

    def test_empty_stats_no_manufactured_metrics(self):
        x=m.stats([]);self.assertEqual(x['trades'],0)
        self.assertIsNone(x['profit_factor'])
        self.assertIsNone(x['win_rate'])

    def test_invalid_data_coverage_rejected(self):
        d=self.minute(5).drop(index=5).reset_index(drop=True)
        with self.assertRaises(ValueError):base.validate_30m(d)

    def test_end_to_end_synthetic_not_market_performance(self):
        import tempfile
        until=base.timestamp_utc('2026-01-01T00:00:00Z');days=430
        ts=np.arange(days*48,dtype=np.int64)*m.MS30+(until-days*m.DAY)
        rng=np.random.default_rng(991)
        close=100*np.exp(np.cumsum(rng.normal(.00008,.004,len(ts))))
        op=np.r_[close[0],close[:-1]]
        candles=pd.DataFrame({'ts':ts,'open':op,'high':np.maximum(op,close)*1.003,
                             'low':np.minimum(op,close)*.997,'close':close,
                             'volume':20+rng.integers(1,70,len(ts))})
        with TemporaryDirectory() as root:
            args=Namespace(end_utc='2026-01-01T00:00:00Z',days=430,warmup_days=320,
                           fee=.0005,slippage=.0005,funding=.0001,turtle_max_days=50,
                           minervini_max_days=35,pause=0.,symbols=['BTC'],
                           output=str(Path(root)/'synthetic.json'),cache=str(Path(root)/'cache'))
            self.assertEqual(m.run(args,loader=lambda *_:candles.copy()),0)
            for suffix in ('.json','.summary.csv','.trades.csv','.diagnostic.csv'):
                target=Path(root)/('synthetic'+suffix)
                self.assertTrue(target.exists(),suffix)
            j=json.loads((Path(root)/'synthetic.json').read_text())
            self.assertEqual(j['status'],'EXPLORATORY_RESEARCH_ONLY_NOT_VALIDATED')
            self.assertEqual(len(j['coverage']),1)
            self.assertFalse(j['ready_for_live'])
            self.assertEqual(len(j['results']),5*3*3)

    def test_synthetic_sample_cannot_be_called_verified_live(self):
        self.assertFalse(m.stats([])['sample100'])
        self.assertEqual(len(m.SOURCES),4)


if __name__=='__main__':unittest.main()
