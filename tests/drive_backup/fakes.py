"""In-memory Drive protocol simulator. This module never contacts Google."""
import re
from copy import deepcopy
from okx_v09_reporting.common import canonical,sha256,read_json
from okx_v09_drive_backup.drive import API,UPLOAD
from okx_v09_drive_backup.errors import BackupError
from okx_v09_drive_backup.policy import FOLDER_ID

class Response:
    def __init__(self,status=200,body=None,headers=None,content=None):
        self.status_code=status;self.headers=headers or {};self.content=canonical(body or {}) if content is None else content
    def json(self):return read_json(self.content)

class MemoryDrive:
    def __init__(self):
        self.files={};self.sessions={};self.counter=0;self.calls=[];self.failure=None
        self.sha_available=True;self.folder_status=200;self.can_write=True;self.shared=False
        self.quota={'limit':str(10**10),'usage':'0'};self.put_faults=[];self.omit_range=False
    def request(self,method,url,**kw):
        self.calls.append((method,url,deepcopy(kw)))
        if self.failure:
            failure=self.failure(method,url,kw)
            if failure is not None:return failure
        params=kw.get('params',{})
        if url==API+'/about':return Response(body={'storageQuota':self.quota})
        if url==API+'/files/generateIds':
            result=[]
            for _ in range(params['count']):self.counter+=1;result.append('id_'+str(self.counter))
            return Response(body={'ids':result})
        if url==API+'/files/'+FOLDER_ID:
            return Response(self.folder_status,dict(id=FOLDER_ID,mimeType='application/vnd.google-apps.folder',trashed=False,
                capabilities={'canAddChildren':self.can_write},**({'driveId':'shared'} if self.shared else {})))
        if method=='GET' and url==API+'/files':
            q=params['q'];kind=re.search("key='kind' and value='([^']+)'",q).group(1)
            match=re.search("key='backupKey' and value='([^']+)'",q);key=match.group(1) if match else None
            rows=[f['meta'] for f in self.files.values() if f['meta']['appProperties']['kind']==kind
                and (not key or f['meta']['appProperties']['backupKey']==key)]
            return Response(body={'files':rows})
        if method=='GET' and url.startswith(API+'/files/'):
            file_id=url.rsplit('/',1)[1];row=self.files.get(file_id)
            if not row:return Response(404)
            if params.get('alt')=='media':return Response(content=row['raw'])
            meta=deepcopy(row['meta'])
            if not self.sha_available:meta.pop('sha256Checksum',None)
            return Response(body=meta)
        if method=='POST' and url==UPLOAD:
            meta=kw['json'];file_id=meta['id']
            if file_id in self.files:return Response(409)
            self.counter+=1;location=UPLOAD+'?upload_id=session_'+str(self.counter)
            self.sessions[location]={'meta':deepcopy(meta),'data':bytearray(),'size':int(kw['headers']['X-Upload-Content-Length'])}
            return Response(200,headers={'Location':location})
        if method=='PUT' and url in self.sessions:
            session=self.sessions[url];total=session['size'];cr=kw['headers']['Content-Range']
            if cr.startswith('bytes */'):
                if len(session['data'])==total:return Response(200)
                return self.progress(session)
            fault=self.put_faults.pop(0) if self.put_faults else None
            if fault=='before':return Response(503)
            if fault=='expire':del self.sessions[url];return Response(410)
            start,end=map(int,re.search(r'bytes (\d+)-(\d+)/',cr).groups())
            if start!=len(session['data']):return Response(400)
            payload=kw['data']
            if fault=='partial':payload=payload[:max(1,len(payload)//2)]
            session['data'].extend(payload)
            if len(session['data'])==total:
                raw=bytes(session['data']);meta=dict(session['meta'],size=str(total),sha256Checksum=sha256(raw),trashed=False)
                self.files[meta['id']]={'raw':raw,'meta':meta}
            if fault=='after':raise BackupError('NETWORK_UNCERTAIN','drive',True)
            if len(session['data'])==total:return Response(200)
            return self.progress(session)
        return Response(404)
    def progress(self,session):
        n=len(session['data']);headers={'Range':'bytes=0-'+str(n-1)} if n and not self.omit_range else {}
        return Response(308,headers=headers)
