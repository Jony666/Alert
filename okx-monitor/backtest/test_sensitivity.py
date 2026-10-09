import unittest
from argparse import Namespace
from datetime import datetime, timezone

import numpy as np
import pandas as pd

import sensitivity_backtest as s
import historical_backtest as bt
import diagnostic_backtest as dg


class ThirdRoundTests(unittest.TestCase):
    def test_unique_predeclared_scenarios(self):
        self.assertEqual(len(s.SCENARIOS), len({x.name for x in s.SCENARIOS}))
        self.assertTrue(any(x.cost_mult==2 for x in s.SCENARIOS))
        self.assertEqual(set(x.strategy for x in s.SCENARIOS), {'A','B'})

    def test_exact_baseline_flags(self):
        a={x:True for x in dg.A_KEYS}
        a[dg.A_KEYS[4]]=False
        self.assertFalse(s.matches(a,{},0,s.SCENARIO_LOOKUP['A_BASE']))
        self.assertTrue(s.matches(a,{},0,s.SCENARIO_LOOKUP['A_NO_1H_BREAKOUT']))
        self.assertFalse(a[dg.A_KEYS[4]],'ablation must not mutate baseline flags')

    def test_each_ablation_modifies_only_one_gate(self):
        for x in ['A3','A5','A6','A7']:
            flags=dict.fromkeys(dg.A_KEYS,True)
            k=next(k for k in flags if k.startswith(x+'_'))
            flags[k]=False
            variant=next(t for t in s.SCENARIOS if t.gate_change==x)
            self.assertTrue(s.matches(flags,{},0,variant))
            other=list(filter(lambda z:z != k,dg.A_KEYS))[0]
            flags[other]=False
            self.assertFalse(s.matches(flags,{},0,variant))

    def test_B_3_of_8_still_requires_other_gates(self):
        f=dict.fromkeys(dg.B_KEYS,True)
        f[dg.B_KEYS[0]]=False
        scenario=s.SCENARIO_LOOKUP['B_REQUIRE_3_OF_8']
        self.assertFalse(s.matches(f,{},2,scenario))
        self.assertTrue(s.matches(f,{},3,scenario))
        f[dg.B_KEYS[1]]=False
        self.assertFalse(s.matches(f,{},3,scenario))

    def test_B_confirm_only_breakout_or_volume(self):
        f=dict.fromkeys(dg.B_KEYS,True)
        f[dg.B_KEYS[3]]=False
        br=s.SCENARIO_LOOKUP['B_BREAKOUT_ONLY_NO_VOLUME']
        vol=s.SCENARIO_LOOKUP['B_VOLUME_ONLY_NO_BREAKOUT']
        self.assertTrue(s.matches(f,{'breakout_either_tf':True,'volume_either_tf':False},4,br))
        self.assertFalse(s.matches(f,{'breakout_either_tf':True,'volume_either_tf':False},4,vol))
        self.assertTrue(s.matches(f,{'breakout_either_tf':False,'volume_either_tf':True},4,vol))

    @staticmethod
    def sample_plan_frames():
        h=pd.DataFrame({'low':[99.]*100,'high':[102.]*100,
                        'atr':[2.]*100,'close':[100.]*100})
        h.loc[44,'high']=103.3
        h.loc[45,'high']=105
        h.loc[46,'high']=107
        h4=h.copy()
        h4.loc[47,'high']=106.5
        m=pd.DataFrame({'close':[100.]})
        return {'1H':h,'4H':h4,'30m':m}

    def test_original_trade_plan_does_not_change(self):
        d=self.sample_plan_frames()
        original=bt.plan(d,100.1,.0005,.0005,.0001)
        now,err=s.trade_plan(d,100.1,.0005,.0005,.0001,2.)
        self.assertIsNone(err)
        self.assertIsNotNone(now)
        self.assertEqual(original,now)
        self.assertGreaterEqual(now[3],2.)
        self.assertGreaterEqual(now[4],2.8)

    def test_rr_only_sensitivity(self):
        d=self.sample_plan_frames()
        base,_=s.trade_plan(d,100.1,.0005,.0005,.0001,2.0)
        relaxed,_=s.trade_plan(d,100.1,.0005,.0005,.0001,1.5)
        self.assertIsNotNone(base)
        self.assertIsNotNone(relaxed)
        self.assertEqual(base[0],relaxed[0],'stop must not move')
        self.assertEqual(base[2],relaxed[2],'highest resistance must not move')
        self.assertLess(relaxed[1],base[1],'TP1 should be the only economic threshold changed')

    def test_upper_entry_only_one_gate(self):
        est=100.;atr=2.;real=100.18
        kw=dict(estimated=est,atr=atr,stop=98.,tp1=108.,fee=.0005,slip=.0005,funding=.0001,rr1_min=2.)
        self.assertEqual(s.entry_reason(real,max_upper_atr=0.,**kw),'next_open_outside_entry_band')
        self.assertIsNone(s.entry_reason(real,max_upper_atr=.12,**kw))
        self.assertEqual(s.entry_reason(99.7,max_upper_atr=.12,**kw),'next_open_outside_entry_band')

    def test_net_rr_can_degrade_only_after_next_open(self):
        self.assertEqual(s.entry_reason(100.1,100.,2,97.5,104.0,.002,.001,.0001,2.,.12),'net_rr_degraded_after_fill')

    def test_temporal_split_and_embargo(self):
        b=s.split_bounds(100,1100)
        self.assertEqual(b['train'],(100,700))
        self.assertEqual(b['validation'],(700,900))
        self.assertEqual(s.locate_split(699,b),'train')
        self.assertEqual(s.locate_split(700,b),'validation')
        self.assertEqual(s.locate_split(900,b),'temporal_test_not_pristine_oos')
        self.assertIsNone(s.locate_split(1100,b))

    def test_statistics_do_not_invent_returns_for_no_trades(self):
        x=s.stats([])
        self.assertEqual(x['trades'],0)
        self.assertIsNone(x['win_rate'])
        self.assertFalse(x['sample_sufficient_for_validation'])

    def test_performance_statistics_only_trade_sequential(self):
        fake=[]
        for j,ret in enumerate((-2.0,1.0,-1.0,2.0)):
            fake.append({'return_pct':ret,'exit_time_ms':j,'instrument':'BTC',
                         'scenario':'A_BASE','entry_actual':100.,'stop':97.,'hold_30m_bars':2})
        z=s.stats(fake)
        self.assertEqual(z['trades'],4)
        self.assertEqual(z['win_rate'],.5)
        self.assertEqual(z['mean_net_return_pct'],0.)
        self.assertEqual(z['losing_streak_max'],1)
        self.assertGreater(z['illustrative_sequential_dd'],0)

    def test_utc_time_only_and_reproducible_cutoff(self):
        self.assertEqual(s.ensure_datetime('2026-10-01T00:00:00Z'),1790812800000)
        with self.assertRaises(ValueError):s.ensure_datetime('2026-10-01T00:00:00')
        with self.assertRaises(ValueError):s.ensure_datetime('2026-10-01T08:00:00+08:00')

    def test_research_smoke_with_real_indicators_no_network(self):
        # Local synthetic continuous confirmed candles, never presented as returns.
        days=110;N=days*48;start=s.ensure_datetime('2025-01-01T00:00:00Z')
        r=np.random.default_rng(230)
        close=100*np.exp(np.cumsum(r.normal(.00001,.001,N)))
        op=np.concatenate(([100.],close[:-1]))
        data=pd.DataFrame({'ts':start+np.arange(N,dtype=np.int64)*s.MS_30M,
            'open':op,'high':np.maximum(op,close)*1.002,
            'low':np.minimum(op,close)*.998,'close':close,
            'volume':r.uniform(10,100,N)})
        args=Namespace(since_ms=start,until_ms=start+days*s.MS_DAY,warmup=90,
            max_hold=96,fee=.0005,slippage=.0005,funding=.0001)
        trades,counters,audit,bounds=s.research(data,'SYNTHETIC-NO-ORDERS',args)
        self.assertGreater(audit['evaluated_decisions'],0)
        self.assertGreaterEqual(len(counters),0)
        for t in trades:
            self.assertLessEqual(t['exit_time_ms'],bounds[t['split']][1])

if __name__=='__main__':unittest.main()
