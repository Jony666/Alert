import ast
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
import pandas as pd
from v09.config import CONFIG, DAY, STRATEGIES, STEPS
from v09.data import validate, timestamp, download, get, COLS
from v09.strategies import features, signal
from v09.exits import Plan, structure_plan, costs, bar_exit
from v09.risk import State, day, single_cap, size, cross_stress
from v09.events import assess
from v09.ledger import Ledger
from v09.research import wilson, evidence, qualification, splits, simulate
from v09.report import candidate, markdown

T = '2025-01-01T00:00:00Z'
INST = dict(instId='BTC-USDT-SWAP',ctVal='.01',ctValCcy='BTC',lotSz='1',minSz='1')

def candles(n=24*90, step=3600000):
    ts = np.arange(n)*step+timestamp('2024-01-01T00:00:00Z')
    c = 100+np.sin(np.arange(n)/10)*3+np.arange(n)*.01
    return pd.DataFrame(dict(ts=ts,open=c,high=c+1,low=c-1,close=c+.1,volume=np.full(n,100000.),quote_volume=np.full(n,2e7)))

def good_plan(side=1):
    return Plan(100,98,120,140,1) if side==1 else Plan(100,102,80,60,-1)

class DataTests(unittest.TestCase):
    def test_valid(self):
        self.assertTrue(validate(candles(),STEPS['1H']))
    def test_gap(self):
        with self.assertRaises(ValueError): validate(candles().drop(index=3),STEPS['1H'])
    def test_duplicate(self):
        x=candles();x.loc[2,'ts']=x.loc[1,'ts']
        with self.assertRaises(ValueError): validate(x,STEPS['1H'])
    def test_nan(self):
        x=candles();x.loc[3,'close']=np.nan
        with self.assertRaises(ValueError): validate(x,STEPS['1H'])
    def test_bad_ohlc(self):
        x=candles();x.loc[3,'high']=1
        with self.assertRaises(ValueError): validate(x,STEPS['1H'])
    def test_partial_history(self):
        x=candles()
        with self.assertRaises(ValueError): validate(x,STEPS['1H'],x.ts.iloc[0]-3600000,x.ts.iloc[-1]+3600000)
    def test_naive_time(self):
        with self.assertRaises(ValueError): timestamp('2025-01-01')
    def test_private_endpoint_rejected(self):
        with self.assertRaises(ValueError): get('trade/order')
    def test_unconfirmed_candle_rejected(self):
        start=timestamp('2024-01-01T00:00:00Z');end=start+3600000
        row=[str(start),'100','101','99','100','1','1','100','0']
        with tempfile.TemporaryDirectory() as d, patch('v09.data.get',return_value=[row]):
            with self.assertRaises(ValueError): download('BTC-USDT-SWAP','1H',start,end,d)
    def test_features_prefix_invariance(self):
        x=candles();full=features(x,STEPS['1H']);prefix=features(x.iloc[:1500],STEPS['1H'])
        pd.testing.assert_frame_equal(full.iloc[:1500],prefix)
    def test_daily_context_not_early(self):
        x=features(candles(),STEPS['1H'])
        self.assertTrue(x.mean25.iloc[:24*25-1].isna().all())
        self.assertTrue(np.isfinite(x.mean25.iloc[24*25-1]))
    def test_three_strategies_causal(self):
        x=features(candles(),STEPS['1H']);y=x.copy();y.loc[1600:,'close']*=10
        for s in STRATEGIES: self.assertEqual(signal(x,1500,s),signal(y,1500,s))
    def test_unknown_strategy(self):
        with self.assertRaises(ValueError): signal(candles(),60,'UNKNOWN')

