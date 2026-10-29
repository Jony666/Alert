import copy
from dataclasses import replace
from datetime import timedelta
import io
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock,patch
from urllib.parse import parse_qs,urlparse,urlencode
import zipfile
import requests
from okx_v09_reporting.common import canonical,sha256,read_json,now
from okx_v09_reporting.artifact import verify_artifact,archive_files
from okx_v09_reporting.model import Model
from okx_v09_drive_backup.policy import FOLDER_ID,SCOPE,LivePermit,load_config,permit_live,validate_config
from okx_v09_drive_backup.oauth import UserCredentials,OAuthSession,exact_scope,TOKEN_URL
from okx_v09_drive_backup.authorize import authorize_url,callback_values,exchange_and_verify,secure_credentials_file
from okx_v09_drive_backup.drive import DriveClient,HTTPTransport,API,UPLOAD,allowed_session
from okx_v09_drive_backup.github_source import GitHubSource,Selection,PREFIX
from okx_v09_drive_backup.errors import BackupError
from okx_v09_drive_backup.package import prepare_package,verify_package,source_identity
from okx_v09_drive_backup.service import BackupService
from okx_v09_drive_backup.__main__ import run,main
from fakes import MemoryDrive,Response

CONFIG=Path('docs/okx-v09-drive-backup/config.json')
MODEL=os.environ.get('FROZEN_MODEL_ROOT','/workspace/work/Alert-frozen/okx-monitor/backtest')
SOURCE=Path(os.environ.get('PHASE7_SOURCE_ARCHIVE','/workspace/attachments/2dff3fe3-6393-45e6-9f01-739359a9abfb/v09-phase7-source-revision20.zip'))
CONTEXT=Path(os.environ.get('PHASE7_SOURCE_CONTEXT','/workspace/work/phase7-evidence/source-40851188958.json'))
PARENT=Path(os.environ.get('PHASE7_PARENT_ARCHIVE','/workspace/attachments/b8f2a591-19a7-49a3-8b05-e77dd911998d/v09-schedule-40805798260.zip'))
PCONTEXT=Path(os.environ.get('PHASE7_PARENT_CONTEXT','/workspace/work/phase7-evidence/source-40805798260.json'))


def credentials():return dict(type='authorized_user',client_id='fake-client',client_secret='SECRET_CLIENT_SENTINEL',refresh_token='SECRET_REFRESH_SENTINEL',token_uri=TOKEN_URL,scopes=[SCOPE],folder_grant=dict(id=FOLDER_ID,oauth_client_id='fake-client',picker_selected=True,drive_file_get_verified=True))

