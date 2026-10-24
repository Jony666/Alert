import copy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
import numpy as np
import pandas as pd
from test_swing_v09 import candles
import test_swing_v09 as baseline_fixtures
from v09.config import CONFIG,DAY
from v09.data import timestamp
from v09.exits import costs,bar_exit
from v09.research import simulate
from v09.phase3.strategies import enrich,signal,plan,STRATEGIES,NEW,RULES,rules_hash
from v09.phase3.evaluation import summarize,rank,interval

class Phase3Causality(unittest.TestCase):
    def test_completed_higher_and_btc_prefix(self):
        raw=candles(24*100);x=enrich(raw,3600000,raw);y=enrich(raw.iloc[:1800],3600000,raw.iloc[:1800])
        pd.testing.assert_frame_equal(x.iloc[:1800],y)
    def test_future_btc_cannot_change_past(self):
        raw=candles(24*100);btc=raw.copy();a=enrich(raw,3600000,btc)
        btc.loc[1800:,'close']*=2;btc.loc[1800:,'high']*=3
        b=enrich(raw,3600000,btc)
        pd.testing.assert_frame_equal(a.iloc[:1800],b.iloc[:1800])
    def test_no_incomplete_week_visible(self):
        raw=candles(220,DAY);x=enrich(raw,DAY,raw)
        self.assertTrue(x.hslow.iloc[:170].isna().all())
    def test_frozen_parameters(self):
        from dataclasses import FrozenInstanceError
        with self.assertRaises(FrozenInstanceError):RULES.atr_min=.0001
        self.assertEqual(len(rules_hash()),64)
    def test_risk_thresholds_unchanged(self):
        self.assertEqual((CONFIG.initial,CONFIG.reserve,CONFIG.total_risk,CONFIG.min_win_lower,CONFIG.max_days),(10000,1000,2000,.60,14))

class Phase3Signals(unittest.TestCase):
    def records(self):
        r=dict(atr_fraction=.02,atr=2.,hfast=110.,hslow=100.,btc_move24h=.01,btc_direction=1.,width_reference=.1,
               width20=.05,close=111.,ema50=105.,fast=108.,macd=1.,signal=0.,open=109.,low=108.,high=112.,
               volume=200.,vol20=100.,hi20=110.,lo20=90.,prior_break_up=True,prior_break_down=False,retest_high=110.,retest_low=90.)
        return [SimpleNamespace(**r) for _ in range(100)]
    def test_breakout_retest_long(self):self.assertEqual(signal(self.records(),99,'BREAKOUT_RETEST')[0],1)
    def test_momentum_long(self):
        x=self.records();x[-2].close=107.;self.assertEqual(signal(x,99,'MOMENTUM_CONTINUATION')[0],1)
    def test_squeeze_long(self):self.assertEqual(signal(self.records(),99,'VOL_SQUEEZE')[0],1)
    def test_sr_long(self):
        x=self.records();x[-1].low=89.;self.assertEqual(signal(x,99,'SR_REVERSAL')[0],1)
    def test_mtf_long(self):
        x=self.records();x[-2].macd=-1.;self.assertEqual(signal(x,99,'MTF_TREND')[0],1)
    def test_all_new_reject_btc_shock(self):
        x=self.records();x[-1].btc_move24h=-.11
        for s in NEW:self.assertEqual(signal(x,99,s),(0,0.))
    def test_all_new_reject_opposite_btc(self):
        x=self.records();x[-1].btc_direction=-1
        for s in NEW:self.assertEqual(signal(x,99,s),(0,0.))
    def test_atr_filter(self):
        x=self.records();x[-1].atr_fraction=.10
        for s in NEW:self.assertEqual(signal(x,99,s),(0,0.))
    def test_short_retest(self):
        x=self.records();a=x[-1];a.close=89.;a.open=91.;a.high=92.;a.ema50=95.;a.hfast=90.;a.hslow=100.;a.btc_direction=-1;a.prior_break_down=True
        self.assertEqual(signal(x,99,'BREAKOUT_RETEST')[0],-1)
    def test_unknown_strategy(self):
        with self.assertRaises(ValueError):signal(self.records(),99,'FAKE')
    def test_plan_is_structure_not_forced_three_r(self):
        p=SimpleNamespace(lo5=98.,hi5=102.,atr=2.,hi55=120.,lo55=80.,hi20=115.,lo20=85.)
        for s in NEW:
            q=plan(s,p,100,1);self.assertIsNotNone(q);self.assertEqual(q.weights,(.5,.5))
            self.assertEqual(q.stop,97.)
        weak=SimpleNamespace(lo5=1.,hi5=200.,atr=20.,hi55=101.,lo55=99.,hi20=100.5,lo20=99.5)
        self.assertLess(costs(plan('MTF_TREND',weak,100,1))['net_rr'],3)

