import copy,fcntl,io,json,os,tempfile,unittest,zipfile
from pathlib import Path
from unittest.mock import patch
from datetime import datetime,timezone
import pandas as pd
import test_swing_v09 as legacy
from v09.forward.freeze import create,digest,verify,seal
from v09.forward.engine import initialize,advance,eligible_open,qualification_reasons,observe,close_piece
from v09.forward.state import validate,financial_hash,event,read_bundle,atomic_bundle
from v09.forward.report import write_bundle,summary
from v09.forward.storage import resolve,safe_extract
from v09.forward.__main__ import fault_checks
from v09.data import iso,timestamp
from v09.risk import day

ROOT=Path(__file__).parent/'v09';HOUR=3600000

def freeze():
    f=create(ROOT,'0'*40);f.pop('config_hash');f.pop('study_id')
    f.update(symbols=['BTC'],bars={'1H':HOUR},warmup_days=35,freeze_timestamp_utc='2024-01-01T00:00:00Z')
    return seal(f)

def snapshot(n=840,model='MARKET'):
    frame=legacy.candles(n)
    overrides={840:(100,101,99.5,100),841:(100,125,99.5,110),842:(110,115,99.6,103),843:(103,104,101,102)}
    for i,values in overrides.items():
        if i<n:
            for name,v in zip(('open','high','low','close'),values):frame.loc[i,name]=v
    now=int(frame.ts.iloc[-1])+HOUR+300000
    return dict(origin='SYNTHETIC_TEST',observed_at_utc=iso(now),frames={'BTC-USDT-SWAP|1H':[[int(r.ts),float(r.open),float(r.high),float(r.low),float(r.close),float(r.volume),float(r.quote_volume)] for r in frame.itertuples()]},
                metadata=[],instruments={'BTC-USDT-SWAP':dict(instId='BTC-USDT-SWAP',ctVal='.01',ctValCcy='BTC',lotSz='1',minSz='1',tickSz='.1',lever='100')},
                marks={'BTC-USDT-SWAP':dict(price=float(frame.close.iloc[-1]),time=now)},funding={'BTC-USDT-SWAP':[]},
                news=dict(status='UNKNOWN_COVERAGE',events=[]))

def initial(f=None):
    f=f or freeze();x=snapshot();s=initialize(f,x,'1',x['observed_at_utc'],allow_test=True);return f,x,s

def pending(s,x,execution='CONDITIONAL_MARKET_NEXT_FULL_OPEN',units=2):
    t=timestamp(x['observed_at_utc']);eligible=eligible_open(t,HOUR)
    p=dict(id='order',core='VOL_SQUEEZE',strategy_id=s['freeze']['strategy_ids']['VOL_SQUEEZE'],symbol='BTC-USDT-SWAP',bar='1H',side=1,
           strategy_version=s['freeze']['strategy_version'],code_commit_sha=s['freeze']['code_commit_sha'],freeze_timestamp_utc=s['freeze']['freeze_timestamp_utc'],
           execution=execution,expected_entry=100.,stop=98.,tp1=120.,tp2=140.,eligible_open_ts=eligible,expires_at=eligible+3*HOUR,
           original_units=units,remaining_units=units,risk_twd=900.,notional_twd=units*100*32,regime='UP_LOW_ATR')
    s['pending']['order']=p
    s['candidates']['order']=dict(id='order',core='VOL_SQUEEZE',state='PENDING',reasons=[],book='PAPER_FORWARD',live_qualified=False)
    return p

def next_step(s,f,n,run='2',parent=None):
    x=snapshot(n);return advance(s,f,x,run,x['observed_at_utc'],parent or s['last_success_at'],allow_test=True),x

