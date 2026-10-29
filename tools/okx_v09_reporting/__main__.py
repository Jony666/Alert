import argparse
import json
from pathlib import Path
from .artifact import verify_artifact
from .backup import create_backup, restore_backup, run_plan
from .common import VerificationError, read_json
from .model import Model
from .reporting import write_report


def main(argv=None):
    parser=argparse.ArgumentParser(description='OKX v0.9 離線唯讀報表與本機備份；沒有外部傳輸、策略、下單或推播功能')
    sub=parser.add_subparsers(dest='command',required=True)
    for name in ('verify','report','backup'):
        p=sub.add_parser(name);p.add_argument('artifact');p.add_argument('--source-context',required=True)
        p.add_argument('--model-root',required=True,help='精確凍結 commit 的 okx-monitor/backtest 目錄')
        p.add_argument('--parent-artifact');p.add_argument('--parent-context')
        if name!='verify':p.add_argument('--out',required=True)
    p=sub.add_parser('restore');p.add_argument('backup_directory');p.add_argument('--backup-sha256',required=True)
    p.add_argument('--model-root',required=True);p.add_argument('--out',required=True)
    p=sub.add_parser('backup-plan');p.add_argument('--config',required=True)
    args=parser.parse_args(argv)
    try:
        if args.command=='backup-plan':
            config=read_json(Path(args.config).read_bytes())
            # CLI does not enable an automatic writer. Plan activation is a separately reviewed integration.
            result=run_plan(config)
        else:
            model=Model(args.model_root)
            if args.command=='restore':
                result=restore_backup(args.backup_directory,args.backup_sha256,args.out,model)
            else:
                if bool(args.parent_artifact)!=bool(args.parent_context):raise VerificationError('both parent artifact and context are required')
                parent=verify_artifact(args.parent_artifact,args.parent_context,model) if args.parent_artifact else None
                source=verify_artifact(args.artifact,args.source_context,model,parent)
                if args.command=='verify':result=source.provenance()
                elif args.command=='report':result=dict(status='REPORT_WRITTEN',directory=str(write_report(source,args.out)),source=source.provenance())
                else:result=create_backup(source,model,args.out)
        print(json.dumps(result,indent=2,ensure_ascii=False));return 0
    except (VerificationError,OSError,ValueError,KeyError) as exc:
        print(json.dumps(dict(status='REFUSED',error_type=type(exc).__name__,message=str(exc).split('?')[0],
                              source_unchanged=True,production_resume_authorized=False),ensure_ascii=False))
        return 2

if __name__=='__main__':raise SystemExit(main())
