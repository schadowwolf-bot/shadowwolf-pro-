#!/usr/bin/env python3
import os, time, math, json, traceback
from datetime import datetime, timezone
import requests
import numpy as np

BASE_URL = "https://api.bitget.com"
PRODUCT_TYPE = os.getenv("PRODUCT_TYPE", "mix").strip().lower()  # "mix" (futures) o "spot"

# MULTI-SYMBOL: legge da env SYMBOLS (comma separated) o usa default
def _defaults_by_pt(pt: str) -> list[str]:
    if pt == "mix":
        return ["BTCUSDT_UMCBL", "ETHUSDT_UMCBL"]
    return ["BTCUSDT", "ETHUSDT"]
SYMBOLS = [s.strip() for s in os.getenv("SYMBOLS", ",".join(_defaults_by_pt(PRODUCT_TYPE))).split(",") if s.strip()]

INTERVAL_SEC = 60
HEARTBEAT_EVERY = 7200   # 2h
ERROR_COOLDOWN  = 1800   # 30m
MAX_RETRIES_HTTP = 3
TIMEOUT_HTTP = 10

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID   = os.environ["CHAT_ID"]

LAST_ERROR = None
LAST_ERROR_TS = 0
last_heartbeat = 0
last_regime = {sym: None for sym in SYMBOLS}  # regime per simbolo

def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def http_get(url: str, params: dict = None):
    if params is None: params = {}
    for i in range(MAX_RETRIES_HTTP):
        try:
            r = requests.get(url, params=params, timeout=TIMEOUT_HTTP)
            if r.status_code == 200:
                return r.json()
        except Exception:
            if i == MAX_RETRIES_HTTP - 1:
                raise
        time.sleep(0.4 + i*0.5)
    raise RuntimeError(f"HTTP GET fallita: {url}")

def send_telegram(text: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10
        )
    except Exception:
        pass

def _pick_price(obj):
    if isinstance(obj, dict):
        for k in ("price","last","lastPr","lastPrice","close","markPrice","askPx","bidPx"):
            v = obj.get(k)
            if v is not None:
                try:
                    p = float(v)
                    if p > 0 and math.isfinite(p): return p
                except: pass
        bid = obj.get("bidPx") or obj.get("bidPrice")
        ask = obj.get("askPx") or obj.get("askPrice")
        try:
            if bid and ask:
                pb, pa = float(bid), float(ask)
                if pb>0 and pa>0: return (pb+pa)/2.0
            if bid:
                pb = float(bid)
                if pb>0: return pb
            if ask:
                pa = float(ask)
                if pa>0: return pa
        except: return None
        return None
    if isinstance(obj, (list,tuple)):
        for e in obj:
            r = _pick_price(e)
            if r: return r
    return None

def fetch_ticker_price(symbol: str) -> float | None:
    if PRODUCT_TYPE == "mix":
        j = http_get(f"{BASE_URL}/api/mix/v1/market/ticker", {"symbol": symbol})
    else:
        j = http_get(f"{BASE_URL}/api/spot/v1/market/ticker", {"symbol": symbol})
    data = j.get("data", j)
    return _pick_price(data)

def fetch_kline_close(symbol: str, tf: str, limit: int = 200) -> list[float]:
    if PRODUCT_TYPE == "mix":
        j = http_get(f"{BASE_URL}/api/mix/v1/market/candles",
                     {"symbol": symbol, "granularity": str(tf), "limit": str(limit)})
        data = j.get("data", j)
        if not isinstance(data, list): return []
        closes = []
        for row in data:
            try: closes.append(float(row[4]))
            except: pass
        return closes[::-1]
    else:
        j = http_get(f"{BASE_URL}/api/spot/v1/market/candles",
                     {"symbol": symbol, "period": tf, "limit": str(limit)})
        data = j.get("data", j)
        if not isinstance(data, list): return []
        closes = []
        for row in data:
            try: closes.append(float(row[4]))
            except: pass
        return closes[::-1]

