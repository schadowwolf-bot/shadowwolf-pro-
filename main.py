#!/usr/bin/env python3
# Shadowwolf Trader PRO — Bitget (USDT-FUTURES) — Swing-only, 1-file
# - Dati pubblici Bitget v2: candles 1H/4H + ticker
# - RSI(14) su candele CHIUSE (no intrabar)
# - Anti-scalping forte: persistenza, gap minimo, movimento minimo, cooldown
# - Telegram: solo segnali + “evento importante” + heartbeat 2h
# - HTTP /health (stdlib) per Render Free (niente dipendenze extra)
# - Stato locale: shadowwolf_state.json

import os, sys, json, time, statistics, signal
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Tuple, Optional
from threading import Thread
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

# ================= CONFIG =================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID   = os.getenv("CHAT_ID", "").strip()

PRODUCT_TYPE = "usdt-futures"
SYMBOLS = ["BTCUSDT", "ETHUSDT"]

INTERVAL_SECONDS   = int(os.getenv("INTERVAL_SECONDS", "60"))   # frequenza loop
PERSIST_CYCLES     = int(os.getenv("PERSIST_CYCLES", "4"))     # conferme anti-scalping
COOLDOWN_MIN       = int(os.getenv("COOLDOWN_MIN", "180"))     # 3h senza nuovi segnali
MIN_SIGNAL_GAP_MIN = int(os.getenv("MIN_SIGNAL_GAP_MIN", "240"))# 4h min tra segnali
MIN_MOVE_PCT       = float(os.getenv("MIN_MOVE_PCT", "0.6"))    # min movimento vs ultimo segnale
HEARTBEAT_MIN      = int(os.getenv("HEARTBEAT_MIN", "120"))     # ping “attivo” ogni 2h

STATE_FILE = "shadowwolf_state.json"
TIMEOUT = 12

# HTTP server per Render Free
PORT = int(os.getenv("PORT", "8000"))

# Endpoints Bitget v2
BASE = "https://api.bitget.com"
CANDLES = "/api/v2/mix/market/candles"
TICKER  = "/api/v2/mix/market/ticker"

# ================ UTILS ===================
def now_utc_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def http_get(path: str, params: Dict[str, str]) -> Dict:
    url = BASE + path
    r = requests.get(url, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()

def load_state() -> Dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(st: Dict) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def send_telegram(text: str) -> None:
    if not (BOT_TOKEN and CHAT_ID):
        print("[WARN] Telegram non configurato (BOT_TOKEN/CHAT_ID mancanti).")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"[WARN] Telegram error: {e}")

# ============== INDICATORI ================
def rsi14(closes: List[float]) -> float:
    period = 14
    if len(closes) < period + 1:
        return float("nan")
    gains, losses = [], []
    for i in range(-period, 0):
        diff = closes[i] - closes[i-1]
        (gains if diff > 0 else losses).append(abs(diff))
    avg_gain = sum(gains)/period
    avg_loss = sum(losses)/period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0/(1.0 + rs)

def dyn_bands(values: List[float]) -> Tuple[float,float,float]:
    if not values:
        return 50.0, 45.0, 55.0
    m = statistics.mean(values)
    sd = statistics.pstdev(values) if len(values) > 1 else 0.0
    k = 0.5
    lo = max(10.0, min(90.0, m - k*sd))
    hi = max(10.0, min(90.0, m + k*sd))
    return m, lo, hi

# ============== DATA (BITGET) =============
def fetch_closes(symbol: str, granularity: str, limit: int=120) -> List[float]:
    """Ritorna close in ordine crescente e scarta l’ultima candela se non è chiusa."""
    params = {"symbol": symbol, "productType": PRODUCT_TYPE, "granularity": granularity, "limit": str(limit)}
    j = http_get(CANDLES, params)
    if j.get("code") != "00000":
        raise RuntimeError(f"Bitget error candles {symbol} {granularity}: {j}")
    rows = j.get("data", [])
    rows_sorted = sorted(rows, key=lambda x: int(x[0]))  # per timestamp ASC
    closes = [float(x[4]) for x in rows_sorted]

    # scarta candela non CHIUSA (come TradingView)
    now = datetime.now(timezone.utc)
    step = timedelta(hours=1) if granularity.upper()=="1H" else timedelta(hours=4)
    last_ts = datetime.fromtimestamp(int(rows_sorted[-1][0]) / 1000, tz=timezone.utc)
    if (now - last_ts) < step:
        closes = closes[:-1]

    return closes

