from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import urlparse

OFFICIAL = ('okx.com', 'federalreserve.gov', 'bls.gov', 'bea.gov', 'sec.gov', 'cftc.gov')
KINDS = ('FED', 'US_ELECTION', 'QUADRUPLE_WITCHING', 'REGULATORY', 'PROJECT', 'OTHER')

def aware(value):
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('event timezone required')
    return dt

def assess(events, at, symbol):
    now = aware(at)
    visible = []
    for e in events:
        published = aware(e['published_utc'])
        if published > now:
            continue  # point-in-time publication gate, including retrospective sources
        host = urlparse(e['source']).hostname or ''
        reliable = any(host == h or host.endswith('.'+h) for h in OFFICIAL)
        if not reliable and not e.get('official_source_verified', False):
            continue
        if e['kind'] not in KINDS:
            raise ValueError('unknown event kind')
        if e.get('symbols') and symbol not in e['symbols']:
            continue
        event_time = aware(e['event_utc'])
        if abs((event_time-now).total_seconds()) > 86400:
            continue
        visible.append({**e, 'published_taipei': published.astimezone(ZoneInfo('Asia/Taipei')).isoformat(),
                        'win_rate_adjustment': None, 'quantification': '無法可靠量化',
                        'warning': e.get('major', False) or e['kind'] in ('FED', 'US_ELECTION', 'QUADRUPLE_WITCHING')})
    return {'events': visible, 'win_rate_adjustment': None,
            'coverage': 'USER_SUPPLIED_VERIFIED_EVENTS_ONLY; NO COMPLETE HISTORICAL NEWS FEED',
            'requires_review': any(e['warning'] for e in visible)}
