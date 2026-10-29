"""Synthetic values below are unit-test cases only, never accepted as forward data."""
import copy
from dataclasses import replace
from datetime import timedelta
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from okx_v09_reporting.artifact import archive_files, verify_artifact, verify_parent, reconcile_finances, csv_reconcile, projection_equal
from okx_v09_reporting.backup import create_backup,restore_backup,verify_backup,index_records,run_plan
from okx_v09_reporting.common import VerificationError,canonical,sha256,read_json,atomic_directory,local_path,now
from okx_v09_reporting.model import Model,registry
from okx_v09_reporting.reporting import realized_metrics,percentile,table,equity_svg,write_report,csv_export,documents
from okx_v09_reporting.__main__ import main

SOURCE=Path(os.environ.get('PHASE7_SOURCE_ARCHIVE','/workspace/attachments/2dff3fe3-6393-45e6-9f01-739359a9abfb/v09-phase7-source-revision20.zip'))
CONTEXT=Path(os.environ.get('PHASE7_SOURCE_CONTEXT','/workspace/work/phase7-evidence/source-40851188958.json'))
PARENT=Path(os.environ.get('PHASE7_PARENT_ARCHIVE','/workspace/attachments/b8f2a591-19a7-49a3-8b05-e77dd911998d/v09-schedule-40805798260.zip'))
PCONTEXT=Path(os.environ.get('PHASE7_PARENT_CONTEXT','/workspace/work/phase7-evidence/source-40805798260.json'))
MODEL=os.environ.get('FROZEN_MODEL_ROOT','/workspace/work/Alert-frozen/okx-monitor/backtest')


def small_zip(names):
    b=io.BytesIO()
    with zipfile.ZipFile(b,'w') as z:
        for name,data in names:z.writestr(name,data)
    return b.getvalue()

class PrimitiveTests(unittest.TestCase):
    def test_json_duplicate_keys_rejected(self):
        with self.assertRaises(VerificationError):read_json('{"cash":1,"cash":2}')
    def test_nonfinite_json_rejected(self):
        for text in ('NaN','Infinity','-Infinity'):
            with self.subTest(text=text),self.assertRaises(VerificationError):read_json(text)
    def test_path_traversal_absolute_backslash_and_normalization_rejected(self):
        for name in ('../state.json','/state.json','a/../state.json','a//state.json','a\\state.json','./state.json'):
            with self.subTest(name=name),self.assertRaises(VerificationError):archive_files(small_zip([(name,b'{}')]))
    def test_duplicate_and_case_colliding_zip_members_rejected(self):
        for names in [('a.json','a.json'),('a.json','A.json')]:
            with self.subTest(names=names),self.assertRaises(VerificationError):archive_files(small_zip([(n,b'{}') for n in names]))
    def test_symlink_zip_rejected(self):
        b=io.BytesIO()
        with zipfile.ZipFile(b,'w') as z:
            i=zipfile.ZipInfo('state.json');i.external_attr=(stat.S_IFLNK|0o777)<<16;z.writestr(i,'/etc/passwd')
        with self.assertRaises(VerificationError):archive_files(b.getvalue())
    def test_zip_bomb_limit_rejected(self):
        with patch('okx_v09_reporting.artifact.zipfile.ZipFile') as fake:
            member=type('Member',(),{'file_size':257*1024*1024})()
            fake.return_value.__enter__.return_value.infolist.return_value=[member]
            with self.assertRaises(VerificationError):archive_files(b'zip')
    def test_csv_nested_semantics_not_dict_key_order(self):
        csv_reconcile(b'id,entries\na,"[{""units"": 1, ""price"": 2}]"\n',['id','entries'],[dict(id='a',entries=[dict(price=2,units=1)])])
    def test_projection_roundoff_does_not_hide_profit_or_count_changes(self):
        projection_equal({'cash':10000.0000004},{'cash':10000.0})
        with self.assertRaises(VerificationError):projection_equal({'cash':10000.01},{'cash':10000.0})
        with self.assertRaises(VerificationError):projection_equal({'count':2.0},{'count':2})
        with self.assertRaises(VerificationError):csv_reconcile(b'price\n0.0000011\n',['price'],[{'price':0.000001}])
    def test_csv_wrong_columns_and_missing_rows(self):
        with self.assertRaises(VerificationError):csv_reconcile(b'wrong\n',['id'],[])
        with self.assertRaises(VerificationError):csv_reconcile(b'id\n',['id'],[dict(id='a')])
    def test_atomic_write_never_replaces_existing_empty_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)/'out';out.mkdir()
            with self.assertRaises(VerificationError):atomic_directory(out,{'x':b'new'})
            self.assertEqual(list(out.iterdir()),[])
    def test_atomic_write_failure_leaves_no_committed_directory(self):
        with tempfile.TemporaryDirectory() as tmp,patch('okx_v09_reporting.common.rename_exclusive',side_effect=OSError('disk failure')):
            out=Path(tmp)/'out'
            with self.assertRaises(OSError):atomic_directory(out,{'x':b'new'})
            self.assertFalse(out.exists());self.assertFalse(list(Path(tmp).glob('.out-*')))
    def test_local_paths_only_no_symlinks(self):
        for name in ('s3://bucket','https://example.test','../vault','a\\vault'):
            with self.subTest(name=name),self.assertRaises(VerificationError):local_path(name)
        with tempfile.TemporaryDirectory() as tmp:
            link=Path(tmp)/'link';link.symlink_to(Path(tmp),target_is_directory=True)
            with self.assertRaises(VerificationError):local_path(link/'out')