class ForwardLifecycleTests(unittest.TestCase):
    def test_first_initialization_no_historical_trades(self):
        f,x,s=initial();self.assertEqual((len(s['closed']),len(s['pending']),len(s['candidates'])),(0,0,0));self.assertEqual(s['cash_twd'],10000.)
        self.assertEqual(s['watermarks']['BTC-USDT-SWAP|1H'],x['frames']['BTC-USDT-SWAP|1H'][-1][0])
    def test_second_restore_and_third_continue_nonzero(self):
        f,x,s=initial();pending(s,x)
        (s2,_),x2=next_step(s,f,842);self.assertEqual(len(s2['positions']),1);self.assertTrue(s2['positions']['order']['tp1_done'])
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'bundle';write_bundle(p,s2,x2,'2');r,m=read_bundle(p,f)
            (s3,_),_=next_step(r,f,843,'3');self.assertEqual(len(s3['closed']),1);self.assertFalse(s3['positions']);self.assertGreater(s3['cash_twd'],10000)
    def test_same_input_rerun_is_financially_idempotent(self):
        f,x,s=initial();pending(s,x);(s2,_),x2=next_step(s,f,842)
        s3,d=advance(s2,f,x2,'3',x2['observed_at_utc'],s2['last_success_at'],allow_test=True)
        self.assertTrue(d['duplicate_input']);self.assertEqual(financial_hash(s2),financial_hash(s3));self.assertEqual(len(s2['equity']),len(s3['equity']))
    def test_pending_before_observation_cannot_fill_past_open(self):
        f,x,s=initial();p=pending(s,x);self.assertGreater(p['eligible_open_ts'],timestamp(x['observed_at_utc']))
        (s2,_),_=next_step(s,f,841);self.assertFalse(s2['positions']);self.assertTrue(s2['pending'])
    def test_not_durable_before_scheduled_open_refused(self):
        f,x,s=initial();p=pending(s,x)
        (s2,_),_=next_step(s,f,842,parent=iso(p['eligible_open_ts']+1));self.assertFalse(s2['positions']);self.assertFalse(s2['pending'])
    def test_limit_touch_alone_no_fill(self):
        f,x,s=initial();pending(s,x,'LIMIT_STRICT_CROSS_PARTIAL');x2=snapshot(842);x2['frames']['BTC-USDT-SWAP|1H'][-1][3]=100.
        s2,_=advance(s,f,x2,'2',x2['observed_at_utc'],s['last_success_at'],allow_test=True);self.assertFalse(s2['positions'])
    def test_limit_partial_and_cancel_remainder_after_exit(self):
        f,x,s=initial();pending(s,x,'LIMIT_STRICT_CROSS_PARTIAL',units=4)
        (s2,_),_=next_step(s,f,842);p=s2['positions']['order'];self.assertEqual(p['units_total'],2.);self.assertTrue(p['tp1_done']);self.assertNotIn('order',s2['pending'])
    def test_tp1_protection_only_next_bar(self):
        f,x,s=initial();pending(s,x);(s2,_),_=next_step(s,f,842)
        self.assertEqual(len(s2['positions']['order']['exits']),1)
        (s3,_),_=next_step(s2,f,843,'3');self.assertEqual(s3['closed'][0]['exits'][-1]['reason'],'stop')
    def test_stop_target_same_bar_stop_first(self):
        f,x,s=initial();pending(s,x);x2=snapshot(842);x2['frames']['BTC-USDT-SWAP|1H'][-1][3]=90.
        s2,_=advance(s,f,x2,'2',x2['observed_at_utc'],s['last_success_at'],allow_test=True)
        self.assertEqual(s2['closed'][0]['exits'][0]['reason'],'stop')
    def test_cost_cash_reconcile(self):
        f,x,s=initial();pending(s,x);(s2,_),_=next_step(s,f,842);(s3,_),_=next_step(s2,f,843,'3')
        t=s3['closed'][0];gross=sum(t['side']*(e['price']-t['entry'])*e['units']*32 for e in t['exits'])
        self.assertAlmostEqual(gross-t['fees_twd']-t['funding_twd'],t['net_twd']);self.assertAlmostEqual(s3['cash_twd'],10000+t['net_twd'])
    def test_funding_stable_id_no_double_charge(self):
        f,x,s=initial();p=pending(s,x);x2=snapshot(842);x2['funding']['BTC-USDT-SWAP']=[dict(time=p['eligible_open_ts'],rate=-.001)]
        s2,_=advance(s,f,x2,'2',x2['observed_at_utc'],s['last_success_at'],allow_test=True)
        self.assertGreater(s2['positions']['order']['funding_twd'],0)
        s3,_=advance(s2,f,x2,'3',x2['observed_at_utc'],s2['last_success_at'],allow_test=True)
        self.assertEqual(s3['cash_twd'],s2['cash_twd']);self.assertEqual(sum(e['kind']=='FUNDING' for e in s3['journal']),1)
    def test_expired_limit_records_unfilled(self):
        f,x,s=initial();p=pending(s,x,'LIMIT_STRICT_CROSS_PARTIAL');p['eligible_open_ts']-=4*HOUR;p['expires_at']=timestamp(x['observed_at_utc'])
        (s2,_),_=next_step(s,f,842);self.assertFalse(s2['positions']);self.assertFalse(s2['pending'])

