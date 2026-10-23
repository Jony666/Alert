"""Hermetic v0.6 portfolio, chronology, costs, risk, and end-to-end tests."""
import json
import math
from argparse import Namespace
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

import turtle_short_v06 as m
import trader_strategies_v04 as prev

BASE_DAY=16*3_600_000


def config(**kw):
    d=dict(symbols=['BTC'],days=730,warmup_days=320,min_covered=1,
           end_utc='2026-10-01T00:00:00Z',fee=.0005,slippage=.0005,funding=.0001,
           max_hold_days=90,pause=0.,cache='UNUSED',output='UNUSED')
    d.update(kw)
    return Namespace(**d)


def one_market(days=4, stop_on_second=True, first_close=100.0, base_px=100.0):
    times=BASE_DAY+np.arange(days*48,dtype=np.int64)*m.MS30
    op=np.full(len(times),base_px+1.)
    hi=np.full(len(times),base_px+1.5)
    lo=np.full(len(times),base_px+0.5)
    close=np.full(len(times),base_px+1.)
    lo[0]=base_px-.5
    close[0]=first_close
    if stop_on_second:hi[1]=base_px+6.
    day=pd.Series({'low20_prev':base_px,'low55_prev':base_px-1,
                   'high10_prev':base_px+3,'high20_prev':base_px+3,
                   'n20_prev':2.,'close_prev':base_px+1,
                   'sma200_prev':base_px+15,'atr_ratio_prev':2/(base_px+1)})
    return {'ts':times,'open':op,'high':hi,'low':lo,'close':close,
            'day_by_bar':[day]*len(times),'bar_day':np.asarray([m.bucket_of(x) for x in times])}


def synthetic_history(days=370):
    n=days*48;ts=np.arange(n,dtype=np.int64)*m.MS30
    di=np.arange(n)//48
    # Alternating trends ensure completed-day breakouts and possible covers.
    val=100+np.sin(di/13)*12+0.07*di
    op=np.r_[val[0],val[:-1]]
    return pd.DataFrame({'ts':ts,'open':op,'high':np.maximum(op,val)+.8,
                         'low':np.minimum(op,val)-.8,'close':val,
                         'volume':np.full(n,1000.)})


class PublicDefinitionTests(unittest.TestCase):
    def test_predeclared_unique_scenarios(self):
        self.assertEqual(len(m.SCENARIOS),len(m.SCENARIO_BY_NAME))
        self.assertGreaterEqual(len(m.SCENARIOS),10)

    def test_baseline_channels(self):
        self.assertEqual((m.SCENARIO_BY_NAME['S1_SHORT_BASE'].entry_days,m.SCENARIO_BY_NAME['S1_SHORT_BASE'].exit_days),(20,10))
        self.assertEqual((m.SCENARIO_BY_NAME['S2_SHORT_BASE'].entry_days,m.SCENARIO_BY_NAME['S2_SHORT_BASE'].exit_days),(55,20))

    def test_original_rule_distinction(self):
        self.assertTrue('original' in m.__doc__.lower())
        self.assertIn('skip',m.__doc__.lower())

    def test_all_scenarios_risk_limited(self):
        for s in m.SCENARIOS:
            self.assertLessEqual(s.risk_fraction,.01)
            self.assertLessEqual(s.port_risk_cap,.02)
            self.assertLessEqual(s.portfolio_gross_cap,2.)

    def test_entry_gap_below_short_channel(self):
        self.assertAlmostEqual(m.entry_quote(90,89,100,.001),89.91)

    def test_entry_no_breakout(self):
        self.assertIsNone(m.entry_quote(101,100.01,100,.0005))

    def test_cover_worse_stop_when_channel_also_hit(self):
        px,why=m.cover_quote(100,110,105,103,.001)
        self.assertAlmostEqual(px,105*1.001)
        self.assertEqual(why,'stop_first_conservative')

    def test_gap_through_stop_not_favorable_price(self):
        self.assertAlmostEqual(m.cover_quote(109,111,105,103,.001)[0],109*1.001)

    def test_no_cover_without_breakout(self):
        self.assertIsNone(m.cover_quote(90,91,105,103,.0005))

    def test_day_anchor_utc_plus_eight(self):
        self.assertEqual(m.bucket_of(BASE_DAY),BASE_DAY)
        self.assertEqual(m.bucket_of(0),BASE_DAY-m.DAY)

    def test_train_validation_test_partition(self):
        a=m.split_bounds(0,730*m.DAY,320)
        self.assertEqual(a['train'][0],320*m.DAY)
        self.assertEqual(a['validation'][0],a['train'][1])
        self.assertEqual(a['test_not_pristine'][0],a['validation'][1])

    def test_risk_sizing_caps(self):
        self.assertEqual(m.SCENARIO_BY_NAME['S1_SHORT_MAX_2_POSITIONS'].max_positions,2)
        self.assertEqual(m.SCENARIO_BY_NAME['S1_SHORT_DOUBLE_COSTS'].cost_mult,2)


