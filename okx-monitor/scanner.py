"""OKX 30m strategy-first market scanner. RESEARCH ONLY. No exchange credentials/trading.

Requires: requests, pandas. Cron: 2,32 * * * * (UTC).
Only closed OKX bars are considered. Notifications fail closed if not configured.
"""
from __future__ import annotations
import os, time, json, logging, threading, hashlib
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote
import requests
import pandas as pd

UTC = timezone.utc
LOG = logging.getLogger("okx_scan")
OKX = os.environ.get("OKX_BASE_URL", "https://www.okx.com").rstrip("/")
CG = os.environ.get("COINGECKO_BASE_URL", "https://api.coingecko.com/api/v3").rstrip("/")
SYMBOL_LIMIT = 200  # eligible global market-cap rank, not 200 confirmed perpetual instruments
MIN_VOLUME_USD = float(os.environ.get("MIN_VOLUME_USD", "5000000"))
MAX_SPREAD = float(os.environ.get("MAX_SPREAD_PCT", "0.30")) / 100
NOTIFY = os.environ.get("ENABLE_PUSH", "false").lower() == "true"
ACK = os.environ.get("RESEARCH_ACK", "false").lower() == "true"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
REQUEST_TIMEOUT = (7, 16)
BARS = ("30m", "1H", "2H", "4H", "6H", "12H", "1D")
BAR_SECONDS = {"30m": 1800, "1H": 3600, "2H": 7200, "4H": 14400, "6H": 21600, "8H": 28800, "12H": 43200, "1D": 86400}
_lock = threading.Lock()
_next_request_time = 0.0


def rate_wait():
    """Cross-worker conservative OKX throttle; applies to all requests for simplicity."""
    global _next_request_time
    with _lock:
        t = time.monotonic()
        pause = max(0.0, _next_request_time - t)
        _next_request_time = max(t, _next_request_time) + 0.21
    if pause:
        time.sleep(pause)


def api_json(url, params=None, headers=None):
    for attempt in range(3):
        try:
            rate_wait()
            r = requests.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
            if r.status_code in (429, 500, 502, 503, 504):
                raise RuntimeError(f"HTTP {r.status_code}")
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            if attempt == 2:
                raise RuntimeError(f"Request failed: {url}: {exc}") from exc
            time.sleep(1.5 ** (attempt + 1))
    raise RuntimeError("unreachable")


def okx(path, **params):
    payload = api_json(OKX + path, params=params)
    if payload.get("code") != "0":
        raise RuntimeError(f"OKX {path}: {payload.get('code')} {payload.get('msg')}")
    return payload.get("data", [])


def cap_symbols():
    """Take first 200 eligible non-pegged coins ordered by global market cap."""
    key = os.environ.get("COINGECKO_API_KEY", "")
    headers = {"x-cg-demo-api-key": key} if key else {}
    coins = api_json(CG + "/coins/markets", params={
        "vs_currency": "usd", "order": "market_cap_desc", "per_page": 250,
        "page": 1, "sparkline": "false"}, headers=headers)
    if not isinstance(coins, list):
        raise RuntimeError("CoinGecko list not returned; abort rather than claim 200-scanned")
    # Static exclusions for well-known stable, synthetic and wrapped assets; supplemented by naming heuristic.
    excluded_ids = {"tether", "usd-coin", "dai", "ethena-usde", "wrapped-steth", "wrapped-bitcoin",
                    "wrapped-ether", "staked-ether", "weth", "usd1-wlfi", "first-digital-usd", "paypal-usd",
                    "usdd", "frax", "true-usd", "pax-dollar", "binance-bridged-usdt-bnb-smart-chain"}
    blocked_words = ("wrapped ", "bridged ", "stablecoin", "staked ", "synthetic dollar")
    result = []
    seen = set()
    for c in coins:
        sym = str(c.get("symbol", "")).upper()
        name = str(c.get("name", "")).lower()
        rank = c.get("market_cap_rank")
        if not (isinstance(rank, int) and rank > 0 and sym and sym.isalnum()):
            continue
        if c.get("id") in excluded_ids or any(w in name for w in blocked_words):
            continue
        if sym in ("USDT", "USDC", "DAI", "USDE", "FDUSD", "USD1", "USDD", "PYUSD", "TUSD", "USDS", "EURC", "WETH", "WBTC", "STETH"):
            continue
        if sym in seen:
            continue  # collision is handled conservatively; do not guess contract-to-asset identity
        seen.add(sym)
        result.append((rank, sym, c.get("id")))
        if len(result) == SYMBOL_LIMIT:
            break
    if len(result) < 100:
        raise RuntimeError(f"Insufficient market-cap list ({len(result)})")
    return result


