"""GET-only GitHub Artifact restore. No repository writes, deletions or notifications."""
from pathlib import Path
import io,json,re,zipfile
import requests
from .state import read_bundle

REPO='Jony666/Alert';WORKFLOW='okx-v09-forward-paper.yml'

class GitHubStore:
    def __init__(self,token):
        self.session=requests.Session();self.session.headers.update(Authorization='Bearer '+token,Accept='application/vnd.github+json')
    def get(self,path):
        if not path.startswith('/actions/'):raise ValueError('only read-only Actions API allowed')
        r=self.session.get('https://api.github.com/repos/'+REPO+path,timeout=(10,60));r.raise_for_status();return r.json()
    def runs(self):
        result=[]
        for page in range(1,101):
            rows=self.get(f'/actions/workflows/{WORKFLOW}/runs?branch=research/okx-v09&per_page=100&page={page}')['workflow_runs']
            result.extend(rows)
            if len(rows)<100:return result
        raise ValueError('history pagination limit; never assume first run')
    def artifacts(self,run_id):
        result=[]
        for page in range(1,101):
            rows=self.get(f'/actions/runs/{run_id}/artifacts?per_page=100&page={page}')['artifacts'];result+=rows
            if len(rows)<100:return result
        raise ValueError('artifact pagination exceeded')
    def download(self,artifact_id,destination):
        r=self.session.get(f'https://api.github.com/repos/{REPO}/actions/artifacts/{artifact_id}/zip',timeout=(10,120));r.raise_for_status()
        safe_extract(r.content,destination)

def safe_extract(content,destination):
    dest=Path(destination);dest.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        if sum(i.file_size for i in z.infolist())>256*1024*1024:raise ValueError('state archive too large')
        for i in z.infolist():
            p=Path(i.filename)
            if p.is_absolute() or '..' in p.parts or (i.external_attr>>16)&0o170000==0o120000:raise ValueError('unsafe state archive')
        z.extractall(dest)

def resolve(store,freeze,run_id,attempt,mode,anchor,destination):
    runs=store.runs();prior=[r for r in runs if str(r['id'])!=str(run_id)]
    if mode=='initialize':
        if prior or attempt!=1 or anchor:raise ValueError('initialization forbidden: history/anchor exists; never reset')
        return None,None,None
    if not anchor:raise ValueError('resume requires committed genesis anchor')
    successful=[r for r in prior if r['status']=='completed' and r['conclusion']=='success']
    # A rerun of a previously completed run must first recover its earlier attempt,
    # not skip it and accidentally roll back to an older successful run.
    if attempt>1:
        current=next((r for r in runs if str(r['id'])==str(run_id)),None)
        if current:
            arts=store.artifacts(run_id)
            if any(a['name'].startswith('forward-state-') for a in arts):successful.append(current)
    if not successful:raise ValueError('no prior successful state; no implicit initialization')
    latest=max(successful,key=lambda r:(r['run_number'],r.get('run_attempt',1)))
    arts=store.artifacts(latest['id'])
    prefix='forward-state-'+freeze['study_id']+'-'
    matching=[a for a in arts if a['name'].startswith(prefix)]
    if not matching:raise ValueError('LATEST successful run has no matching state Artifact; no older fallback')
    artifact=max(matching,key=lambda a:a['id'])
    if artifact['expired']:raise ValueError('LATEST state Artifact expired; manual recovery required')
    store.download(artifact['id'],destination)
    manifests=list(Path(destination).rglob('run_manifest.json'))
    if len(manifests)!=1:raise ValueError('ambiguous/missing committed bundle')
    folder=manifests[0].parent
    state,manifest=read_bundle(folder,freeze,anchor=anchor)
    if str(manifest['run_id'])!=str(latest['id']):raise ValueError('restored run provenance mismatch')
    parent=dict(run_id=str(latest['id']),artifact_id=artifact['id'],artifact_name=artifact['name'],
                state_hash=manifest['state_hash'],revision=state['revision'],completed_at=artifact.get('created_at',latest['updated_at']))
    return folder,state,parent