class MetricsTests(unittest.TestCase):
    def test_empty_book_is_unverified_not_zero_win_rate(self):
        m=realized_metrics([])
        for key in ('observed_win_rate','win_ci_95','realized_payoff','avg_net_r','avg_entry_planned_net_rr','avg_hold_days'):
            self.assertIsNone(m[key])
        self.assertFalse(m['live_qualified'])
    def test_three_rr_measures_are_different(self):
        # Unit fixture: two independent numerical examples, not market observations.
        trades=[dict(net_twd=300,net_r=3,planned_net_rr=3.5,hold_days=1),dict(net_twd=-100,net_r=-1,planned_net_rr=4.5,hold_days=3)]
        m=realized_metrics(trades)
        self.assertEqual(m['observed_win_rate'],.5);self.assertEqual(m['realized_payoff'],3)
        self.assertEqual(m['profit_factor'],3);self.assertEqual(m['avg_net_r'],1)
        self.assertEqual(m['avg_entry_planned_net_rr'],4);self.assertEqual(m['net_twd'],200)
        self.assertEqual(m['avg_hold_days'],2);self.assertEqual(m['median_hold_days'],2)
        self.assertAlmostEqual(m['p90_hold_days'],2.8);self.assertAlmostEqual(m['p95_hold_days'],2.9)
        self.assertEqual(m['max_hold_days'],3);self.assertEqual(m['target_1_to_3_days_fraction'],1)
        self.assertLess(m['win_ci_95'][0],.6);self.assertFalse(m['live_qualified'])
    def test_all_wins_or_losses_do_not_manufacture_payoff(self):
        for net in (100,-100,0):
            m=realized_metrics([dict(net_twd=net,net_r=net/100,planned_net_rr=3,hold_days=2)])
            self.assertIsNone(m['realized_payoff']);self.assertFalse(m['live_qualified'])
            self.assertIsNone(m['independent_sample_n'])
    def test_holding_tail_is_visible_not_filtered_away(self):
        m=realized_metrics([dict(net_twd=1,net_r=.1,planned_net_rr=3,hold_days=15)])
        self.assertEqual(m['holding_over_14_days'],1);self.assertEqual(m['max_hold_days'],15)
    def test_html_and_markdown_escape_untrusted_cells(self):
        text='<script>alert(1)</script>|[x](javascript:bad)'
        html=table(['x'],[[text]],True);md=table(['x'],[[text]])
        self.assertNotIn('<script>',html);self.assertIn('&lt;script&gt;',html)
        self.assertIn('\\|',md);self.assertIn('\\[',md)
    def test_csv_formula_is_not_executed(self):
        data=csv_export(['symbol','net'],[dict(symbol='=cmd()',net=-12)]).decode('utf-8-sig')
        self.assertIn("'=cmd()",data);self.assertIn('-12',data)
    def test_zero_equity_and_single_point_curve_are_valid_svg(self):
        svg=equity_svg([dict(time='2026-01-01T00:00:00Z',equity_twd=0)])
        self.assertIn('<svg',svg);self.assertNotIn('nan',svg.lower());self.assertNotIn('http://example',svg)