class ExitTests(unittest.TestCase):
    def test_weighted_rr_not_far_tp_only(self):
        p=Plan(100,90,101,160,1,(.9,.1))
        self.assertLess(costs(p)['net_rr'],3)
    def test_good_both_sides(self):
        for side in (1,-1): self.assertGreater(costs(good_plan(side))['net_rr'],3)
    def test_double_costs(self):
        self.assertLess(costs(good_plan(),2)['net_rr'],costs(good_plan())['net_rr'])
    def test_funding_14day_bound(self):
        self.assertAlmostEqual(costs(good_plan())['funding_bound_per_unit'],100*.0003*42)
    def test_weights(self):
        with self.assertRaises(ValueError): Plan(100,90,120,140,1,(.4,.4)).validate()
    def test_wrong_direction(self):
        with self.assertRaises(ValueError): Plan(100,110,120,140,1).validate()
    def test_stop_priority(self):
        out=bar_exit(good_plan(),100,150,90,98,1,False,.001)
        self.assertEqual(out[0][2],'stop');self.assertEqual(len(out),1)
    def test_gap_exceeds_budget(self):
        out=bar_exit(good_plan(),80,100,70,98,1,False,.001)
        self.assertAlmostEqual(out[0][1],79.92)
    def test_both_targets(self):
        out=bar_exit(good_plan(),100,150,99,98,1,False,0)
        self.assertEqual([e[0] for e in out],[.5,.5])
    def test_partial_remainder(self):
        out=bar_exit(good_plan(),125,150,110,100,.5,True,0)
        self.assertEqual(out,[(.5,140,'tp2')])
    def test_short_stop(self):
        self.assertEqual(bar_exit(good_plan(-1),100,105,50,102,1,False,0)[0][2],'stop')
    def test_trailing_scenario_not_target_profit(self):
        self.assertLess(costs(good_plan())['tp1_then_protection_net'],costs(good_plan())['reward_per_unit'])

class RiskTests(unittest.TestCase):
    def test_base_cap(self): self.assertEqual(single_cap(),1000)
    def test_staircase(self):
        for capital,cap in ((10000,1000),(14999,1000),(15000,1375),(20000,1750),(25000,2000),(30000,2000)):
            self.assertEqual(single_cap(capital),cap)
    def test_downshift(self): self.assertEqual(single_cap(12000),1000)
    def test_single_rejected(self): self.assertEqual(State().gate('A',1001,T),'SINGLE_RISK_CAP')
    def test_strategy_third(self):
        s=State(positions=[dict(strategy='A',risk_twd=400)]*2)
        self.assertEqual(s.gate('A',400,T),'STRATEGY_POSITION_CAP')
    def test_account_fourth(self):
        s=State(positions=[dict(strategy=k,risk_twd=400) for k in 'ABC'])
        self.assertEqual(s.gate('D',400,T),'ACCOUNT_POSITION_CAP')
    def test_total_risk(self):
        s=State(positions=[dict(strategy='A',risk_twd=1000),dict(strategy='B',risk_twd=600)])
        self.assertEqual(s.gate('C',401,T),'TOTAL_RISK_CAP')
    def test_three_partial_budgets(self):
        s=State(positions=[dict(strategy='A',risk_twd=700),dict(strategy='B',risk_twd=650)])
        self.assertEqual(s.gate('C',650,T),'ALLOW_RESEARCH')
    def test_drawdown_boundary(self): self.assertEqual(State(capital=8000).gate('A',100,T),'PERSISTENT_HALT')
    def test_drawdown_persistent(self):
        s=State(capital=7999);s.observe();s.capital=10000
        self.assertEqual(s.gate('A',100,'2025-01-02T00:00:00Z'),'PERSISTENT_HALT')
    def test_hwm_drawdown(self): self.assertEqual(State(capital=12000,peak=15000).gate('A',100,T),'PERSISTENT_HALT')
    def test_fee_boundary(self): self.assertEqual(State(reserve=500).gate('A',100,T),'PERSISTENT_HALT')
    def test_fee_halt_persists(self):
        s=State(reserve=400);s.observe();s.reserve=1000
        self.assertEqual(s.gate('A',100,T),'PERSISTENT_HALT')
    def test_state_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            s=State(capital=8000);s.save(Path(d)/'s.json');loaded=State.load(Path(d)/'s.json')
            self.assertTrue(loaded.halted)
    def losses(self): return State(closes=[dict(time=T,strategy=k,net=-100) for k in 'AB'])
    def test_two_losses(self): self.assertEqual(self.losses().gate('C',100,T),'DAILY_TWO_LOSSES')
    def test_different_strategy_exception_only(self): self.assertIn('RESEARCH_ONLY',self.losses().gate('C',100,T,True))
    def test_same_strategy_no_exception(self): self.assertEqual(self.losses().gate('A',100,T,True),'DAILY_TWO_LOSSES')
    def test_third_loss_no_fourth(self):
        s=self.losses();s.closes.append(dict(time=T,strategy='C',net=-1))
        self.assertEqual(s.gate('D',100,T,True),'DAILY_STOP_NO_FOURTH')
    def test_day_reset_only_daily(self): self.assertEqual(self.losses().gate('A',100,'2025-01-02T00:00:00Z'),'ALLOW_RESEARCH')
    def test_taipei_midnight(self): self.assertNotEqual(day('2025-01-01T15:59:59Z'),day('2025-01-01T16:00:00Z'))
    def test_realized_loss_not_open_risk(self): self.assertEqual(State(closes=[dict(time=T,strategy='A',net=-1800)]).gate('B',1000,T),'ALLOW_RESEARCH')
    def test_pending_blocks(self): self.assertEqual(State(reconciliation_pending=True).gate('A',100,T),'RECONCILIATION_REQUIRED')
    def test_size_risk_and_lot(self):
        c=costs(good_plan());p=size(good_plan(),c['risk_per_unit'],32,T,'assumed',INST,700)
        self.assertLessEqual(p['risk_twd'],700);self.assertEqual(p['contracts']%1,0)
    def test_minimum(self):
        self.assertIsNone(size(good_plan(),100000,32,T,'assumed',INST,1))
    def test_no_100x(self):
        with self.assertRaises(ValueError): size(good_plan(),10,32,T,'assumed',INST,1000,100)
    def test_fx_required(self):
        with self.assertRaises(ValueError): size(good_plan(),10,32,T,'',INST)
    def test_cross_not_verified(self):
        r=cross_stress([dict(notional_twd=20000)]*3,10000)
        self.assertGreater(r['joint_gap_loss_twd'],2000);self.assertFalse(r['official_cross_liquidation_verified'])

