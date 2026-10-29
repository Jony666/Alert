"""Append-only local backup vault. There is deliberately no network transport."""
import fcntl
from pathlib import Path
from . import VERSION
from .artifact import verify_artifact, verify_parent
from .common import VerificationError, atomic_directory, canonical, local_path, now, read_json, sha256
from .reporting import code_version

INDEX = 'backup-index.jsonl'
PAYLOAD_FILES = {'original-artifact.zip','source-context.json','source-inventory.json','tool-version.json'}


def _sealed_files(directory, expected_seal):
    directory=local_path(directory)
    if not isinstance(expected_seal,str) or len(expected_seal)!=64:
        raise VerificationError('an independently saved backup manifest SHA-256 is required')
    if directory.is_symlink() or not directory.is_dir(): raise VerificationError('backup directory missing/unsafe')
    entries=list(directory.iterdir())
    if any(p.is_symlink() or not p.is_file() for p in entries): raise VerificationError('unsafe backup member')
    if {p.name for p in entries} != PAYLOAD_FILES|{'backup-manifest.json'}:
        raise VerificationError('backup missing or unexpected files')
    raw=(directory/'backup-manifest.json').read_bytes()
    if sha256(raw)!=expected_seal: raise VerificationError('backup seal mismatch; never trust a recomputed replacement')
    manifest=read_json(raw)
    if manifest['schema_version']!=1 or manifest['backup_format']!='OKX_V09_LOCAL_COMPLETE_ARTIFACT_V1':
        raise VerificationError('unsupported backup version')
    if set(manifest['files'])!=PAYLOAD_FILES: raise VerificationError('backup manifest incomplete')
    files={name:(directory/name).read_bytes() for name in PAYLOAD_FILES}
    for name,expected in manifest['files'].items():
        if sha256(files[name])!=expected: raise VerificationError('backup file checksum mismatch: '+name)
    return manifest,files


def verify_backup(directory, expected_seal, model):
    manifest,files=_sealed_files(directory,expected_seal)
    artifact=verify_artifact(files['original-artifact.zip'],read_json(files['source-context.json']),model)
    if (manifest['source_zip_sha256']!=artifact.digest or manifest['revision']!=artifact.state['revision']
            or manifest['source_state_hash']!=artifact.manifest['state_hash'] or manifest['study_id']!=artifact.state['study_id']
            or manifest['run_id']!=artifact.manifest['run_id']
            or read_json(files['source-inventory.json'])!=artifact.inventory):
        raise VerificationError('backup provenance/inventory mismatch')
    version=read_json(files['tool-version.json'])
    if version['report_schema']!=1 or version['report_version']!=manifest['report_version']:
        raise VerificationError('backup tool-version mismatch')
    return artifact,manifest


def index_records(vault):
    vault=local_path(vault);path=vault/INDEX
    if not path.exists():return []
    if path.is_symlink():raise VerificationError('unsafe backup index')
    raw=path.read_bytes()
    if raw and not raw.endswith(b'\n'):raise VerificationError('interrupted index; manual audit required')
    rows=[read_json(line) for line in raw.splitlines()]
    seen=set()
    for row in rows:
        key=(row['study_id'],row['revision'])
        if key in seen:raise VerificationError('duplicate/forked backup revision in index')
        seen.add(key)
        rel=Path(row['path'])
        if rel.is_absolute() or '..' in rel.parts:raise VerificationError('unsafe backup index path')
        manifest,_=_sealed_files(vault/rel,row['backup_manifest_sha256'])
        if any(manifest[k]!=row[k] for k in ('revision','study_id','run_id','source_zip_sha256','source_state_hash')):
            raise VerificationError('backup index provenance mismatch')
    return rows


def _append_index(vault, record):
    import os
    fd=os.open(vault/INDEX,os.O_WRONLY|os.O_APPEND|os.O_CREAT|getattr(os,'O_NOFOLLOW',0),0o600)
    try:
        data=canonical(record)+b'\n'
        if os.write(fd,data)!=len(data):raise VerificationError('incomplete backup index append')
        os.fsync(fd)
    finally:os.close(fd)


