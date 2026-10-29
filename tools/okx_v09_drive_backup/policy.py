from dataclasses import dataclass
from okx_v09_reporting.common import read_json
from .errors import BackupError

FOLDER_ID = '1OlXqvYUPTl7sK5Eds71X_3gcRUQv1G-q'
SCOPE = 'https://www.googleapis.com/auth/drive.file'
APP = 'okx-v09-research-backup'

@dataclass(frozen=True)
class LivePermit:
    folder_id: str
    enabled: bool
    approval: str


def validate_config(config):
    if (config.get('schema_version')!=1 or config.get('folder_id')!=FOLDER_ID
            or config.get('oauth_scopes')!=[SCOPE] or config.get('timezone')!='Asia/Taipei'
            or config.get('planned_utc_cron')!='0 0 * * *'
            or type(config.get('live_upload_enabled')) is not bool
            or config.get('overwrite_existing') is not False or config.get('delete_existing') is not False):
        raise BackupError('CONFIG_UNREVIEWED','policy')
    if config.get('historical_restore_interval_days')!=7:
        raise BackupError('RESTORE_POLICY_UNREVIEWED','policy')
    return config


def permit_live(config, approval):
    validate_config(config)
    if not config['live_upload_enabled'] or approval!='I_APPROVE_ONE_DRIVE_BACKUP':
        raise BackupError('LIVE_TRANSFER_NOT_APPROVED','policy')
    return LivePermit(FOLDER_ID,True,approval)


def load_config(path):
    return validate_config(read_json(path.read_bytes()))