def fetch_price(symbol: str) -> float:
    p = fetch_ticker_price(symbol)
    if p and p>0: return p
    closes = fetch_kline_close(symbol, 60 if PRODUCT_TYPE=="mix" else "1min", 1)
    if closes: return closes[-1]
    raise RuntimeError(f"Prezzo non disponibile per {symbol}")

def rsi(series: list[float], period: int = 14) -> float | None:
    if series is None or len(series) <= period: return None
    arr = np.array(series, dtype=float)
    diff = np.diff(arr)
    gain = np.where(diff>0, diff, 0.0)
    loss = np.where(diff<0, -diff, 0.0)
    avg_gain = np.empty_like(arr); avg_loss = np.empty_like(arr)
    avg_gain[:] = np.nan; avg_loss[:] = np.nan
    avg_gain[period] = gain[:period].mean()
    avg_loss[period] = loss[:period].mean()
    for i in range(period+1, len(arr)):
        avg_gain[i] = (avg_gain[i-1]*(period-1) + gain[i-1]) / period
        avg_loss[i] = (avg_loss[i-1]*(period-1) + loss[i-1]) / period
    rs = avg_gain[-1] / (avg_loss[-1] + 1e-12)
    return float(100.0 - (100.0 / (1.0 + rs)))

def regime_from_rsi(rsi1h: float | None, rsi4h: float | None) -> str:
    if rsi1h is None or rsi4h is None: return "WAIT"
    up, dn = 55.0, 45.0
    if rsi1h>up and rsi4h>up: return "LONG"
    if rsi1h<dn and rsi4h<dn: return "SHORT"
    return "WAIT"

def main():
    global LAST_ERROR, LAST_ERROR_TS, last_heartbeat, last_regime

    send_telegram("🐺 Shadowwolf avviato — modalità swing, no scalping. Notifiche solo a segnale + heartbeat 2h.")

    while True:
        t0 = time.time()
        try:
            for sym in SYMBOLS:
                # dati & indicatori
                price = fetch_price(sym)
                if PRODUCT_TYPE == "mix":
                    c1h = fetch_kline_close(sym, 3600, 200)
                    c4h = fetch_kline_close(sym, 14400, 200)
                else:
                    c1h = fetch_kline_close(sym, "1hour", 200)
                    c4h = fetch_kline_close(sym, "4hour", 200)
                r1 = rsi(c1h,14); r4 = rsi(c4h,14)
                regime = regime_from_rsi(r1, r4)

                prev = last_regime.get(sym)
                if regime != prev and regime in ("LONG","SHORT"):
                    emoji = "🟢 LONG" if regime=="LONG" else "🔴 SHORT"
                    send_telegram(
                        f"⏰ {utc_now()}\n{sym} | RSI 1H: {None if r1 is None else round(r1,1)} | "
                        f"RSI 4H: {None if r4 is None else round(r4,1)}\nRegime: {emoji}\nPrezzo: {price}"
                    )
                    last_regime[sym] = regime
                elif prev is None:
                    last_regime[sym] = regime
                    send_telegram(
                        f"⏰ {utc_now()}\n{sym} | RSI 1H: {None if r1 is None else round(r1,1)} | "
                        f"RSI 4H: {None if r4 is None else round(r4,1)}\nRegime iniziale: {regime}\nPrezzo: {price}"
                    )

            if time.time() - last_heartbeat >= HEARTBEAT_EVERY:
                send_telegram("🐺 Shadowwolf attivo — heartbeat 2h.")
                last_heartbeat = time.time()

            LAST_ERROR = None; LAST_ERROR_TS = 0

        except Exception as e:
            now = int(time.time())
            msg = f"{type(e).__name__}: {e}"
            if LAST_ERROR != msg or (now - LAST_ERROR_TS) >= ERROR_COOLDOWN:
                send_telegram(f"⚠️ Errore ciclo: {msg}")
                LAST_ERROR, LAST_ERROR_TS = msg, now

        dt = time.time() - t0
        time.sleep(max(5.0, INTERVAL_SEC - dt))

if __name__ == "__main__":
    main()
