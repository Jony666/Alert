"""Minimal Drive v3: create-only files, GET/list, resumable uploads and byte verification."""
import re
import time
from urllib.parse import urlparse
import requests
from okx_v09_reporting.common import sha256,read_json
from .errors import BackupError
from .policy import APP,FOLDER_ID,LivePermit

API='https://www.googleapis.com/drive/v3'
UPLOAD='https://www.googleapis.com/upload/drive/v3/files'
META='id,name,mimeType,parents,size,sha256Checksum,appProperties,trashed,createdTime'
TRANSIENT={429,500,502,503,504}


def allowed_session(url):
    p=urlparse(url)
    if (p.scheme!='https' or p.hostname not in ('www.googleapis.com','content.googleapis.com')
            or p.port not in (None,443) or p.username or p.password
            or not p.path.startswith('/upload/drive/v3/files')):
        raise BackupError('UNTRUSTED_UPLOAD_SESSION','drive')
    return url


class HTTPTransport:
    def __init__(self,oauth,permit,session=None):
        if not isinstance(permit,LivePermit) or not permit.enabled or permit.folder_id!=FOLDER_ID or permit.approval!='I_APPROVE_ONE_DRIVE_BACKUP':
            raise BackupError('LIVE_TRANSFER_NOT_APPROVED','policy')
        self.oauth=oauth;self.session=session or requests.Session()
    def __repr__(self):return 'HTTPTransport(<redacted>)'
    def request(self,method,url,**kwargs):
        if method not in ('GET','POST','PUT'):raise BackupError('DRIVE_MUTATION_FORBIDDEN','drive')
        if method=='PUT':allowed_session(url)
        elif not (url==API+'/about' or url.startswith(API+'/files') or (method=='POST' and url==UPLOAD)):
            raise BackupError('UNTRUSTED_DRIVE_ENDPOINT','drive')
        if method=='POST' and url!=UPLOAD:raise BackupError('DRIVE_CREATE_ONLY','drive')
        headers=dict(kwargs.pop('headers',{}));headers['Authorization']='Bearer '+self.oauth.access_token()
        try:
            response=self.session.request(method,url,headers=headers,timeout=(10,120),allow_redirects=False,**kwargs)
            if response.status_code==401:
                headers['Authorization']='Bearer '+self.oauth.access_token(force=True)
                response=self.session.request(method,url,headers=headers,timeout=(10,120),allow_redirects=False,**kwargs)
            return response
        except requests.RequestException:raise BackupError('NETWORK_UNCERTAIN','drive',True) from None


def _rate_limited(response):
    if response.status_code!=403:return False
    try:
        payload=response.json();reasons=[e.get('reason') for e in payload.get('error',{}).get('errors',[])]
        return any(r in ('rateLimitExceeded','userRateLimitExceeded') for r in reasons)
    except (ValueError,TypeError,AttributeError):return False