class ForwardSafetyTests(unittest.TestCase):
    def test_default_quality_and_unknown_news_fail_closed(self):
        f=freeze();r=qualification_reasons(f,snapshot(),'2024-02-05T01:00:00Z','BTC-USDT-SWAP')
        self.assertIn('WIN_RATE_NOT_VERIFIED',r);self.assertIn('CROSS_NOT_VERIFIED',r);self.assertIn('NEWS_UNKNOWN_NOT_NO_EVENT',r)
    def test_real_runner_cannot_use_synthetic_data(self):
        f=freeze();x=snapshot()
        with self.assertRaises(ValueError):initialize(f,x,'1',x['observed_at_utc'])
    def test_missing_bar_refused_preserves_old_state(self):
        f,x,s=initial();old=copy.deepcopy(s);x2=snapshot(842);x2['frames']['BTC-USDT-SWAP|1H'].pop(-2)
        with self.assertRaises(ValueError):advance(s,f,x2,'2',x2['observed_at_utc'],s['last_success_at'],allow_test=True)
        self.assertEqual(s,old)
    def test_stale_snapshot_refused(self):
        f,x,s=initial();x2=snapshot(842);x2['observed_at_utc']=iso(timestamp(x2['observed_at_utc'])+HOUR)
        with self.assertRaises(ValueError):advance(s,f,x2,'2',x2['observed_at_utc'],s['last_success_at'],allow_test=True)
    def test_config_change_cannot_mix(self):
        f,x,s=initial();changed=copy.deepcopy(f);changed['slippage_per_fill']=.001
        with self.assertRaises(ValueError):validate(s,changed)
    def test_book_isolation(self):
        f,x,s=initial();s['book']='ACTUAL_USER_CONFIRMED'
        with self.assertRaises(ValueError):validate(s,f)
    def test_historical_book_refused(self):
        f,x,s=initial();s['book']='HISTORICAL_BACKTEST'
        with self.assertRaises(ValueError):validate(s,f)
    def test_risk_cap_restore_refuses_invalid_pending(self):
        f,x,s=initial();p=pending(s,x);p['risk_twd']=1001
        with self.assertRaises(ValueError):validate(s,f)
    def test_total_risk_cap_and_slots_restore(self):
        f,x,s=initial();p=pending(s,x)
        for i in range(3):s['pending'][str(i)]=dict(p,risk_twd=700)
        with self.assertRaises(ValueError):validate(s,f)
    def test_drawdown_persistent_after_recovery(self):
        f,x,s=initial();s['cash_twd']=8000;observe(s,x['observed_at_utc']);s['cash_twd']=10000;observe(s,x['observed_at_utc']);self.assertTrue(s['risk']['halted'])
    def test_fee_budget_halt(self):
        f,x,s=initial();s['expense_used_twd']=500;observe(s,x['observed_at_utc']);self.assertTrue(s['risk']['halted'])
    def test_paper_profit_does_not_raise_verified_capital(self):
        f,x,s=initial();s['cash_twd']=20000;r,_=observe(s,x['observed_at_utc']);self.assertIsNone(r.verified_capital)
    def test_latched_daily_halt_cancels_pending(self):
        f,x,s=initial();p=pending(s,x);s['daily_halts'][day(iso(p['eligible_open_ts']))]='TWO_LOSSES'
        (s2,_),_=next_step(s,f,842);self.assertFalse(s2['positions']);self.assertFalse(s2['pending'])
    def test_expired_state_does_not_reset(self):
        f,x,s=initial()
        with self.assertRaises(ValueError):validate(s,f,'2024-03-01T00:00:00Z')
    def test_mark_time_unknown_refused(self):
        f,x,s=initial();x2=snapshot(842);x2['marks']['BTC-USDT-SWAP']['time']=0
        with self.assertRaises(ValueError):advance(s,f,x2,'2',x2['observed_at_utc'],s['last_success_at'],allow_test=True)

