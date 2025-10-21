#!/usr/bin/env python3
"""
Shadowwolf Trader PRO (solo analisi) - versione stabile, 1 file.
- Dati: prezzo/klines pubblici (Binance) → niente chiavi API, niente ordini.
- Autonomo: RSI 1H e 4H + trend/regime dinamici, NO parametri manuali RSI.
- Anti-scalping: serve persistenza del segnale ≥ 3 cicli + cooldown 30m.
- Notifiche Telegram: SOLO entrata/uscita/inversione + heartbeat ogni 2 ore.
- Anti-spam: max 1 msg/min; errori compressi.
Compatibile con Render (free). Non richiede storage.
"""

import os, time, math, json, traceback
from datetime import datetime, timezone, timedelta

import requests
import pandas as pd
import numpy as np
from ta.momentum import RSIIndicator

# ──────────────────────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID   = os.getenv("CHAT_ID", "").strip()

SYMBOLS = ["BTCUSDT", "ETHUSDT"]
BASE_URL = "https://api.binance.com"  # SOLO lettura
USER_AGENT = "shadowwolf-analyst/1.0"

POLL_SECS   = 60
HEARTBEAT_H = 2
PERSIST_CYCLES = 3          # anti-scalping: persistenza minima
COOLDOWN_MIN  = 30          # anti-scalping: pausa dopo segnale

# ──────────────────────────────────────────────────────────────────────────────
# TELEGRAM
# ──────────────────────────────────────────────────────────────────────────────
def tg_send(text: str):
    """Invia messaggio Telegram con rate-limit semplice (max 1/min)."""
    if not BOT_TOKEN or not CHAT_ID:
        return
    now = time.time()
    last = _runtime["last_tg"]
    if last and now - last < 60:  # anti-spam
        _runtime["tg_buffer"].append(text)
        return
    _runtime["last_tg"] = now
    # Se c'è buffer, aggrega
    if _runtime["tg_buffer"]:
        text = "\n".join(_runtime["tg_buffer"][-4:] + [text])
        _runtime["tg_buffer"].clear()
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text},
            timeout=10,
        )
    except Exception:
        pass

def tg_start_banner():
    tg_send("🐺 Shadowwolf avviato — modalità swing, no scalping. "
            "Notifiche solo a segnale + heartbeat 2h.")