class PolicyOAuthTests(unittest.TestCase):
    def test_default_disabled(self):
        config=load_config(CONFIG)
        self.assertFalse(config['live_upload_enabled'])
        with self.assertRaisesRegex(BackupError,'LIVE_TRANSFER_NOT_APPROVED'):permit_live(config,'I_APPROVE_ONE_DRIVE_BACKUP')
    def test_two_independent_gates(self):
        config=load_config(CONFIG);config['live_upload_enabled']=True
        with self.assertRaises(BackupError):permit_live(config,'yes')
        self.assertTrue(permit_live(config,'I_APPROVE_ONE_DRIVE_BACKUP').enabled)
    def test_unreviewed_config_fields_rejected(self):
        for key,value in dict(folder_id='other',overwrite_existing=True,delete_existing=True,planned_utc_cron='0 * * * *',historical_restore_interval_days=1,live_upload_enabled=1).items():
            config=load_config(CONFIG);config[key]=value
            with self.subTest(key=key),self.assertRaises(BackupError):validate_config(config)
    def test_scope_exact_only(self):
        for scopes in (None,[],[SCOPE,'https://www.googleapis.com/auth/drive'],SCOPE+' openid'):
            with self.subTest(scopes=scopes),self.assertRaises(BackupError):exact_scope(scopes)
    def test_service_account_rejected(self):
        value=credentials();value['type']='service_account'
        with self.assertRaisesRegex(BackupError,'USER_OAUTH'):UserCredentials.parse(value)
    def test_grant_must_be_same_client_and_target(self):
        for field,value in [('oauth_client_id','other'),('id','other'),('picker_selected',False),('drive_file_get_verified',False)]:
            data=credentials();data['folder_grant'][field]=value
            with self.subTest(field=field),self.assertRaises(BackupError):UserCredentials.parse(data)
    def test_token_endpoint_cannot_be_changed(self):
        value=credentials();value['token_uri']='https://evil.test/token'
        with self.assertRaises(BackupError):UserCredentials.parse(value)
    def test_representations_hide_secrets(self):
        value=UserCredentials.parse(credentials())
        self.assertNotIn('SECRET_',repr(value)+repr(OAuthSession(value)))
    def test_refresh_scope_rechecked_and_no_redirects(self):
        session=Mock();session.post.return_value=Response(body={'scope':SCOPE,'access_token':'short','expires_in':3600})
        oauth=OAuthSession(UserCredentials.parse(credentials()),session)
        self.assertEqual(oauth.access_token(),'short');self.assertEqual(oauth.access_token(),'short')
        self.assertEqual(session.post.call_count,1);self.assertFalse(session.post.call_args.kwargs['allow_redirects'])
    def test_refresh_rejects_broader_scope(self):
        session=Mock();session.post.return_value=Response(body={'scope':SCOPE+' openid','access_token':'SECRET_TOKEN','expires_in':3600})
        with self.assertRaises(BackupError):OAuthSession(UserCredentials.parse(credentials()),session).access_token()
    def test_expired_refresh_is_safe_error(self):
        session=Mock();session.post.return_value=Response(400,{'error_description':'SECRET_REFRESH_SENTINEL'})
        with self.assertRaises(BackupError) as raised:OAuthSession(UserCredentials.parse(credentials()),session).access_token()
        self.assertNotIn('SECRET_',str(raised.exception));self.assertEqual(raised.exception.code,'OAUTH_REAUTH_REQUIRED')
    def test_refresh_rotation_stops(self):
        session=Mock();session.post.return_value=Response(body={'refresh_token':'rotated','access_token':'x','expires_in':3600})
        with self.assertRaisesRegex(BackupError,'ROTATION'):OAuthSession(UserCredentials.parse(credentials()),session).access_token()
    def test_desktop_picker_pkce_parameters(self):
        url=authorize_url('client','http://127.0.0.1:123/callback','state','v'*64);q=parse_qs(urlparse(url).query)
        self.assertEqual(q['scope'],[SCOPE]);self.assertEqual(q['file_ids'],[FOLDER_ID]);self.assertEqual(q['trigger_onepick'],['true'])
        self.assertEqual(q['allow_folder_selection'],['true']);self.assertEqual(q['code_challenge_method'],['S256'])
        self.assertNotIn('v'*64,url);self.assertEqual(q['access_type'],['offline'])
    def test_callback_exact_selected_folder(self):
        value=callback_values(urlencode(dict(state='state',code='code',picked_file_ids=FOLDER_ID)),'state')
        self.assertEqual(value['code'],'code')
    def test_callback_rejects_csrf_multi_folder_cancel_and_duplicates(self):
        base=dict(state='state',code='code',picked_file_ids=FOLDER_ID)
        cases=[dict(base,state='other'),dict(base,picked_file_ids=FOLDER_ID+',other'),dict(base,error='access_denied'),dict(base,code='')]
        for case in cases:
            with self.subTest(case=case),self.assertRaises(BackupError):callback_values(urlencode(case),'state')
        with self.assertRaises(BackupError):callback_values(urlencode(base)+'&state=state','state')
    def test_authorization_verifies_folder_read_only(self):
        session=Mock();session.post.return_value=Response(body=dict(access_token='short',refresh_token='refresh',scope=SCOPE))
        session.get.return_value=Response(body=dict(id=FOLDER_ID,mimeType='application/vnd.google-apps.folder',capabilities={'canAddChildren':True}))
        value=exchange_and_verify(dict(client_id='client',client_secret='secret'),'code','http://127.0.0.1/callback','v'*64,session)
        self.assertTrue(value['folder_grant']['drive_file_get_verified']);self.assertEqual(session.post.call_args.args[0],TOKEN_URL)
        self.assertEqual(session.get.call_count,1)
    def test_folder_404_does_not_escalate_scope(self):
        session=Mock();session.post.return_value=Response(body=dict(access_token='short',refresh_token='refresh',scope=SCOPE))
        session.get.return_value=Response(404)
        with self.assertRaisesRegex(BackupError,'NO_SCOPE_ESCALATION'):exchange_and_verify(dict(client_id='client',client_secret='secret'),'code','redirect','verifier',session)
        self.assertEqual(session.post.call_count,1)
    def test_credentials_external_0600_exclusive(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo=Path(tmp)/'repo';repo.mkdir();out=Path(tmp)/'private'/'auth.json'
            secure_credentials_file(out,credentials(),repo)
            self.assertEqual(out.stat().st_mode&0o777,0o600)
            with self.assertRaises(FileExistsError):secure_credentials_file(out,credentials(),repo)
            with self.assertRaises(BackupError):secure_credentials_file(repo/'auth.json',credentials(),repo)
    def test_credentials_symlink_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'real').mkdir();(root/'link').symlink_to(root/'real',target_is_directory=True)
            with self.assertRaises(BackupError):secure_credentials_file(root/'link'/'auth.json',credentials(),root/'repo')
    def test_disabled_upload_reads_no_secret_and_calls_no_network(self):
        class NoSecret(dict):
            def __getitem__(self,key):raise AssertionError('secret read forbidden')
        args=SimpleNamespace(command='upload',config=str(CONFIG),model_root=MODEL)
        with patch('requests.sessions.Session.request',side_effect=AssertionError('network forbidden')):
            with self.assertRaisesRegex(BackupError,'LIVE_TRANSFER_NOT_APPROVED'):run(args,NoSecret())
    def test_cli_failure_redacts_exception_and_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp,patch('okx_v09_drive_backup.__main__.run',side_effect=RuntimeError('SECRET_TOKEN')):
            with patch('builtins.print') as output:
                code=main(['plan','--config',str(CONFIG),'--model-root',MODEL,'--result',tmp+'/result.json'])
            self.assertEqual(code,2);self.assertNotIn('SECRET_',str(output.call_args_list)+Path(tmp+'/result.json').read_text())

