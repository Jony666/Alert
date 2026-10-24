import copy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch,Mock
import requests
from test_phase3_v09 import Phase3Signals as SignalFixture
import test_swing_v09 as fixtures
from v09.config import CONFIG
from v09.exits import Plan,costs,bar_exit,quantize_plan
from v09.research import simulate
from v09.phase3.strategies import signal as old_signal,plan as old_plan
from v09.phase4.design import staged_signal,make_plan,experiments,CORES,EXITS,DESIGN,digest
from v09.phase4.evaluation import summarize,classify
from v09.phase4.sources import public_get

# Import fixture class via module instead of exposing it to discovery.
import test_phase3_v09 as phase3_fixtures
# Remove imported fixture class from module's unittest discovery namespace.
del SignalFixture

class Phase4DesignTests(unittest.TestCase):
    def test_only_two_registered_cores(self):
        self.assertEqual(CORES,('VOL_SQUEEZE','SR_REVERSAL'));self.assertEqual(len(experiments()),244)
        self.assertTrue(all(c[0] in CORES for c in experiments()))
    def test_frozen_design(self):
        from dataclasses import FrozenInstanceError
        with self.assertRaises(FrozenInstanceError):DESIGN.rank_win_lower=.59
        self.assertEqual(len(digest()),64)
    def test_single_target_real_weight(self):
        p=Plan(100,98,120,120,1,(1.,0.)).validate()
        self.assertEqual(p.weights,(1.,0.));self.assertGreater(costs(p)['net_rr'],3)
        self.assertEqual(bar_exit(p,100,125,99,98,1,False,0),[(1.,120,'tp1')])
    def test_single_short(self):
        p=Plan(100,102,80,80,-1,(1.,0.)).validate()
        self.assertEqual(bar_exit(p,100,101,75,102,1,False,0),[(1.,80,'tp1')])
    def test_zero_weight_cannot_inflate_reward(self):
        p=Plan(100,95,101,10000,1,(1.,0.)).validate()
        self.assertLess(costs(p)['net_rr'],3)
    def test_single_tick_rounding(self):
        p=quantize_plan(Plan(100.01,98.09,120.09,120.09,1,(1.,0.)),'.1')
        self.assertEqual((p.entry,p.stop,p.tp1,p.tp2),(100.1,98.,120.,120.))
    def test_negative_exit_fraction_rejected(self):
        with self.assertRaises(ValueError):Plan(100,98,120,140,1,(1.1,-.1)).validate()
    def test_same_bar_stop_precedes_single_target(self):
        p=Plan(100,98,120,120,1,(1.,0.));self.assertEqual(bar_exit(p,90,150,80,98,1,False,0)[0][2],'stop')
    def test_baseline_signal_identity(self):
        x=phase3_fixtures.Phase3Signals().records()
        for s in CORES:
            for i in range(len(x)):
                self.assertEqual(staged_signal(x,i,s)[:2],old_signal(x,i,s))
    def test_vol_neighbor_is_predeclared(self):
        x=phase3_fixtures.Phase3Signals().records();x[-1].volume=120.
        self.assertEqual(staged_signal(x,99,'VOL_SQUEEZE','base')[0],0)
        self.assertEqual(staged_signal(x,99,'VOL_SQUEEZE','neighbor_low')[0],1)
    def test_reversal_stop_neighbor_not_signal_tuning(self):
        p=SimpleNamespace(lo5=99.,hi5=101.,atr=2.,hi55=120.,lo55=80.,hi20=115.,lo20=85.)
        low=make_plan('SR_REVERSAL','neighbor_low','split_atr',p,100,1)
        high=make_plan('SR_REVERSAL','neighbor_high','split_atr',p,100,1)
        self.assertEqual((low.tp1,low.tp2),(high.tp1,high.tp2));self.assertLess(high.stop,low.stop)
    def test_protect_and_fixed_use_same_initial_plan(self):
        p=SimpleNamespace(lo5=98.,hi5=102.,atr=2.,hi55=120.,lo55=80.,hi20=115.,lo20=85.)
        self.assertEqual(make_plan('VOL_SQUEEZE','base','split_fixed',p,100,1),old_plan('VOL_SQUEEZE',p,100,1))
    def test_unregistered_strategy_refused(self):
        with self.assertRaises(ValueError):make_plan('THIRD','base','split_atr',None,100,1)
    def test_public_endpoint_forbids_private(self):
        with self.assertRaises(ValueError):public_get('account/trade-fee',{})
    def test_public_429_retry(self):
        r=Mock();r.raise_for_status.side_effect=[requests.HTTPError('429'),None];r.json.return_value={'code':'0','data':[{'mmr':'.01'}]}
        with patch('v09.phase4.sources.requests.get',return_value=r) as get,patch('v09.phase4.sources.time.sleep'):
            self.assertEqual(public_get('public/position-tiers',{}),[{'mmr':'.01'}]);self.assertEqual(get.call_count,2)

