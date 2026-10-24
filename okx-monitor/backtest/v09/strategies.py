"""All assets share these frozen parameters. Only completed bars enter features."""
import numpy as np
import pandas as pd
from .config import STRATEGIES, DAY

def features(raw, step):
    x = raw.copy()
    prev = x.close.shift()
    tr = pd.concat([x.high-x.low, (x.high-prev).abs(), (x.low-prev).abs()], axis=1).max(axis=1)
    x['atr'] = tr.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    x['fast'] = x.close.ewm(span=12, adjust=False, min_periods=26).mean()
    x['slow'] = x.close.ewm(span=26, adjust=False, min_periods=26).mean()
    x['macd'] = x.fast-x.slow
    x['signal'] = x.macd.ewm(span=9, adjust=False, min_periods=9).mean()
    x['ema50'] = x.close.ewm(span=50, adjust=False, min_periods=50).mean()
    for n in (20, 55):
        x[f'hi{n}'] = x.high.shift().rolling(n).max()
        x[f'lo{n}'] = x.low.shift().rolling(n).min()
    x['vol20'] = x.volume.shift().rolling(20).mean()
    x['liquidity'] = x.quote_volume.rolling(20).mean()*DAY/step
    # UTC daily bars; join at close availability, never at period open.
    d = raw.assign(bucket=raw.ts//DAY*DAY)
    rows = []
    for t, g in d.groupby('bucket'):
        if len(g) == DAY//step and g.ts.iloc[0] == t and g.ts.iloc[-1]+step == t+DAY:
            rows.append((t+DAY, g.close.iloc[-1]))
    daily = pd.DataFrame(rows, columns=['available', 'daily_close'])
    daily['mean25'] = daily.daily_close.rolling(25).mean()
    daily['std25'] = daily.daily_close.rolling(25).std()
    x = pd.merge_asof(x.assign(available=x.ts+step), daily, on='available')
    x['bias'] = x.close/x.mean25-1
    x['z'] = (x.close-x.mean25)/x.std25.replace(0, np.nan)
    return x

def signal(x, i, strategy):
    if strategy not in STRATEGIES:
        raise ValueError('unknown strategy')
    if i < 55:
        return 0, 0.
    a, b = (x[i], x[i-1]) if isinstance(x, list) else (x.iloc[i], x.iloc[i-1])
    if not np.isfinite([a.atr, a.hi55, a.ema50]).all() or a.atr <= 0:
        return 0, 0.
    hist, previous = a.macd-a.signal, b.macd-b.signal
    side = 0
    if strategy == 'BIAS25_REVERSAL':
        if a.bias < -.025 and a.z < -1.5 and hist > previous and a.close > b.close:
            side = 1
        if a.bias > .025 and a.z > 1.5 and hist < previous and a.close < b.close:
            side = -1
    elif strategy == 'MACD_TREND':
        if previous <= 0 < hist and a.close > a.ema50:
            side = 1
        if previous >= 0 > hist and a.close < a.ema50:
            side = -1
    else:
        if a.volume > 1.25*a.vol20:
            if a.close > a.hi20 and a.close > a.ema50:
                side = 1
            if a.close < a.lo20 and a.close < a.ema50:
                side = -1
    return side, float(abs(hist)/a.atr) if side else 0.