class DriveProtocolTests(unittest.TestCase):
    def setUp(self):self.memory=MemoryDrive();self.client=DriveClient(self.memory,sleep=lambda _:None,chunk_size=256*1024)
    def create(self,raw=b'backup payload',file_id='file1'):
        return self.client.create(file_id,'test.zip',raw,'application/zip','package','a'*64,20)
    def test_live_transport_requires_exact_approval(self):
        for permit in (None,LivePermit(FOLDER_ID,True,'yes'),LivePermit('other',True,'I_APPROVE_ONE_DRIVE_BACKUP')):
            with self.subTest(permit=permit),self.assertRaises(BackupError):HTTPTransport(Mock(),permit)
    def test_untrusted_session_hosts_and_protocols(self):
        for url in ('http://www.googleapis.com/upload/drive/v3/files','https://evil.test/upload/drive/v3/files','https://www.googleapis.com@evil.test/upload/drive/v3/files','https://www.googleapis.com:444/upload/drive/v3/files','https://www.googleapis.com/drive/v3/files'):
            with self.subTest(url=url),self.assertRaises(BackupError):allowed_session(url)
    def test_no_patch_delete_or_arbitrary_post(self):
        transport=HTTPTransport(Mock(),LivePermit(FOLDER_ID,True,'I_APPROVE_ONE_DRIVE_BACKUP'),Mock())
        for method,url in [('PATCH',API+'/files/x'),('DELETE',API+'/files/x'),('POST',API+'/files/x')]:
            with self.subTest(method=method),self.assertRaises(BackupError):transport.request(method,url)
    def test_preflight_personal_folder_quota(self):self.assertTrue(self.client.preflight(100)['quota_checked'])
    def test_missing_folder_stops_no_scope_upgrade(self):
        self.memory.folder_status=404
        with self.assertRaisesRegex(BackupError,'SAME_APP_PICKER'):self.client.preflight()
    def test_shared_drive_is_not_assumed_personal(self):
        self.memory.shared=True
        with self.assertRaises(BackupError):self.client.preflight()
    def test_no_quota_assumption(self):
        self.memory.quota={}
        with self.assertRaisesRegex(BackupError,'QUOTA_UNVERIFIED'):self.client.preflight()
    def test_insufficient_quota_stops(self):
        self.memory.quota=dict(limit='100',usage='90')
        with self.assertRaisesRegex(BackupError,'QUOTA_INSUFFICIENT'):self.client.preflight(100)
    def test_upload_download_sha_and_no_update_methods(self):
        self.create();self.assertEqual(self.client.download('file1',sha256(b'backup payload')),b'backup payload')
        self.assertFalse(any(c[0] in ('PATCH','DELETE') for c in self.memory.calls))
    def test_no_api_sha_still_full_download_verified(self):
        self.memory.sha_available=False;self.create();self.assertEqual(self.client.download('file1',sha256(b'backup payload')),b'backup payload')
    def test_same_id_same_bytes_idempotent(self):
        self.create();count=len([c for c in self.memory.calls if c[0]=='POST']);self.create()
        self.assertEqual(len(self.memory.files),1);self.assertEqual(count,len([c for c in self.memory.calls if c[0]=='POST']))
    def test_same_id_different_bytes_refused(self):
        self.create()
        with self.assertRaisesRegex(BackupError,'MISMATCH'):self.create(b'other')
        self.assertEqual(self.memory.files['file1']['raw'],b'backup payload')
    def test_download_corruption_detected(self):
        self.create();self.memory.files['file1']['raw']=b'corrupt'
        with self.assertRaisesRegex(BackupError,'MISMATCH'):self.client.download('file1')
    def test_external_parent_refused(self):
        self.create();self.memory.files['file1']['meta']['parents']=['other']
        with self.assertRaisesRegex(BackupError,'OUTSIDE_TARGET'):self.client.download('file1')
    def test_oversized_remote_rejected_before_media_fetch(self):
        self.create();self.memory.files['file1']['meta']['size']=str(65*1024*1024);before=len(self.memory.calls)
        with self.assertRaisesRegex(BackupError,'TOO_LARGE'):self.client.download('file1')
        self.assertEqual(len(self.memory.calls),before+1)
    def test_rate_limits_retried(self):
        calls=[]
        def failure(method,url,kw):
            if url==API+'/about':
                calls.append(1)
                if len(calls)<3:return Response(429)
        self.memory.failure=failure;self.client.preflight();self.assertEqual(len(calls),3)
    def test_permission_failure_not_retried_as_rate_limit(self):
        self.memory.failure=lambda m,u,k:Response(403,{'error':{'errors':[{'reason':'insufficientPermissions'}]}})
        with self.assertRaises(BackupError):self.client.preflight()
        self.assertEqual(len(self.memory.calls),1)
    def test_transient_failure_is_bounded(self):
        self.memory.failure=lambda m,u,k:Response(503)
        with self.assertRaisesRegex(BackupError,'RETRIES_EXHAUSTED'):self.client.preflight()
        self.assertEqual(len(self.memory.calls),5)
    def test_chunked_partial_acknowledgement(self):
        self.memory.put_faults=['partial'];raw=b'x'*(700*1024);self.create(raw)
        self.assertEqual(self.client.download('file1'),raw)
    def test_uncertain_completion_queries_status_not_duplicate(self):
        self.memory.put_faults=['after'];self.create();self.assertEqual(len(self.memory.files),1)
        self.assertTrue(any(c[2].get('headers',{}).get('Content-Range','').startswith('bytes */') for c in self.memory.calls))
    def test_transient_chunk_failure_recovers(self):
        self.memory.put_faults=['before'];self.create();self.assertEqual(len(self.memory.files),1)
    def test_expired_resumable_session_new_session_same_id(self):
        self.memory.put_faults=['expire'];self.create();self.assertEqual(len(self.memory.files),1)
        posts=[c for c in self.memory.calls if c[0]=='POST'];self.assertEqual(len(posts),2)
        self.assertEqual(posts[0][2]['json']['id'],posts[1][2]['json']['id'])
    def test_invalid_range_and_rollback_fail(self):
        with self.assertRaises(BackupError):self.client._offset(Response(308,headers={'Range':'bytes=1-3'}),10)
        with self.assertRaises(BackupError):self.client._offset(Response(308,headers={'Range':'bytes=0-20'}),10)
    def test_rate_limit_403_retried_without_scope_change(self):
        seen=[]
        def fault(method,url,kwargs):
            if url==API+'/about':
                seen.append(1)
                if len(seen)==1:return Response(403,{'error':{'errors':[{'reason':'userRateLimitExceeded'}]}})
        self.memory.failure=fault
        self.assertTrue(self.client.preflight()['quota_checked']);self.assertEqual(len(seen),2)
    def test_create_conflict_verifies_exact_bytes(self):
        self.create()
        original=self.client.metadata
        first=[True]
        def stale_lookup(file_id,missing_ok=False):
            if missing_ok and first[0]:first[0]=False;return None
            return original(file_id,missing_ok)
        self.client.metadata=stale_lookup
        self.create();self.assertEqual(len(self.memory.files),1)
    def test_missing_range_after_acknowledged_chunk_stops(self):
        self.memory.omit_range=True
        with self.assertRaises(BackupError):self.create(b'x'*(700*1024))
        self.assertFalse(self.memory.files)
    def test_invalid_chunk_size(self):
        with self.assertRaises(BackupError):DriveClient(self.memory,chunk_size=100)

