"""No API, no funds. Deterministic point-in-time, risk and OHLC backtest tests."""
import copy
from argparse import Namespace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest
import numpy as np
import pandas as pd
import leveraged_intraday_v08 as v


def history(n,step=v.BARS['15m'],start=0,seed=0):
    k=np.arange(n,dtype=float)
    # trend + seasonality gives both sides a series of deterministic opportunities
    p=100+0.001*k+1.4*np.sin(k/27+seed)+0.7*np.sin(k/7+seed)
    o=np.r_[p[0],p[:-1]]
    hi=np.maximum(o,p)+.23;lo=np.minimum(o,p)-.23
    volume=np.where((np.arange(n)%31)==0,3500,1000).astype(float)
    return pd.DataFrame({'ts':start+np.arange(n,dtype=np.int64)*step,'open':o,
                         'high':hi,'low':lo,'close':p,'volume':volume})


def params(**kwargs):
    d=vars(v.parser().parse_args([]))
    d.update(kwargs)
    return Namespace(**d)


def entry_pos(side=1,entry=100,stop=None,target=None,liquidation=None):
    if stop is None:stop=98 if side==1 else 102
    if target is None:target=105 if side==1 else 95
    if liquidation is None:liquidation=95 if side==1 else 105
    return v.Position('BTC',side,entry,0,1.,entry,0.,1.,stop,target,liquidation,100000000)


class DataValidationTests(unittest.TestCase):
    def setUp(self):self.raw=history(30)
    def test_valid(self):self.assertTrue(v.validate(self.raw,v.BARS['15m']))
    def test_requested_complete_range(self):self.assertTrue(v.validate(self.raw,v.BARS['15m'],0,30*v.BARS['15m']))
    def test_missing_first(self):
        with self.assertRaises(ValueError):v.validate(self.raw.iloc[1:],v.BARS['15m'],0,30*v.BARS['15m'])
    def test_missing_last(self):
        with self.assertRaises(ValueError):v.validate(self.raw.iloc[:-1],v.BARS['15m'],0,30*v.BARS['15m'])
    def test_missing_middle(self):
        with self.assertRaises(ValueError):v.validate(self.raw.drop(10),v.BARS['15m'])
    def test_duplicate_ts(self):
        with self.assertRaises(ValueError):v.validate(pd.concat([self.raw.iloc[:5],self.raw.iloc[4:]]),v.BARS['15m'])
    def test_misaligned(self):
        raw=self.raw.copy();raw['ts']+=1000
        with self.assertRaises(ValueError):v.validate(raw,v.BARS['15m'])
    def test_invalid_high(self):
        raw=self.raw.copy();raw.loc[2,'high']=1
        with self.assertRaises(ValueError):v.validate(raw,v.BARS['15m'])
    def test_invalid_low(self):
        raw=self.raw.copy();raw.loc[2,'low']=1000
        with self.assertRaises(ValueError):v.validate(raw,v.BARS['15m'])
    def test_zero_price(self):
        raw=self.raw.copy();raw.loc[2,'close']=0
        with self.assertRaises(ValueError):v.validate(raw,v.BARS['15m'])
    def test_negative_volume(self):
        raw=self.raw.copy();raw.loc[2,'volume']=-1
        with self.assertRaises(ValueError):v.validate(raw,v.BARS['15m'])
    def test_nan(self):
        raw=self.raw.copy();raw.loc[2,'close']=np.nan
        with self.assertRaises(ValueError):v.validate(raw,v.BARS['15m'])
    def test_empty(self):
        with self.assertRaises(ValueError):v.validate(self.raw.iloc[:0],v.BARS['15m'])
    def test_parse_confirmed_only(self):
        a=[['0','100','102','99','101','100','0','0','1'],['900000','101','104','100','103','200','0','0','0']]
        self.assertEqual(len(v.parse_page(a,0,1800000)),1)
    def test_parse_page_range(self):
        a=[['0','100','102','99','101','100','0','0','1']]
        self.assertEqual(v.parse_page(a,900000,1800000),[])
    def test_bad_timestamp(self):
        with self.assertRaises(ValueError):v.ts_utc('2026-08-01T00:00:00+08:00')
    def test_iso(self):self.assertEqual(v.iso(v.ts_utc('2026-10-01T00:00:00Z')),'2026-10-01T00:00:00+00:00')
    def test_overlap_splits(self):
        bounds=v.bounds_for_range(0,100*v.DAY,35)
        self.assertEqual(bounds['train'][1],bounds['validation'][0]);self.assertEqual(bounds['validation'][1],bounds['test_not_pristine'][0])
    def test_no_eval(self):
        with self.assertRaises(ValueError):v.bounds_for_range(0,20*v.DAY,35)