def fetch_price(symbol: str) -> float:
    """Prezzo live dal ticker (last/close/price/bestAsk)."""
    params = {"symbol": symbol, "productType": PRODUCT_TYPE}
    j = http_get(TICKER, params)
    if j.get("code") != "00000":
        raise RuntimeError(f"Bitget error ticker {symbol}: {j}")
    data = j.get("data", {}) or {}
    for k in ("last", "close", "price", "bestAsk"):
        try:
            v = float(data.get(k, "0") or 0)
        except (TypeError, ValueError):
            v = 0.0
        if v > 0:
            return v
    return 0.0

# ============== DECISIONE ================
def decide(symbol: str, price: float, rsi1h: float, rsi4h: float,
           rsi_hist1h: List[float]) -> Tuple[str, Dict[str, float], str]:
    """Ritorna (decisione, livelli, trend)."""
    _, lo, hi = dyn_bands(rsi_hist1h[-60:])  # 60h ~ 2.5 giorni
    trend = "BULL" if rsi4h >= 55 else ("BEAR" if rsi4h <= 45 else "NEUTRAL")
    decision = "WAIT"
    if rsi1h > hi and rsi4h >= 50:
        decision = "LONG"
    elif rsi1h < lo and rsi4h < 50:
        decision = "SHORT"

    # TP/SL “informativi” (nessun ordine)
    tp = price * (1.008 if decision == "LONG" else (0.992 if decision == "SHORT" else 1.0))
    sl = price * (0.993 if decision == "LONG" else (1.007 if decision == "SHORT" else 1.0))
    levels = {"tp": tp, "sl": sl, "lo": lo, "hi": hi}
    return decision, levels, trend

def should_alert(st: Dict, symbol: str, decision: str, trend: str, price: float) -> bool:
    """
    Invio segnale SOLO se:
      - decisione diversa dalla precedente
      - persistenza >= PERSIST_CYCLES
      - min gap temporale tra segnali (MIN_SIGNAL_GAP_MIN)
      - min movimento % vs ultimo segnale (MIN_MOVE_PCT)
      - non in cooldown
    """
    info = st.setdefault(symbol, {
        "last_signal": "INIT", "persist": 0, "cooldown_until": 0.0,
        "last_signal_time": 0.0, "last_signal_price": 0.0,
    })
    now_ts = time.time()
    if now_ts < info.get("cooldown_until", 0.0):
        return False

    last = info.get("last_signal", "INIT")
    persist = info.get("persist", 0)
    if decision == last:
        persist = min(persist + 1, PERSIST_CYCLES)
    else:
        persist = 1
    info["persist"] = persist

    if persist < PERSIST_CYCLES or decision == last:
        st[symbol] = info
        return False

    last_t = info.get("last_signal_time", 0.0)
    if last_t and (now_ts - last_t) < (MIN_SIGNAL_GAP_MIN * 60.0):
        st[symbol] = info
        return False

    last_px = info.get("last_signal_price", 0.0)
    if last_px > 0:
        move_pct = abs(price - last_px) / last_px * 100.0
        if move_pct < MIN_MOVE_PCT:
            st[symbol] = info
            return False

    info["last_signal"] = decision
    info["persist"] = 0
    info["last_signal_time"] = now_ts
    info["last_signal_price"] = float(price)
    info["cooldown_until"] = now_ts + COOLDOWN_MIN * 60.0
    st[symbol] = info
    return True

def fmt_msg(symbol: str, price: float, r1: float, r4: float, levels: Dict, trend: str, decision: str) -> str:
    ts_local = datetime.now().strftime("%Y-%m-%d %H:%M")
    return (
        f"<b>Shadowwolf Trader PRO</b> | <b>{symbol}</b>\n"
        f"🕒 {ts_local}\n"
        f"Prezzo LIVE: <b>{price:.2f}</b>\n"
        f"RSI 1H: <b>{r1:.1f}</b> (zone {levels['lo']:.1f}-{levels['hi']:.1f}) | RSI 4H: <b>{r4:.1f}</b>\n"
        f"Trend 4H: <b>{trend}</b>\n"
        f"Decisione: <b>{'🟢 LONG' if decision=='LONG' else ('🔴 SHORT' if decision=='SHORT' else '⚪️ ATTESA')}</b>\n"
        + (f"🎯 TP: <b>{levels['tp']:.2f}</b> | 🛑 SL: <b>{levels['sl']:.2f}</b>\n" if decision!='WAIT' else "")
        + f"Solo lettura. Anti-scalping: persistenza {PERSIST_CYCLES} cicli + cooldown {COOLDOWN_MIN}m."
    )