class Phase3Engine(unittest.TestCase):
    def test_fast_schedule_equivalent_to_legacy(self):
        m=baseline_fixtures.IntegratedV09Tests().market();markets={'BTC':m};strategy='MACD_TREND'
        span=(int(m['frame'].ts.iloc[60]),int(m['frame'].ts.iloc[-1])+3600000)
        legacy=simulate(markets,strategy,span,3600000)
        schedule={strategy:{i+1:[(-score,-m['records'][i].liquidity,'BTC',strategy,side)] for i,(side,score) in enumerate(m['signals'][strategy][:-1]) if side}}
        fast=simulate(markets,strategy,span,3600000,candidate_schedule=schedule)
        self.assertEqual(legacy,fast)
    def test_three_day_override_all_costs_reconcile(self):
        m=baseline_fixtures.IntegratedV09Tests().market();span=(int(m['frame'].ts.iloc[60]),int(m['frame'].ts.iloc[-1])+3600000)
        row,trades,curve=simulate({'BTC':m},'MACD_TREND',span,3600000,hold_days_by_strategy={'MACD_TREND':3})
        self.assertGreater(len(trades),0)
        self.assertTrue(all(t['hold_days']<=3 for t in trades))
        self.assertAlmostEqual(sum(t['net_twd'] for t in trades),curve[-1]['equity_twd']-10000)
        for t in trades:
            self.assertAlmostEqual(sum(f['fraction'] for f in t['fills']),1.)
            gross=sum(t['side']*(f['price']-t['entry'])*t['units']*f['fraction']*32 for f in t['fills'])
            self.assertAlmostEqual(gross-t['cost_twd'],t['net_twd'])
            self.assertGreaterEqual(t['planned_net_rr'],3.)

class Phase3Statistics(unittest.TestCase):
    def trades(self):
        return [dict(net_twd=n,hold_days=2.,planned_net_rr=3.1,net_r=n/100,cost_twd=1.,entry_time=f'2024-01-0{i+1}T00:00:00Z',exit_time=f'2024-01-0{i+3}T00:00:00Z',symbol='BTC') for i,n in enumerate((100,-50,20))]
    def test_three_ratios_separate(self):
        r=summarize(self.trades());self.assertAlmostEqual(r['avg_planned_net_rr'],3.1)
        self.assertAlmostEqual(r['realized_net_payoff'],1.2);self.assertAlmostEqual(r['avg_net_r'],.7/3)
    def test_nonoverlap_thinning_not_aggregate_count(self):
        r=summarize(self.trades());self.assertEqual(r['nonoverlap_14d_proxy_n'],1);self.assertFalse(r['independence_verified'])
    def test_family_interval_more_conservative(self):
        self.assertLess(interval(60,100)[0],interval(60,100,family=1)[0])
    def test_empty_stats(self):
        r=summarize([]);self.assertIsNone(r['win_rate']);self.assertEqual(r['win_ci_95'],[0.,1.])
    def test_hold_summary(self):
        r=summarize(self.trades());self.assertEqual((r['avg_hold_days'],r['median_hold_days'],r['max_hold_days']),(2.,2.,2.))
    def test_ranking_cannot_read_test_results(self):
        row=dict(bar='1H',strategy='MTF_TREND',split='test_2025_contaminated')
        # No validation rows: only insufficient evidence, even if test is perfect.
        first=rank([dict(row,return_pct=100000)])
        second=rank([dict(row,return_pct=-100000)])
        self.assertEqual(first,second);self.assertEqual(first[0]['classification'],'需要改進')
    def test_no_live_qualification_even_all_wins(self):
        r=summarize([self.trades()[0]]*100)
        self.assertEqual(r['qualification'],'NO QUALIFIED SIGNAL');self.assertFalse(r['forward_verified'])

if __name__=='__main__':unittest.main()
