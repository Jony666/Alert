"""Public official sources only. No account/trade/private endpoints or keys."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import hashlib,json,re,html
import requests
import time

ROOT='https://www.okx.com/api/v5/'
ALLOWED=('public/position-tiers','public/mark-price','public/instruments')

def public_get(path,params):
    if path not in ALLOWED:raise ValueError('private/trade endpoint forbidden')
    for attempt in range(4):
        try:
            time.sleep(.2)  # public API rate budget; never bypass throttling
            r=requests.get(ROOT+path,params=params,timeout=(10,40));r.raise_for_status()
            x=r.json()
            if x.get('code')!='0':raise ValueError('public API '+str(x.get('code')))
            return x['data']
        except (requests.RequestException,ValueError):
            if attempt==3:raise
            time.sleep(.5*2**attempt)

def audit(snapshot,out):
    out=Path(out);out.mkdir(parents=True,exist_ok=True)
    now=datetime.now(timezone.utc);catalog=json.loads(Path(snapshot).read_text())['current_instruments']
    result=dict(observed_utc=now.isoformat(),scope='CURRENT_PUBLIC_DATA_ONLY_NOT_HISTORICAL_CERTIFICATION',
                official_cross_verified=False,allow_recommendation=False,failures={},sources={})
    specs=public_get('public/instruments',{'instType':'SWAP'})
    marks=public_get('public/mark-price',{'instType':'SWAP'})
    markmap={x['instId']:x for x in marks};specmap={x['instId']:x for x in specs}
    def tier(inst):return inst,public_get('public/position-tiers',{'instType':'SWAP','tdMode':'cross','instFamily':inst.rsplit('-',1)[0]})
    tiers={}
    with ThreadPoolExecutor(max_workers=1) as pool:
        for inst in sorted(catalog):
            # Futures awaited independently; bounded pool avoids public rate bursts.
            tiers[inst]=pool.submit(tier,inst)
        for inst,future in list(tiers.items()):
            try:tiers[inst]=future.result()[1]
            except Exception as e:tiers[inst]=[];result['failures'][inst]=type(e).__name__+': '+str(e)
    checks=[]
    for inst,old in sorted(catalog.items()):
        spec=specmap.get(inst,{});mark=markmap.get(inst,{})
        rows=tiers[inst]
        valid=bool(rows) and all(0<float(t['mmr'])<=float(t['imr'])<=1 and float(t['maxSz'])>float(t['minSz']) for t in rows)
        age=(now.timestamp()*1000-int(mark.get('ts',0)))/1000
        checks.append(dict(instrument=inst,tier_rows=len(rows),tier_values_valid=valid,
                           mark_age_seconds=age,mark_fresh_at_capture=-60<=age<=300,
                           spec_fields_changed=[k for k in ('ctVal','ctValCcy','lotSz','minSz','tickSz','lever','listTime') if spec.get(k)!=old.get(k)],
                           source='OKX public current cross position-tiers + mark-price + instruments',
                           historical_tiers_verified=False,account_mode_equity_fees_verified=False))
    result.update(checks=checks,current_tiers=tiers,current_marks={k:markmap.get(k) for k in catalog},
                  current_specs={k:specmap.get(k) for k in catalog},
                  endpoint_sources=[ROOT+p for p in ALLOWED],
                  fee_status='User-specific actual fees require private account data; NOT fetched; taker/funding remain disclosed research assumptions',
                  liquidation_status='Public tiers/marks are necessary but insufficient: account mode, collateral, liabilities, ADL, official liquidation sequence not certified')
    (out/'official_audit.json').write_text(json.dumps(result,indent=2)+'\n')
    return result

BLS=(('CPI','2024-09-11','cpi_09112024.htm','September 11, 2024'),
     ('NFP','2024-09-06','empsit_09062024.htm','September 6, 2024'),
     ('CPI','2025-01-15','cpi_01152025.htm','January 15, 2025'),
     ('NFP','2025-01-10','empsit_01102025.htm','January 10, 2025'))

def news(out,base_events):
    out=Path(out);events=json.loads(Path(base_events).read_text());checks=[]
    for label,date,file,expected_date in BLS:
        url='https://www.bls.gov/news.release/archives/'+file
        check=dict(kind=label,source=url,usable=False)
        try:
            response=requests.get(url,timeout=(10,40));response.raise_for_status()
            plain=' '.join(html.unescape(re.sub('<[^>]+>',' ',response.text)).split())
            verified=bool(re.search(r'8:30\s*a\.m\.',plain,re.I)) and expected_date in plain
            check.update(http_status=response.status_code,time_header_verified=verified,sha256=hashlib.sha256(response.content).hexdigest())
            if verified:
                dt=datetime.fromisoformat(date+'T08:30:00').replace(tzinfo=ZoneInfo('America/New_York')).astimezone(timezone.utc)
                t=dt.isoformat()
                events.append(dict(kind='OTHER',subkind=label,published_utc=t,event_utc=t,source=url,major=True,
                                   expected=True,direction='UNKNOWN',valid_hours_before=24,valid_hours_after=24,
                                   evidence='Official BLS archived 8:30am ET release header/date; no pre-release calendar reconstruction',
                                   win_rate_adjustment=None))
                check['usable']=True
        except Exception as e:check['error']=type(e).__name__+': '+str(e)
        checks.append(check)
    # These source categories are intentionally NOT backdated into historical masks.
    for label,url in [('ELECTION','https://www.eac.gov/voters/register-and-vote-in-your-state'),
                      ('REGULATORY','https://www.sec.gov/newsroom/press-releases/2024-4'),
                      ('OKX_ANNOUNCEMENTS','https://www.okx.com/help/section/announcements-new-listings')]:
        check=dict(kind=label,source=url,usable=False,reason='Historical original publication UTC not reliably established; NOT used in backtest')
        try:
            response=requests.get(url,timeout=(10,40));check['http_status']=response.status_code
            check['sha256']=hashlib.sha256(response.content).hexdigest()
        except Exception as e:check['error']=type(e).__name__+': '+str(e)
        checks.append(check)
    (out/'events_expanded.json').write_text(json.dumps(events,indent=2)+'\n')
    (out/'news_audit.json').write_text(json.dumps(dict(checks=checks,coverage='PARTIAL_OFFICIAL_EVENTS_ONLY',win_rate_adjustment=None),indent=2)+'\n')