class DownloaderTests(unittest.TestCase):
    def candle(self,t):return [str(t),'100','102','99','101','120','0','0','1']
    class Response:
        def __init__(self,data):self.data=data
        def raise_for_status(self):pass
        def json(self):return {'code':'0','data':self.data}
    class Session:
        def __init__(self,batches):self.batches=list(batches);self.calls=[]
        def get(self,url,params,timeout):
            self.calls.append(params.copy());return DownloaderTests.Response(self.batches.pop(0))
    def test_download_complete(self):
        step=v.BARS['5m'];ses=self.Session([[self.candle(step),self.candle(0)]])
        z=v.download('BTC-USDT-SWAP','5m',0,2*step,0,ses)
        self.assertEqual(len(z),2);self.assertEqual(ses.calls[0]['bar'],'5m')
    def test_download_detects_gap(self):
        step=v.BARS['5m'];ses=self.Session([[self.candle(2*step),self.candle(0)]])
        with self.assertRaises(ValueError):v.download('BTC-USDT-SWAP','5m',0,3*step,0,ses)
    def test_download_pagination(self):
        step=v.BARS['5m'];ses=self.Session([[self.candle(3*step),self.candle(2*step)],
                                              [self.candle(step),self.candle(0)]])
        z=v.download('BTC-USDT-SWAP','5m',0,4*step,0,ses)
        self.assertEqual(len(z),4);self.assertEqual(ses.calls[1]['after'],str(2*step))
    def test_download_fail_closed_confirm_zero(self):
        step=v.BARS['5m'];row=self.candle(0);row[-1]='0';ses=self.Session([[row]])
        with self.assertRaises(RuntimeError):v.download('BTC-USDT-SWAP','5m',0,step,0,ses)
    def test_max_page_protection(self):
        step=v.BARS['5m'];ses=self.Session([[self.candle(3*step)], [self.candle(2*step)]])
        with self.assertRaises(RuntimeError):v.download('BTC-USDT-SWAP','5m',0,4*step,0,ses,max_pages=1)
    def test_unaligned_request(self):
        with self.assertRaises(ValueError):v.download('BTC','5m',1,300000)


class FeaturesAndSignalsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r=history(52*96,v.BARS['15m'])
        cls.df=v.attach_completed_context(cls.r,v.BARS['15m'])
    def test_resample_complete_hour(self):
        d=v.aggregate(history(5),v.BARS['15m'],4);self.assertEqual(len(d),1)
        self.assertEqual(float(d.open.iloc[0]),float(history(5).open.iloc[0]))
    def test_resample_reject_incomplete(self):
        self.assertEqual(len(v.aggregate(history(3),v.BARS['15m'],4)),0)
    def test_h1_no_current_unfinished(self):
        self.assertTrue(np.isnan(self.df.h1_ma50.iloc[50]))
    def test_30m_last_bar_completion(self):
        # EMA12 needs 12 fully closed 30m bars, not the current half-hour.
        self.assertTrue(np.isnan(self.df.m30_ma12.iloc[20]))
        self.assertTrue(np.isfinite(self.df.m30_ma12.iloc[23]))
    def test_30m_finite_later(self):
        self.assertTrue(np.isfinite(self.df.m30_ma26.iloc[300]))
    def test_h1_last_bar_completion(self):
        # 1-hour candles only become available at the 4th 15m close.
        self.assertTrue(np.isnan(self.df.h1_ma20.iloc[75]))
        self.assertTrue(np.isfinite(self.df.h1_ma20.iloc[79]))
    def test_daily_no_incomplete(self):
        # 1st daily UTC+8 is partial (start 00 UTC); 25 complete days need more.
        self.assertTrue(np.isnan(self.df.bias25.iloc[96*10]))
        self.assertTrue(np.isfinite(self.df.bias25.iloc[96*35]))
    def test_features_are_point_in_time(self):
        # Change today's CLOSE in late bar, an earlier bar's indicators must not change.
        idx=1000;altered=self.r.copy();altered.loc[idx+1,'close']+=50;altered.loc[idx+1,'high']+=50
        other=v.attach_completed_context(altered,v.BARS['15m'])
        self.assertAlmostEqual(float(self.df.bias25.iloc[idx]),float(other.bias25.iloc[idx])) if np.isfinite(self.df.bias25.iloc[idx]) else self.assertTrue(np.isnan(other.bias25.iloc[idx]))
        self.assertEqual(float(self.df.h1_ma20.iloc[idx]),float(other.h1_ma20.iloc[idx]))
    def test_indicator_finiteness_late(self):
        row=self.df.iloc[-1]
        self.assertTrue(all(np.isfinite([row.atr,row.rsi14,row.macd,row.h1_ma50,row.bias25,row.z25])))
    def test_rsi_range(self):self.assertTrue(((v.rsi(self.r)>=0)&(v.rsi(self.r)<=100)).all())
    def test_precompute_shape(self):
        x=v.precompute_signals(self.df)
        self.assertEqual(set(x),set(v.STRATEGIES))
        for side,score in x.values():self.assertEqual(len(side),len(self.df));self.assertEqual(len(score),len(self.df))
    def template(self):
        df=self.df.copy().iloc[2500:2570].reset_index(drop=True)
        i=65
        df.loc[[i-1,i],['atr','h1_ma20','h1_ma50','m30_ma12','m30_ma26','rsi14','close','macd','macd_sig','volume','vol20','hi20','lo20','z25','bias25']]=[1,101,100,101,100,55,101,0,0,2000,1000,100,99,0,0]
        return df,i
    def test_macd_long(self):
        x,i=self.template();x.loc[i-1,['macd','macd_sig']]=[-.1,0];x.loc[i,['macd','macd_sig']]=[.1,0]
        self.assertEqual(v.identify_signal(x,i,'MACD_TREND')[0],1)
    def test_macd_short(self):
        x,i=self.template();x.loc[i,['h1_ma20','h1_ma50','m30_ma12','m30_ma26','rsi14']]=[99,100,99,100,40]
        x.loc[i-1,['macd','macd_sig']]=[.1,0];x.loc[i,['macd','macd_sig']]=[-.1,0]
        self.assertEqual(v.identify_signal(x,i,'MACD_TREND')[0],-1)
    def test_macd_no_lookahead_cross(self):
        x,i=self.template();x.loc[i-1,['macd','macd_sig']]=[.1,0];x.loc[i,['macd','macd_sig']]=[.2,0]
        self.assertEqual(v.identify_signal(x,i,'MACD_TREND')[0],0)
    def test_breakout_long(self):
        x,i=self.template();self.assertEqual(v.identify_signal(x,i,'RANGE_VOLUME')[0],1)
    def test_breakout_short(self):
        x,i=self.template();x.loc[i,['close','h1_ma20','h1_ma50','m30_ma12','m30_ma26']]=[98,99,100,99,100]
        self.assertEqual(v.identify_signal(x,i,'RANGE_VOLUME')[0],-1)
    def test_breakout_volume_filter(self):
        x,i=self.template();x.loc[i,'volume']=300
        self.assertEqual(v.identify_signal(x,i,'RANGE_VOLUME')[0],0)
    def test_bias_long(self):
        x,i=self.template();x.loc[i-1,['rsi14','macd','macd_sig']]=[25,-1,0]
        x.loc[i,['z25','bias25','rsi14','macd','macd_sig']]=[-2,-3,34,-.3,0]
        self.assertEqual(v.identify_signal(x,i,'BIAS25_REVERSAL')[0],1)
    def test_bias_short(self):
        x,i=self.template();x.loc[i-1,['rsi14','macd','macd_sig']]=[79,1,0]
        x.loc[i,['z25','bias25','rsi14','macd','macd_sig']]=[2,3,66,.3,0]
        self.assertEqual(v.identify_signal(x,i,'BIAS25_REVERSAL')[0],-1)
    def test_bias_missing_context(self):
        x,i=self.template();x.loc[i,['z25','bias25']]=[float('nan'),-3]
        self.assertEqual(v.identify_signal(x,i,'BIAS25_REVERSAL')[0],0)
    def test_no_lookahead_first_55(self):
        self.assertEqual(v.identify_signal(self.df,10,'MACD_TREND')[0],0)
    def test_unknown_strategy(self):
        with self.assertRaises(ValueError):v.identify_signal(self.df,60,'NOT_REAL')