class EvidenceEventTests(unittest.TestCase):
    def test_small_sample_rejected(self): self.assertFalse(evidence([dict(net_twd=1)]*5,True,True)['trusted_60_percent'])
    def test_training_or_no_forward_rejected(self): self.assertFalse(evidence([dict(net_twd=1)]*1000)['trusted_60_percent'])
    def test_lower_bound_not_point_estimate(self):
        self.assertFalse(evidence([dict(net_twd=1)]*60+[dict(net_twd=-1)]*40,True,True)['trusted_60_percent'])
    def test_positive_evidence(self): self.assertTrue(evidence([dict(net_twd=1)]*90+[dict(net_twd=-1)]*10,True,True)['trusted_60_percent'])
    def test_cross_always_required(self):
        ev=evidence([dict(net_twd=1)]*1000,True,True)
        self.assertEqual(qualification(costs(good_plan()),ev)['conclusion'],'NO QUALIFIED SIGNAL')
    def test_low_rr_rejected(self):
        self.assertIn('NET_RR_BELOW_3',qualification({'net_rr':2.99},{'trusted_60_percent':True},True)['reasons'])
    def test_wilson_empty(self): self.assertEqual(wilson(0,0),[0,1])
    def event(self): return dict(published_utc=T,event_utc=T,source='https://www.federalreserve.gov/a',kind='FED',major=True)
    def test_missing_news_neutral(self):
        r=assess([],T,'BTC');self.assertFalse(r['requires_review']);self.assertIsNone(r['win_rate_adjustment'])
    def test_future_news_invisible(self):
        self.assertEqual(assess([self.event()],'2024-12-31T23:59:59Z','BTC')['events'],[])
    def test_major_news(self):
        r=assess([self.event()],T,'BTC');self.assertTrue(r['requires_review']);self.assertIsNone(r['events'][0]['win_rate_adjustment'])
    def test_source_spoof(self):
        e=self.event();e['source']='https://federalreserve.gov.evil.example/a'
        self.assertEqual(assess([e],T,'BTC')['events'],[])
    def test_event_taipei(self): self.assertIn('+08:00',assess([self.event()],T,'BTC')['events'][0]['published_taipei'])
    def test_embargo(self):
        s=splits(timestamp('2024-01-01T00:00:00Z'),timestamp('2026-01-01T00:00:00Z'))
        self.assertGreaterEqual(s['holdout'][0]-s['validation'][1],14*DAY)

