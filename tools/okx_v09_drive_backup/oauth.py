"""User OAuth only. Secrets stay in memory / a user-owned external secret file."""
from dataclasses import dataclass,field
import time
import requests
from .errors import BackupError
from .policy import SCOPE,FOLDER_ID

TOKEN_URL='https://oauth2.googleapis.com/token'
AUTH_URL='https://accounts.google.com/o/oauth2/v2/auth'


def exact_scope(value):
    scopes=value.split() if isinstance(value,str) else value
    if scopes!=[SCOPE]:raise BackupError('OAUTH_SCOPE_NOT_MINIMAL','oauth')
    return [SCOPE]

@dataclass(repr=False)
class UserCredentials:
    client_id: str=field(repr=False)
    client_secret: str=field(repr=False)
    refresh_token: str=field(repr=False)
    folder_grant: dict=field(repr=False)
    def __repr__(self):return 'UserCredentials(<redacted>)'

    @classmethod
    def parse(cls,value):
        if value.get('type')!='authorized_user':raise BackupError('USER_OAUTH_REQUIRED_NO_SERVICE_ACCOUNT','oauth')
        exact_scope(value.get('scopes'))
        if value.get('token_uri')!=TOKEN_URL:raise BackupError('UNTRUSTED_TOKEN_ENDPOINT','oauth')
        for key in ('client_id','client_secret','refresh_token'):
            if not isinstance(value.get(key),str) or not value[key]:raise BackupError('OAUTH_CREDENTIAL_INCOMPLETE','oauth')
        grant=value.get('folder_grant',{})
        if (grant.get('id')!=FOLDER_ID or grant.get('oauth_client_id')!=value['client_id']
                or grant.get('picker_selected') is not True or grant.get('drive_file_get_verified') is not True):
            raise BackupError('FOLDER_NOT_GRANTED_TO_THIS_OAUTH_APP','oauth')
        return cls(value['client_id'],value['client_secret'],value['refresh_token'],grant)


class OAuthSession:
    def __init__(self,credentials,session=None):
        self.credentials=credentials;self.session=session or requests.Session()
        self._access_token=None;self._until=0
    def __repr__(self):return 'OAuthSession(<redacted>)'
    def access_token(self,force=False):
        if not force and self._access_token and time.monotonic()<self._until:return self._access_token
        try:
            response=self.session.post(TOKEN_URL,data=dict(grant_type='refresh_token',
                client_id=self.credentials.client_id,client_secret=self.credentials.client_secret,
                refresh_token=self.credentials.refresh_token),timeout=(10,40),allow_redirects=False)
        except requests.RequestException:raise BackupError('OAUTH_NETWORK_FAILED','oauth',True) from None
        if response.status_code!=200:raise BackupError('OAUTH_REAUTH_REQUIRED','oauth')
        try:body=response.json()
        except (ValueError,TypeError):raise BackupError('OAUTH_RESPONSE_INVALID','oauth') from None
        if 'scope' in body:exact_scope(body['scope'])
        # Missing scope on refresh inherits the original, independently checked grant.
        if not isinstance(body.get('access_token'),str) or not body['access_token']:
            raise BackupError('OAUTH_ACCESS_TOKEN_MISSING','oauth')
        if body.get('refresh_token') and body['refresh_token']!=self.credentials.refresh_token:
            raise BackupError('OAUTH_ROTATION_REQUIRES_SECURE_REAUTH','oauth')
        try:ttl=int(body.get('expires_in',0))
        except (ValueError,TypeError):raise BackupError('OAUTH_RESPONSE_INVALID','oauth') from None
        if ttl<=60:raise BackupError('OAUTH_ACCESS_TOKEN_TOO_SHORT','oauth')
        self._access_token=body['access_token'];self._until=time.monotonic()+ttl-60
        return self._access_token