class ForwardPersistenceTests(unittest.TestCase):
    def test_bundle_roundtrip_reports_and_provenance(self):
        f,x,s=initial()
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'bundle';m=write_bundle(p,s,x,'1');restored,manifest=read_bundle(p,f)
            self.assertEqual(restored,s);self.assertEqual(m,manifest);self.assertEqual(json.loads((p/'summary.json').read_text())['financial_hash'],financial_hash(s))
    def test_missing_state_never_initializes(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(OSError):read_bundle(d,freeze())
    def test_corrupt_state_rejected(self):
        f,x,s=initial()
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'b';write_bundle(p,s,x,'1');(p/'state.json').write_text('{}')
            with self.assertRaises(ValueError):read_bundle(p,f)
    def test_atomic_interruption_has_no_commit(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'b'
            with self.assertRaises(RuntimeError):atomic_bundle(p,{'state.json':b'{}'},True)
            self.assertFalse(p.exists());self.assertEqual(len(list(Path(d).glob('b.tmp-*'))),0)
    def test_concurrent_writer_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'b'
            with open(str(p)+'.lock','a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                with self.assertRaises(RuntimeError):atomic_bundle(p,{'state.json':b'{}'})
    def test_append_chain_cannot_rewrite_previous_event(self):
        f,x,s=initial();event(s,'RUN_COMPLETE','a',{})
        s['journal'][0]['payload']['run_id']='tampered'
        with self.assertRaises(ValueError):validate(s,f)
    def test_anchor_mismatch_refused(self):
        f,x,s=initial();anchor=dict(root_run_id='wrong',genesis_hash=s['journal'][0]['hash'],study_id=s['study_id'])
        with self.assertRaises(ValueError):validate(s,f,anchor=anchor)
    def test_cloud_style_fault_checks_preserve_source(self):
        f,x,s=initial()
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'b';write_bundle(p,s,x,'1');self.assertTrue(all(fault_checks(p,f).values()))
    def test_zip_slip_refused(self):
        b=io.BytesIO()
        with zipfile.ZipFile(b,'w') as z:z.writestr('../escape',b'bad')
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):safe_extract(b.getvalue(),d)

class StoreMock:
    def __init__(self,runs,arts):self.rs=runs;self.arts=arts
    def runs(self):return self.rs
    def artifacts(self,rid):return self.arts.get(str(rid),[])
    def download(self,*args):raise AssertionError('unexpected fallback/download')

class ForwardArtifactTests(unittest.TestCase):
    def test_first_only_initialization(self):
        self.assertEqual(resolve(StoreMock([],{}),freeze(),'1',1,'initialize',None,'none'),(None,None,None))
    def test_history_prevents_silent_reset(self):
        with self.assertRaises(ValueError):resolve(StoreMock([dict(id=1)],{}),freeze(),'2',1,'initialize',None,'none')
    def test_anchor_prevents_history_deleted_reset(self):
        with self.assertRaises(ValueError):resolve(StoreMock([],{}),freeze(),'2',1,'initialize',{'root_run_id':'1'},'none')
    def test_latest_missing_artifact_no_older_fallback(self):
        runs=[dict(id=2,status='completed',conclusion='success',run_number=2),dict(id=1,status='completed',conclusion='success',run_number=1)]
        with self.assertRaisesRegex(ValueError,'LATEST'):resolve(StoreMock(runs,{}),freeze(),'3',1,'resume',{'root_run_id':'1'},'none')
    def test_expired_artifact_rejected(self):
        f=freeze();runs=[dict(id=1,status='completed',conclusion='success',run_number=1)]
        arts={'1':[dict(id=11,name='forward-state-'+f['study_id']+'-1-1',expired=True)]}
        with self.assertRaisesRegex(ValueError,'expired'):resolve(StoreMock(runs,arts),f,'2',1,'resume',{'root_run_id':'1'},'none')
    def test_unverified_profitability_is_null_with_no_trades(self):
        f,x,s=initial();r=summary(s,x)
        self.assertEqual(r['portfolio']['closed_trades'],0);self.assertIsNone(r['portfolio']['win_rate']);self.assertFalse(r['live_qualified'])



class ForwardExtraRegressionTests(unittest.TestCase):
    def test_closed_trade_replay_no_duplicate_fees_or_pnl(self):
        f,x,s=initial();pending(s,x);(s2,_),_=next_step(s,f,842);(s3,_),x3=next_step(s2,f,843,'3')
        s4,d=advance(s3,f,x3,'4',x3['observed_at_utc'],s3['last_success_at'],allow_test=True)
        self.assertEqual(len(s4['closed']),1);self.assertEqual(financial_hash(s3),financial_hash(s4));self.assertEqual(s3['expense_used_twd'],s4['expense_used_twd'])
    def test_unknown_funding_preserves_open_position(self):
        f,x,s=initial();pending(s,x);(s2,_),_=next_step(s,f,842);x3=snapshot(843);x3['funding']={}
        old=copy.deepcopy(s2)
        with self.assertRaises(ValueError):advance(s2,f,x3,'3',x3['observed_at_utc'],s2['last_success_at'],allow_test=True)
        self.assertEqual(old,s2)
    def test_revised_processed_bar_rejected(self):
        f,x,s=initial();x2=snapshot(842);x2['frames']['BTC-USDT-SWAP|1H'][839][4]+=.001
        with self.assertRaises(ValueError):advance(s,f,x2,'2',x2['observed_at_utc'],s['last_success_at'],allow_test=True)
    def test_pending_without_qualified_evidence_is_not_created(self):
        f,x,s=initial();x2=snapshot(841)
        with patch('v09.forward.engine.signal',return_value=(1,1.)):
            s2,_=advance(s,f,x2,'2',x2['observed_at_utc'],s['last_success_at'],allow_test=True)
        self.assertTrue(s2['candidates']);self.assertFalse(s2['pending']);self.assertFalse(s2['positions'])
        self.assertTrue(all('WIN_RATE_NOT_VERIFIED' in c['reasons'] for c in s2['candidates'].values()))
    def test_no_retrospective_signals_after_offline_gap(self):
        f,x,s=initial();(s2,_),_=next_step(s,f,843)
        self.assertEqual(sum(e['kind']=='MISSED_SIGNAL_BARS' for e in s2['journal']),1)
    def test_forward_workflow_safety(self):
        text=(Path(__file__).resolve().parents[2]/'.github/workflows/okx-v09-forward-paper.yml').read_text()
        for required in ('workflow_dispatch:','contents: read','actions: read','cancel-in-progress: false','retention-days: 90','Checkout immutable model code'):
            self.assertIn(required,text)
        for forbidden in ('schedule:','pull_request:','secrets.','render.com','trade/order'):
            self.assertNotIn(forbidden,text)
    def test_journal_payload_is_immutable_copy(self):
        f,x,s=initial();payload={'a':[1]};event(s,'TEST','a',payload);payload['a'].append(2)
        validate(s,f);self.assertEqual(s['journal'][-1]['payload'],{'a':[1]})



class ForwardVersionTests(unittest.TestCase):
    def test_execution_change_requires_new_version_and_book(self):
        f,x,s=initial();new=copy.deepcopy(f);new['execution']='LIMIT_STRICT_CROSS_PARTIAL';new=seal(new)
        self.assertNotEqual(new['strategy_version'],f['strategy_version']);self.assertNotEqual(new['study_id'],f['study_id'])
        with self.assertRaises(ValueError):validate(s,new)

if __name__=='__main__':unittest.main()