# ──────────────────────────────────────────────────────────────────────────────
# HTTP helpers robusti (gestiscono dict/list, errori silenziosi)
# ──────────────────────────────────────────────────────────────────────────────
def http_get(url, params=None, timeout=10):
    try:
        r = requests.get(url, params=params or {}, headers={"User-Agent": USER_AGENT}, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        raise RuntimeError(f"http error: {e}")

def parse_number(x, default=None):
    try:
        if isinstance(x, (float, int)): return float(x)
        if isinstance(x, str): return float(x.strip())
        return default
    except Exception:
        return default

# ──────────────────────────────────────────────────────────────────────────────
# DATA: prezzo live + klines per RSI
# ──────────────────────────────────────────────────────────────────────────────
def fetch_price(symbol):
    """
    Prova 1: /ticker/price → {'symbol':'BTCUSDT','price':'...'}
    Prova 2: /ticker/bookTicker → {'bidPrice':..., 'askPrice':...}
    """
    # 1) ticker/price
    try:
        j = http_get(f"{BASE_URL}/api/v3/ticker/price", {"symbol": symbol})
        # alcune lib trasformano in list se symbol non passato; protezione:
        if isinstance(j, list) and j:
            j = next((it for it in j if it.get("symbol")==symbol), j[0])
        p = parse_number(j.get("price"))
        if p: return p
    except Exception:
        pass

    # 2) bookTicker → media bid/ask
    try:
        j = http_get(f"{BASE_URL}/api/v3/ticker/bookTicker", {"symbol": symbol})
        if isinstance(j, list) and j:
            j = next((it for it in j if it.get("symbol")==symbol), j[0])
        bid = parse_number(j.get("bidPrice"))
        ask = parse_number(j.get("askPrice"))
        if bid and ask: return (bid + ask) / 2.0
        if bid: return bid
        if ask: return ask
    except Exception:
        pass

    raise RuntimeError(f"prezzo non disponibile per {symbol}")

def fetch_klines(symbol, interval="1h", limit=200):
    """
    /api/v3/klines → list di liste:
    [ openTime, open, high, low, close, volume, closeTime, ...]
    Ritorna DataFrame con colonna 'close' float.
    """
    j = http_get(f"{BASE_URL}/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    if not isinstance(j, list) or not j:
        raise RuntimeError("klines vuote")
    closes = [parse_number(row[4]) for row in j]
    ts     = [int(row[6]) for row in j]  # closeTime ms
    df = pd.DataFrame({"close": closes}, index=pd.to_datetime(ts, unit="ms", utc=True))
    return df

# ──────────────────────────────────────────────────────────────────────────────
# STRATEGIA: RSI 1H/4H + trend/regime dinamici (nessuna soglia fissa)
# ──────────────────────────────────────────────────────────────────────────────
def compute_rsi(close_series, period=14):
    return RSIIndicator(close_series, window=period).rsi()

def decide(symbol, price, rsi1h, rsi4h):
    """
    Logica semplice ma robusta:
    - regime LONG se entrambe le RSIs sopra la banda dinamica mediana (mediana mobile 50) e non in ipercomprato forte
    - regime SHORT se entrambe sotto la mediana e non in ipervenduto forte
    - altrimenti WAIT
    """
    med1 = pd.Series(rsi1h).rolling(50, min_periods=5).median().iloc[-1]
    med4 = pd.Series(rsi4h).rolling(50, min_periods=5).median().iloc[-1]
    r1   = rsi1h[-1]; r4 = rsi4h[-1]

    # fasce dinamiche
    up1 = min(80, med1 + 10)
    dn1 = max(20, med1 - 10)
    up4 = min(80, med4 + 10)
    dn4 = max(20, med4 - 10)

    long_ok  = (r1 > med1) and (r4 > med4) and (r1 < up1) and (r4 < up4)
    short_ok = (r1 < med1) and (r4 < med4) and (r1 > dn1) and (r4 > dn4)

    if long_ok:  return "LONG"
    if short_ok: return "SHORT"
    return "WAIT"

# ──────────────────────────────────────────────────────────────────────────────
# RUNTIME / PERSISTENZA IN MEMORIA
# ──────────────────────────────────────────────────────────────────────────────
_runtime = {
    "last_signal": {s: "WAIT" for s in SYMBOLS},
    "persist":     {s: 0 for s in SYMBOLS},   # conteggio persistenza
    "cooldown":    {s: 0.0 for s in SYMBOLS}, # epoch time fine cooldown
    "last_tg":     0.0,
    "tg_buffer":   [],
    "last_hb":     0.0,
}

def pretty_ts():
    return datetime.now(timezone(timedelta(hours=0))).strftime("%Y-%m-%d %H:%M:%S UTC")

def heartbeat_if_needed():
    now = time.time()
    if now - _runtime["last_hb"] >= HEARTBEAT_H*3600:
        _runtime["last_hb"] = now
        tg_start_banner()

# ──────────────────────────────────────────────────────────────────────────────
# LOOP
# ──────────────────────────────────────────────────────────────────────────────
def main_loop():
    tg_start_banner()
    while True:
        loop_start = time.time()
        try:
            heartbeat_if_needed()
            for sym in SYMBOLS:
                try:
                    price = fetch_price(sym)
                    df1h  = fetch_klines(sym, "1h", 200)
                    df4h  = fetch_klines(sym, "4h", 200)

                    rsi1h = compute_rsi(df1h["close"]).dropna().values
                    rsi4h = compute_rsi(df4h["close"]).dropna().values
                    if len(rsi1h) < 5 or len(rsi4h) < 5:
                        continue

                    raw_sig = decide(sym, price, rsi1h, rsi4h)

                    # Anti-scalping: richiede persistenza + cooldown
                    now = time.time()
                    if now < _runtime["cooldown"][sym]:
                        # In cooldown → non generare nuovi segnali
                        final_sig = "WAIT"
                        _runtime["persist"][sym] = 0
                    else:
                        # Persiste?
                        if raw_sig == _runtime["last_signal"][sym]:
                            _runtime["persist"][sym] += 1
                        else:
                            _runtime["persist"][sym] = 1
                        final_sig = raw_sig
                        # Applica barriera persistenza su LONG/SHORT
                        if final_sig in ("LONG","SHORT") and _runtime["persist"][sym] < PERSIST_CYCLES:
                            final_sig = "WAIT"

                    # Se cambia regime effettivo → invia segnale una volta
                    prev = _runtime["last_signal"][sym]
                    if final_sig != prev:
                        _runtime["last_signal"][sym] = final_sig
                        if final_sig in ("LONG","SHORT"):
                            # entra segnale → imposta cooldown
                            _runtime["cooldown"][sym] = time.time() + COOLDOWN_MIN*60

                        r1 = round(float(rsi1h[-1]),1); r4 = round(float(rsi4h[-1]),1)
                        msg = (f"⏱ {pretty_ts()}\n"
                               f"{sym} | RSI1H {r1} | RSI4H {r4}\n"
                               f"Prezzo: {price:,.2f}\n"
                               f"Decisione: {final_sig}\n"
                               f"Anti-scalping: persistenza {PERSIST_CYCLES} cicli + cooldown {COOLDOWN_MIN}m.")
                        tg_send(msg)

                except Exception as e_sym:
                    tg_send(f"⚠️ Errore ciclo {sym}: {str(e_sym)[:120]}")
                    continue

        except Exception as e:
            # errore globale → non spammare stack, messaggio breve
            tg_send(f"⚠️ Errore loop: {str(e)[:160]}")

        # sleep fino a completare 60s
        spent = time.time() - loop_start
        wait  = max(5, POLL_SECS - spent)
        time.sleep(wait)

# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    main_loop()