# ============== IMPORTANT & HEARTBEAT ================
def maybe_send_important(symbol: str, price: float, r1: float, r4: float, closes_1h: List[float]) -> None:
    """Evento importante: Δ1h >= soglia oppure RSI estremo (>=70 o <=30)."""
    try:
        imp_move = float(os.getenv("IMPORTANT_MOVE_PCT", "1.5"))
        rsi_extreme = float(os.getenv("RSI_EXTREME", "70"))
    except Exception:
        imp_move, rsi_extreme = 1.5, 70.0

    big_move = False
    move_pct = 0.0
    if len(closes_1h) >= 2:
        last_close = closes_1h[-2]  # chiusura H precedente
        move_pct = abs(price - last_close) / max(1e-9, last_close) * 100.0
        big_move = move_pct >= imp_move

    extreme = (r1 >= rsi_extreme or r1 <= (100 - rsi_extreme))

    if big_move or extreme:
        msg = (
            f"⚠️ <b>{symbol}</b> evento rilevante\n"
            f"Prezzo: <b>{price:.2f}</b> | Δ1h: {move_pct:.2f}%\n"
            f"RSI1H: <b>{r1:.1f}</b> | RSI4H: <b>{r4:.1f}</b>"
        )
        send_telegram(msg)

def maybe_send_heartbeat(st: Dict) -> None:
    meta = st.setdefault("_meta", {"last_heartbeat": 0.0, "boot_banner_sent": False})
    now = time.time()
    if now - meta.get("last_heartbeat", 0.0) >= HEARTBEAT_MIN * 60:
        send_telegram(f"✅ Shadowwolf attivo — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        meta["last_heartbeat"] = now

# ============== LOOP ================
def run_once(symbol: str, st: Dict) -> None:
    closes_1h = fetch_closes(symbol, "1H", limit=120)
    closes_4h = fetch_closes(symbol, "4H", limit=120)

    r1 = rsi14(closes_1h)
    r4 = rsi14(closes_4h)
    price = fetch_price(symbol)

    decision, levels, trend = decide(symbol, price, r1, r4, closes_1h)

    info = st.setdefault(symbol, {"last_signal": "INIT", "persist": 0, "cooldown_until": 0.0,
                                  "last_signal_time": 0.0, "last_signal_price": 0.0})
    first = (info["last_signal"] == "INIT")
    if first:
        # primo giro: inizializza senza inviare stato per simbolo
        info["last_signal"] = decision
        info["persist"] = 0
        info["cooldown_until"] = 0.0
        st[symbol] = info
        return

    # SOLO segnali; se non scatta, valuta “importante”
    if should_alert(st, symbol, decision, trend, price):
        send_telegram(fmt_msg(symbol, price, r1, r4, levels, trend, decision))
    else:
        maybe_send_important(symbol, price, r1, r4, closes_1h)

# HTTP /health (per Render Free)
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/health"):
            body = b'{"ok":true,"service":"shadowwolf"}'
            self.send_response(200)
            self.send_header("Content-Type","application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404); self.end_headers()
    def log_message(self, *_):  # silenzia access log
        return

def start_http():
    srv = ThreadingHTTPServer(("", PORT), Handler)
    print(f"[HTTP] listening on :{PORT} /health")
    srv.serve_forever()

def main():
    # Boot banner una sola volta
    st = load_state()
    meta = st.setdefault("_meta", {"last_heartbeat": 0.0, "boot_banner_sent": False})
    if not meta.get("boot_banner_sent"):
        send_telegram("🐺 Shadowwolf avviato — modalità swing, no scalping. Notifiche solo a segnale + heartbeat 2h.")
        meta["boot_banner_sent"] = True
        save_state(st)

    stop = {"flag": False}
    def _sig(*_): stop["flag"] = True
    for s in (signal.SIGINT, signal.SIGTERM):
        signal.signal(s, _sig)

    while not stop["flag"]:
        loop_start = time.time()
        try:
            for sym in SYMBOLS:
                run_once(sym, st)
            save_state(st)
            maybe_send_heartbeat(st)
        except Exception as e:
            err = f"⚠️ Errore ciclo: {e}"
            print(err)
            send_telegram(err)
        finally:
            elapsed = time.time() - loop_start
            time.sleep(max(5.0, INTERVAL_SECONDS - elapsed))

if __name__ == "__main__":
    # avvia HTTP /health in background
    Thread(target=start_http, daemon=True).start()
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