class SourceSelectionTests(unittest.TestCase):
    def setUp(self):
        self.source=GitHubSource('fake-token',Mock());self.context=read_json(CONTEXT.read_bytes())
        self.run=dict(self.context['run'],updated_at=now().isoformat(),run_number=100)
        self.artifact=copy.deepcopy(self.context['artifact']);self.artifact['expires_at']=(now()+timedelta(days=89)).isoformat()
        self.runs=[self.run];self.artifacts=[self.artifact];self.inventory=[self.artifact]
        def pages(path,key):
            if path.startswith('/actions/workflows/'):return self.runs
            if path.startswith('/actions/runs/'):return self.artifacts
            return self.inventory
        self.source.pages=pages
    def test_selects_declared_latest_success(self):self.assertEqual(self.source.select().context['artifact']['id'],self.artifact['id'])
    def test_missing_latest_never_falls_back(self):
        self.artifacts=[]
        with self.assertRaisesRegex(BackupError,'MISSING_OR_AMBIGUOUS'):self.source.select()
    def test_duplicate_source_artifacts_refused(self):
        self.artifacts.append(copy.deepcopy(self.artifact))
        with self.assertRaisesRegex(BackupError,'AMBIGUOUS'):self.source.select()
    def test_expired_source_no_fallback(self):
        self.artifact['expired']=True
        with self.assertRaisesRegex(BackupError,'EXPIRED'):self.source.select()
    def test_expiry_timestamp_checked_independently(self):
        self.artifact['expires_at']=(now()-timedelta(days=1)).isoformat()
        with self.assertRaisesRegex(BackupError,'EXPIRED'):self.source.select()
    def test_newer_failed_publisher_blocks_stale_source(self):
        newer=dict(self.artifact,id=999999999999,created_at=(now()+timedelta(seconds=1)).isoformat());self.inventory.append(newer)
        with self.assertRaisesRegex(BackupError,'NEWER_PUBLISHED'):self.source.select()
    def test_uses_completion_time_not_run_number(self):
        self.runs.append(dict(self.run,id=1,run_number=101,updated_at=(now()-timedelta(days=1)).isoformat()))
        self.assertEqual(self.source.select().context['run']['id'],self.run['id'])
    def test_rerun_requires_review(self):
        self.run['run_attempt']=2
        with self.assertRaisesRegex(BackupError,'RERUN'):self.source.select()
    def test_expiry_warnings_include_unselected_hourly_snapshots(self):
        self.artifact['expires_at']=(now()+timedelta(days=20)).isoformat()
        self.inventory.append(dict(self.artifact,id=1,created_at='2026-01-01T00:00:00Z'))
        result=self.source.select();self.assertTrue(result.expiry['latest_warning']);self.assertEqual(result.expiry['older_unselected_artifacts_expiring_within_30_days'],1)
        self.assertFalse(result.expiry['all_hourly_raw_snapshots_preserved'])
    def test_changed_selection_requires_restart(self):
        selected=self.source.select();self.artifact['digest']='sha256:'+'0'*64
        # Selection must be a snapshot, not a live alias of response objects.
        selected=replace(selected,context=copy.deepcopy(self.context))
        with self.assertRaisesRegex(BackupError,'SOURCE_ADVANCED'):self.source.assert_current(selected)
    def test_pagination_exhaustion_fail_closed(self):
        source=GitHubSource('fake',Mock());source.get=lambda path:{'artifacts':[{}]*100}
        with self.assertRaisesRegex(BackupError,'PAGINATION_INCOMPLETE'):source.pages('/actions/artifacts','artifacts')

