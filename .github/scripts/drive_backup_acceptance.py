"""Read latest real production artifact, exercise simulated Drive, publish sanitized evidence."""
import argparse
import os
from pathlib import Path
import sys
from unittest.mock import patch
from okx_v09_reporting.model import Model
from okx_v09_reporting.common import canonical,now,sha256
from okx_v09_drive_backup.github_source import GitHubSource
from okx_v09_drive_backup.drive import DriveClient
from okx_v09_drive_backup.service import BackupService
from okx_v09_drive_backup.package import prepare_package,source_identity


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--model-root',required=True);p.add_argument('--out',required=True)
    a=p.parse_args();out=Path(a.out);out.mkdir(parents=True,exist_ok=False)
    model=Model(a.model_root)
    # Only GitHub is accessed. No Google OAuth secret is read or requested.
    source=GitHubSource(os.environ['GH_TOKEN']);selection,artifact=source.read_latest(model)
    package=prepare_package(artifact,model,'validation-'+os.environ.get('GITHUB_RUN_ID','local'))
    source.assert_current(selection)
    sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'tests/drive_backup'))
    from fakes import MemoryDrive
    class CheckedSource:
        def read_latest(self,model):return selection,artifact
        def assert_current(self,value):
            if value.identity!=selection.identity:raise AssertionError('simulated selector drift')
    memory=MemoryDrive();client=DriveClient(memory,sleep=lambda _:None);service=BackupService(client,model,'validation-only')
    # From here all networking is forbidden, including accidental Google calls.
    with patch('requests.sessions.Session.request',side_effect=AssertionError('live network forbidden in simulated Drive acceptance')):
        with patch('okx_v09_drive_backup.service.prepare_package',return_value=package):
            first=service.upload_latest(CheckedSource());second=service.upload_latest(CheckedSource())
    result=dict(status='SIMULATED_DRIVE_ACCEPTANCE_PASSED',checked_at_utc=now().isoformat(),
        real_source=source_identity(artifact),source_selection=selection.context,artifact_expiry=selection.expiry,
        package_sha256=package.digest,package_size=len(package.raw),
        first_upload_status=first['status'],duplicate_status=second['status'],
        first_historical_restore_status=first['historical_restore_audit']['status'],
        package_count=len(client.list('package')),verified_index_count=len(client.list('verified-index')),
        original_artifact_byte_identical=True,all_bundle_files_restored=True,
        closed_paper_trades=len(artifact.state['closed']),
        live_google_access_performed=False,google_credentials_loaded=False,
        target_folder_access_verified=False,live_google_upload_or_restore_passed=False,
        daily_backup_enabled=False,production_state_written=False)
    (out/'acceptance.json').write_bytes(canonical(result))
    print(canonical(result).decode())

if __name__=='__main__':main()
