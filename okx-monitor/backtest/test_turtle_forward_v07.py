"""Hermetic tests for future-only state, provenance, and fail-closed design."""
import copy, io, json, math, tempfile, unittest, zipfile
from pathlib import Path
from unittest.mock import patch
import turtle_forward_v07 as m


class PaperV07Tests(unittest.TestCase):
    def setUp(self):
        self.symbols=('BTC','ETH')
        self.days=self.make_daily()
        self.now=350*m.DAY+8*60_000
        self.target=(self.now//m.MS30)*m.MS30-m.MS30
        self.cost={'fee':.0005,'slip':.0005,'funding':.0001}

    def make_daily(self,n=360,bear=True):
        x=[]
        for i in range(n):
            cl=(1000-.6*i if bear else 1000+.6*i)
            x.append({'ts':-m.TPE+i*m.DAY,'o':cl+1,'h':cl+8,'l':cl-8,'c':cl})
        return {s:copy.deepcopy(x) for s in ('BTC','ETH')}

    def candle(self,ts,o=800,h=803,l=797,c=801):
        return {'ts':ts,'o':o,'h':h,'l':l,'c':c}

    def bars(self,ts,o=800,h=803,l=797,c=801):
        return {s:{ts:self.candle(ts,o,h,l,c)} for s in self.symbols}

    def initial(self):
        return m.paper_step(None,{},self.days,self.now,self.symbols,self.cost)[0]

    def validate_daily_hindsight(self):
        point=self.now
        a=m.daily_features(self.days['BTC'],point)
        future={'ts':-m.TPE+360*m.DAY,'o':10,'h':1e8,'l':1,'c':.01}
        b=m.daily_features(self.days['BTC']+[future],point)
        self.assertEqual(a,b)

    def test_first_start_no_historical_trades(self):
        s=self.initial()
        self.assertEqual(s['last_bar_ts'],self.target)
        for a in s['accounts'].values():
            self.assertEqual(a['trades'],[])
            self.assertEqual(a['equity'],[])
            self.assertTrue(a['pending'])

    def test_bootstrap_unseen_future_bar_only(self):
        s=self.initial()
        for a in s['accounts'].values():
            for t,orders in a['pending'].items():
                self.assertGreater(int(t),self.now)
                self.assertEqual(orders['BTC']['prepared_for'],int(t))

    def test_btc_bear_gate(self):
        day=self.make_daily(bear=False)
        s=m.paper_step(None,{},day,self.now,self.symbols,self.cost)[0]
        self.assertFalse(s['accounts']['S1_BTC_BEAR']['pending'])
        self.assertFalse(s['accounts']['S2_BTC_BEAR']['pending'])

    def test_s1_s2_both_active(self):
        s=self.initial()
        self.assertEqual(set(s['accounts']),set(m.SYSTEMS))

    def test_future_pending_not_deleted_on_intermediate_scan(self):
        s=self.initial()
        old=list(s['accounts']['S1_BTC_BEAR']['pending'])
        nxt=self.now+30*60_000
        t=self.target+m.MS30
        s2,info=m.paper_step(s,self.bars(t),self.days,nxt,self.symbols,self.cost)
        self.assertEqual(info['advanced_bars'],1)
        self.assertTrue(all(k in s2['accounts']['S1_BTC_BEAR']['pending'] for k in old))

    def test_live_entry_after_future_order(self):
        s=self.initial()
        future=int(next(iter(s['accounts']['S1_BTC_BEAR']['pending'])))
        lower=s['accounts']['S1_BTC_BEAR']['pending'][str(future)]['BTC']['trigger']
        t1=self.target+m.MS30
        s,_=m.paper_step(s,self.bars(t1),self.days,self.now+30*60_000,self.symbols,self.cost)
        # Next scan observes only the previously armed future candle.
        d=self.bars(future,lower+2,lower+4,lower-1,lower)
        for sy in ('BTC','ETH'):
            d[sy][t1]=self.candle(t1)
        s,_=m.paper_step(s,d,self.days,self.now+60*60_000,self.symbols,self.cost)
        self.assertIn('BTC',s['accounts']['S1_BTC_BEAR']['positions'])
        self.assertGreater(s['accounts']['S1_BTC_BEAR']['cash'],0)

    def test_missing_unarmed_bar_does_not_create_historical_entry(self):
        s=self.initial()
        t=self.target+m.MS30
        lower=s['accounts']['S1_BTC_BEAR']['pending'][str(t+m.MS30)]['BTC']['trigger']
        d=self.bars(t,lower+3,lower+4,lower-2,lower)
        s,_=m.paper_step(s,d,self.days,self.now+30*60_000,self.symbols,self.cost)
        self.assertEqual(len(s['accounts']['S1_BTC_BEAR']['positions']),0)

    def test_replay_same_bar_not_duplicate_trade(self):
        s=self.initial()
        old=copy.deepcopy(s)
        s,info=m.paper_step(s,{},self.days,self.now,self.symbols,self.cost)
        self.assertEqual(info['advanced_bars'],0)
        self.assertEqual(s['accounts']['S1_BTC_BEAR']['orders_armed'],old['accounts']['S1_BTC_BEAR']['orders_armed'])

    def test_catchup_missing_bar_fails_closed(self):
        s=self.initial()
        with self.assertRaisesRegex(RuntimeError,'missing completed'):
            m.paper_step(s,{},self.days,self.now+30*60_000,self.symbols,self.cost)

    def test_too_many_missed_bars_rejected(self):
        s=self.initial()
        with self.assertRaisesRegex(RuntimeError,'too many'):
            m.paper_step(s,{},self.days,self.now+104*m.MS30,self.symbols,self.cost)

    def test_clock_rollback_rejected(self):
        s=self.initial()
        with self.assertRaisesRegex(RuntimeError,'clock moved backwards'):
            m.paper_step(s,{},self.days,self.now-40*60_000,self.symbols,self.cost)

    def test_corrupt_sha_rejected(self):
        s=self.initial();s['accounts']['S1_BTC_BEAR']['cash']=1
        with self.assertRaisesRegex(ValueError,'integrity failure'):
            m.paper_step(s,{},self.days,self.now,self.symbols,self.cost)

    def test_universe_swap_rejected(self):
        s=self.initial()
        with self.assertRaisesRegex(ValueError,'universe changed'):
            m.paper_step(s,{},self.days,self.now,self.symbols[::-1],self.cost)

    def test_bad_state_version_rejected(self):
        s=self.initial();s['version']=2;m.seal(s)
        with self.assertRaisesRegex(ValueError,'version mismatch'):
            m.paper_step(s,{},self.days,self.now,self.symbols,self.cost)

    def test_no_current_day_data_lookahead(self):self.validate_daily_hindsight()

    def test_200_day_average_history(self):
        d=self.days['BTC']
        f=m.daily_features(d,self.now)
        allowed=[x['c'] for x in d if x['ts']+m.DAY<=self.now]
        self.assertAlmostEqual(f['sma200'],sum(allowed[-200:])/200)

    def test_20_day_low_and_55_day_low(self):
        f=m.daily_features(self.days['BTC'],self.now)
        allowed=[x for x in self.days['BTC'] if x['ts']+m.DAY<=self.now]
        self.assertEqual(f['low20'],min(x['l'] for x in allowed[-20:]))
        self.assertEqual(f['low55'],min(x['l'] for x in allowed[-55:]))

    def test_different_system_channel_periods(self):
        self.assertEqual(m.SYSTEMS['S1_BTC_BEAR'],{'entry':20,'exit':10})
        self.assertEqual(m.SYSTEMS['S2_BTC_BEAR'],{'entry':55,'exit':20})

    def test_empty_daily_fails_closed(self):
        with self.assertRaisesRegex(ValueError,'insufficient 225'):
            m.paper_step(None,{}, {'BTC':[],'ETH':[]},self.now,self.symbols,self.cost)

    def test_cost_validation(self):
        with self.assertRaises(ValueError):
            m.paper_step(None,{},self.days,self.now,self.symbols,{'fee':-.01,'slip':0,'funding':0})

    def test_btc_required(self):
        with self.assertRaisesRegex(ValueError,'BTC required'):
            m.paper_step(None,{},self.days,self.now,('ETH',),self.cost)

    def test_confirm_flag_excludes_current_bar(self):
        rows=[['0','10','11','9','10','1','1','1','1'],[str(m.MS30),'10','11','9','10','1','1','1','0']]
        self.assertEqual(len(m.parse_ohlc(rows,m.MS30)),1)

    def test_gap_rejected(self):
        b=[self.candle(0),self.candle(2*m.MS30)]
        with self.assertRaisesRegex(ValueError,'missing or duplicated'):
            m.check_bars(b,m.MS30)

    def test_daily_timezone_anchor(self):
        m.check_bars([self.days['BTC'][1]],m.DAY)
        with self.assertRaises(ValueError):
            m.check_bars([self.candle(0)],m.DAY)

    def test_same_bar_entry_stop_worst_fill(self):
        s=self.initial()
        future=int(next(iter(s['accounts']['S1_BTC_BEAR']['pending'])))
        pending=s['accounts']['S1_BTC_BEAR']['pending'][str(future)]['BTC']
        trigger=pending['trigger']
        t1=self.target+m.MS30
        s,_=m.paper_step(s,self.bars(t1),self.days,self.now+30*60_000,self.symbols,self.cost)
        d=self.bars(future,trigger+2,trigger+100,trigger-1,trigger+20)
        s,_=m.paper_step(s,d,self.days,self.now+60*60_000,self.symbols,self.cost)
        a=s['accounts']['S1_BTC_BEAR']
        self.assertFalse(a['positions'])
        self.assertTrue(any(x['reason']=='same_bar_adverse_stop' for x in a['trades']))

    def test_funding_reduces_cash(self):
        s=self.initial();a=s['accounts']['S1_BTC_BEAR']
        a['positions']['BTC']={'entry':800,'stop':900,'qty':1.,'notional':800.,'entry_ts':self.target-3*m.H8,
                                'entry_fee':.4,'funding_paid':0.,'next_funding_ts':self.target}
        m.seal(s)
        t=self.target+m.MS30
        d=self.bars(t,750,780,740,750)
        s,_=m.paper_step(s,d,self.days,self.now+30*60_000,self.symbols,self.cost)
        self.assertGreater(s['accounts']['S1_BTC_BEAR']['positions']['BTC']['funding_paid'],0)

    def test_gap_stop_price_conservative(self):
        s=self.initial();a=s['accounts']['S1_BTC_BEAR']
        a['positions']['BTC']={'entry':800,'stop':900,'qty':1.,'notional':800.,'entry_ts':self.target,
                                'entry_fee':.4,'funding_paid':0.,'next_funding_ts':self.target+m.H8}
        m.seal(s)
        t=self.target+m.MS30
        d=self.bars(t,950,960,945,952)
        s,_=m.paper_step(s,d,self.days,self.now+30*60_000,self.symbols,self.cost)
        trade=s['accounts']['S1_BTC_BEAR']['trades'][0]
        self.assertGreaterEqual(trade['exit'],950)
        self.assertLess(trade['net_pnl_usdt'],0)

    def test_drawdown_halt_next_bar(self):
        s=self.initial();a=s['accounts']['S1_BTC_BEAR']
        a['positions']['BTC']={'entry':800,'stop':1800,'qty':3.,'notional':2400.,'entry_ts':self.target,
                                'entry_fee':1.2,'funding_paid':0.,'next_funding_ts':self.target+m.H8}
        m.seal(s)
        t=self.target+m.MS30
        d=self.bars(t,1500,1600,1499,1520)
        s,_=m.paper_step(s,d,self.days,self.now+30*60_000,self.symbols,self.cost)
        self.assertTrue(s['accounts']['S1_BTC_BEAR']['halt_next'])
        t2=t+m.MS30;d=self.bars(t2,1520,1540,1510,1525)
        s,_=m.paper_step(s,d,self.days,self.now+60*60_000,self.symbols,self.cost)
        self.assertTrue(s['accounts']['S1_BTC_BEAR']['halted'])
        self.assertFalse(s['accounts']['S1_BTC_BEAR']['positions'])

    def test_report_writing(self):
        s=self.initial()
        with tempfile.TemporaryDirectory() as d:
            m.save_outputs(s,d)
            files={x.name for x in Path(d).iterdir()}
            self.assertEqual(files,{'state.json','summary.json','trades.csv','equity.csv','events.csv'})
            self.assertFalse(json.loads((Path(d)/'summary.json').read_text())['status'].find('NO_ORDERS')<0)
            self.assertEqual(json.loads((Path(d)/'state.json').read_text())['sha256'],s['sha256'])

    def test_restore_requires_token(self):
        with self.assertRaisesRegex(RuntimeError,'missing GitHub read'):
            m.restore_from_github('a/b',m.WORKFLOW,'123','','/tmp/nope')

    def test_restore_raises_when_missing_old_state(self):
        run={'id':1,'created_at':'2026-10-01T00:00:00Z','status':'completed','conclusion':'success'}
        old=m.urllib.request.urlopen
        def resp(data):
            class Resp:
                def __enter__(self):return io.BytesIO(data)
                def __exit__(self,*args):return False
            return Resp()
        def fake(req,timeout=None):
            url=req.full_url
            if url.endswith('/runs?per_page=100'):return resp(json.dumps({'workflow_runs':[run]}).encode())
            return resp(json.dumps({'artifacts':[]}).encode())
        with patch.object(m.urllib.request,'urlopen',fake):
            with self.assertRaisesRegex(RuntimeError,'lacks state artifact'):
                m.restore_from_github('a/b',m.WORKFLOW,'2','test-token','/tmp/nowhere')

    def test_restore_does_not_silently_restart_after_failed_job(self):
        run={'id':1,'created_at':'2026-10-01T00:00:00Z','status':'completed','conclusion':'failure'}
        class Resp:
            def __enter__(self):return io.BytesIO(json.dumps({'workflow_runs':[run]}).encode())
            def __exit__(self,*args):return False
        with patch.object(m.urllib.request,'urlopen',lambda *args,**kw:Resp()):
            with self.assertRaisesRegex(RuntimeError,'no successful state'):
                m.restore_from_github('a/b',m.WORKFLOW,'2','test-token','/tmp/nowhere')

    def test_restore_valid_state_zip(self):
        original=self.initial();buf=io.BytesIO()
        with zipfile.ZipFile(buf,'w') as z:z.writestr('state.json',json.dumps(original))
        run={'id':1,'created_at':'2026-10-01T00:00:00Z','status':'completed','conclusion':'success'}
        artifact={'name':m.ARTIFACT,'id':42,'expired':False}
        def resp(data):
            class R:
                def __enter__(self):return io.BytesIO(data)
                def __exit__(self,*args):return False
            return R()
        def fake(req,timeout=None):
            url=req.full_url
            if url.endswith('/runs?per_page=100'):return resp(json.dumps({'workflow_runs':[run]}).encode())
            if url.endswith('/artifacts?per_page=100'):return resp(json.dumps({'artifacts':[artifact]}).encode())
            return resp(buf.getvalue())
        with tempfile.TemporaryDirectory() as d,patch.object(m.urllib.request,'urlopen',fake):
            path=Path(d)/'state.json'
            self.assertTrue(m.restore_from_github('a/b',m.WORKFLOW,'2','test-token',path))
            self.assertEqual(json.loads(path.read_text())['sha256'],original['sha256'])

    def test_restore_no_previous_run_is_bootstrap_only(self):
        class Resp:
            def __enter__(self):return io.BytesIO(json.dumps({'workflow_runs':[]}).encode())
            def __exit__(self,*args):return False
        with patch.object(m.urllib.request,'urlopen',lambda *a,**k:Resp()):
            self.assertFalse(m.restore_from_github('a/b',m.WORKFLOW,'2','test-token','/tmp/not-created'))

    def test_invalid_future_order_provenance_fails(self):
        s=self.initial();t=self.target+m.MS30
        s,_=m.paper_step(s,self.bars(t),self.days,self.now+30*60_000,self.symbols,self.cost)
        future=t+m.MS30
        s['accounts']['S1_BTC_BEAR']['pending'][str(future)]['BTC']['prepared_utc']=m.iso(future+100)
        m.seal(s)
        with self.assertRaisesRegex(ValueError,'future order provenance'):
            m.paper_step(s,self.bars(future,700,730,690,700),self.days,self.now+60*60_000,self.symbols,self.cost)

    def test_all_nonexistent_pending_prevents_entries(self):
        s=self.initial()
        for a in s['accounts'].values():a['pending']={}
        m.seal(s)
        t=self.target+m.MS30
        s,_=m.paper_step(s,self.bars(t,200,300,190,220),self.days,self.now+30*60_000,self.symbols,self.cost)
        self.assertEqual(sum(len(a['trades'])+len(a['positions']) for a in s['accounts'].values()),0)

    def test_order_not_armed_in_bull_market(self):
        days=self.make_daily(bear=False)
        s=m.paper_step(None,{},days,self.now,self.symbols,self.cost)[0]
        t=self.target+m.MS30
        s,_=m.paper_step(s,self.bars(t,100,110,95,100),days,self.now+30*60_000,self.symbols,self.cost)
        self.assertFalse(s['accounts']['S1_BTC_BEAR']['positions'])

    def test_independent_accounts_not_combined(self):
        s=self.initial();a=s['accounts']['S1_BTC_BEAR'];a['cash']=7777.;m.seal(s)
        self.assertEqual(s['accounts']['S2_BTC_BEAR']['cash'],m.START_CASH)
        self.assertNotEqual(a['cash'],s['accounts']['S2_BTC_BEAR']['cash'])

    def test_status_never_claims_real_trading(self):
        s=self.initial()
        with tempfile.TemporaryDirectory() as d:
            m.save_outputs(s,d)
            r=json.loads((Path(d)/'summary.json').read_text())
            self.assertIn('NO_ORDERS',r['status'])
            self.assertEqual(set(r['systems']),set(m.SYSTEMS))

    def test_repeat_seal_same_digest(self):
        s=self.initial();v=s['sha256'];m.seal(s);self.assertEqual(v,s['sha256'])

if __name__=='__main__':unittest.main()
