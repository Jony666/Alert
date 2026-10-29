"""Local Desktop OAuth + PKCE + Google-hosted Picker. Never runs in CI."""
import argparse
import base64
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler,HTTPServer
import os
from pathlib import Path
import secrets
import time
from urllib.parse import urlencode,urlparse,parse_qs
import webbrowser
import requests
from okx_v09_reporting.common import canonical,now,read_json
from .errors import BackupError
from .oauth import AUTH_URL,TOKEN_URL,exact_scope
from .policy import SCOPE,FOLDER_ID


def authorize_url(client_id,redirect_uri,state,verifier):
    challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    return AUTH_URL+'?'+urlencode(dict(client_id=client_id,redirect_uri=redirect_uri,response_type='code',scope=SCOPE,
        access_type='offline',prompt='consent',include_granted_scopes='false',state=state,
        code_challenge=challenge,code_challenge_method='S256',trigger_onepick='true',allow_multiple='false',
        allow_folder_selection='true',mimetypes='application/vnd.google-apps.folder',file_ids=FOLDER_ID))


def callback_values(query,expected_state):
    values=parse_qs(query,keep_blank_values=True)
    if any(len(v)!=1 for v in values.values()):raise BackupError('OAUTH_CALLBACK_DUPLICATE_FIELDS','authorization')
    flat={k:v[0] for k,v in values.items()}
    if not hmac.compare_digest(flat.get('state',''),expected_state):raise BackupError('OAUTH_STATE_MISMATCH','authorization')
    if flat.get('error'):raise BackupError('OAUTH_USER_CANCELLED_OR_POLICY_DENIED','authorization')
    if flat.get('picked_file_ids')!=FOLDER_ID:raise BackupError('PICKER_MUST_EXPLICITLY_SELECT_EXACT_TARGET','authorization')
    if not flat.get('code') or len(flat['code'])>4096:raise BackupError('OAUTH_CODE_MISSING','authorization')
    if 'scope' in flat:exact_scope(flat['scope'])
    return flat


def exchange_and_verify(client,code,redirect_uri,verifier,session=None):
    session=session or requests.Session()
    try:
        r=session.post(TOKEN_URL,data=dict(grant_type='authorization_code',client_id=client['client_id'],
            client_secret=client['client_secret'],code=code,redirect_uri=redirect_uri,code_verifier=verifier),
            timeout=(10,40),allow_redirects=False)
        if r.status_code!=200:raise BackupError('OAUTH_EXCHANGE_FAILED','authorization')
        body=r.json();exact_scope(body.get('scope'))
        if not body.get('refresh_token') or not body.get('access_token'):
            raise BackupError('OFFLINE_REFRESH_GRANT_REQUIRED','authorization')
        f=session.get('https://www.googleapis.com/drive/v3/files/'+FOLDER_ID,
            params={'fields':'id,mimeType,trashed,driveId,capabilities(canAddChildren)'},
            headers={'Authorization':'Bearer '+body['access_token']},timeout=(10,40),allow_redirects=False)
        if f.status_code!=200:raise BackupError('DRIVE_FILE_FOLDER_NOT_ACCESSIBLE_NO_SCOPE_ESCALATION','authorization')
        folder=f.json()
        if (folder.get('id')!=FOLDER_ID or folder.get('mimeType')!='application/vnd.google-apps.folder'
                or folder.get('trashed') or folder.get('driveId') or not folder.get('capabilities',{}).get('canAddChildren')):
            raise BackupError('PICKED_PERSONAL_FOLDER_NOT_WRITABLE','authorization')
    except requests.RequestException:raise BackupError('OAUTH_NETWORK_FAILED','authorization',True) from None
    except (ValueError,TypeError,KeyError) as exc:
        if isinstance(exc,BackupError):raise
        raise BackupError('OAUTH_RESPONSE_INVALID','authorization') from None
    return dict(type='authorized_user',client_id=client['client_id'],client_secret=client['client_secret'],
        refresh_token=body['refresh_token'],token_uri=TOKEN_URL,scopes=[SCOPE],folder_grant=dict(
            id=FOLDER_ID,oauth_client_id=client['client_id'],picker_selected=True,
            drive_file_get_verified=True,verified_at_utc=now().isoformat()))