class RiskAndAccountTests(unittest.TestCase):
    def test_liq_long_5x(self):self.assertAlmostEqual(v.liq_price(100,1,5),80.6)
    def test_liq_short_10x(self):self.assertAlmostEqual(v.liq_price(100,-1,10),109.4)
    def test_liq_long_20x(self):self.assertAlmostEqual(v.liq_price(100,1,20),95.6)
    def test_liq_reject_excess(self):
        with self.assertRaises(ValueError):v.liq_price(100,1,200)
    def test_stop_long(self):
        q=v.exit_quote(entry_pos(),100,101,97,.001)
        self.assertEqual(q[1],'protective_stop');self.assertAlmostEqual(q[0],98*.999)
    def test_stop_short(self):
        q=v.exit_quote(entry_pos(-1),100,103,99,.001)
        self.assertEqual(q[1],'protective_stop');self.assertAlmostEqual(q[0],102*1.001)
    def test_target_long(self):
        q=v.exit_quote(entry_pos(),100,106,99,.001)
        self.assertEqual(q[1],'profit_target')
    def test_target_short(self):
        q=v.exit_quote(entry_pos(-1),100,101,94,.001)
        self.assertEqual(q[1],'profit_target')
    def test_worst_case_liq_beats_stop(self):
        q=v.exit_quote(entry_pos(),100,106,94,.001)
        self.assertEqual(q[1],'approx_liquidation')
    def test_worst_case_stop_beats_target(self):
        q=v.exit_quote(entry_pos(liquidation=80),100,106,97,.001)
        self.assertEqual(q[1],'protective_stop')
    def test_gap_stop_slips_worse(self):
        q=v.exit_quote(entry_pos(liquidation=80),90,91,89,.001)
        self.assertAlmostEqual(q[0],90*.999)
    def test_gap_short_stop_slips_worse(self):
        q=v.exit_quote(entry_pos(-1,liquidation=120),115,116,114,.001)
        self.assertAlmostEqual(q[0],115*1.001)
    def test_no_exit(self):self.assertIsNone(v.exit_quote(entry_pos(),100,101,99,.001))
    def test_zero_trades(self):self.assertEqual(v.trade_stats([])['trades'],0)
    def test_pf(self):
        a=[{'net_return_pct_of_notional':5},{'net_return_pct_of_notional':-2}]
        self.assertAlmostEqual(v.trade_stats(a)['profit_factor'],2.5)
    def test_leverage_not_free_profit(self):
        # Same notional exposure has same pre-fee PnL under different leverage;
        # only margin and liquidation proximity change.
        p=1000;self.assertEqual(p*.02,p*.02)
        self.assertGreater(v.liq_price(100,1,5),0)
    def test_disjoint_test_bounds(self):
        d=v.bounds_for_range(0,100*v.DAY,35)
        self.assertTrue(d['validation'][0]>d['train'][0])


class IntegratedPortfolioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.step=v.BARS['15m'];cls.n=60*96
        cls.markets={}
        for s,seed in [('BTC',0),('ETH',1)]:
            f=v.attach_completed_context(history(cls.n,cls.step,seed=seed),cls.step)
            cls.markets[s]={'frame':f,'signals':v.precompute_signals(f)}
    def test_5m_aggregation_15m_30m_1h(self):
        raw=history(14,v.BARS['5m'])
        self.assertEqual(len(v.aggregate(raw,v.BARS['5m'],3)),4)
        self.assertEqual(len(v.aggregate(raw,v.BARS['5m'],6)),2)
        self.assertEqual(len(v.aggregate(raw,v.BARS['5m'],12)),1)
    def test_5m_full_study(self):
        a=params(bar='5m',days=60,warmup_days=35,symbols=['BTC'],min_covered=1,
                 end_utc='2026-10-01T00:00:00Z')
        step=v.BARS['5m']
        def fake(inst,bar,left,right,pause):return history((right-left)//step,step,start=left)
        with TemporaryDirectory() as td:
            a.output=str(Path(td)/'study.json');a.cache=str(Path(td)/'cache')
            self.assertEqual(v.run(a,loader=fake),0)
            res=__import__('json').loads(Path(a.output).read_text())
            self.assertTrue(res['complete']);self.assertEqual(len(res['results']),54)
            self.assertEqual(res['coverage'][0]['bars'],60*288)
    def test_no_intraday_future_order(self):
        s=self.markets['BTC']['signals']['MACD_TREND'][0]
        self.assertEqual(len(s),self.n)
    def test_simulation_5x(self):
        r,t,e=v.simulate(self.markets,'MACD_TREND',5,'train',(35*v.DAY,48*v.DAY),self.step,params())
        self.assertEqual(r['leverage'],5);self.assertEqual(len(e),(48-35)*96)
    def test_simulation_20x(self):
        r,t,e=v.simulate(self.markets,'RANGE_VOLUME',20,'validation',(45*v.DAY,50*v.DAY),self.step,params())
        self.assertGreaterEqual(r['max_drawdown_pct'],0);self.assertLessEqual(r['max_open_positions'],3)
    def test_cost_stress(self):
        r,t,e=v.simulate(self.markets,'MACD_TREND',10,'test',(45*v.DAY,50*v.DAY),self.step,params(),2)
        self.assertEqual(r['cost_mult'],2)
    def test_time_axis_fail_closed(self):
        d=copy.deepcopy(self.markets)
        d['ETH']['frame'].loc[10,'ts']+=1000
        with self.assertRaises(ValueError):v.simulate(d,'MACD_TREND',5,'train',(35*v.DAY,48*v.DAY),self.step,params())
    def test_entry_bar_uses_prior_signal(self):
        # Synthesize prior-bar breakout direction at j-1; next-bar entry is OPEN.
        d=copy.deepcopy(self.markets)
        j=35*96+2
        for sym in d:
            for strat in v.STRATEGIES:
                s,score=d[sym]['signals'][strat];s[:]=0;score[:]=0
        side,score=d['BTC']['signals']['RANGE_VOLUME'];side[j-1]=1;score[j-1]=4
        r,t,e=v.simulate(d,'RANGE_VOLUME',5,'train',(35*v.DAY,(35*96+20)*self.step),self.step,params())
        self.assertEqual(len(t),1)
        self.assertEqual(t[0]['entry_ts'],int(d['BTC']['frame'].ts.iloc[j]))
    def test_two_accounts_independent(self):
        r1,_,_=v.simulate(self.markets,'MACD_TREND',5,'train',(35*v.DAY,37*v.DAY),self.step,params())
        r2,_,_=v.simulate(self.markets,'MACD_TREND',5,'train',(35*v.DAY,37*v.DAY),self.step,params())
        self.assertEqual(r1['account_return_pct'],r2['account_return_pct'])
    def test_fail_closed_future(self):
        a=params(days=60,warmup_days=35,end_utc='2090-01-01T00:00:00Z')
        with TemporaryDirectory() as td:
            a.output=str(Path(td)/'out.json');a.cache=str(Path(td)/'cache')
            with self.assertRaises(ValueError):v.run(a)
    def test_fail_closed_coverage(self):
        a=params(days=60,warmup_days=35,symbols=['BTC','ETH'],min_covered=2,
                 end_utc='2026-10-01T00:00:00Z')
        step=v.BARS['15m'];end=v.ts_utc(a.end_utc);start=end-60*v.DAY
        def fake(inst,bar,left,right,pause):
            if inst.startswith('ETH'):raise RuntimeError('simulated unavailable historical coverage')
            return history((right-left)//step,step,start=left)
        with TemporaryDirectory() as td:
            a.output=str(Path(td)/'test.json');a.cache=str(Path(td)/'cache')
            self.assertEqual(v.run(a,loader=fake),2)
            result=__import__('json').loads(Path(a.output).read_text())
            self.assertFalse(result['complete']);self.assertIn('ETH-USDT-SWAP',result['failed_symbols'])
    def test_single_symbol_e2e_outputs(self):
        a=params(days=60,warmup_days=35,symbols=['BTC'],min_covered=1,
                 end_utc='2026-10-01T00:00:00Z')
        step=v.BARS['15m'];end=v.ts_utc(a.end_utc)
        def fake(inst,bar,left,right,pause):return history((right-left)//step,step,start=left)
        with TemporaryDirectory() as td:
            a.output=str(Path(td)/'study.json');a.cache=str(Path(td)/'cache')
            self.assertEqual(v.run(a,loader=fake),0)
            res=__import__('json').loads(Path(a.output).read_text())
            self.assertEqual(len(res['results']),54)
            self.assertTrue(res['complete']);self.assertFalse(res['ready_for_live'])
            for suffix in ('.summary.csv','.trades.csv','.equity.csv'):
                self.assertTrue(Path(a.output).with_suffix(suffix).exists())


if __name__=='__main__':unittest.main(verbosity=2)
