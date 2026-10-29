"""GET-only producer for Apps Script: keep all existing Python verification intact."""
import argparse
import os
from pathlib import Path
from okx_v09_reporting.model import Model
from okx_v09_reporting.common import canonical,now
from okx_v09_drive_backup.github_source import GitHubSource
from okx_v09_drive_backup.package import prepare_package,source_identity,fingerprint,verify_package


def build(source,model,execution_id,out):
    selected,artifact=source.read_latest(model)
    package=prepare_package(artifact,model,execution_id)
    verify_package(package.raw,model,artifact)
    source.assert_current(selected)
    envelope=dict(schema=1,format='OKX_V09_APPS_SCRIPT_EXPORT_V1',
        generated_at=now().isoformat(),source=source_identity(artifact),source_context=selected.context,
        backup_key=package.backup_key,package_sha256=package.digest,package_size=len(package.raw),
        exporter_fingerprint=fingerprint(),backup_manifest_sha256=package.manifest['backup_manifest_sha256'],
        python_full_verification=True,python_full_restore_verified=True,
        expiry=selected.expiry,production_state_written=False)
    # Never emit a plausible smaller/partial package when Apps Script limits are exceeded.
    if len(package.raw)>12*1024*1024:raise ValueError('APPS_SCRIPT_PACKAGE_CAP_EXCEEDED')
    out=Path(out);out.mkdir(parents=True,exist_ok=False)
    (out/'package.zip').write_bytes(package.raw);(out/'export.json').write_bytes(canonical(envelope))
    return envelope

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--model-root',required=True);p.add_argument('--out',required=True)
    a=p.parse_args()
    try:
        result=build(GitHubSource(os.environ['GH_TOKEN']),Model(a.model_root),os.environ.get('GITHUB_RUN_ID','local'),a.out)
        print(canonical(dict(status='LOCAL_EXPORT_FULLY_VERIFIED_NO_DRIVE_UPLOAD',source=result['source'],package_sha256=result['package_sha256'])).decode())
    except Exception:
        print('EXPORT_FAILED_SAFE_STOP_NO_DRIVE_UPLOAD');raise SystemExit(2)