class LedgerReportTests(unittest.TestCase):
    def ledger(self,book='actual_confirmed'):
        l=Ledger();l.propose('1','BTC','A',1,book);return l
    def fill(self): return dict(time=T,price=100.,units=10.,fee=1.,leverage=5.,stop=90.,tp1=120.,tp2=140.,fx=32.,fx_time=T,fx_source='manual')
    def test_proposal_not_fill(self): self.assertEqual(self.ledger().performance('actual_confirmed'),0)
    def test_declined(self):
        l=self.ledger();l.response('1',False);self.assertEqual(l.records['1']['state'],'not_placed')
    def test_missing_details_pending(self):
        l=self.ledger();l.response('1',True);self.assertEqual(l.records['1']['state'],'pending_reconciliation')
    def test_partial_pnl(self):
        l=self.ledger();l.response('1',True,self.fill());net=l.exit('1',T,110.,5.,.5,.2)
        self.assertAlmostEqual(net,48.8);self.assertEqual(l.records['1']['state'],'partially_closed')
    def test_full_pnl(self):
        l=self.ledger();l.response('1',True,self.fill());l.exit('1',T,110.,5.,.5,.2);l.exit('1',T,90.,5.,.5,.2)
        self.assertAlmostEqual(l.performance('actual_confirmed'),-2.4);self.assertEqual(l.records['1']['state'],'closed')
    def test_paper_separate(self):
        l=self.ledger('paper_backtest');l.response('1',True,self.fill());l.exit('1',T,110.,10.,1.)
        self.assertEqual(l.performance('actual_confirmed'),0)
    def test_no_close_without_fill(self):
        with self.assertRaises(ValueError): self.ledger().exit('1',T,110.,1.,0.)
    def test_overclose(self):
        l=self.ledger();l.response('1',True,self.fill())
        with self.assertRaises(ValueError): l.exit('1',T,110.,11.,0.)
    def test_cancel_open_rejected(self):
        l=self.ledger();l.response('1',True,self.fill())
        with self.assertRaises(ValueError): l.cancel('1')
    def test_transfer_not_profit(self):
        l=self.ledger();l.transfer('1',500,T,'manual');self.assertEqual(l.performance('actual_confirmed'),0)
    def test_report_fields(self):
        r=candidate('BTC','A',good_plan(),evidence([]),{'cross_verified':False},assess([],T,'BTC'),'ALLOW_RESEARCH',700)
        self.assertEqual(r['conclusion'],'NO QUALIFIED SIGNAL');self.assertIsNone(r['actual_exit'])
        for key in ('strategy','entry','stop','tp1','tp2','costs','evidence','events','risk_state','decision','allocation','trailing'):
            self.assertIn(key,r)
        self.assertIn('非當下訊號',markdown(r))

class SafetyTests(unittest.TestCase):
    def test_http_is_get_only(self):
        for p in (Path(__file__).parent/'v09').glob('*.py'):
            tree=ast.parse(p.read_text())
            for node in ast.walk(tree):
                if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute) and isinstance(node.func.value,ast.Name) and node.func.value.id=='requests':
                    self.assertEqual(node.func.attr,'get')
    def test_workflow_manual_only(self):
        p=Path(__file__).resolve().parents[2]/'.github/workflows/okx-swing-v09.yml'
        text=p.read_text();self.assertIn('workflow_dispatch:',text);self.assertIn('contents: read',text)
        for forbidden in ('schedule:','push:','pull_request:','secrets.','render.com','trade/order'):
            self.assertNotIn(forbidden,text)


class IntegratedV09Tests(unittest.TestCase):
    def market(self):
        x=features(candles(24*90),STEPS['1H'])
        x['lo20']=98.;x['hi55']=120.;x['lo55']=90.;x['atr']=1.;x['liquidity']=1e9
        records=list(x.itertuples(index=False))
        signals={s:[(1,1.) if i%48==0 else (0,0.) for i in range(len(x))] for s in STRATEGIES}
        return dict(frame=x,records=records,signals=signals,instrument=dict(INST,tickSz='.1'))
    def test_portfolio_all_strategies_constraints(self):
        m=self.market();markets={s:dict(m,instrument=dict(m['instrument'],instId=s+'-USDT-SWAP',ctValCcy=s)) for s in ('BTC','ETH','SOL','XRP')}
        row,trades,curve=simulate(markets,'ALL',(int(m['frame'].ts.iloc[60]),int(m['frame'].ts.iloc[-1])+STEPS['1H']),STEPS['1H'])
        self.assertLessEqual(row['max_open'],3);self.assertLessEqual(row['max_planned_risk_twd'],2000+1e-7)
        self.assertTrue(all(t['planned_net_rr']>=3 for t in trades));self.assertTrue(all(t['hold_days']<=14 for t in trades))
        self.assertTrue(all(t['book']=='paper_backtest' for t in trades));self.assertTrue(len(curve)>0)
    def test_next_bar_entry(self):
        m=self.market();_,trades,_=simulate({'BTC':m},'MACD_TREND',(int(m['frame'].ts.iloc[60]),int(m['frame'].ts.iloc[-1])+STEPS['1H']),STEPS['1H'])
        self.assertGreater(len(trades),0)
        for t in trades:
            i=int((timestamp(t['entry_time'])-m['frame'].ts.iloc[0])/STEPS['1H'])
            self.assertEqual((i-1)%48,0)
    def test_ledger_reconcile_later(self):
        l=Ledger();l.propose('a','BTC','A',1);l.response('a',True);l.response('a',True,LedgerReportTests().fill())
        self.assertEqual(l.records['a']['state'],'open')
    def test_ledger_persist(self):
        with tempfile.TemporaryDirectory() as d:
            l=Ledger();l.propose('a','BTC','A',1);l.save(Path(d)/'l.json')
            self.assertEqual(Ledger.load(Path(d)/'l.json').records,l.records)
    def test_tick_conservative(self):
        from v09.exits import quantize_plan
        p=quantize_plan(Plan(100.01,98.09,120.09,140.09,1),'.1')
        self.assertEqual((p.entry,p.stop,p.tp1,p.tp2),(100.1,98.,120.,140.))