class PortfolioTests(unittest.TestCase):
    def test_signal_enters_then_protective_stop(self):
        raw=one_market()
        o=m.simulate_portfolio({'BTC-USDT-SWAP':raw},m.SCENARIO_BY_NAME['S1_SHORT_BASE'],(int(raw['ts'][0]),int(raw['ts'][-1]+m.MS30)),config())
        self.assertEqual(o['counts']['filled_short_entries'],1)
        self.assertEqual(len(o['trades']),1)
        self.assertLess(o['trades'][0]['net_return_pct_of_notional'],0)
        self.assertEqual(o['trades'][0]['exit_reason'],'stop_first_conservative')
        self.assertEqual(o['max_simultaneous_positions'],1)

    def test_same_bar_worst_case_stop(self):
        raw=one_market(stop_on_second=False)
        raw['high'][0]=110
        o=m.simulate_portfolio({'BTC-USDT-SWAP':raw},m.SCENARIO_BY_NAME['S1_SHORT_BASE'],(int(raw['ts'][0]),int(raw['ts'][-1]+m.MS30)),config())
        self.assertEqual(o['trades'][0]['exit_reason'],'same_bar_adverse_stop')
        self.assertLess(o['ending_equity'],1)

    def test_no_lookahead_close_for_position_sizing(self):
        x=one_market(first_close=95,stop_on_second=True)
        y=one_market(first_close=130,stop_on_second=True)
        a=m.simulate_portfolio({'BTC-USDT-SWAP':x},m.SCENARIO_BY_NAME['S1_SHORT_BASE'],(int(x['ts'][0]),int(x['ts'][-1]+m.MS30)),config())
        b=m.simulate_portfolio({'BTC-USDT-SWAP':y},m.SCENARIO_BY_NAME['S1_SHORT_BASE'],(int(y['ts'][0]),int(y['ts'][-1]+m.MS30)),config())
        self.assertAlmostEqual(a['trades'][0]['notional_pct_of_equity_at_start'],b['trades'][0]['notional_pct_of_equity_at_start'])

    def test_slippage_and_fees_included(self):
        x=one_market()
        lo= config(fee=0,slippage=0,funding=0)
        hi= config(fee=.002,slippage=.002,funding=.001)
        a=m.simulate_portfolio({'BTC-USDT-SWAP':x},m.SCENARIO_BY_NAME['S1_SHORT_BASE'],(int(x['ts'][0]),int(x['ts'][-1]+m.MS30)),lo)
        b=m.simulate_portfolio({'BTC-USDT-SWAP':x},m.SCENARIO_BY_NAME['S1_SHORT_BASE'],(int(x['ts'][0]),int(x['ts'][-1]+m.MS30)),hi)
        self.assertLess(b['trades'][0]['net_return_pct_of_notional'],a['trades'][0]['net_return_pct_of_notional'])

    def test_portfolio_concurrent_cap(self):
        x=one_market(stop_on_second=False)
        scenario=replace(m.SCENARIO_BY_NAME['S1_SHORT_BASE'],max_positions=1)
        o=m.simulate_portfolio({'BTC-USDT-SWAP':x,'ETH-USDT-SWAP':one_market(stop_on_second=False)},scenario,(int(x['ts'][0]),int(x['ts'][-1]+m.MS30)),config())
        self.assertEqual(o['counts']['filled_short_entries'],1)
        self.assertGreater(o['counts']['max_positions_rejected'],0)

    def test_no_cross_split_leakage(self):
        x=one_market(stop_on_second=False)
        end=int(x['ts'][48]-m.MS30)
        o=m.simulate_portfolio({'BTC-USDT-SWAP':x},m.SCENARIO_BY_NAME['S1_SHORT_BASE'],(int(x['ts'][0]),end+m.MS30),config())
        self.assertEqual(o['trades'][0]['exit_reason'],'research_split_end_close')
        self.assertEqual(o['trades'][0]['exit_ts'],end+m.MS30)

    def test_bear_filter_precludes_incorrect_regime(self):
        x=one_market()
        d=x['day_by_bar'][0].copy();d['close_prev']=120;d['sma200_prev']=105
        x['day_by_bar']=[d]*len(x['ts'])
        o=m.simulate_portfolio({'BTC-USDT-SWAP':x},m.SCENARIO_BY_NAME['S1_SHORT_BTC_BEAR'],(int(x['ts'][0]),int(x['ts'][-1]+m.MS30)),config())
        self.assertEqual(o['counts'].get('filled_short_entries',0),0)
        self.assertGreater(o['counts']['btc_regime_rejected'],0)

    def test_atr_band_rejects_wrong_volatility(self):
        x=one_market()
        d=x['day_by_bar'][0].copy();d['atr_ratio_prev']=.3
        x['day_by_bar']=[d]*len(x['ts'])
        o=m.simulate_portfolio({'BTC-USDT-SWAP':x},m.SCENARIO_BY_NAME['S1_SHORT_ATR_1_TO_10PCT'],(int(x['ts'][0]),int(x['ts'][-1]+m.MS30)),config())
        self.assertEqual(o['counts'].get('filled_short_entries',0),0)

    def test_mark_to_market_dd_reported(self):
        x=one_market()
        o=m.simulate_portfolio({'BTC-USDT-SWAP':x},m.SCENARIO_BY_NAME['S1_SHORT_BASE'],(int(x['ts'][0]),int(x['ts'][-1]+m.MS30)),config())
        self.assertGreater(o['mark_to_market_max_dd_pct'],0)
        self.assertTrue(all(len(z)==4 for z in o['equity_points']))

    def test_per_split_reset_account(self):
        x=one_market()
        k={'BTC-USDT-SWAP':x};a=int(x['ts'][0]);mid=int(x['ts'][48]);end=int(x['ts'][-1]+m.MS30)
        p=m.SCENARIO_BY_NAME['S1_SHORT_BASE']
        s1=m.simulate_portfolio(k,p,(a,mid),config())
        s2=m.simulate_portfolio(k,p,(mid,end),config())
        self.assertEqual(s1['starting_equity'],1)
        self.assertEqual(s2['starting_equity'],1)

    def test_stats_zero_trade_not_fake_profit(self):
        z=m.trade_stats([])
        self.assertIsNone(z['profit_factor'])
        self.assertEqual(z['trades'],0)

    def test_btc_absent_bear_filter_fails(self):
        x=one_market()
        o=m.simulate_portfolio({'ETH-USDT-SWAP':x},m.SCENARIO_BY_NAME['S1_SHORT_BTC_BEAR'],(int(x['ts'][0]),int(x['ts'][-1]+m.MS30)),config())
        self.assertEqual(o['counts'].get('filled_short_entries',0),0)

    def test_wrong_data_alignment_rejected(self):
        x=one_market();y=one_market();y['ts']=y['ts']+m.MS30
        with self.assertRaises(ValueError):m.simulate_portfolio({'BTC-USDT-SWAP':x,'ETH-USDT-SWAP':y},m.SCENARIO_BY_NAME['S1_SHORT_BASE'],(int(x['ts'][0]),int(x['ts'][-1]+m.MS30)),config())


    def test_s1_short_breakout_and_s2_not_triggered(self):
        x=one_market(stop_on_second=False)
        start=int(x['ts'][0]);end=int(x['ts'][-1]+m.MS30)
        s1=m.simulate_portfolio({'BTC-USDT-SWAP':x},m.SCENARIO_BY_NAME['S1_SHORT_BASE'],(start,end),config())
        s2=m.simulate_portfolio({'BTC-USDT-SWAP':x},m.SCENARIO_BY_NAME['S2_SHORT_BASE'],(start,end),config())
        self.assertEqual(s1['counts']['filled_short_entries'],1)
        self.assertEqual(s2['counts'].get('filled_short_entries',0),0)

    def test_lower_fraction_saves_capital(self):
        x=one_market()
        start=int(x['ts'][0]);end=int(x['ts'][-1]+m.MS30)
        a=m.simulate_portfolio({'BTC-USDT-SWAP':x},m.SCENARIO_BY_NAME['S1_SHORT_BASE'],(start,end),config())
        b=m.simulate_portfolio({'BTC-USDT-SWAP':x},m.SCENARIO_BY_NAME['S1_SHORT_RISK_025'],(start,end),config())
        self.assertGreater(b['ending_equity'],a['ending_equity'])

    def test_large_gap_can_trigger_portfolio_halt(self):
        x=one_market(stop_on_second=False)
        x['open'][1]=400;x['high'][1]=401;x['close'][1]=400
        start=int(x['ts'][0]);end=int(x['ts'][-1]+m.MS30)
        o=m.simulate_portfolio({'BTC-USDT-SWAP':x},m.SCENARIO_BY_NAME['S1_SHORT_DD_HALT_10PCT'],(start,end),config())
        self.assertTrue(o['portfolio_halted'])
        self.assertEqual(o['counts']['portfolio_halt_triggered'],1)

    def test_no_position_reentry_after_same_bar_exit(self):
        x=one_market()
        x['low'][1]=99 # Stop exit and 20d low break again in SAME candle.
        start=int(x['ts'][0]);end=int(x['ts'][-1]+m.MS30)
        o=m.simulate_portfolio({'BTC-USDT-SWAP':x},m.SCENARIO_BY_NAME['S1_SHORT_BASE'],(start,end),config())
        self.assertEqual(o['counts']['filled_short_entries'],1)
        self.assertGreater(o['counts']['ambiguous_same_bar_cash_reuse_prevented'],0)

    def test_smaller_portfolio_stop_risk_budget(self):
        x=one_market(stop_on_second=False)
        start=int(x['ts'][0]);end=int(x['ts'][-1]+m.MS30)
        scenario=replace(m.SCENARIO_BY_NAME['S1_SHORT_BASE'],port_risk_cap=.003)
        o=m.simulate_portfolio({'BTC-USDT-SWAP':x},scenario,(start,end),config())
        self.assertAlmostEqual(o['counts'].get('filled_short_entries',0),1)
        self.assertLessEqual(o['max_simultaneous_stop_risk_units'],.003+1e-8)

    def test_funding_never_credited_to_short(self):
        x=one_market(stop_on_second=False)
        start=int(x['ts'][0]);end=int(x['ts'][-1]+m.MS30)
        a=m.simulate_portfolio({'BTC-USDT-SWAP':x},m.SCENARIO_BY_NAME['S1_SHORT_BASE'],(start,end),config(funding=0))
        b=m.simulate_portfolio({'BTC-USDT-SWAP':x},m.SCENARIO_BY_NAME['S1_SHORT_BASE'],(start,end),config(funding=.001))
        self.assertLess(b['ending_equity'],a['ending_equity'])

    def test_s2_regime_filter_is_independent_scenario(self):
        s=m.SCENARIO_BY_NAME['S2_SHORT_BTC_BEAR']
        self.assertEqual((s.entry_days,s.exit_days,s.market_filter),(55,20,'btc_below_sma200'))



class HistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data=synthetic_history()

    def test_previous_day_indicator_no_future_leak(self):
        a=m.prepare_market(self.data)
        day=a['day']
        self.assertAlmostEqual(day.low20_prev.iloc[250],float(day.low.iloc[230:250].min()))
        # Corrupt TODAY high/low: previous channels cannot change.
        altered=self.data.copy();mask=altered.ts.ge(int(day.ts.iloc[250])) & altered.ts.lt(int(day.ts.iloc[250])+m.DAY)
        altered.loc[mask,'low']=.01
        b=m.prepare_market(altered)
        self.assertEqual(float(a['day'].low20_prev.iloc[250]),float(b['day'].low20_prev.iloc[250]))

    def test_strict_missing_candle_rejected(self):
        d=self.data.drop(index=200)
        with self.assertRaises(ValueError):m.prepare_market(d)

    def test_strict_total_coverage_rejected(self):
        with TemporaryDirectory() as temp:
            args=config(days=730,min_covered=1,cache=temp,output=str(Path(temp)/'out.json'))
            def fake(inst,start,end,pause):
                ts=np.arange(start,end,m.MS30,dtype=np.int64)
                ts=ts[1:]
                return pd.DataFrame({'ts':ts,'open':100.,'high':101.,'low':99.,'close':100.,'volume':1.})
            x,fail,c=m.load_data(args,fake)
            self.assertFalse(x);self.assertTrue(fail)
            self.assertFalse(c)

    def test_synthetic_end_to_end_files_written_and_not_live(self):
        # Patch the scenario list to two in a hermetic integration test. Longer
        # real 12-symbol/1095d execution belongs to GitHub Actions, not mock QA.
        from datetime import timedelta
        from datetime import datetime, timezone
        d=synthetic_history(550)
        dt=datetime(2026,10,1,tzinfo=timezone.utc)
        since=int(dt.timestamp()*1000)-550*m.DAY
        def offline(inst,start,end,pause):
            assert start==since
            result=d.copy();result['ts']=result.ts+start
            return result
        with TemporaryDirectory() as temp:
            args=config(days=550,min_covered=1,cache=str(Path(temp)/'cache'),output=str(Path(temp)/'out.json'),symbols=['BTC'])
            with patch.object(m,'SCENARIOS',(m.SCENARIO_BY_NAME['S1_SHORT_BASE'],m.SCENARIO_BY_NAME['S2_SHORT_BASE'])):
                status=m.run(args,loader=offline)
            self.assertEqual(status,0)
            j=json.loads(Path(args.output).read_text())
            self.assertFalse(j['ready_for_live'])
            self.assertEqual(len(j['results']),6)
            for ext in ('.summary.csv','.trades.csv','.diagnostic.csv','.equity.csv'):
                self.assertTrue(Path(args.output).with_suffix(ext).exists())

    def test_reject_future_end(self):
        with self.assertRaises(ValueError):m.run(config(end_utc='2099-01-01T00:00:00Z'))

    def test_reject_inadequate_eval_length(self):
        with self.assertRaises(ValueError):m.run(config(days=400,warmup_days=320))


if __name__=='__main__':unittest.main()