def select_contracts(market_coins):
    instruments = okx("/api/v5/public/instruments", instType="SWAP")
    tickers = okx("/api/v5/market/tickers", instType="SWAP")
    by_id = {t.get("instId"): t for t in tickers}
    available = {x.get("instId"): x for x in instruments}
    now_ms = int(time.time() * 1000)
    eligible = []
    for rank, symbol, cg_id in market_coins:
        inst_id = f"{symbol}-USDT-SWAP"
        inst = available.get(inst_id)
        t = by_id.get(inst_id)
        if not inst or not t or inst.get("state") != "live":
            continue
        if inst.get("settleCcy") != "USDT":
            continue
        try:
            launch = int(inst.get("listTime", "0"))
            if not launch or now_ms - launch < 90 * 86400 * 1000:
                continue
            bid = float(t["bidPx"]); ask = float(t["askPx"]); last = float(t["last"])
            vol_base = float(t.get("volCcy24h") or 0)
            vol_quote = float(t.get("volCcyQuote24h") or vol_base * last)
            spread = (ask - bid) / ((ask + bid) / 2)
            if min(bid, ask, last) <= 0 or ask < bid or spread > MAX_SPREAD:
                continue
            if vol_quote < MIN_VOLUME_USD:
                continue
            # Reject stale tickers; OKX ticker ts is in UTC milliseconds.
            if now_ms - int(t["ts"]) > 180000:
                continue
            eligible.append({"rank":rank, "cg_id":cg_id, "id":inst_id, "bid":bid,
                             "ask":ask, "last":last, "spread":spread, "volume_usd_est":vol_quote})
        except (KeyError, ValueError, TypeError, ZeroDivisionError):
            continue
    return eligible


def candles(inst_id, bar):
    raw = okx("/api/v5/market/candles", instId=inst_id, bar=bar, limit="240")
    records = []
    for a in raw:
        try:
            if len(a) < 9 or str(a[8]) != "1":  # never analyze running candle
                continue
            ts = int(a[0]); o,h,l,c,v = map(float, (a[1],a[2],a[3],a[4],a[5]))
            if min(o,h,l,c)>0 and h>=max(o,c,l) and l<=min(o,c,h):
                records.append((ts, o,h,l,c,v))
        except (ValueError, TypeError, IndexError):
            continue
    if len(records)<65:
        raise RuntimeError(f"Too few confirmed {bar} candles for {inst_id}")
    records = sorted(set(records))
    frame = pd.DataFrame(records, columns=["ts","open","high","low","close","volume"]).drop_duplicates("ts")
    step_ms = BAR_SECONDS[bar] * 1000
    if (frame.ts.diff().dropna() != step_ms).any():
        raise RuntimeError(f"Gap in {inst_id} {bar} candles")
    max_age = step_ms + 180000  # latest bar ended less than 3min before last scheduled boundary
    age_since_latest_close = int(time.time()*1000) - (int(frame.ts.iloc[-1]) + step_ms)
    if age_since_latest_close < -1000 or age_since_latest_close > max_age:
        raise RuntimeError(f"Stale/future bar {inst_id} {bar}")
    return frame.reset_index(drop=True)