def secure_credentials_file(path,credentials,repo_root):
    path=Path(path).expanduser().absolute();repo=Path(repo_root).resolve()
    if path.resolve().is_relative_to(repo):raise BackupError('CREDENTIALS_MUST_BE_OUTSIDE_REPOSITORY','authorization')
    for p in (path,*path.parents):
        if p.is_symlink():raise BackupError('CREDENTIAL_PATH_SYMLINK_REFUSED','authorization')
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
    try:
        payload=canonical(credentials)
        with os.fdopen(fd,'wb') as stream:stream.write(payload);stream.flush();os.fsync(stream.fileno())
    except Exception:
        # Never delete or replace any existing credential file; failed new file needs manual handling.
        raise BackupError('SECURE_CREDENTIAL_WRITE_FAILED','authorization') from None


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--client-json',required=True)
    p.add_argument('--credentials-out',required=True);p.add_argument('--port',type=int,default=0)
    args=p.parse_args(argv)
    if os.environ.get('GITHUB_ACTIONS')=='true':raise BackupError('AUTHORIZE_ON_PERSONAL_COMPUTER_NOT_ACTIONS','authorization')
    repo=Path(__file__).resolve().parents[2]
    client_path=Path(args.client_json).expanduser().resolve()
    if client_path.is_relative_to(repo):raise BackupError('CLIENT_JSON_MUST_BE_OUTSIDE_REPOSITORY','authorization')
    value=read_json(client_path.read_bytes());client=value.get('installed')
    if not client or not client.get('client_id') or not client.get('client_secret'):
        raise BackupError('DESKTOP_OAUTH_CLIENT_JSON_REQUIRED','authorization')
    out=Path(args.credentials_out).expanduser().absolute()
    if out.exists() or out.resolve().is_relative_to(repo):raise BackupError('CREDENTIAL_OUTPUT_UNSAFE_OR_EXISTS','authorization')
    state=secrets.token_urlsafe(32);verifier=secrets.token_urlsafe(64);result={}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass # Callback URLs contain codes. Never log them.
        def do_GET(self):
            if self.headers.get('Host')!='127.0.0.1:'+str(self.server.server_port) or urlparse(self.path).path!='/callback':
                self.send_response(404);self.end_headers();return
            try:
                fields=callback_values(urlparse(self.path).query,state)
                credentials=exchange_and_verify(client,fields['code'],redirect,verifier)
                secure_credentials_file(out,credentials,repo)
                result['status']='MINIMAL_SCOPE_AND_TARGET_READ_VERIFIED_NO_UPLOAD'
                message='已完成 drive.file 與指定資料夾選取／讀取驗證。憑證已安全儲存；尚未上傳任何備份。'
            except BackupError as exc:
                result['error']=exc;message='授權未完成，未擴大權限，未上傳。請回終端查看安全錯誤代碼。'
            self.send_response(200);self.send_header('Content-Type','text/plain; charset=utf-8')
            self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(message.encode())
    server=HTTPServer(('127.0.0.1',args.port),Handler);server.timeout=1
    redirect='http://127.0.0.1:'+str(server.server_port)+'/callback'
    try:
        if not webbrowser.open(authorize_url(client['client_id'],redirect,state,verifier)):
            raise BackupError('SYSTEM_BROWSER_REQUIRED','authorization')
        print('請在系統瀏覽器授權並明確選取指定資料夾；不會上傳備份。')
        deadline=time.monotonic()+600
        while not result and time.monotonic()<deadline:server.handle_request()
        if not result:raise BackupError('OAUTH_USER_TIMEOUT','authorization')
        if 'error' in result:raise result['error']
        print(result['status']);print('請依文件將檔案內容交由 gh secret set 的 stdin 儲存，勿貼到聊天或日誌。')
        return 0
    finally:server.server_close()

if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as exc:
        print(exc.code if isinstance(exc,BackupError) else 'AUTHORIZATION_LOCAL_FAILURE');raise SystemExit(2)