class StrategyRulesV09Tests(unittest.TestCase):
    def rows(self):
        from types import SimpleNamespace
        r=dict(atr=1.,hi55=110.,ema50=100.,macd=0.,signal=0.,bias=0.,z=0.,close=101.,volume=200.,vol20=100.,hi20=105.,lo20=95.)
        return [SimpleNamespace(**r) for _ in range(60)]
    def test_macd_long_and_short(self):
        x=self.rows();x[-1].macd=1.
        self.assertEqual(signal(x,59,'MACD_TREND')[0],1)
        x[-1].macd=-1.;x[-1].close=99.
        self.assertEqual(signal(x,59,'MACD_TREND')[0],-1)
    def test_bias_long_and_short(self):
        x=self.rows();a=x[-1];a.bias=-.03;a.z=-2.;a.close=102.;a.macd=1.
        self.assertEqual(signal(x,59,'BIAS25_REVERSAL')[0],1)
        a.bias=.03;a.z=2.;a.close=99.;a.macd=-1.
        self.assertEqual(signal(x,59,'BIAS25_REVERSAL')[0],-1)
    def test_range_long_and_short(self):
        x=self.rows();x[-1].close=106.
        self.assertEqual(signal(x,59,'RANGE_VOLUME')[0],1)
        x[-1].close=94.
        self.assertEqual(signal(x,59,'RANGE_VOLUME')[0],-1)
    def test_range_volume_filter(self):
        x=self.rows();x[-1].close=106.;x[-1].volume=100.
        self.assertEqual(signal(x,59,'RANGE_VOLUME')[0],0)



class MoreBoundaryV09Tests(unittest.TestCase):
    def test_partial_entry_basis_and_fees(self):
        l=Ledger();l.propose('x','BTC','A',1);f=LedgerReportTests().fill();l.response('x',True,f)
        second=dict(f,price=110.,units=10.,fee=1.)
        l.add_entry_fill('x',second)
        self.assertEqual(l.records['x']['remaining'],20.)
        self.assertAlmostEqual(l.exit('x',T,120.,20.,2.),296.)
    def test_report_stressed_rr(self):
        p=good_plan();r=candidate('BTC','A',p,evidence([]),{'cross_verified':False},assess([],T,'BTC'),'ALLOW_RESEARCH',500,2.)
        self.assertAlmostEqual(r['costs']['net_rr'],costs(p,2.)['net_rr'])
    def test_unrealized_does_not_raise_cap(self):
        s=State(capital=20000,verified_capital=None)
        self.assertEqual(s.gate('A',1375,T),'SINGLE_RISK_CAP')
    def test_loss_day_ordered_taipei(self):
        s=State(closes=[dict(time='2025-01-01T15:00:00Z',strategy='A',net=-1),dict(time='2025-01-01T15:30:00Z',strategy='B',net=-1)])
        self.assertEqual(s.gate('C',100,'2025-01-01T16:00:00Z'),'ALLOW_RESEARCH')


class ChronologyBoundaryTests(unittest.TestCase):
    def test_future_loss_not_counted(self):
        s=State(closes=[dict(time='2025-01-01T01:00:00Z',strategy='A',net=-1)]*2)
        self.assertEqual(s.gate('B',100,T),'ALLOW_RESEARCH')
    def test_exchange_max_leverage(self):
        with self.assertRaises(ValueError): size(good_plan(),10,32,T,'assumed',dict(INST,lever='5'),1000,10)

if __name__=='__main__': unittest.main()