def create_backup(artifact, model, vault):
    artifact.assert_unchanged()
    vault=local_path(vault)
    if (vault/'run_manifest.json').exists() or (vault/'state.json').exists():
        raise VerificationError('an existing ledger directory cannot become a backup vault')
    vault.mkdir(parents=True,exist_ok=True)
    lock_path=vault/'.backup.lock'
    if lock_path.is_symlink():raise VerificationError('unsafe backup lock')
    with lock_path.open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as exc:raise VerificationError('concurrent backup writer refused') from exc
        rows=index_records(vault)
        target=Path(artifact.state['study_id'])/f'revision-{artifact.state["revision"]:06d}'/f'run-{artifact.manifest["run_id"]}'/artifact.digest
        absolute=vault/target
        for row in rows:
            if row['revision']==artifact.state['revision']:
                if row['source_zip_sha256']!=artifact.digest or row['run_id']!=artifact.manifest['run_id']:
                    raise VerificationError('different source for an existing revision; no replacement')
                verify_backup(vault/row['path'],row['backup_manifest_sha256'],model)
                return dict(status='ALREADY_VERIFIED',**row)
        lineage='CURRENT_CUMULATIVE_JOURNAL_VERIFIED; EARLIER_ARTIFACTS_NOT_ALL_PRESENT'
        if rows:
            latest=max(rows,key=lambda x:x['revision'])
            old,_=verify_backup(vault/latest['path'],latest['backup_manifest_sha256'],model)
            earlier,later=(old,artifact) if old.state['revision']<artifact.state['revision'] else (artifact,old)
            if (later.state['journal'][:len(earlier.state['journal'])]!=earlier.state['journal']
                    or later.state['closed'][:len(earlier.state['closed'])]!=earlier.state['closed']
                    or any(later.state['watermarks'][k]<v for k,v in earlier.state['watermarks'].items())):
                raise VerificationError('backup history fork/rollback refused')
            if earlier.state['revision']+1==later.state['revision']:
                verify_parent(earlier,later);lineage='DIRECT_PARENT_AND_JOURNAL_VERIFIED'
            else:lineage='JOURNAL_PREFIX_VERIFIED; INTERMEDIATE_ARTIFACT_GAP_DISCLOSED'
        payload={'original-artifact.zip':artifact.raw,'source-context.json':canonical(artifact.context),
                 'source-inventory.json':canonical(artifact.inventory),'tool-version.json':canonical(code_version())}
        manifest=dict(schema_version=1,backup_format='OKX_V09_LOCAL_COMPLETE_ARTIFACT_V1',report_version=VERSION,
            created_at_utc=now().isoformat(),study_id=artifact.state['study_id'],revision=artifact.state['revision'],
            run_id=artifact.manifest['run_id'],source_zip_sha256=artifact.digest,source_state_hash=artifact.manifest['state_hash'],
            lineage=lineage,external_transfer_performed=False,production_resume_authorized=False,
            files={name:sha256(raw) for name,raw in payload.items()})
        seal=sha256(canonical(manifest))
        if absolute.exists():
            # Recover an intact orphan left after directory commit but before index append.
            old_manifest=read_json((absolute/'backup-manifest.json').read_bytes())
            old_seal=sha256(canonical(old_manifest))
            recovered,_=verify_backup(absolute,old_seal,model)
            if recovered.raw!=artifact.raw or canonical(recovered.context)!=canonical(artifact.context):
                raise VerificationError('orphan does not match source; never overwrite')
            seal=old_seal
        else:
            atomic_directory(absolute,{**payload,'backup-manifest.json':canonical(manifest)})
        record=dict(study_id=artifact.state['study_id'],revision=artifact.state['revision'],run_id=artifact.manifest['run_id'],
                    source_zip_sha256=artifact.digest,source_state_hash=artifact.manifest['state_hash'],
                    path=target.as_posix(),backup_manifest_sha256=seal)
        _append_index(vault,record)
        return dict(status='LOCAL_BACKUP_COMMITTED',**record)


def restore_backup(directory, expected_seal, destination, model):
    artifact,manifest=verify_backup(directory,expected_seal,model)
    # Export only to a new independent directory. Never choose a production checkpoint.
    files={**{'bundle/'+n:b for n,b in artifact.files.items()},'original-artifact.zip':artifact.raw,
           'source-context.json':canonical(artifact.context),'backup-manifest.json':canonical(manifest),
           'RESTORE-READ-ME.txt':('離線恢復副本；不是正式交易帳本啟用。\n不得直接匯入排程或忽略原新鮮度、資金、新聞與全倉檢查。\n').encode()}
    out=atomic_directory(destination,files)
    return dict(status='RESTORED_TO_NEW_OFFLINE_DIRECTORY',destination=str(out),revision=artifact.state['revision'],
                original_zip_sha256=artifact.digest,production_resume_authorized=False)


def run_plan(config, artifact=None, model=None):
    expected={'schema_version','enabled','destination','destination_confirmed','external_transfer_enabled'}
    if set(config)!=expected or config['schema_version']!=1:raise VerificationError('unknown backup plan schema')
    if any(type(config[k]) is not bool for k in ('enabled','destination_confirmed','external_transfer_enabled')):
        raise VerificationError('backup flags must be booleans')
    if config['external_transfer_enabled']:
        raise VerificationError('external transfer is not implemented or authorized')
    if not config['enabled']:
        return dict(status='DISABLED_STORAGE_DESTINATION_UNCONFIRMED',external_transfer_performed=False)
    if not config['destination_confirmed'] or not config['destination']:
        raise VerificationError('backup destination requires explicit confirmation')
    local_path(config['destination'])
    if artifact is None or model is None:raise VerificationError('verified source and model required for local backup')
    return create_backup(artifact,model,config['destination'])