class DriveClient:
    def __init__(self,transport,folder_id=FOLDER_ID,sleep=time.sleep,max_attempts=5,chunk_size=8*1024*1024):
        if folder_id!=FOLDER_ID:raise BackupError('UNREVIEWED_DESTINATION','drive')
        if chunk_size<=0 or chunk_size%(256*1024):raise BackupError('UPLOAD_CHUNK_SIZE_INVALID','drive')
        self.transport=transport;self.folder_id=folder_id;self.sleep=sleep;self.max_attempts=max_attempts;self.chunk_size=chunk_size
    def _retry(self,method,url,**kwargs):
        for attempt in range(self.max_attempts):
            try:response=self.transport.request(method,url,**kwargs)
            except BackupError as exc:
                if not exc.retryable:raise
                response=None
            if response is not None and response.status_code not in TRANSIENT and not _rate_limited(response):return response
            if attempt+1<self.max_attempts:self.sleep(min(2**attempt,8))
        raise BackupError('DRIVE_RETRIES_EXHAUSTED','drive',True)
    def _json(self,response):
        try:return read_json(response.content)
        except (ValueError,TypeError):raise BackupError('DRIVE_JSON_INVALID','drive') from None
    def _success(self,response):
        if response.status_code not in (200,201):
            code='DRIVE_ACCESS_DENIED_NO_SCOPE_ESCALATION' if response.status_code in (401,403,404) else 'DRIVE_HTTP_FAILED'
            raise BackupError(code,'drive')
        return self._json(response)
    def preflight(self,required_bytes=0):
        response=self._retry('GET',API+'/files/'+self.folder_id,params={'fields':'id,mimeType,trashed,driveId,capabilities(canAddChildren)'})
        if response.status_code in (401,403,404):raise BackupError('DRIVE_FILE_FOLDER_NOT_ACCESSIBLE_USE_SAME_APP_PICKER','authorization')
        folder=self._success(response)
        if (folder.get('id')!=self.folder_id or folder.get('mimeType')!='application/vnd.google-apps.folder'
                or folder.get('trashed') or not folder.get('capabilities',{}).get('canAddChildren')
                or folder.get('driveId')):
            raise BackupError('PERSONAL_FOLDER_NOT_WRITABLE','authorization')
        about=self._success(self._retry('GET',API+'/about',params={'fields':'storageQuota(limit,usage)'}))
        try:quota=about['storageQuota'];limit=int(quota['limit']);usage=int(quota['usage'])
        except (KeyError,TypeError,ValueError):raise BackupError('PERSONAL_STORAGE_QUOTA_UNVERIFIED','quota') from None
        if limit<=0 or usage<0:raise BackupError('PERSONAL_STORAGE_QUOTA_UNVERIFIED','quota')
        if limit-usage<required_bytes+65536:raise BackupError('PERSONAL_DRIVE_QUOTA_INSUFFICIENT','quota')
        return dict(folder_access_verified=True,quota_checked=True,available_bytes=limit-usage)
    def list(self,kind,backup_key=None):
        if kind not in ('intent','package','verified-index','audit','restore-audit'):
            raise BackupError('DRIVE_QUERY_KIND_INVALID','drive')
        q="'"+self.folder_id+"' in parents and trashed = false and appProperties has { key='application' and value='"+APP+"' } and appProperties has { key='kind' and value='"+kind+"' }"
        if backup_key:
            if not re.fullmatch('[0-9a-f]{64}',backup_key):raise BackupError('BACKUP_KEY_INVALID','drive')
            q+=" and appProperties has { key='backupKey' and value='"+backup_key+"' }"
        rows=[];page=None;seen=set()
        for _ in range(100):
            params={'q':q,'spaces':'drive','pageSize':100,'fields':'nextPageToken,files('+META+')'}
            if page:params['pageToken']=page
            body=self._success(self._retry('GET',API+'/files',params=params));rows+=body.get('files',[])
            page=body.get('nextPageToken')
            if not page:return rows
            if page in seen:raise BackupError('DRIVE_PAGINATION_STALLED','drive')
            seen.add(page)
        raise BackupError('DRIVE_PAGINATION_INCOMPLETE','drive')
    def ids(self,count):
        body=self._success(self._retry('GET',API+'/files/generateIds',params={'count':count,'space':'drive','type':'files'}))
        ids=body.get('ids',[])
        if len(ids)!=count or len(set(ids))!=count or any(not re.fullmatch('[A-Za-z0-9_-]+',x) for x in ids):raise BackupError('DRIVE_IDS_INVALID','drive')
        return ids
    def metadata(self,file_id,missing_ok=False):
        if not re.fullmatch('[A-Za-z0-9_-]+',str(file_id)):raise BackupError('DRIVE_FILE_ID_INVALID','drive')
        response=self._retry('GET',API+'/files/'+file_id,params={'fields':META})
        if response.status_code==404 and missing_ok:return None
        return self._success(response)
    def download(self,file_id,expected_sha=None,expected_size=None):
        meta=self.metadata(file_id)
        if meta.get('trashed') or self.folder_id not in meta.get('parents',[]):raise BackupError('REMOTE_FILE_OUTSIDE_TARGET','integrity')
        try:size=int(meta['size'])
        except (KeyError,ValueError,TypeError):raise BackupError('REMOTE_SIZE_INVALID','integrity') from None
        if not 0<=size<=64*1024*1024:raise BackupError('REMOTE_FILE_TOO_LARGE','integrity')
        response=self._retry('GET',API+'/files/'+file_id,params={'alt':'media'})
        if response.status_code!=200:raise BackupError('DRIVE_DOWNLOAD_FAILED','integrity')
        raw=response.content
        if len(raw)>128*1024*1024:raise BackupError('REMOTE_FILE_TOO_LARGE','integrity')
        digest=sha256(raw)
        if str(len(raw))!=str(meta.get('size')) or (expected_size is not None and len(raw)!=expected_size):raise BackupError('REMOTE_SIZE_MISMATCH','integrity')
        if meta.get('sha256Checksum') and meta['sha256Checksum']!=digest:raise BackupError('REMOTE_API_SHA256_MISMATCH','integrity')
        if expected_sha and digest!=expected_sha:raise BackupError('REMOTE_DOWNLOADED_SHA256_MISMATCH','integrity')
        return raw
    def _offset(self,response,total):
        if response.status_code in (200,201):return total
        if response.status_code!=308:raise BackupError('UPLOAD_STATUS_INVALID','upload',response.status_code in TRANSIENT)
        value=response.headers.get('Range')
        if not value:return 0
        match=re.fullmatch(r'bytes=0-(\d+)',value)
        if not match:raise BackupError('UPLOAD_RANGE_INVALID','upload')
        offset=int(match.group(1))+1
        if offset>total:raise BackupError('UPLOAD_RANGE_INVALID','upload')
        return offset
    def create(self,file_id,name,raw,mime,kind,backup_key,revision):
        if not raw or len(raw)>128*1024*1024:raise BackupError('UPLOAD_SIZE_INVALID','upload')
        existing=self.metadata(file_id,missing_ok=True)
        if existing:
            self.download(file_id,sha256(raw),len(raw));return existing
        meta=dict(id=file_id,name=name,mimeType=mime,parents=[self.folder_id],appProperties=dict(
            application=APP,kind=kind,backupKey=backup_key,revision=str(revision)))
        total=len(raw);digest=sha256(raw)
        for restart in range(self.max_attempts):
            response=self._retry('POST',UPLOAD,params={'uploadType':'resumable','fields':META},json=meta,
                headers={'X-Upload-Content-Type':mime,'X-Upload-Content-Length':str(total)})
            if response.status_code==409:
                self.download(file_id,digest,total);return self.metadata(file_id)
            if response.status_code not in (200,201):raise BackupError('UPLOAD_SESSION_CREATE_FAILED','upload')
            location=allowed_session(response.headers.get('Location',''))
            offset=0;failures=0;restart_session=False
            while offset<total:
                end=min(offset+self.chunk_size,total)
                try:reply=self.transport.request('PUT',location,data=raw[offset:end],headers={
                    'Content-Type':mime,'Content-Length':str(end-offset),'Content-Range':f'bytes {offset}-{end-1}/{total}'})
                except BackupError as exc:
                    if not exc.retryable:raise
                    reply=None
                if reply is not None and reply.status_code in (200,201,308):
                    acknowledged=self._offset(reply,total)
                    if acknowledged<=offset and acknowledged<total:
                        failures+=1
                    else:failures=0
                    if acknowledged<offset:raise BackupError('UPLOAD_OFFSET_ROLLBACK','upload')
                    offset=acknowledged
                else:
                    failures+=1
                    if reply is not None and reply.status_code not in TRANSIENT and reply.status_code not in (404,410) and not _rate_limited(reply):
                        raise BackupError('UPLOAD_PERMISSION_OR_PROTOCOL_FAILED','upload')
                    self.sleep(min(2**min(failures-1,3),8))
                    status=self._retry('PUT',location,data=b'',headers={'Content-Length':'0','Content-Range':f'bytes */{total}'})
                    if status.status_code in (404,410):restart_session=True;break
                    new_offset=self._offset(status,total)
                    if new_offset<offset:raise BackupError('UPLOAD_OFFSET_ROLLBACK','upload')
                    offset=new_offset
                if failures>=self.max_attempts:raise BackupError('UPLOAD_RETRIES_EXHAUSTED','upload',True)
            if not restart_session:
                self.download(file_id,digest,total)
                return self.metadata(file_id)
            completed=self.metadata(file_id,missing_ok=True)
            if completed:
                self.download(file_id,digest,total);return completed
        raise BackupError('UPLOAD_SESSION_RESTARTS_EXHAUSTED','upload',True)
