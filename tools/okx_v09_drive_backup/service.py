"""Append-only remote transaction journal. Never edits or deletes Drive files."""
import re
from okx_v09_reporting.common import canonical,read_json,sha256,now,at
from . import VERSION
from .errors import BackupError
from .package import prepare_package,verify_package,source_identity,backup_key


def _document(raw,kind):
    try:
        doc=read_json(raw)
        if (doc['schema_version']!=1 or doc['kind']!=kind or doc['exporter_version']!=VERSION
                or not re.fullmatch('[0-9a-f]{64}',doc['backup_key'])):raise ValueError()
        source=doc['source']
        if type(source['revision']) is not int or source['revision']<1:raise ValueError()
        for field in ('artifact_sha256','state_hash','config_hash'):
            if not re.fullmatch('[0-9a-f]{64}',source[field]):raise ValueError()
        at(doc['recorded_at_utc'])
        if kind in ('intent','verified-index'):
            if not re.fullmatch('[0-9a-f]{64}',doc['package_sha256']):raise ValueError()
            if type(doc['package_size']) is not int or not 0<doc['package_size']<=64*1024*1024:raise ValueError()
            for field in ('intent_id','package_id','index_id'):
                if not re.fullmatch('[A-Za-z0-9_-]+',doc[field]):raise ValueError()
        if kind=='verified-index' and doc['status']!='UPLOADED_DOWNLOADED_SHA256_AND_RESTORE_VERIFIED':raise ValueError()
        return doc
    except (KeyError,TypeError,ValueError):raise BackupError('REMOTE_JOURNAL_INVALID','integrity') from None


