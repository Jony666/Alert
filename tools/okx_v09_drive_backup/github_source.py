"""GET-only latest-success selection; never fall back when its artifact is absent."""
from dataclasses import dataclass
import requests
from okx_v09_reporting.common import now,at,sha256
from okx_v09_reporting.artifact import verify_artifact
from .errors import BackupError

REPO='Jony666/Alert'
WORKFLOW='okx-v09-forward-scheduled.yml'
PREFIX='forward-state-okx-v095-4e85ee42786c6564-'

@dataclass(frozen=True)
class Selection:
    context: dict
    selected_at: str
    expiry: dict
    @property
    def identity(self):
        a=self.context['artifact'];return (str(self.context['run']['id']),a['id'],a['digest'])

class GitHubSource:
    def __init__(self,token,session=None):
        self.session=session or requests.Session()
        self.session.headers.update(Authorization='Bearer '+token,Accept='application/vnd.github+json')
    def get(self,path,binary=False):
        if not path.startswith('/actions/'):raise BackupError('GITHUB_GET_ONLY','source')
        try:
            r=self.session.get('https://api.github.com/repos/'+REPO+path,timeout=(10,120),allow_redirects=True)
            r.raise_for_status();return r.content if binary else r.json()
        except (requests.RequestException,ValueError):raise BackupError('GITHUB_READ_FAILED','source',True) from None
    def pages(self,path,key):
        rows=[]
        for page in range(1,101):
            data=self.get(path+('&' if '?' in path else '?')+'per_page=100&page='+str(page))[key]
            rows.extend(data)
            if len(data)<100:return rows
        raise BackupError('GITHUB_PAGINATION_INCOMPLETE','source')
    def select(self):
        runs=self.pages('/actions/workflows/'+WORKFLOW+'/runs?branch=master','workflow_runs')
        success=[r for r in runs if r['status']=='completed' and r['conclusion']=='success']
        if not success:raise BackupError('NO_SUCCESSFUL_PRODUCTION_STATE','source')
        run=max(success,key=lambda r:(r['updated_at'],int(r['id'])))
        if run.get('run_attempt',1)!=1:raise BackupError('SOURCE_RERUN_REQUIRES_REVIEW','source')
        artifacts=self.pages('/actions/runs/'+str(run['id'])+'/artifacts','artifacts')
        expected=PREFIX+str(run['id'])+'-1'
        matching=[a for a in artifacts if a['name']==expected]
        if len(matching)!=1:raise BackupError('LATEST_SUCCESS_ARTIFACT_MISSING_OR_AMBIGUOUS','source')
        artifact=matching[0]
        if artifact['expired']:raise BackupError('LATEST_ARTIFACT_EXPIRED_NO_FALLBACK','source')
        # Detect a newer published state attached to an unsuccessful/rerun owner.
        inventory=self.pages('/actions/artifacts','artifacts')
        study=[a for a in inventory if a['name'].startswith(PREFIX)]
        if not study:raise BackupError('PRODUCTION_INVENTORY_EMPTY','source')
        newest=max(study,key=lambda a:(a['created_at'],a['id']))
        if newest['id']!=artifact['id']:raise BackupError('NEWER_PUBLISHED_STATE_NOT_LATEST_SUCCESS','source')
        context=dict(schema_version=1,repository=REPO,
            artifact={k:artifact[k] for k in ('id','name','digest','size_in_bytes','created_at','expires_at','expired','workflow_run')},
            run={k:run[k] for k in ('id','workflow_id','head_branch','head_sha','event','status','conclusion','run_attempt','path','created_at')})
        remaining=(at(artifact['expires_at'])-now()).total_seconds()/86400
        if remaining<=0:raise BackupError('LATEST_ARTIFACT_EXPIRED_NO_FALLBACK','source')
        older=[a for a in study if a['id']!=artifact['id'] and not a['expired']]
        soon=[a for a in older if (at(a['expires_at'])-now()).total_seconds()<30*86400]
        expiry=dict(latest_remaining_days=round(remaining,3),latest_warning=remaining<30,
                    older_unselected_artifacts_expiring_within_30_days=len(soon),
                    oldest_unselected_expiry=min([a['expires_at'] for a in older],default=None),
                    all_hourly_raw_snapshots_preserved=False,
                    note='Daily latest cumulative state preserves trading history, not every intraday raw snapshot')
        return Selection(context,now().isoformat(),expiry)
    def read_latest(self,model):
        selection=self.select();artifact=selection.context['artifact']
        raw=self.get('/actions/artifacts/'+str(artifact['id'])+'/zip',binary=True)
        if artifact['digest']!='sha256:'+sha256(raw):raise BackupError('SOURCE_ZIP_SHA256_FAILED','source')
        verified=verify_artifact(raw,selection.context,model)
        self.assert_current(selection)
        return selection,verified
    def assert_current(self,selection):
        if self.select().identity!=selection.identity:raise BackupError('SOURCE_ADVANCED_RETRY_FROM_LATEST','source',True)
