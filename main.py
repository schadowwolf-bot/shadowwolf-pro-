#!/usr/bin/env python3
# 🐺 Shadowwolf Trader PRO — single-file, Render Free friendly
# - Solo dati pubblici Bitget (futures USDT)
# - RSI(14) 1H/4H Wilder, EMA adattive, MFI, OBV
# - Anti-scalping (persistenza + cooldown), Telegram
# - Web server /health stdlib per Render Free (niente dipendenze)
# - Stato locale: shadowwolf_state.json

import os, sys, json, time, math
from datetime import datetime
from collections import deque
from urllib.parse import urlencode
from urllib.request import urlopen, Request
from zoneinfo import ZoneInfo
from threading import Thread
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ===== CONFIG =====
TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT  = os.getenv("CHAT_ID", "").strip()
PRODUCT_TYPE = os.getenv("PRODUCT_TYPE", "usdt-futures").strip()  # usdt-futures

SYMBOLS = [s.strip() for s in os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT").split(",") if s.strip()]

POLL_SECS     = int(os.getenv("POLL_SECS", "60"))
PERSIST_N     = int(os.getenv("PERSIST_N", "3"))
COOLDOWN_SECS = int(os.getenv("COOLDOWN_SECS", str(30*60)))
K_SIGMA       = float(os.getenv("K_SIGMA", "0.8"))
STATUS_MODE   = os.getenv("STATUS_MODE", "on_change")  # on_change | interval | off
STATUS_EVERY  = int(os.getenv("STATUS_EVERY", "300"))
PRICE_TOL_PCT = float(os.getenv("PRICE_TOL_PCT", "0.15"))  # %
RSI_TOL       = float(os.getenv("RSI_TOL", "0.5"))
TZ_NAME       = os.getenv("TZ_NAME", "Europe/Rome")
PORT          = int(os.getenv("PORT", "8000"))  # Render setta questa

STATE_FILE = "shadowwolf_state.json"
TZ = ZoneInfo(TZ_NAME)

# ===== ENDPOINTS BITGET =====
CANDLES_URL = "https://api.bitget.com/api/v2/mix/market/candles"
TICKER_URL  = "https://api.bitget.com/api/v2/mix/market/ticker"

def http_get(url:str, params:dict, timeout:float=15.0):
    q = urlencode(params)
    req = Request(url + "?" + q, headers={"User-Agent":"shadowwolf/1.0"})
    with urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

# ===== TELEGRAM =====
def tg(text:str):
    if not (TOKEN and CHAT):
        print("[TG]", text); return
    try:
        data = urlencode({"chat_id": CHAT, "text": text, "parse_mode":"HTML", "disable_web_page_preview":"true"}).encode()
        req = Request(f"https://api.telegram.org/bot{TOKEN}/sendMessage", data=data)
        with urlopen(req, timeout=15): pass
    except Exception as e:
        print("[TG warn]", e)

def now_ce():
    label = "CEST" if datetime.now(TZ).dst() else "CET"
    return datetime.now(TZ).strftime(f"%Y-%m-%d %H:%M:%S {label}")

# ===== INDICATORI (stdlib) =====
def ema_series(values, span):
    if span < 2: span = 2
    out = []; k = 2/(span+1); s = None
    for v in values:
        if v is None: out.append(None); continue
        s = v if s is None else (v*k + s*(1-k)); out.append(s)
    return out

def rsi_wilder(values, n=14):
    diffs=[None]
    for i in range(1,len(values)):
        a,b = values[i], values[i-1]
        diffs.append(None if (a is None or b is None) else a-b)
    gq=deque(maxlen=n); lq=deque(maxlen=n)
    avg_g=avg_l=None; out=[]
    for i,d in enumerate(diffs):
        if d is None or values[i] is None: out.append(None); continue
        g = max(d,0.0); l = max(-d,0.0)
        if len(gq)<n:
            gq.append(g); lq.append(l)
            if len(gq)==n:
                avg_g=sum(gq)/n; avg_l=sum(lq)/n
                rs = avg_g/(avg_l if avg_l>0 else 1e-9)
                out.append(100-100/(1+rs))
            else: out.append(None)
        else:
            avg_g=(avg_g*(n-1)+g)/n; avg_l=(avg_l*(n-1)+l)/n
            rs = avg_g/(avg_l if avg_l>0 else 1e-9)
            out.append(100-100/(1+rs))
    return out

def rolling_std_pct(values, window=30):
    if len(values)<window+1: return 0.0
    ch=[]
    for i in range(1,len(values)):
        a,b=values[i],values[i-1]
        ch.append( (a-b)/b if (a and b) else 0.0 )
    seg=ch[-window:]; mean=sum(seg)/len(seg)
    var=sum((x-mean)**2 for x in seg)/len(seg)
    return (var**0.5)*100.0

def obv_series(closes, vols):
    obv=0.0; out=[]
    for i in range(len(closes)):
        if i==0 or closes[i] is None or closes[i-1] is None or vols[i] is None:
            out.append(obv); continue
        if closes[i]>closes[i-1]: obv+=vols[i]
        elif closes[i]<closes[i-1]: obv-=vols[i]
        out.append(obv)
    return out

def slope(values, w=12):
    if len(values)<w: return 0.0
    y=values[-w:]; x=list(range(w))
    xm=sum(x)/w; ym=sum(y)/w
    num=sum((x[i]-xm)*(y[i]-ym) for i in range(w))
    den=sum((x[i]-xm)**2 for i in range(w)) or 1e-9
    return num/den

def adaptive_bounds(rsi_list, k=K_SIGMA):
    base=[v for v in rsi_list if v is not None][-200:]
    if len(base)<20: return 60.0,40.0
    m=sum(base)/len(base); var=sum((x-m)**2 for x in base)/len(base); sd=var**0.5
    up  = max(55.0, min(75.0, m + k*sd))
    low = min(45.0, max(25.0, m - k*sd))
    return up, low

# ===== FETCH DATI BITGET =====
def fetch_candles(symbol, gran="1H", limit=400):
    data = http_get(CANDLES_URL, {"symbol":symbol,"productType":PRODUCT_TYPE,"granularity":gran,"limit":min(limit,1000)})
    rows = list(reversed((data or {}).get("data", [])))
    out=[]
    for r in rows:
        ts=int(r[0]); o=float(r[1]); h=float(r[2]); l=float(r[3]); c=float(r[4])
        qv=float(r[5] or 0.0); bv=float(r[6] or 0.0)
        out.append({"ts":ts,"o":o,"h":h,"l":l,"c":c,"qv":qv,"bv":bv})
    return out

def fetch_price(symbol):
    try:
        d = http_get(TICKER_URL, {"symbol":symbol,"productType":PRODUCT_TYPE})
        data = (d or {}).get("data", {}) or {}
        for k in ("lastPr","last","price","close"):
            try:
                v=float(data.get(k,"")); if v: return v
            except: pass
        bid=float(data.get("bestBid","") or 0); ask=float(data.get("bestAsk","") or 0)
        if bid>0 and ask>0: return (bid+ask)/2
    except: pass
    return None

# ===== STATO =====
def load_state():
    st={}
    if os.path.exists(STATE_FILE):
        try: st=json.load(open(STATE_FILE,"r",encoding="utf-8"))
        except: st={}
    for s in SYMBOLS:
        st.setdefault(s,{"last":"WAIT","persist":0,"last_change":0.0,"last_votes":[0,0],
                         "snap":{"price":None,"rsi1":None,"rsi4":None,"bull":None},
                         "last_status_ts":0.0})
    return st

def save_state(st):
    tmp=STATE_FILE+".tmp"
    json.dump(st, open(tmp,"w",encoding="utf-8"), ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)

state=load_state()

# ===== DECISIONE =====
def decide(symbol):
    d1=fetch_candles(symbol,"1H",400); d4=fetch_candles(symbol,"4H",400)
    if not d1 or not d4: raise RuntimeError("no data")

    px=fetch_price(symbol)
    if px:
        for d in (d1,d4):
            d[-1]["c"]=px; d[-1]["h"]=max(d[-1]["h"],px); d[-1]["l"]=min(d[-1]["l"],px)

    c1=[r["c"] for r in d1]; v1=[r["bv"] if r["bv"]>0 else r["qv"] for r in d1]
    c4=[r["c"] for r in d4]

    volp=rolling_std_pct(c1,30)
    boost=1.0+min(0.5, volp/3.0)
    f1=max(10, int(len(d1)*0.12/boost)); s1=max(30, int(len(d1)*0.40/boost))
    f4=max(10, int(len(d4)*0.12/boost)); s4=max(30, int(len(d4)*0.40/boost))

    ef1=ema_series(c1,f1); es1=ema_series(c1,s1)
    ef4=ema_series(c4,f4); es4=ema_series(c4,s4)
    r1=rsi_wilder(c1,14); r4=rsi_wilder(c4,14)
    ob1=obv_series(c1,v1)

    up1,lw1=adaptive_bounds(r1); up4,lw4=adaptive_bounds(r4)
    last_r1=next((x for x in reversed(r1) if x is not None), 50.0)
    last_r4=next((x for x in reversed(r4) if x is not None), 50.0)
    last_c = c1[-1]

    bull1 = (ef1[-1] or 0) > (es1[-1] or 0)
    bull4 = (ef4[-1] or 0) > (es4[-1] or 0)
    ob_sl = slope(ob1,12)

    votes_long=votes_short=0
    votes_long+=int(bull1); votes_short+=int(not bull1)
    if last_r1>=up1: votes_long+=1
    if last_r1<=lw1: votes_short+=1
    if bull4 and last_r4>=up4: votes_long+=1
    if (not bull4) and last_r4<=lw4: votes_short+=1
    if ob_sl>0: votes_long+=1
    if ob_sl<0: votes_short+=1

    regime="WAIT"; VOTE_NEED=3
    if votes_long>=VOTE_NEED and votes_long>votes_short: regime="LONG"
    elif votes_short>=VOTE_NEED and votes_short>votes_long: regime="SHORT"

    # ATR semplice
    trs=[]
    for i in range(1,len(d1)):
        h,l,cprev=d1[i]["h"],d1[i]["l"],d1[i-1]["c"]
        trs.append(max(h-l, abs(h-cprev), abs(l-cprev)))
    atr = (sum(trs[-14:])/min(14,len(trs))) if trs else (last_c*0.004)

    ctx={"price":last_c,"rsi1":round(last_r1,1),"rsi4":round(last_r4,1),
         "up1":round(up1,1),"low1":round(lw1,1),"up4":round(up4,1),"low4":round(lw4,1),
         "bull1":bool(bull1),"bull4":bool(bull4),
         "votes_long":int(votes_long),"votes_short":int(votes_short),
         "tp":round(last_c+1.5*atr,2),"sl":round(last_c-1.0*atr,2)}
    return regime, ctx

def materially_changed(ctx,snap):
    if snap["price"] is None: return True
    if bool(ctx["bull1"]) != bool(snap["bull"]): return True
    if abs(ctx["price"]-snap["price"])/max(1.0,abs(snap["price"])) > (PRICE_TOL_PCT/100): return True
    if abs(ctx["rsi1"]-(snap["rsi1"] or 0)) >= RSI_TOL: return True
    if abs(ctx["rsi4"]-(snap["rsi4"] or 0)) >= RSI_TOL: return True
    return False

def send_status(sym, ctx, regime):
    trend = "📈" if ctx["bull1"] else "📉"
    tg("🕒 "+now_ce()+f"\n{sym} | RSI1H {ctx['rsi1']:.1f} (dyn {ctx['low1']:.1f}-{ctx['up1']:.1f}) | "
       f"RSI4H {ctx['rsi4']:.1f} | Trend: {trend} | Regime: {regime} | Px: {ctx['price']:.2f}")

def send_signal(sym, regime, ctx):
    decision="🟢 LONG" if regime=="LONG" else ("🔴 SHORT" if regime=="SHORT" else "🟡 ATTESA")
    trend="📈 BULL" if ctx["bull1"] else "📉 BEAR"
    tg(
        f"<b>Shadowwolf Trader PRO</b> | {sym}\n"
        f"⏰ {now_ce()}\n"
        f"Prezzo: <b>{ctx['price']:.2f}</b>\n"
        f"RSI 1H: <b>{ctx['rsi1']:.1f}</b> (dyn {ctx['low1']:.1f}-{ctx['up1']:.1f}) | RSI 4H: <b>{ctx['rsi4']:.1f}</b>\n"
        f"Trend 1H: <b>{trend}</b> | Voti L/S: {ctx['votes_long']}/{ctx['votes_short']}\n"
        f"Decisione: <b>{decision}</b>\n"
        f"🎯 TP: <b>{ctx['tp']}</b> | 🛑 SL: <b>{ctx['sl']}</b>\n"
        f"<i>Solo lettura. Anti-scalping: persistenza {PERSIST_N} cicli + cooldown {COOLDOWN_SECS//60}m.</i>"
    )

# ===== LOOP BOT =====
def bot_loop():
    tg("🐺 Shadowwolf Trader PRO avviato — autonomo, anti-scalping, prezzo live.")
    state = load_state()
    while True:
        try:
            for sym in SYMBOLS:
                try:
                    regime, ctx = decide(sym)
                except Exception as e:
                    tg(f"⚠️ {sym}: errore dati ({e})"); continue
                st = state[sym]; prev=st["last"]; votes_tuple=[ctx["votes_long"],ctx["votes_short"]]

                # status
                send = False
                if STATUS_MODE=="on_change":
                    if materially_changed(ctx, st["snap"]) and (time.time()-st["last_status_ts"]>=60): send=True
                elif STATUS_MODE=="interval":
                    if time.time()-st["last_status_ts"]>=STATUS_EVERY: send=True
                if send:
                    send_status(sym, ctx, prev)
                    st["snap"]={"price":ctx["price"],"rsi1":ctx["rsi1"],"rsi4":ctx["rsi4"],"bull":ctx["bull1"]}
                    st["last_status_ts"]=time.time()

                # persistenza + cooldown
                strong = regime in ("LONG","SHORT")
                if strong and regime!=prev:
                    st["persist"]= st["persist"]+1 if votes_tuple==st["last_votes"] else 1
                else:
                    st["persist"]=0
                st["last_votes"]=votes_tuple

                if strong and regime!=prev and st["persist"]>=PERSIST_N and (time.time()-st["last_change"]>=COOLDOWN_SECS):
                    send_signal(sym, regime, ctx)
                    st["last"]=regime; st["last_change"]=time.time(); st["persist"]=0

            save_state(state)
            time.sleep(POLL_SECS)
        except Exception as e:
            tg(f"⚠️ Errore ciclo: {e}"); time.sleep(5)

# ===== WEB SERVER /health (Render Free) =====
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

    def log_message(self, fmt, *args):  # silenzia log HTTP
        return

def start_http():
    srv = ThreadingHTTPServer(("", PORT), Handler)
    print(f"[HTTP] listening on :{PORT} /health")
    srv.serve_forever()

if __name__ == "__main__":
    # avvia web server in thread
    Thread(target=start_http, daemon=True).start()
    # avvia bot loop
    bot_loop()