class Phase4EngineTests(unittest.TestCase):
    def market(self):return fixtures.IntegratedV09Tests().market()
    def run_case(self,**kwargs):
        m=self.market();span=(int(m['frame'].ts.iloc[60]),int(m['frame'].ts.iloc[-1])+3600000)
        return simulate({'BTC':m},'MACD_TREND',span,3600000,**kwargs)
    def test_exit_default_still_identical(self):self.assertEqual(self.run_case(),self.run_case(exit_mode='atr',gap_fraction=0.))
    def test_invalid_exit_refused(self):
        with self.assertRaises(ValueError):self.run_case(exit_mode='optimistic')
    def test_gap_stress_does_not_guarantee_budget_cap(self):
        row,trades,_=self.run_case(gap_fraction=.1)
        self.assertTrue(any(t['net_twd'] < -t['risk_twd'] for t in trades))
        self.assertLessEqual(row['max_planned_risk_twd'],2000+1e-6)
    def test_all_modes_cost_and_partial_reconciliation(self):
        for mode in ('fixed','protect','atr','fib'):
            row,trades,curve=self.run_case(exit_mode=mode)
            self.assertAlmostEqual(sum(t['net_twd'] for t in trades),curve[-1]['equity_twd']-10000)
            self.assertTrue(all(abs(sum(f['fraction'] for f in t['fills'])-1)<1e-8 for t in trades))
            self.assertEqual(row['counts'].get('entries',0),row['counts'].get('all_risk_gates_pass',0))
    def test_hard_caps_remain(self):
        self.assertEqual((CONFIG.initial,CONFIG.reserve,CONFIG.total_risk,CONFIG.min_win_lower),(10000,1000,2000,.6))
    def test_both_cores_simultaneous_shared_account(self):
        m=self.market();m['signals']={s:m['signals']['MACD_TREND'] for s in CORES}
        row,_,_=simulate({'BTC':m},'ALL',(int(m['frame'].ts.iloc[60]),int(m['frame'].ts.iloc[-1])+3600000),3600000,strategy_names=CORES)
        self.assertLessEqual(row['max_open'],3);self.assertLessEqual(row['max_planned_risk_twd'],2000+1e-6)

class Phase4StatisticsTests(unittest.TestCase):
    def sample(self):return phase3_fixtures.Phase3Statistics().trades()
    def test_percentile_holding_distribution(self):
        r=summarize(self.sample());self.assertEqual(r['hold_percentiles_days']['95'],2.)
    def test_effective_n_not_invented(self):
        r=summarize(self.sample());self.assertIsNone(r['effective_independent_n']);self.assertFalse(r['independence_verified'])
    def test_phase4_multiple_family_label(self):
        r=summarize(self.sample());self.assertIn('simultaneous_ci_family42',r);self.assertNotIn('simultaneous_ci_95_family24',r)
    def test_abc_cannot_use_test_returns(self):
        row=dict(core='VOL_SQUEEZE',bar='1H',variant='base',exit='split_atr',split='test_2025_contaminated')
        a=classify([dict(row,return_pct=1000)]);b=classify([dict(row,return_pct=-1000)])
        self.assertEqual(a,b);self.assertTrue(all(p['category']=='B' for p in a['core_decisions']))
    def test_nets_do_not_substitute_rr(self):
        r=summarize(self.sample());self.assertAlmostEqual(r['avg_planned_net_rr'],3.1);self.assertAlmostEqual(r['realized_net_payoff'],1.2)
    def test_sparse_negative_is_not_stop_research_forever(self):
        r=classify([]);self.assertTrue(all(x['category']=='B' for x in r['core_decisions']))



class Phase4PathTests(unittest.TestCase):
    def test_single_path_equals_full_net_plan_both_sides(self):
        from v09.phase4.analysis import protected_path_rr
        for side in (1,-1):
            for pressure,mult in (('base',1.),('double_cost',2.)):
                p=Plan(100,98 if side==1 else 102,120 if side==1 else 80,120 if side==1 else 80,side,(1.,0.))
                t=dict(entry=p.entry,stop=p.stop,tp1=p.tp1,tp2=p.tp2,side=p.side,weights=p.weights,pressure=pressure)
                self.assertAlmostEqual(protected_path_rr(t),costs(p,mult)['net_rr'])
    def test_distant_tp2_does_not_improve_protection_path(self):
        from v09.phase4.analysis import protected_path_rr
        t=dict(entry=100,stop=98,tp1=110,tp2=120,side=1,weights=(.5,.5),pressure='base')
        farther=dict(t,tp2=10000)
        self.assertEqual(protected_path_rr(t),protected_path_rr(farther))



class Phase4ContextTests(unittest.TestCase):
    def test_context_requires_only_completed_explanatory_values(self):
        from v09.phase4.context import explain
        p=SimpleNamespace(hfast=104.,hslow=100.,atr=2.,volume=150.,vol20=100.,close=105.,lo20=100.,hi20=110.,atr_fraction=.02,width20=.03,width_reference=.1,btc_move24h=.01)
        r=explain(p,1)
        self.assertEqual((r['environment'],r['volume_bin'],r['reclaim_bin']),('TREND','1.25_TO_2','DEEP'))
        self.assertNotIn('net_twd',r)

if __name__=='__main__':unittest.main()
