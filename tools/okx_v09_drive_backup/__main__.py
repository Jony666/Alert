"""Independent backup CLI. Defaults to GET-only plan; live transfer needs two gates."""
import argparse
import os
from pathlib import Path
from okx_v09_reporting.model import Model
from okx_v09_reporting.common import canonical,read_json
from .policy import load_config,permit_live
from .errors import BackupError
from .github_source import GitHubSource
from .oauth import UserCredentials,OAuthSession
from .drive import HTTPTransport,DriveClient
from .service import BackupService
from .package import source_identity


def run(args,environ=None,source_factory=GitHubSource,transport_factory=HTTPTransport):
    env=os.environ if environ is None else environ
    config=load_config(Path(args.config))
    permit=None
    # Refuse before reading the credential secret or constructing a network client.
    if args.command!='plan':permit=permit_live(config,env.get('OKX_V09_DRIVE_APPROVAL',''))
    model=Model(args.model_root)
    token=env.get('GH_TOKEN','')
    if not token:raise BackupError('GITHUB_READ_TOKEN_REQUIRED','source')
    source=source_factory(token)
    if args.command=='plan':
        selection,artifact=source.read_latest(model)
        return dict(status='PLAN_ONLY_NO_GOOGLE_ACCESS',source=source_identity(artifact),artifact_expiry=selection.expiry,
            intended_folder_id=config['folder_id'],external_upload_performed=False,
            oauth_folder_access_verified=False,daily_schedule_enabled=False)
    try:credentials=UserCredentials.parse(read_json(env['OKX_V09_DRIVE_OAUTH']))
    except (KeyError,TypeError,ValueError) as exc:
        if isinstance(exc,BackupError):raise
        raise BackupError('OAUTH_SECRET_MISSING_OR_INVALID','oauth') from None
    drive=DriveClient(transport_factory(OAuthSession(credentials),permit))
    service=BackupService(drive,model,env.get('GITHUB_RUN_ID','local')+'-'+env.get('GITHUB_RUN_ATTEMPT','1'))
    return service.upload_latest(source)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['plan','upload'],default='plan',nargs='?')
    parser.add_argument('--config',required=True);parser.add_argument('--model-root',required=True)
    parser.add_argument('--result',required=True)
    args=parser.parse_args(argv)
    try:result=run(args);exit_code=0
    except Exception as exc:
        # Deliberately suppress raw HTTP bodies, exception strings, URLs, and tracebacks.
        result=exc.record() if isinstance(exc,BackupError) else BackupError('UNEXPECTED_SAFE_STOP','validation').record()
        exit_code=2
    path=Path(args.result)
    if path.exists():
        print(canonical(BackupError('RESULT_PATH_EXISTS','local').record()).decode());return 2
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('xb') as stream:stream.write(canonical(result))
    print(canonical(result).decode())
    return exit_code

if __name__=='__main__':raise SystemExit(main())
