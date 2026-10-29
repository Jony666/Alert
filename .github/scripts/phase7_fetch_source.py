"""Explicit CI input acquisition: GitHub GET only, no external backup destination or upload."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import requests

REPO='Jony666/Alert'

def fetch(run_id, artifact_id, destination):
    if not re.fullmatch(r'[1-9][0-9]*',str(run_id)) or not re.fullmatch(r'[1-9][0-9]*',str(artifact_id)):
        raise ValueError('numeric GitHub identities required')
    session=requests.Session();session.headers.update(Authorization='Bearer '+os.environ['GH_TOKEN'],Accept='application/vnd.github+json')
    root='https://api.github.com/repos/'+REPO+'/'
    def get(path):
        if not path.startswith('actions/'):raise ValueError('only Actions GET paths supported')
        r=session.get(root+path,timeout=(10,120));r.raise_for_status();return r
    run=get('actions/runs/'+str(run_id)).json();artifact=get('actions/artifacts/'+str(artifact_id)).json()
    if run['status']!='completed' or run['conclusion']!='success' or artifact['expired'] or artifact['workflow_run']['id']!=int(run_id):
        raise ValueError('source run/artifact is not a valid available success')
    raw=get('actions/artifacts/'+str(artifact_id)+'/zip').content
    if artifact['digest']!='sha256:'+hashlib.sha256(raw).hexdigest():raise ValueError('download SHA256 mismatch')
    context=dict(schema_version=1,repository=REPO,
        artifact={k:artifact[k] for k in ('id','name','digest','size_in_bytes','created_at','expires_at','expired','workflow_run')},
        run={k:run[k] for k in ('id','workflow_id','head_branch','head_sha','event','status','conclusion','run_attempt','path','created_at')})
    destination=Path(destination)
    if destination.exists():raise ValueError('input acquisition cannot overwrite an existing directory')
    destination.mkdir(parents=True)
    (destination/'artifact.zip').write_bytes(raw);(destination/'source-context.json').write_text(json.dumps(context,indent=2)+'\n')
    print(json.dumps(dict(status='EXISTING_GITHUB_INPUT_DOWNLOADED',run_id=run_id,artifact_id=artifact_id,sha256=hashlib.sha256(raw).hexdigest())))

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--run',required=True);p.add_argument('--artifact',required=True);p.add_argument('--out',required=True)
    a=p.parse_args()
    try:fetch(a.run,a.artifact,a.out)
    except Exception as exc:
        print('INPUT_ACQUISITION_REFUSED',type(exc).__name__,'signed URLs and credentials omitted');raise SystemExit(2)
