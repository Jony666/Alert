"""Read-only E2E: report, local backup, seal verification, restore, repeat and parent history."""
from pathlib import Path
import json
import os
import argparse

from okx_v09_reporting.model import Model
from okx_v09_reporting.artifact import verify_artifact
from okx_v09_reporting.backup import create_backup,restore_backup,index_records,run_plan
from okx_v09_reporting.reporting import write_report
from okx_v09_reporting.common import sha256,read_json

def main(args):
    out=Path(args.out);out.mkdir(parents=True,exist_ok=False)
    model=Model(args.model_root)
    parent=verify_artifact(args.parent_artifact,args.parent_context,model)
    source=verify_artifact(args.artifact,args.source_context,model,parent)
    before=sha256(Path(args.artifact).read_bytes())
    write_report(source,out/'report')
    first=create_backup(parent,model,out/'local-test-vault')
    latest=create_backup(source,model,out/'local-test-vault')
    repeated=create_backup(source,model,out/'local-test-vault')
    assert repeated['status']=='ALREADY_VERIFIED' and repeated['backup_manifest_sha256']==latest['backup_manifest_sha256']
    result=restore_backup(out/'local-test-vault'/latest['path'],latest['backup_manifest_sha256'],out/'offline-restored-copy',model)
    assert (out/'offline-restored-copy/original-artifact.zip').read_bytes()==source.raw
    for name,raw in source.files.items():assert (out/'offline-restored-copy/bundle'/name).read_bytes()==raw
    assert sha256(Path(args.artifact).read_bytes())==before
    plan=run_plan(read_json(Path(args.plan).read_bytes()))
    assert plan['status']=='DISABLED_STORAGE_DESTINATION_UNCONFIRMED'
    report=dict(status='PHASE7_REAL_ARTIFACT_ACCEPTANCE_PASSED',source=source.provenance(),
                source_revision=source.state['revision'],closed_trades=len(source.state['closed']),
                report_created=True,full_22_file_inventory_verified=len(source.inventory)==22,
                original_source_unchanged=True,byte_identical_restore=True,backup_idempotent=True,
                backup_revisions=[r['revision'] for r in index_records(out/'local-test-vault')],
                backup_manifest_sha256=latest['backup_manifest_sha256'],backup_result=result,
                automatic_backup_enabled=False,external_transfer_enabled=False,production_ledger_modified=False)
    (out/'acceptance.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'],'a') as f:
            f.write('Phase 7 report/backup acceptance: no production ledger or schedule changes.\n\n```json\n'+json.dumps(report,indent=2)+'\n```\n')

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for arg in ('artifact','source-context','parent-artifact','parent-context','model-root','plan','out'):p.add_argument('--'+arg,required=True)
    main(p.parse_args())