class IsolationTests(unittest.TestCase):
    def test_only_development_ci_is_active(self):
        import yaml
        doc=yaml.load(Path('.github/workflows/okx-v09-drive-backup-validation.yml').read_text(),Loader=yaml.BaseLoader)
        self.assertEqual(doc['on']['push']['branches'],['research/okx-v09-phase7-drive-backup'])
        self.assertNotIn('schedule',doc['on']);self.assertEqual(doc['permissions'],{'contents':'read','actions':'read'})
        self.assertNotIn('secrets.',canonical(doc).decode())
    def test_daily_workflow_is_only_disabled_design(self):
        import yaml
        path=Path('docs/okx-v09-drive-backup/daily-workflow.yml.disabled')
        doc=yaml.load(path.read_text(),Loader=yaml.BaseLoader)
        self.assertEqual(doc['on']['schedule'][0]['cron'],'0 0 * * *')
        self.assertFalse(Path('.github/workflows/okx-v09-drive-daily.yml').exists())
        self.assertIn('REVIEW_REQUIRED_FIXED_40_HEX_COMMIT_SHA',path.read_text())

class PackageServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model=Model(MODEL);cls.artifact=verify_artifact(SOURCE.read_bytes(),read_json(CONTEXT.read_bytes()),cls.model)
        cls.parent=verify_artifact(PARENT.read_bytes(),read_json(PCONTEXT.read_bytes()),cls.model)
        cls.package=prepare_package(cls.artifact,cls.model,'offline-fixture')
    def setUp(self):
        self.memory=MemoryDrive();self.drive=DriveClient(self.memory,sleep=lambda _:None)
        self.service=BackupService(self.drive,self.model,'offline-run-1')
        self.selection=Selection(self.artifact.context,now().isoformat(),{'latest_remaining_days':90})
        self.source=Mock(spec=GitHubSource);self.source.read_latest.return_value=(self.selection,self.artifact)
    def upload(self):
        with patch('okx_v09_drive_backup.service.prepare_package',return_value=self.package):return self.service.upload_latest(self.source)
    def test_complete_real_package_and_original_zip_preserved(self):
        manifest,artifact=verify_package(self.package.raw,self.model,self.artifact)
        self.assertEqual(artifact.raw,SOURCE.read_bytes());self.assertEqual(manifest['source']['revision'],20)
        files=archive_files(self.package.raw)
        self.assertIn('report/report.md',files);self.assertIn('report/equity.svg',files);self.assertIn('backup/original-artifact.zip',files)
    def test_corrupt_package_refused(self):
        raw=bytearray(self.package.raw);raw[len(raw)//2]^=1
        with self.assertRaises(ValueError):verify_package(bytes(raw),self.model)
    def test_package_tamper_even_if_outer_zip_rebuilt(self):
        files=archive_files(self.package.raw);files['report/metrics.json']=b'{}';data=io.BytesIO()
        with zipfile.ZipFile(data,'w') as z:
            for n,b in files.items():z.writestr(n,b)
        with self.assertRaisesRegex(BackupError,'SHA256'):verify_package(data.getvalue(),self.model)
    def test_package_cannot_restore_as_other_source(self):
        with self.assertRaisesRegex(BackupError,'NOT_SELECTED'):verify_package(self.package.raw,self.model,self.parent)
    def test_upload_verified_index_only_after_restore(self):
        result=self.upload();self.assertEqual(result['status'],'UPLOADED_DOWNLOADED_SHA256_AND_RESTORE_VERIFIED')
        self.assertEqual(result['source']['revision'],20);self.assertEqual(result['ledger_closed_trades'],0)
        self.assertEqual(result['historical_restore_audit']['status'],'HISTORICAL_DOWNLOAD_AND_RESTORE_VERIFIED')
        self.assertEqual(len(self.drive.list('package')),1);self.assertEqual(len(self.drive.list('verified-index')),1)
    def test_duplicate_backup_reverified_no_second_package(self):
        first=self.upload();before={k:v['raw'] for k,v in self.memory.files.items()}
        second=self.upload();self.assertEqual(second['status'],'ALREADY_VERIFIED')
        self.assertEqual(len(self.drive.list('package')),1)
        for key,raw in before.items():self.assertEqual(self.memory.files[key]['raw'],raw)
    def test_failure_before_index_keeps_source_and_recovers_completed_upload(self):
        original=self.service._json_create
        def fail_index(file_id,name,doc,kind,key,rev):
            if kind=='verified-index':raise BackupError('SIMULATED_INDEX_NETWORK_FAILURE','test',True)
            return original(file_id,name,doc,kind,key,rev)
        self.service._json_create=fail_index
        with self.assertRaisesRegex(BackupError,'SIMULATED'):self.upload()
        self.assertEqual(len(self.drive.list('package')),1);self.assertEqual(len(self.drive.list('verified-index')),0)
        self.service._json_create=original
        self.assertEqual(self.upload()['status'],'UPLOADED_DOWNLOADED_SHA256_AND_RESTORE_VERIFIED')
        self.assertEqual(len(self.drive.list('package')),1)
    def test_failed_package_does_not_create_verified_index(self):
        original=self.drive.create
        def fail(file_id,name,raw,mime,kind,key,revision):
            if kind=='package':raise BackupError('SIMULATED_UPLOAD_FAILED','test')
            return original(file_id,name,raw,mime,kind,key,revision)
        self.drive.create=fail
        with self.assertRaises(BackupError):self.upload()
        self.assertEqual(len(self.drive.list('intent')),1);self.assertEqual(len(self.drive.list('verified-index')),0)
        self.drive.create=original;self.upload();self.assertEqual(len(self.drive.list('intent')),2)
    def test_remote_newer_ledger_refuses_old_source(self):
        self.upload();self.source.read_latest.return_value=(self.selection,self.parent)
        with self.assertRaisesRegex(BackupError,'NEWER_THAN_SELECTED'):self.upload()
        self.assertEqual(len(self.drive.list('package')),1)
    def test_ledger_history_continuity_checked(self):
        self.service._continuity(self.parent,self.artifact)
        bad=copy.deepcopy(self.artifact);bad.state['journal']=bad.state['journal'][1:]
        with self.assertRaisesRegex(BackupError,'HISTORY_ROLLBACK'):self.service._continuity(self.parent,bad)
    def test_source_advancing_leaves_no_false_latest_index(self):
        self.source.assert_current.side_effect=[None,BackupError('SOURCE_ADVANCED_RETRY_FROM_LATEST','source',True)]
        with self.assertRaisesRegex(BackupError,'SOURCE_ADVANCED'):self.upload()
        self.assertEqual(len(self.drive.list('package')),1);self.assertEqual(len(self.drive.list('verified-index')),0)
    def test_remote_corruption_never_silently_reuploaded(self):
        result=self.upload();self.memory.files[result['package_id']]['raw']=b'corrupt'
        with self.assertRaises(BackupError):self.upload()
        self.assertEqual(len(self.drive.list('package')),1)
    def test_weekly_restore_audit_is_append_only(self):
        self.upload();self.assertEqual(self.service.audit_if_due()['status'],'NOT_DUE')
        self.service.clock=lambda:now()+timedelta(days=8)
        self.assertEqual(self.service.audit_if_due()['status'],'HISTORICAL_DOWNLOAD_AND_RESTORE_VERIFIED')
        self.assertEqual(len(self.drive.list('restore-audit')),2)
    def test_invalid_remote_index_stops(self):
        self.upload();meta=self.drive.list('verified-index')[0];row=self.memory.files[meta['id']]
        row['raw']=b'{}';row['meta'].update(size='2',sha256Checksum=sha256(b'{}'))
        with self.assertRaisesRegex(BackupError,'REMOTE_JOURNAL_INVALID'):self.upload()
    def test_secret_sentinels_absent_from_package_and_remote_files(self):
        self.upload()
        for row in self.memory.files.values():
            for value in (b'SECRET_CLIENT_SENTINEL',b'SECRET_REFRESH_SENTINEL'):self.assertNotIn(value,row['raw'])
    def test_untrusted_execution_id_rejected(self):
        with self.assertRaises(BackupError):BackupService(self.drive,self.model,'../../bad')

if __name__=='__main__':unittest.main()