class FinancialTests(unittest.TestCase):
    def fixture(self):
        e=dict(time='2026-01-01T00:00:00Z',price=100,units=1,fee_twd=1.6)
        x=dict(time='2026-01-02T00:00:00Z',price=110,units=1,fee_twd=1.76,reason='TIME_EXIT_3D')
        t=dict(id='unit-only',book='PAPER_FORWARD',config_hash='unit',side=1,entry=100,units_total=1,
               risk_twd=1000,planned_net_rr=3.5,net_twd=311.64,net_r=.31164,hold_days=1,
               entry_time=e['time'],exit_time=x['time'],fees_twd=3.36,funding_twd=5,slippage_twd=2,
               entries=[e],exits=[x])
        return dict(closed=[t],positions={},config_hash='unit',cash_twd=10311.64,expense_used_twd=8.36,last_processed_at='2026-01-03T00:00:00Z',
                    freeze=dict(fee_per_fill=.0005,fx_twd_per_usdt=32),equity=[],journal=[
                    dict(kind='ENTRY_FILL',key='unit-only|entry',payload=e.copy()),
                    dict(kind='EXIT_FILL',key='unit-only|exit',payload=x.copy()),
                    dict(kind='FUNDING',key='unit-only|fund',payload=dict(twd=5))])
    def test_net_costs_match_without_double_deducting_slippage(self):
        reconcile_finances(self.fixture())
    def test_fees_funding_r_and_holding_corruption_fail(self):
        for field,value in [('fees_twd',0),('funding_twd',0),('net_r',3.5),('hold_days',2),('net_twd',309.64)]:
            s=self.fixture();s['closed'][0][field]=value
            with self.subTest(field=field),self.assertRaises(VerificationError):reconcile_finances(s)
    def split_fixture(self):
        s=self.fixture();t=s['closed'][0]
        t.update(entry=101,units_total=2,net_twd=404.328,net_r=.404328,hold_days=2,
                 exit_time='2026-01-03T00:00:00Z',fees_twd=6.672,initial_stop=98,stop=105,tp1=110,tp2=140,
                 symbol='BTC-USDT-SWAP',bar='1H',strategy_id='VOLATILITY_CONTRACTION_BREAKOUT')
        t['entries']=[dict(time='2026-01-01T00:00:00Z',price=100,units=1,fee_twd=1.6),
                      dict(time='2026-01-01T01:00:00Z',price=102,units=1,fee_twd=1.632)]
        t['exits']=[dict(time='2026-01-02T00:00:00Z',price=110,units=1,fee_twd=1.76,reason='tp1'),
                    dict(time='2026-01-03T00:00:00Z',price=105,units=1,fee_twd=1.68,reason='stop')]
        s.update(cash_twd=10404.328,expense_used_twd=11.672,last_processed_at='2026-01-03T00:30:00Z')
        s['journal']=[dict(kind=kind,key='unit-only|'+str(i),payload=fill.copy())
                      for kind,fills in [('ENTRY_FILL',t['entries']),('EXIT_FILL',t['exits'])] for i,fill in enumerate(fills)]
        s['journal'].append(dict(kind='FUNDING',key='unit-only|fund',payload=dict(twd=5)))
        return s
    def test_partial_entries_and_tp1_protective_stop_costs_reconcile(self):
        reconcile_finances(self.split_fixture())
    def test_nonzero_rendering_is_a_marked_synthetic_unit_case_only(self):
        from types import SimpleNamespace
        s=self.split_fixture();trade=s['closed'][0]
        s.update(revision=1,candidates={},pending={},root_run_id='UNIT_ONLY',research_start_timestamp_utc=trade['entry_time'])
        s['freeze']['code_commit_sha']='UNIT_TEST_ONLY'
        view=SimpleNamespace(state=s,manifest=dict(run_id='SYNTHETIC_UNIT_TEST_ONLY',state_hash='UNIT_ONLY'),
                             digest='UNIT_TEST_ONLY',context={'run':{'head_sha':'UNIT_TEST_ONLY'}},
                             parent_verified=False,files={'operations_receipt.json':b'{"spec_review_payload_commit":"UNIT_ONLY"}'})
        metrics=dict(account=dict(open_positions=0,pending_orders=0,equity_twd=10404.328,fee_reserve_remaining_twd=988.328,realized_net_twd=404.328,max_drawdown_pct=0),
                     portfolio=realized_metrics([trade]),strategies={trade['strategy_id']:realized_metrics([trade])},
                     news_status='SYNTHETIC_UNIT_TEST',source=dict(artifact_id='UNIT_ONLY',exceeds_production_age_limit=False),
                     version=dict(code_inventory_sha256='UNIT_ONLY'))
        md,html,svg=documents(view,metrics)
        self.assertIn('SYNTHETIC_UNIT_TEST_ONLY',md)
        self.assertIn('第一段止盈',md);self.assertIn('停損／保護性停損',md)
        self.assertIn('2026-01-01 09:00:00 +08:00',md)
        self.assertIn('404.33',md);self.assertIn('6.67',md)
        self.assertIn('入場記錄淨 RR',html);self.assertIn('<svg',svg)
    def test_future_fill_is_not_accepted_as_a_real_observation(self):
        s=self.fixture();s['closed'][0]['exits'][0]['time']='2027-01-01T00:00:00Z'
        with self.assertRaisesRegex(VerificationError,'observation'):reconcile_finances(s)
    def test_fill_journal_and_missing_fills_fail(self):
        s=self.fixture();s['journal'][1]['payload']['price']=111
        with self.assertRaises(VerificationError):reconcile_finances(s)
        s=self.fixture();s['closed'][0]['entries']=[]
        with self.assertRaises(VerificationError):reconcile_finances(s)

class ActualArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model=Model(MODEL);cls.raw=SOURCE.read_bytes();cls.ctx=read_json(CONTEXT.read_bytes())
        cls.parent=verify_artifact(PARENT,PCONTEXT,cls.model)
        cls.source=verify_artifact(cls.raw,cls.ctx,cls.model,cls.parent)
        cls.initial_sha=sha256(cls.raw)
    @classmethod
    def tearDownClass(cls):
        assert sha256(SOURCE.read_bytes())==cls.initial_sha
    def changed_zip(self,change):
        files=dict(self.source.files);change(files)
        raw=small_zip(list(files.items()));ctx=copy.deepcopy(self.ctx)
        ctx['artifact']['digest']='sha256:'+sha256(raw);ctx['artifact']['size_in_bytes']=len(raw)
        return raw,ctx
    def reseal(self,files):
        m=read_json(files['run_manifest.json'])
        for name in m['files']:m['files'][name]=sha256(files[name])
        files['run_manifest.json']=canonical(m)
    def test_real_revision_20_and_direct_parent_19(self):
        self.assertEqual(self.source.state['revision'],20);self.assertTrue(self.source.parent_verified)
        self.assertEqual(len(self.source.inventory),22);self.assertEqual(self.source.state['closed'],[])
    def test_outer_hash_catches_extra_asset_corruption(self):
        ctx=copy.deepcopy(self.ctx);ctx['artifact']['digest']='sha256:'+'0'*64
        with self.assertRaisesRegex(VerificationError,'outer'):verify_artifact(self.raw,ctx,self.model)
    def test_unknown_workflow_or_unsuccessful_run_cannot_claim_verified(self):
        for field,value in [('conclusion','failure'),('head_sha','0'*40),('event','push'),('run_attempt',2),('head_branch','research/other')]:
            ctx=copy.deepcopy(self.ctx);ctx['run'][field]=value
            with self.subTest(field=field),self.assertRaises(VerificationError):verify_artifact(self.raw,ctx,self.model)
    def test_missing_or_unexpected_file_refused(self):
        for action in (lambda f:f.pop('events.csv'),lambda f:f.update({'extra.py':b'bad'})):
            raw,ctx=self.changed_zip(action)
            with self.assertRaises(VerificationError):verify_artifact(raw,ctx,self.model)
    def test_modified_fixed_asset_refused_even_with_new_outer_hash(self):
        raw,ctx=self.changed_zip(lambda f:f.update({'export_artifacts.py':b'print("bad")'}))
        with self.assertRaisesRegex(VerificationError,'asset'):verify_artifact(raw,ctx,self.model)
    def test_manifest_omission_refused(self):
        def change(files):
            m=read_json(files['run_manifest.json']);del m['files']['events.csv'];files['run_manifest.json']=canonical(m)
        raw,ctx=self.changed_zip(change)
        with self.assertRaises(VerificationError):verify_artifact(raw,ctx,self.model)
    def test_synthetic_origin_is_never_market_proof(self):
        def change(files):
            snap=read_json(files['snapshot.json']);snap['origin']='SYNTHETIC_TEST';files['snapshot.json']=canonical(snap);self.reseal(files)
        raw,ctx=self.changed_zip(change)
        with self.assertRaisesRegex(VerificationError,'synthetic'):verify_artifact(raw,ctx,self.model)
    def test_summary_cannot_invent_profit(self):
        def change(files):
            report=read_json(files['summary.json']);report['cash_twd']=999999;files['summary.json']=canonical(report);self.reseal(files)
        raw,ctx=self.changed_zip(change)
        with self.assertRaisesRegex(VerificationError,'summary'):verify_artifact(raw,ctx,self.model)
    def test_mismatched_csv_is_refused(self):
        def change(files):files['trades.csv']+=b'fake\n';self.reseal(files)
        raw,ctx=self.changed_zip(change)
        with self.assertRaisesRegex(VerificationError,'CSV'):verify_artifact(raw,ctx,self.model)
    def test_validated_object_cannot_be_changed_before_report(self):
        for attr in ('state','summary','manifest','context','inventory'):
            value=copy.deepcopy(getattr(self.source,attr));value['tampered']=True
            altered=replace(self.source,**{attr:value})
            with self.subTest(attr=attr),self.assertRaises(VerificationError):altered.assert_unchanged()
    def test_parent_state_hash_must_match(self):
        m=copy.deepcopy(self.source.manifest);m['parent']['state_hash']='0'*64
        altered=replace(self.source,manifest=m)
        with self.assertRaises(VerificationError):verify_parent(self.parent,altered)
    def test_offline_report_and_backup_do_not_use_network(self):
        with tempfile.TemporaryDirectory() as tmp,patch('socket.socket',side_effect=AssertionError('network forbidden')):
            out=write_report(self.source,Path(tmp)/'report');record=create_backup(self.source,self.model,Path(tmp)/'vault')
            self.assertTrue((out/'report.html').is_file());self.assertEqual(record['revision'],20)
    def test_zero_trade_report_is_honest_and_self_contained(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=write_report(self.source,Path(tmp)/'report')
            metrics=read_json((out/'metrics.json').read_bytes());md=(out/'report.md').read_text();html=(out/'report.html').read_text()
            self.assertEqual(metrics['portfolio']['closed_trades'],0)
            self.assertIsNone(metrics['portfolio']['observed_win_rate']);self.assertIn('未驗證',md)
            self.assertIn('逐筆入場成交',md);self.assertIn('逐筆止盈',md)
            self.assertIn('<svg',html);self.assertNotIn('<script',html);self.assertNotIn('<link',html)
            self.assertEqual((out/'source-trades.csv').read_bytes(),self.source.files['trades.csv'])
            seal=read_json((out/'report-manifest.json').read_bytes())
            for name,expected in seal['files'].items():self.assertEqual(sha256((out/name).read_bytes()),expected)
    def test_disabled_automatic_backup_needs_no_destination_and_writes_nothing(self):
        config=dict(schema_version=1,enabled=False,destination=None,destination_confirmed=False,external_transfer_enabled=False)
        self.assertIn('DISABLED',run_plan(config)['status'])
        config['enabled']=True
        with self.assertRaises(VerificationError):run_plan(config,self.source,self.model)
        config.update(destination='https://example.test/upload',destination_confirmed=True)
        with self.assertRaises(VerificationError):run_plan(config,self.source,self.model)
        config['external_transfer_enabled']=True
        with self.assertRaises(VerificationError):run_plan(config,self.source,self.model)
    def test_backup_restores_every_original_byte_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault=Path(tmp)/'vault';record=create_backup(self.source,self.model,vault)
            before=(vault/'backup-index.jsonl').read_bytes()
            repeated=create_backup(self.source,self.model,vault)
            self.assertEqual(repeated['status'],'ALREADY_VERIFIED');self.assertEqual((vault/'backup-index.jsonl').read_bytes(),before)
            result=restore_backup(vault/record['path'],record['backup_manifest_sha256'],Path(tmp)/'restored',self.model)
            self.assertFalse(result['production_resume_authorized'])
            self.assertEqual((Path(tmp)/'restored/original-artifact.zip').read_bytes(),self.raw)
            for name,data in self.source.files.items():self.assertEqual((Path(tmp)/'restored/bundle'/name).read_bytes(),data)
    def test_bad_seal_corruption_and_missing_backup_members_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault=Path(tmp)/'vault';r=create_backup(self.source,self.model,vault);directory=vault/r['path']
            with self.assertRaises(VerificationError):verify_backup(directory,'0'*64,self.model)
            p=directory/'original-artifact.zip';raw=p.read_bytes();p.write_bytes(b'corrupt')
            with self.assertRaises(VerificationError):verify_backup(directory,r['backup_manifest_sha256'],self.model)
            p.write_bytes(raw);(directory/'tool-version.json').unlink()
            with self.assertRaises(VerificationError):verify_backup(directory,r['backup_manifest_sha256'],self.model)
    def test_restore_cannot_overwrite_a_live_or_existing_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault=Path(tmp)/'vault';r=create_backup(self.source,self.model,vault)
            target=Path(tmp)/'live';target.mkdir();(target/'state.json').write_text('KEEP')
            with self.assertRaises(VerificationError):restore_backup(vault/r['path'],r['backup_manifest_sha256'],target,self.model)
            self.assertEqual((target/'state.json').read_text(),'KEEP')
    def test_backup_keeps_old_revisions_and_verifies_journal_continuity(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault=Path(tmp)/'vault';first=create_backup(self.parent,self.model,vault);second=create_backup(self.source,self.model,vault)
            rows=index_records(vault);self.assertEqual([r['revision'] for r in rows],[19,20])
            self.assertTrue((vault/first['path']).is_dir());self.assertTrue((vault/second['path']).is_dir())
    def test_older_backup_added_later_does_not_replace_latest(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault=Path(tmp)/'vault';create_backup(self.source,self.model,vault);create_backup(self.parent,self.model,vault)
            self.assertEqual(max(index_records(vault),key=lambda r:r['revision'])['revision'],20)
    def test_partial_index_requires_manual_audit_not_silent_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault=Path(tmp)/'vault';r=create_backup(self.source,self.model,vault)
            with (vault/'backup-index.jsonl').open('ab') as f:f.write(b'{broken')
            with self.assertRaises(VerificationError):create_backup(self.source,self.model,vault)
            self.assertTrue((vault/r['path']/'original-artifact.zip').is_file())
    def test_interrupted_backup_commit_does_not_destroy_source(self):
        with tempfile.TemporaryDirectory() as tmp,patch('okx_v09_reporting.backup.atomic_directory',side_effect=OSError('injected disk failure')):
            vault=Path(tmp)/'vault'
            with self.assertRaises(OSError):create_backup(self.source,self.model,vault)
            self.assertFalse((vault/'backup-index.jsonl').exists())
            self.assertEqual(sha256(SOURCE.read_bytes()),self.initial_sha)
    def test_concurrent_backup_writer_refused(self):
        import fcntl
        with tempfile.TemporaryDirectory() as tmp:
            vault=Path(tmp)/'vault';vault.mkdir()
            with (vault/'.backup.lock').open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                with self.assertRaisesRegex(VerificationError,'concurrent'):create_backup(self.source,self.model,vault)
            self.assertFalse((vault/'backup-index.jsonl').exists())
    def test_reencoded_artifact_cannot_replace_same_revision(self):
        raw,ctx=self.changed_zip(lambda files:None)
        ctx['artifact']['id']+=1
        alternate=verify_artifact(raw,ctx,self.model)
        with tempfile.TemporaryDirectory() as tmp:
            vault=Path(tmp)/'vault';original=create_backup(self.source,self.model,vault)
            before=(vault/'backup-index.jsonl').read_bytes()
            with self.assertRaisesRegex(VerificationError,'existing revision'):create_backup(alternate,self.model,vault)
            self.assertEqual((vault/'backup-index.jsonl').read_bytes(),before)
            self.assertEqual((vault/original['path']/'original-artifact.zip').read_bytes(),self.raw)
    def test_archived_expired_cloud_source_is_not_production_restore_authorization(self):
        future=now()+timedelta(days=400)
        ctx=copy.deepcopy(self.ctx);ctx['artifact']['expired']=True
        with patch('okx_v09_reporting.artifact.now',return_value=future):
            archived=verify_artifact(self.raw,ctx,self.model)
            info=archived.provenance()
            self.assertTrue(info['cloud_artifact_expired']);self.assertTrue(info['exceeds_production_age_limit'])
            self.assertFalse(info['production_resume_authorized'])
    def test_source_model_must_be_complete_before_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'v09';root.mkdir();(root/'__init__.py').write_text('raise RuntimeError("must not execute")')
            with self.assertRaisesRegex(VerificationError,'file set'):Model(tmp)
    def test_orphan_after_index_failure_recovers_without_rewriting_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault=Path(tmp)/'vault'
            with patch('okx_v09_reporting.backup._append_index',side_effect=OSError('index disk failure')):
                with self.assertRaises(OSError):create_backup(self.source,self.model,vault)
            paths=list(vault.rglob('original-artifact.zip'));self.assertEqual(len(paths),1)
            before=paths[0].read_bytes();record=create_backup(self.source,self.model,vault)
            self.assertEqual(paths[0].read_bytes(),before);self.assertEqual(len(index_records(vault)),1)
            verify_backup(vault/record['path'],record['backup_manifest_sha256'],self.model)
    def test_cli_report_and_backup_plan_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp,patch('sys.stdout',new_callable=io.StringIO) as output:
            self.assertEqual(main(['report',str(SOURCE),'--source-context',str(CONTEXT),'--model-root',MODEL,'--out',str(Path(tmp)/'report')]),0)
            self.assertEqual(main(['report',str(SOURCE),'--source-context',str(CONTEXT),'--model-root',MODEL,'--out',str(Path(tmp)/'report')]),2)
            self.assertIn('REFUSED',output.getvalue())

if __name__=='__main__':unittest.main(verbosity=2)
