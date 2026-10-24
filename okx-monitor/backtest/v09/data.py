"""Public GET-only data, strict complete calendars and explicit coverage failures."""
from datetime import datetime, timezone
import hashlib
import time
from pathlib import Path
import numpy as np
import pandas as pd
import requests
from .config import STEPS

ROOT = 'https://www.okx.com/api/v5/'
COLS = ['ts', 'open', 'high', 'low', 'close', 'volume', 'quote_volume']

def timestamp(value):
    d = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if d.tzinfo is None or d.utcoffset().total_seconds() != 0:
        raise ValueError('explicit UTC required')
    return int(d.timestamp() * 1000)

def iso(ts):
    return datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat()

def get(path, params=None):
    # Fixed allowlist; no private endpoints, credentials or arbitrary destinations.
    if path not in ('market/history-candles', 'public/instruments'):
        raise ValueError('public research endpoint only')
    for attempt in range(4):
        try:
            r = requests.get(ROOT + path, params=params, timeout=(10, 40))
            r.raise_for_status()
            obj = r.json()
            if obj.get('code') != '0':
                raise ValueError('OKX ' + str(obj.get('code')) + ': ' + str(obj.get('msg')))
            return obj['data']
        except (requests.RequestException, ValueError, KeyError):
            if attempt == 3:
                raise
            time.sleep(1 + attempt)

def instruments():
    rows = get('public/instruments', {'instType': 'SWAP'})
    return {r['instId']: r for r in rows if r['settleCcy'] == 'USDT'
            and r['ctType'] == 'linear' and r['state'] == 'live'}

def validate(df, step, start=None, end=None):
    if df.empty or not np.isfinite(df[COLS].to_numpy(dtype=float)).all():
        raise ValueError('empty/nonfinite candles')
    ts = df.ts.to_numpy()
    if np.any(ts % step) or np.any(np.diff(ts) != step):
        raise ValueError('gap/duplicate/misaligned candle')
    if (df[['open', 'high', 'low', 'close']] <= 0).any().any() or (df[['volume', 'quote_volume']] < 0).any().any():
        raise ValueError('invalid price/volume')
    if (df.high < df[['open', 'close', 'low']].max(axis=1)).any() or (df.low > df[['open', 'close', 'high']].min(axis=1)).any():
        raise ValueError('invalid OHLC')
    if start is not None and (ts[0] != start or ts[-1] + step != end):
        raise ValueError('partial requested interval (listing/delisting/data failure)')
    return True

def download(inst, bar, start, end, cache):
    step = STEPS[bar]
    if start % step or end % step or end > int(time.time()*1000):
        raise ValueError('unaligned or unfinished interval')
    path = Path(cache) / f'{inst}_{bar}_{start}_{end}.csv'
    if path.exists():
        df = pd.read_csv(path)
        validate(df, step, start, end)
        return df, hashlib.sha256(path.read_bytes()).hexdigest()
    cursor = end
    records = {}
    for _ in range(3000):
        rows = get('market/history-candles', {'instId': inst, 'bar': '1Dutc' if bar == '1D' else bar,
                                             'after': str(cursor), 'limit': '300'})
        if not rows:
            break
        for r in rows:
            if len(r) < 9 or str(r[8]) != '1':
                continue
            t = int(r[0])
            if start <= t < end:
                row = (t, *map(float, r[1:6]), float(r[7]))
                if t in records and records[t] != row:
                    raise ValueError('conflicting duplicates')
                records[t] = row
        oldest = min(int(r[0]) for r in rows)
        if oldest <= start:
            break
        if oldest >= cursor:
            raise ValueError('stalled pagination')
        cursor = oldest
        time.sleep(.12)
    df = pd.DataFrame([records[t] for t in sorted(records)], columns=COLS)
    validate(df, step, start, end)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, float_format='%.12g')
    # Compute using the same serialization on initial and cached runs.
    return pd.read_csv(path), hashlib.sha256(path.read_bytes()).hexdigest()