class BackupService:
    def __init__(self,drive,model,execution_id,clock=now):
        if not re.fullmatch('[A-Za-z0-9_.-]{1,100}',execution_id):raise BackupError('EXECUTION_ID_INVALID','policy')
        self.drive=drive;self.model=model;self.execution_id=execution_id;self.clock=clock
    def _base(self,kind,key,source):
        return dict(schema_version=1,kind=kind,exporter_version=VERSION,backup_key=key,
            source=source,execution_id=self.execution_id,recorded_at_utc=self.clock().isoformat())
    def _json_create(self,file_id,name,doc,kind,key,revision):
        self.drive.create(file_id,name,canonical(doc),'application/json',kind,key,revision)
        if read_json(self.drive.download(file_id,sha256(canonical(doc))))!=doc:
            raise BackupError('REMOTE_JOURNAL_READBACK_FAILED','integrity')
    def catalog(self):
        result=[];seen=set();revisions={}
        for meta in self.drive.list('verified-index'):
            doc=_document(self.drive.download(meta['id']),'verified-index')
            if doc['index_id']!=meta['id'] or doc['backup_key'] in seen:raise BackupError('REMOTE_INDEX_AMBIGUOUS','integrity')
            revision=doc['source']['revision']
            if revision in revisions and revisions[revision]!=doc['source']:raise BackupError('REMOTE_INDEX_REVISION_FORK','integrity')
            revisions[revision]=doc['source']
            seen.add(doc['backup_key']);result.append(doc)
        return result
    def _verify_remote(self,doc,expected=None):
        raw=self.drive.download(doc['package_id'],doc['package_sha256'],doc['package_size'])
        manifest,artifact=verify_package(raw,self.model,expected)
        if manifest['source']!=doc['source'] or manifest['backup_key']!=doc['backup_key']:
            raise BackupError('REMOTE_PACKAGE_INDEX_MISMATCH','integrity')
        if manifest['backup_manifest_sha256']!=doc['backup_manifest_sha256']:
            raise BackupError('REMOTE_BACKUP_SEAL_MISMATCH','integrity')
        return artifact
    def _continuity(self,prior,current):
        p=prior.state;c=current.state
        if p['study_id']!=c['study_id'] or p['config_hash']!=c['config_hash'] or p['revision']>c['revision']:
            raise BackupError('REMOTE_LEDGER_NEWER_OR_DIFFERENT','integrity')
        if p['revision']==c['revision']:
            if source_identity(prior)!=source_identity(current):raise BackupError('LEDGER_REVISION_FORK','integrity')
        elif (c['journal'][:len(p['journal'])]!=p['journal'] or c['closed'][:len(p['closed'])]!=p['closed']
              or any(c['watermarks'].get(k,'')<v for k,v in p['watermarks'].items())):
            raise BackupError('LEDGER_HISTORY_ROLLBACK','integrity')
    def upload_latest(self,source):
        selection,artifact=source.read_latest(self.model);key=backup_key(artifact);identity=source_identity(artifact)
        self.drive.preflight();catalog=self.catalog()
        if catalog:
            if any(d['source']['study_id']!=identity['study_id'] for d in catalog):raise BackupError('REMOTE_STUDY_MISMATCH','integrity')
            latest=max(catalog,key=lambda d:d['source']['revision'])
            if latest['source']['revision']>identity['revision']:raise BackupError('REMOTE_LEDGER_NEWER_THAN_SELECTED','integrity')
            self._continuity(self._verify_remote(latest),artifact)
        same=[d for d in catalog if d['backup_key']==key]
        if same:
            self._verify_remote(same[0],artifact);source.assert_current(selection)
            audit=self._base('audit',key,identity);audit.update(status='ALREADY_VERIFIED',package_id=same[0]['package_id'])
            self._json_create(self.drive.ids(1)[0],self.execution_id+'-duplicate-verification.json',audit,'audit',key,identity['revision'])
            result=dict(same[0]);result['status']='ALREADY_VERIFIED';result['this_execution_id']=self.execution_id
        else:
            recover=[]
            for meta in self.drive.list('intent',key):
                doc=_document(self.drive.download(meta['id']),'intent')
                if doc['intent_id']!=meta['id'] or doc['source']!=identity or doc['backup_key']!=key:
                    raise BackupError('REMOTE_INTENT_MISMATCH','integrity')
                if self.drive.metadata(doc['package_id'],missing_ok=True):
                    self._verify_remote(doc,artifact);recover.append(doc)
            if len(recover)>1:raise BackupError('MULTIPLE_COMPLETE_INTENTS_REQUIRE_REVIEW','integrity')
            if recover:doc=recover[0]
            else:
                package=prepare_package(artifact,self.model,self.execution_id)
                self.drive.preflight(len(package.raw)+32768)
                source.assert_current(selection)
                intent_id,package_id,index_id=self.drive.ids(3)
                doc=self._base('intent',key,identity)
                doc.update(intent_id=intent_id,package_id=package_id,index_id=index_id,
                    package_sha256=package.digest,package_size=len(package.raw),
                    backup_manifest_sha256=package.manifest['backup_manifest_sha256'])
                self._json_create(intent_id,self.execution_id+'-intent.json',doc,'intent',key,identity['revision'])
                self.drive.create(package_id,'okx-v09-r'+str(identity['revision'])+'-'+key[:16]+'.zip',
                    package.raw,'application/zip','package',key,identity['revision'])
                self._verify_remote(doc,artifact)
            # Never label a source latest after it has advanced. Complete unindexed uploads
            # remain immutable evidence, and can be reviewed; no stale index is committed.
            source.assert_current(selection)
            result=dict(doc,kind='verified-index',status='UPLOADED_DOWNLOADED_SHA256_AND_RESTORE_VERIFIED',
                verified_at_utc=self.clock().isoformat(),verified_execution_id=self.execution_id,
                ledger_closed_trades=len(artifact.state['closed']),full_offline_restore_verified=True)
            self._json_create(doc['index_id'],self.execution_id+'-verified-index.json',result,'verified-index',key,identity['revision'])
        result=dict(result,artifact_expiry=selection.expiry,latest_at_verified_check=True,
            production_state_written=False,external_notifications=False)
        result['historical_restore_audit']=self.audit_if_due()
        return result
    def audit_if_due(self):
        catalog=self.catalog()
        if not catalog:return dict(status='NO_VERIFIED_BACKUPS')
        last={}
        for meta in self.drive.list('restore-audit'):
            doc=_document(self.drive.download(meta['id']),'restore-audit')
            if doc.get('status')!='HISTORICAL_DOWNLOAD_AND_RESTORE_VERIFIED':raise BackupError('RESTORE_AUDIT_INVALID','integrity')
            stamp=at(doc['recorded_at_utc']);last[doc['backup_key']]=max(stamp,last.get(doc['backup_key'],stamp))
        # Oldest unchecked/least recently checked version first, bounded to one per day.
        target=min(catalog,key=lambda d:(last.get(d['backup_key'],at(d['recorded_at_utc'])),d['source']['revision']))
        checked=last.get(target['backup_key'])
        if checked and (self.clock()-checked).total_seconds()<7*86400:return dict(status='NOT_DUE',interval_days=7)
        self._verify_remote(target)
        key=target['backup_key'];doc=self._base('restore-audit',key,target['source'])
        doc.update(status='HISTORICAL_DOWNLOAD_AND_RESTORE_VERIFIED',package_id=target['package_id'],
            package_sha256=target['package_sha256'],production_resume_authorized=False)
        self._json_create(self.drive.ids(1)[0],self.execution_id+'-restore-audit.json',doc,'restore-audit',key,target['source']['revision'])
        return dict(status=doc['status'],revision=target['source']['revision'],package_id=target['package_id'])