def resample_8h(frame_4h):
    """Build confirmed 8H candles from complete 4H pairs, UTC+8 midnight aligned."""
    arr = frame_4h.copy()
    arr["bucket"] = (arr["ts"] // (8 * 3600 * 1000)) * (8 * 3600 * 1000)
    groups = []
    for bucket, group in arr.groupby("bucket", sort=True):
        group = group.sort_values("ts")
        if len(group)!=2 or int(group.ts.iloc[0])!=bucket or int(group.ts.iloc[1])!=bucket+4*3600*1000:
            continue
        groups.append((bucket, float(group.open.iloc[0]), float(group.high.max()),
                       float(group.low.min()), float(group.close.iloc[-1]), float(group.volume.sum())))
    out = pd.DataFrame(groups, columns=["ts","open","high","low","close","volume"])
    if len(out)<45:
        raise RuntimeError("Insufficient complete 8H candles")
    return out


def signals_df(frame):
    df = frame.copy()
    close = df.close.astype(float)
    fast = close.ewm(span=12, adjust=False).mean()
    slow = close.ewm(span=26, adjust=False).mean()
    df["dif"] = fast - slow
    df["dea"] = df["dif"].ewm(span=9, adjust=False).mean()
    df["hist"] = df["dif"] - df["dea"]
    delta = close.diff()
    gains = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    losses = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    df["rsi"] = 100 - 100/(1+gains/(losses.replace(0, 1e-12)))
    prev = close.shift(1)
    tr = pd.concat([(df.high-df.low), (df.high-prev).abs(), (df.low-prev).abs()],axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1/14, adjust=False).mean()
    return df


def bottoms(df, left=3, right=3, scan=60):
    """Only pivots fully confirmed by >=right subsequent closed bars."""
    lows = df.low.to_numpy()
    pivots = []
    start = max(left, len(df)-scan)
    for i in range(start, len(df)-right):
        v = lows[i]
        if all(v < a for a in lows[i-left:i]) and all(v < a for a in lows[i+1:i+right+1]):
            pivots.append(i)
    return pivots


def bullish_divergence(df):
    pivots = bottoms(df)
    if len(pivots)<2:
        return None
    a,b = pivots[-2:]
    if b-a < 6 or len(df)-1-b > 12:
        return None
    rowa, rowb = df.iloc[a],df.iloc[b]
    if not (rowb.low < rowa.low and rowb.dif > rowa.dif):
        return None
    return {"old_ts":int(rowa.ts),"new_ts":int(rowb.ts),
            "old_low":float(rowa.low),"new_low":float(rowb.low),
            "old_dif":float(rowa.dif),"new_dif":float(rowb.dif),
            "hist_confirm":bool(rowb["hist"] > rowa["hist"])}


def structural_breakout(df, lookback=12):
    """Breakout happened on last *completed* bar, not simply price above resistance."""
    if len(df) < lookback+22:
        return False
    prior = float(df.high.iloc[-lookback-1:-1].max())
    previous_resistance = float(df.high.iloc[-lookback-2:-2].max())
    return bool(df.close.iloc[-1] > prior and df.close.iloc[-2] <= previous_resistance)


def strong_volume(df):
    avg = float(df.volume.iloc[-21:-1].mean())
    return avg > 0 and float(df.volume.iloc[-1]) >= 1.2 * avg


def strategy_a(d):
    daily = d["1D"]; h = d["1H"]; h4 = d["4H"]
    ma25 = float(daily.close.iloc[-25:].mean())
    sd25 = float(daily.close.iloc[-25:].std())
    last_d = float(daily.close.iloc[-1])
    if sd25<=0:
        return None
    bias=(last_d/ma25-1)*100
    z=(last_d-ma25)/sd25
    hist=h["hist"]
    # Gold cross within the most recent two closed 1H candles.
    crossover = any(h.dif.iloc[i]>h.dea.iloc[i] and h.dif.iloc[i-1] <= h.dea.iloc[i-1] for i in (-1,-2))
    if not (bias<=-2.5 and z <=-1.5 and crossover
            and hist.iloc[-1]>hist.iloc[-2]>hist.iloc[-3]
            and structural_breakout(h) and strong_volume(h)
            and h4["hist"].iloc[-1]>=h4["hist"].iloc[-2]
            and 25<=h.rsi.iloc[-1]<=75):
        return None
    return {"strategy":"A_BNF_MACD", "detail":f"25D BIAS={bias:.2f}%, Z={z:.2f}, 1H MACD金叉+量價突破"}


def strategy_b(d):
    result={}
    for period in ("30m","1H","2H","4H","6H","8H","12H","1D"):
        div = bullish_divergence(d[period])
        if div: result[period]=div
    higher = "12H" in result or "1D" in result
    if len(result)<4 or "4H" not in result or not higher:
        return None
    h=d["1H"]
    half=d["30m"]
    if not ((structural_breakout(half, 8) and strong_volume(half))
            or (structural_breakout(h, 12) and strong_volume(h))):
        return None
    if not (h["hist"].iloc[-1] > h["hist"].iloc[-2] and 25 <=h.rsi.iloc[-1] <= 75):
        return None
    return {"strategy":"B_MULTI_TIMEFRAME_DIVERGENCE",
            "detail":f"確認{len(result)}/8週期MACD DIF底背離: {','.join(result.keys())}",
            "divergences":result}


def plan_trade(x,d,signal):
    h=d["1H"]; h4=d["4H"]
    atr=float(h.atr.iloc[-1]); entry=float(x["ask"])
    if atr <=0 or abs(entry-float(h.close.iloc[-1])) > 0.7*atr:
        return None
    stop=float(h.low.iloc[-16:-1].min())-0.2*atr
    risk=entry-stop
    if risk<=0 or risk/entry>0.07 or risk/entry<0.002:
        return None
    # Use reachable *historically observed* high resistance; don't manufacture TP prices from R multiples.
    highs=sorted({float(v) for v in pd.concat([h.high.iloc[-90:],h4.high.iloc[-80:]]) if float(v)>entry+0.25*atr})
    if not highs:
        return None
    # Explicitly include conservative commissions, estimated round-trip slippage and
    # positive long funding. These are cost estimates, not account-specific fees.
    try:
        frdata=okx("/api/v5/public/funding-rate", instId=x["id"])
        funding=max(0.0, float(frdata[0].get("fundingRate", "0"))) if frdata else 0.001
        if funding>0.01:  # anomalous funding => invalid quote
            return None
        book=okx("/api/v5/market/books", instId=x["id"], sz="10")
        if not book:
            return None
        # Require nonempty ten-level book. Exact USD depth requires contract multiplier;
        # do not invent it here. The quote/spread/volume filters already bound minimum liquidity.
        if not book[0].get("asks") or not book[0].get("bids"):
            return None
    except Exception:
        return None
    fee_and_slip = (0.003 + funding)*entry
    rr = lambda price: (price-entry-fee_and_slip)/(risk+fee_and_slip)
    candidates = [v for v in highs if rr(v)>=2.0 and (v-entry)/entry <=0.18]
    if len(candidates)<2:
        return None
    tp1, tp2 = candidates[0], candidates[-1]
    if rr(tp2)<2.8:
        return None
    stamp=int(h.ts.iloc[-1]); event=f"{x['id']}/{signal['strategy']}/{stamp}"
    out={"id":x["id"],"strategy":signal["strategy"],"direction":"LONG (research)",
         "reference_price":x["last"],"entry_bid_ask":(x["bid"],x["ask"]),
         "entry":round(entry,8), "entry_zone":[round(entry-0.12*atr,8),round(entry,8)],
         "stop":round(stop,8),"tp1":round(tp1,8),"tp2":round(tp2,8),
         "rr_tp1_net_est":round(rr(tp1),2),"rr_tp2_net_est":round(rr(tp2),2),
         "vol_usd_est":round(x["volume_usd_est"]),"spread_pct":round(x["spread"]*100,4),
         "estimated_funding_pct":round(funding*100,4),"estimated_total_cost_pct":round((0.003+funding)*100,4),
         "signal_time_utc":datetime.fromtimestamp(stamp/1000,UTC).isoformat(),
         "expiry_utc":datetime.fromtimestamp((stamp+7200000)/1000,UTC).isoformat(),
         "event":hashlib.sha256(event.encode()).hexdigest()[:16],"reason":signal["detail"],
         "divergences":signal.get("divergences",{})}
    return out


def scan_instrument(x):
    try:
        frames={period: signals_df(candles(x["id"],period)) for period in BARS}
        frames["8H"]=signals_df(resample_8h(frames["4H"]))
        candidates=[]
        for fn in (strategy_a,strategy_b):
            hit=fn(frames)
            if hit:
                out=plan_trade(x,frames,hit)
                if out:candidates.append(out)
        return candidates
    except Exception as exc:
        LOG.warning("%s excluded: %s",x["id"], str(exc)[:180])
        return []


def sent_event_ids(topic):
    """Poll public-topic history for idempotency across ephemeral Render Cron containers.
    Fails closed: cannot confirm history -> do not send duplicate-prone alerts.
    """
    url=f"https://ntfy.sh/{quote(topic,safe='')}/json"
    # ntfy json poll returns NDJSON (not one JSON document).
    for attempt in range(2):
        try:
            rr=requests.get(url,params={"poll":"1","since":"24h"}, timeout=(5,12))
            rr.raise_for_status()
            messages=[json.loads(line) for line in rr.text.splitlines() if line.strip()]
            return {str(m.get("message","")).split("EVENT_ID=")[-1].split()[0]
                    for m in messages if "EVENT_ID=" in str(m.get("message",""))}
        except Exception:
            if attempt:raise
            time.sleep(2)
    return set()


def publish(items):
    if not (NOTIFY and ACK and NTFY_TOPIC and len(NTFY_TOPIC)>=20):
        LOG.warning("Push disabled. To enable: ENABLE_PUSH=true, RESEARCH_ACK=true, long NTFY_TOPIC.")
        return
    previous=sent_event_ids(NTFY_TOPIC)
    for s in items:
        if s["event"] in previous:
            continue
        text = (f"{s['id']} {s['strategy']} 多單研究訊號\n"
                f"限價區間 {s['entry_zone'][0]}~{s['entry_zone'][1]} | 參考賣一 {s['entry']}\n"
                f"停損 {s['stop']} | TP1 {s['tp1']} | TP2 {s['tp2']}\n"
                f"淨估風報 TP1 {s['rr_tp1_net_est']}、TP2 {s['rr_tp2_net_est']}\n"
                f"{s['reason']}\n"
                f"確認時間(UTC) {s['signal_time_utc']} | 有效至 {s['expiry_utc']}\n"
                f"研究用，未完成樣本外回測；非即時成交承諾，槓桿可能強平。\nEVENT_ID={s['event']}")
        r=requests.post(f"https://ntfy.sh/{quote(NTFY_TOPIC,safe='')}",
                        data=text.encode(),headers={"Title":"OKX strategy alert", "Priority":"high","Cache":"yes"},
                        timeout=(5,12))
        r.raise_for_status()
        previous.add(s["event"])
        LOG.info("Sent event %s",s["event"])


def main():
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
    start=time.monotonic()
    coins=cap_symbols()
    universe=select_contracts(coins)
    if not universe:
        raise RuntimeError("No valid OKX candidates; no signal")
    LOG.info("Eligible global top-cap=%s; matched liquid OKX contracts=%s",len(coins),len(universe))
    results=[]
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures={pool.submit(scan_instrument,x):x for x in universe}
        for f in as_completed(futures):
            results.extend(f.result())
    results.sort(key=lambda s:(-s["rr_tp1_net_est"],-s["vol_usd_est"]))
    picked=[]
    unique_ids=set()
    for candidate in results:
        if candidate["id"] in unique_ids:
            continue
        unique_ids.add(candidate["id"])
        picked.append(candidate)
        if len(picked)>=3:
            break
    print(json.dumps({"date_utc":datetime.now(UTC).isoformat(),"candidate_coins":len(coins),
         "eligible_swaps":len(universe),"qualified_signals":len(results),
         "selected":picked,"push_enabled":bool(NOTIFY and ACK and NTFY_TOPIC),
         "elapsed_seconds":round(time.monotonic()-start,1)}, ensure_ascii=False))
    if picked:publish(picked)


if __name__=="__main__":
    main()
