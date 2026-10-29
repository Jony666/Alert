"""Complete transport package built from unchanged, verified phase-7 functions."""
from dataclasses import dataclass
import io
from pathlib import Path
import tempfile
import zipfile
from okx_v09_reporting import VERSION as REPORT_VERSION
from okx_v09_reporting.artifact import archive_files
from okx_v09_reporting.backup import create_backup,verify_backup,restore_backup
from okx_v09_reporting.common import canonical,read_json,sha256
from okx_v09_reporting.reporting import write_report,build_metrics
from . import VERSION
from .errors import BackupError

@dataclass(frozen=True)
class Package:
    raw: bytes
    manifest: dict
    backup_key: str
    @property
    def digest(self):return sha256(self.raw)


def fingerprint():
    root=Path(__file__).parent
    drive={p.name:sha256(p.read_bytes()) for p in sorted(root.glob('*.py'))}
    from okx_v09_reporting.reporting import code_version
    return sha256(canonical(dict(drive=drive,reporter=code_version()['code_inventory_sha256'])))


def source_identity(artifact):
    return dict(study_id=artifact.state['study_id'],revision=artifact.state['revision'],run_id=artifact.manifest['run_id'],
        artifact_id=artifact.context['artifact']['id'],artifact_sha256=artifact.digest,state_hash=artifact.manifest['state_hash'],
        config_hash=artifact.state['config_hash'],model_commit=artifact.state['freeze']['code_commit_sha'])


def backup_key(artifact):
    return sha256(canonical(dict(source=source_identity(artifact),exporter=VERSION,code=fingerprint())))


def prepare_package(artifact,model,execution_id):
    artifact.assert_unchanged();key=backup_key(artifact)
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp);record=create_backup(artifact,model,root/'vault');backup=root/'vault'/record['path']
        report=write_report(artifact,root/'report')
        files={'backup/'+p.name:p.read_bytes() for p in backup.iterdir() if p.is_file()}
        files.update({'report/'+p.name:p.read_bytes() for p in report.iterdir() if p.is_file()})
    manifest=dict(schema_version=1,format='OKX_V09_DRIVE_COMPLETE_V1',exporter_version=VERSION,
        report_version=REPORT_VERSION,exporter_code_fingerprint=fingerprint(),backup_key=key,
        source=source_identity(artifact),backup_manifest_sha256=record['backup_manifest_sha256'],
        backup_execution_id=execution_id,files={n:sha256(b) for n,b in files.items()},production_resume_authorized=False)
    files['package-manifest.json']=canonical(manifest)
    data=io.BytesIO()
    with zipfile.ZipFile(data,'w',compression=zipfile.ZIP_STORED) as z:
        for name,raw in sorted(files.items()):
            info=zipfile.ZipInfo(name,date_time=(1980,1,1,0,0,0));info.external_attr=0o100600<<16;z.writestr(info,raw)
    package=Package(data.getvalue(),manifest,key)
    verify_package(package.raw,model,artifact)
    return package


def verify_package(raw,model,expected_source=None):
    files=archive_files(raw);manifest=read_json(files.get('package-manifest.json',b'{}'))
    if manifest.get('schema_version')!=1 or manifest.get('format')!='OKX_V09_DRIVE_COMPLETE_V1':
        raise BackupError('PACKAGE_VERSION_UNSUPPORTED','restore')
    if (manifest.get('production_resume_authorized') is not False
            or manifest.get('backup_key')!=sha256(canonical(dict(source=manifest['source'],exporter=manifest['exporter_version'],code=manifest['exporter_code_fingerprint'])))):
        raise BackupError('PACKAGE_IDENTITY_INVALID','restore')
    if set(files)!=set(manifest['files'])|{'package-manifest.json'}:raise BackupError('PACKAGE_FILE_SET_MISMATCH','restore')
    if any(sha256(files[n])!=h for n,h in manifest['files'].items()):raise BackupError('PACKAGE_SHA256_FAILED','restore')
    for name in files:
        if name!='package-manifest.json' and not (name.startswith('backup/') or name.startswith('report/')):
            raise BackupError('PACKAGE_PATH_UNEXPECTED','restore')
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp)
        for name,body in files.items():
            path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(body)
        artifact,_=verify_backup(root/'backup',manifest['backup_manifest_sha256'],model)
        if manifest['source']!=source_identity(artifact):raise BackupError('PACKAGE_SOURCE_MISMATCH','restore')
        if expected_source and manifest['source']!=source_identity(expected_source):raise BackupError('PACKAGE_IS_NOT_SELECTED_LATEST_SOURCE','restore')
        report_manifest=read_json(files['report/report-manifest.json'])
        actual_report={n[len('report/'):] for n in files if n.startswith('report/')}
        if actual_report!=set(report_manifest['files'])|{'report-manifest.json'}:raise BackupError('REPORT_FILE_SET_MISMATCH','restore')
        if any(sha256(files['report/'+n])!=h for n,h in report_manifest['files'].items()):raise BackupError('REPORT_SHA256_FAILED','restore')
        metrics=read_json(files['report/metrics.json']);derived=build_metrics(artifact)
        if any(canonical(metrics[k])!=canonical(derived[k]) for k in ('portfolio','strategies','account','news_status','live_qualified')):
            raise BackupError('REPORT_DATA_MISMATCH','restore')
        if report_manifest['source_zip_sha256']!=artifact.digest:raise BackupError('REPORT_SOURCE_MISMATCH','restore')
        restore_backup(root/'backup',manifest['backup_manifest_sha256'],root/'offline-restore',model)
        if (root/'offline-restore/original-artifact.zip').read_bytes()!=artifact.raw:raise BackupError('RESTORE_BYTE_MISMATCH','restore')
        for name,body in artifact.files.items():
            if (root/'offline-restore/bundle'/name).read_bytes()!=body:raise BackupError('RESTORE_BYTE_MISMATCH','restore')
    return manifest,artifact
