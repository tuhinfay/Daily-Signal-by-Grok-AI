#!/usr/bin/env python3
"""Daily Signal by Grok AI — clean terminal UI"""

from fastapi import FastAPI, Query, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone
from pydantic import BaseModel, Field
import uvicorn, os, traceback, uuid, threading, time as time_mod

from scanner.binance_client import BinanceClient
from scanner.signal_engine import SignalEngine
import config

app = FastAPI(title="Daily Signal by Grok AI", version="3.2.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_cache: Dict[str, Any] = {
    "signals": [], "last_updated": None, "scanning": False,
    "error": None, "scanned_count": 0,
    "gainers": [], "losers": [], "prices": {},
}
_trades: Dict[str, Dict] = {}
_account = {
    "balance": float(getattr(config, "DEMO_BALANCE", 10000)),
    "equity": float(getattr(config, "DEMO_BALANCE", 10000)),
    "realized_pnl": 0.0,
    "start_balance": float(getattr(config, "DEMO_BALANCE", 10000)),
}
_monitor_running = False


class TradeCreate(BaseModel):
    symbol: str
    side: str
    entry: float
    stop_loss: float
    tp1: float
    tp2: Optional[float] = None
    leverage: int = Field(default=10, ge=1, le=150)
    risk_pct: Optional[float] = 1.0
    note: Optional[str] = ""


class AccountReset(BaseModel):
    balance: float = 10000


def run_scan(top_n: int = 28, symbols: Optional[List[str]] = None):
    global _cache
    _cache["scanning"] = True
    _cache["error"] = None
    try:
        client = BinanceClient(futures=True)
        engine = SignalEngine()

        if symbols:
            pairs = [(s.upper().replace("/", ""), 0) for s in symbols]
        else:
            pairs = client.get_top_usdt_pairs(top_n=top_n, min_volume=config.MIN_VOLUME_USDT)

        # movers snapshot
        try:
            mv = client.get_movers(top_n=10)
            _cache["gainers"] = mv.get("gainers", [])
            _cache["losers"] = mv.get("losers", [])
        except Exception:
            pass

        signals = []
        prices = {}
        for symbol, vol in pairs:
            data = {}
            for tf in ("15m", "1h", "4h", "1d"):
                df = client.fetch_ohlcv(symbol, timeframe=tf, limit=config.CANDLE_LIMIT.get(tf, 100))
                if df is not None and len(df) > 30:
                    data[tf] = df
            if len(data) < 3:
                continue
            try:
                r = engine.analyze_pair(symbol, data, volume_24h=vol)
                if not r:
                    continue
                # live price + max lev
                px = client.get_current_price(symbol)
                if px:
                    r["market_price"] = px
                    prices[symbol] = px
                else:
                    r["market_price"] = r["entry"]
                r["max_leverage"] = client.get_max_leverage(symbol)
                # style tags from score/ADX/setup
                styles = []
                if r.get("adx", 0) >= 22 and r.get("score", 0) >= 58:
                    styles.append("day")
                if r.get("setup") in ("MOMENTUM BREAK", "REVERSAL / MEAN REVERT") or r.get("score", 0) >= 55:
                    styles.append("scalp")
                if r.get("bias_1d") == r.get("bias_4h") and r.get("bias_1d") != "neutral":
                    styles.append("swing")
                if not styles:
                    styles = ["day"]
                r["styles"] = styles
                signals.append(r)
            except Exception:
                continue

        signals.sort(key=lambda x: x["score"], reverse=True)
        _cache["signals"] = signals
        _cache["prices"] = prices
        _cache["last_updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        _cache["scanned_count"] = len(pairs)
    except Exception as e:
        _cache["error"] = str(e)
        traceback.print_exc()
    finally:
        _cache["scanning"] = False


def _calc_equity():
    u = sum((t.get("unrealized_pnl") or 0) for t in _trades.values() if t.get("status") == "OPEN")
    _account["equity"] = round(_account["balance"] + u, 2)


def price_monitor_loop():
    global _monitor_running
    client = BinanceClient(futures=True)
    while _monitor_running:
        try:
            for t in list(_trades.values()):
                if t.get("status") != "OPEN":
                    continue
                price = client.get_current_price(t["symbol"])
                if price is None:
                    continue
                t["current_price"] = price
                _cache["prices"][t["symbol"]] = price
                side, entry, qty, lev = t["side"].upper(), t["entry"], t.get("qty") or 0, t.get("leverage") or 1
                if side == "LONG":
                    pnl = (price - entry) * qty
                    t["pnl_pct"] = round(((price - entry) / entry) * 100 * lev, 2)
                    if price <= t["stop_loss"]:
                        t["hit"], t["needs_alert"] = "SL", True
                    elif t.get("tp2") and price >= t["tp2"]:
                        t["hit"], t["needs_alert"] = "TP2", True
                    elif price >= t["tp1"]:
                        t["hit"], t["needs_alert"] = "TP1", True
                else:
                    pnl = (entry - price) * qty
                    t["pnl_pct"] = round(((entry - price) / entry) * 100 * lev, 2)
                    if price >= t["stop_loss"]:
                        t["hit"], t["needs_alert"] = "SL", True
                    elif t.get("tp2") and price <= t["tp2"]:
                        t["hit"], t["needs_alert"] = "TP2", True
                    elif price <= t["tp1"]:
                        t["hit"], t["needs_alert"] = "TP1", True
                t["unrealized_pnl"] = round(pnl, 2)
            _calc_equity()
        except Exception as e:
            print("mon", e)
        time_mod.sleep(10)


@app.on_event("startup")
def startup():
    global _monitor_running
    _monitor_running = True
    threading.Thread(target=lambda: run_scan(22), daemon=True).start()
    threading.Thread(target=price_monitor_loop, daemon=True).start()


@app.on_event("shutdown")
def shutdown():
    global _monitor_running
    _monitor_running = False


@app.get("/api/signals")
async def api_signals(style: str = Query("all")):
    open_keys = {(t["symbol"], t["side"]) for t in _trades.values() if t.get("status") == "OPEN"}
    out = []
    for s in _cache["signals"]:
        s2 = dict(s)
        s2["already_added"] = (s["symbol"], s["side"]) in open_keys
        # refresh market price from cache if any
        if s2["symbol"] in _cache.get("prices", {}):
            s2["market_price"] = _cache["prices"][s2["symbol"]]
        if style and style != "all":
            if style not in (s2.get("styles") or []):
                continue
        out.append(s2)
    return {
        "last_updated": _cache["last_updated"],
        "scanning": _cache["scanning"],
        "error": _cache["error"],
        "scanned_count": _cache["scanned_count"],
        "count": len(out),
        "signals": out,
        "gainers": _cache.get("gainers", []),
        "losers": _cache.get("losers", []),
        "account": _account,
    }


@app.get("/api/analyze/{symbol}")
async def analyze_one(symbol: str):
    """Manual target coin analysis."""
    sym = symbol.upper().replace("/", "").replace("-", "")
    if not sym.endswith("USDT"):
        sym += "USDT"
    client = BinanceClient(futures=True)
    engine = SignalEngine()
    data = {}
    for tf in ("15m", "1h", "4h", "1d"):
        df = client.fetch_ohlcv(sym, timeframe=tf, limit=config.CANDLE_LIMIT.get(tf, 100))
        if df is not None and len(df) > 30:
            data[tf] = df
    if len(data) < 3:
        return JSONResponse({"ok": False, "error": "Not enough data for " + sym}, status_code=404)
    r = engine.analyze_pair(sym, data, volume_24h=0)
    if not r:
        # return neutral snapshot
        px = client.get_current_price(sym)
        return {"ok": True, "signal": None, "symbol": sym, "market_price": px,
                "max_leverage": client.get_max_leverage(sym),
                "message": "No high-confluence signal right now"}
    r["market_price"] = client.get_current_price(sym) or r["entry"]
    r["max_leverage"] = client.get_max_leverage(sym)
    r["styles"] = ["day", "scalp", "swing"]
    return {"ok": True, "signal": r}


@app.get("/api/account")
async def api_account():
    _calc_equity()
    return {**_account, "open_trades": sum(1 for t in _trades.values() if t["status"] == "OPEN")}


@app.post("/api/account/reset")
async def reset_account(body: AccountReset):
    for t in list(_trades.values()):
        if t.get("status") == "OPEN":
            t["status"] = "CLOSED"
            t["hit"] = "RESET"
            t["needs_alert"] = False
    _account.update({
        "balance": float(body.balance), "equity": float(body.balance),
        "realized_pnl": 0.0, "start_balance": float(body.balance),
    })
    return {"ok": True, "account": _account}


@app.get("/api/trades")
async def api_trades():
    return {"trades": list(_trades.values()), "account": _account}


@app.get("/api/alerts")
async def api_alerts():
    alerts = [t for t in _trades.values() if t.get("status") == "OPEN" and t.get("needs_alert") and t.get("hit")]
    return {"alerts": alerts, "count": len(alerts)}


@app.post("/api/trades")
async def create_trade(body: TradeCreate):
    sym = body.symbol.upper().replace("/", "")
    side = body.side.upper()
    client = BinanceClient(futures=True)
    max_lev = client.get_max_leverage(sym)
    lev = max(1, min(int(body.leverage), max_lev, 150))

    for t in _trades.values():
        if t.get("status") == "OPEN" and t["symbol"] == sym and t["side"] == side:
            return {"ok": True, "trade": t, "duplicate": True}

    risk_pct = max(0.25, min(float(body.risk_pct or 1.0), 5.0))
    risk_usd = _account["balance"] * (risk_pct / 100.0)
    sl_dist = abs(body.entry - body.stop_loss)
    if sl_dist <= 0:
        return JSONResponse({"ok": False, "error": "invalid SL"}, status_code=400)
    qty = risk_usd / sl_dist
    margin = (qty * body.entry) / lev
    if margin > _account["balance"] * 0.95:
        return JSONResponse({"ok": False, "error": "insufficient balance"}, status_code=400)

    tid = str(uuid.uuid4())[:8]
    trade = {
        "id": tid, "symbol": sym, "side": side,
        "entry": body.entry, "stop_loss": body.stop_loss,
        "tp1": body.tp1, "tp2": body.tp2,
        "leverage": lev, "max_leverage": max_lev,
        "qty": round(qty, 6), "margin": round(margin, 2),
        "notional": round(qty * body.entry, 2), "risk_usd": round(risk_usd, 2),
        "note": body.note or "", "status": "OPEN", "hit": None, "needs_alert": False,
        "current_price": body.entry, "pnl_pct": 0.0, "unrealized_pnl": 0.0,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _trades[tid] = trade
    _calc_equity()
    return {"ok": True, "trade": trade, "duplicate": False, "account": _account}


@app.post("/api/trades/{tid}/close")
async def close_trade(tid: str):
    if tid not in _trades:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    t = _trades[tid]
    if t.get("status") != "OPEN":
        return {"ok": True, "trade": t}
    pnl = t.get("unrealized_pnl") or 0.0
    _account["balance"] = round(_account["balance"] + pnl, 2)
    _account["realized_pnl"] = round(_account["realized_pnl"] + pnl, 2)
    t.update({"status": "CLOSED", "needs_alert": False,
              "closed_at": datetime.now(timezone.utc).isoformat(),
              "hit": t.get("hit") or "MANUAL", "realized_pnl": round(pnl, 2)})
    _calc_equity()
    return {"ok": True, "trade": t, "account": _account}


@app.get("/scan")
async def trigger_scan(background_tasks: BackgroundTasks, top: int = Query(28, ge=10, le=40)):
    if not _cache["scanning"]:
        background_tasks.add_task(run_scan, top_n=top)
    return RedirectResponse(url="/", status_code=303)


@app.get("/health")
async def health():
    return {"status": "ok", "scanning": _cache["scanning"]}


@app.get("/", response_class=HTMLResponse)
async def home():
    return HTMLResponse(PAGE)


PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<meta name="theme-color" content="#070b14">
<title>Daily Signal by Grok AI</title>
<style>
:root{--bg:#070b14;--c:#10182a;--bd:#1a2438;--t:#e8eefc;--m:#8494b0;--cy:#22d3ee;--bl:#3b82f6;--gn:#10b981;--rd:#f43f5e;--gd:#fbbf24}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:Inter,system-ui,sans-serif;background:var(--bg);color:var(--t);font-size:13px}
.hdr{position:sticky;top:0;z-index:50;background:#070b14f2;backdrop-filter:blur(10px);border-bottom:1px solid var(--bd);padding:10px 12px}
.hdr h1{font-size:15px;font-weight:800;background:linear-gradient(90deg,var(--cy),var(--bl));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin-top:8px}
.stat{background:var(--c);border:1px solid var(--bd);border-radius:8px;padding:7px 8px}
.stat b{display:block;color:var(--m);font-size:9px;font-weight:600;text-transform:uppercase}
.stat span{font-weight:700;font-size:12px}
.stat span.up{color:var(--gn)}.stat span.dn{color:var(--rd)}
.nav{display:flex;gap:6px;padding:8px 12px;position:sticky;top:86px;z-index:40;background:var(--bg)}
.nav button{flex:1;border:1px solid var(--bd);background:var(--c);color:var(--m);border-radius:8px;padding:8px;font-weight:700;font-size:11px}
.nav button.on{background:linear-gradient(135deg,#1d4ed8,#0891b2);color:#fff;border-color:transparent}
.tools{display:flex;flex-wrap:wrap;gap:6px;padding:0 12px 8px}
.tools input{flex:1;min-width:100px;background:var(--c);border:1px solid var(--bd);color:var(--t);border-radius:8px;padding:8px 10px;font-size:12px}
.btn{border:0;border-radius:8px;padding:8px 11px;font-weight:700;font-size:11px;color:#fff;background:linear-gradient(135deg,#2563eb,#0891b2);cursor:pointer}
.btn.g{background:#1e293b;border:1px solid var(--bd)}
.btn.ok{background:linear-gradient(135deg,#059669,#10b981)}
.btn.rd{background:linear-gradient(135deg,#e11d48,#f43f5e)}
.btn:disabled{opacity:.5}
.chips{display:flex;gap:6px;padding:0 12px 8px;flex-wrap:wrap}
.chip{border:1px solid var(--bd);background:var(--c);color:var(--m);border-radius:999px;padding:5px 10px;font-size:11px;font-weight:600}
.chip.on{background:#132238;color:var(--cy);border-color:#1e4b6b}
.msg{margin:0 12px 8px;padding:8px 10px;background:var(--c);border:1px solid var(--bd);border-radius:8px;color:var(--m);font-size:11px}
/* FLAT signal row — no nested boxes */
.sig{margin:0 10px 8px;background:var(--c);border:1px solid var(--bd);border-radius:10px;padding:11px 12px;border-left:3px solid var(--bd)}
.sig.L{border-left-color:var(--gn)}.sig.S{border-left-color:var(--rd)}
.sig.hit{border-left-color:var(--gd)}
.sig .top{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:6px}
.sig .nm{font-weight:800;font-size:14px}
.sig .sd{font-weight:800;font-size:11px;margin-right:6px}
.sig .sd.L{color:var(--gn)}.sig .sd.S{color:var(--rd)}
.sig .sc{font-weight:900;color:var(--cy);font-size:15px}
.tag{display:inline-block;font-size:9px;font-weight:700;padding:2px 6px;border-radius:4px;margin-left:4px;background:#182033;color:var(--m)}
.tag.a{background:#0b3b2e;color:#6ee7b7}.tag.b{background:#1e3a5f;color:#93c5fd}
.meta{color:var(--m);font-size:11px;margin-bottom:6px;line-height:1.45}
.prices{display:flex;flex-wrap:wrap;gap:10px 14px;margin:6px 0;font-size:12px}
.prices em{font-style:normal;color:var(--m);font-size:10px;display:block}
.prices strong{font-weight:700}
.plan{color:#a9b8d4;font-size:11px;line-height:1.45;margin:6px 0;white-space:pre-wrap}
.rowbtns{display:flex;gap:6px;margin-top:8px;flex-wrap:wrap}
.rowbtns .btn{flex:1;min-width:90px}
.mv{display:flex;gap:8px;overflow-x:auto;padding:0 12px 8px}
.mv .item{min-width:110px;background:var(--c);border:1px solid var(--bd);border-radius:8px;padding:8px}
.mv .item .s{font-weight:700;font-size:12px}
.mv .item .c{font-size:11px;font-weight:700}.mv .up{color:var(--gn)}.mv .dn{color:var(--rd)}
.empty{text-align:center;padding:28px;color:var(--m)}
#toast{position:fixed;bottom:16px;left:12px;right:12px;z-index:100;display:none;padding:12px;border-radius:10px;background:linear-gradient(135deg,#1d4ed8,#0891b2);color:#fff;font-weight:700;text-align:center}
#alertBar{position:fixed;top:0;left:0;right:0;z-index:200;display:none;padding:11px;background:linear-gradient(90deg,#be123c,#f43f5e);color:#fff;font-weight:800;text-align:center;font-size:12px}
.modal-bg{position:fixed;inset:0;background:#000c;z-index:150;display:none;align-items:flex-end;justify-content:center}
.modal{width:100%;max-width:420px;background:var(--c);border:1px solid var(--bd);border-radius:16px 16px 0 0;padding:14px}
.modal h3{font-size:14px;margin-bottom:8px}
.modal label{display:block;color:var(--m);font-size:10px;margin:8px 0 3px;text-transform:uppercase}
.modal input,.modal select{width:100%;background:#0a1020;border:1px solid var(--bd);color:var(--t);border-radius:8px;padding:9px;font-size:13px}
.modal .btns{display:flex;gap:8px;margin-top:12px}
.modal .btns .btn{flex:1;padding:11px}
iframe.chart{width:100%;height:220px;border:0;border-radius:8px;margin-top:8px;background:#000}
.sec{padding:10px 12px 4px;color:var(--m);font-size:10px;text-transform:uppercase;letter-spacing:.05em;font-weight:700}
</style>
</head>
<body>
<div id="alertBar" onclick="showTab('trades')"></div>
<div class="hdr">
  <h1>Daily Signal by Grok AI</h1>
  <div class="stats">
    <div class="stat"><b>Balance</b><span id="sBal">—</span></div>
    <div class="stat"><b>Equity</b><span id="sEq">—</span></div>
    <div class="stat"><b>PnL</b><span id="sReal">—</span></div>
    <div class="stat"><b>Open</b><span id="sOpen">0</span></div>
  </div>
</div>
<div class="nav">
  <button class="on" data-t="signals" onclick="showTab('signals')">Signals</button>
  <button data-t="trades" onclick="showTab('trades')">Trades</button>
  <button data-t="movers" onclick="showTab('movers')">Movers</button>
  <button data-t="account" onclick="showTab('account')">Account</button>
</div>

<div id="tab-signals">
  <div class="tools">
    <input id="symIn" placeholder="BTCUSDT / ETH…" />
    <button class="btn" onclick="analyzeManual()">Analyze</button>
    <button class="btn g" onclick="runScan()">Scan</button>
    <button class="btn g" id="soundBtn" onclick="toggleSound()">Sound</button>
  </div>
  <div class="chips">
    <button class="chip on" data-s="all" onclick="setStyle('all')">All</button>
    <button class="chip" data-s="scalp" onclick="setStyle('scalp')">Scalp</button>
    <button class="chip" data-s="day" onclick="setStyle('day')">Day</button>
    <button class="chip" data-s="swing" onclick="setStyle('swing')">Swing</button>
  </div>
  <div class="msg" id="status">Loading…</div>
  <div id="signalList"></div>
</div>

<div id="tab-trades" style="display:none">
  <div class="msg">TP/SL hit → alert continues until you Close.</div>
  <div id="tradeList"></div>
</div>

<div id="tab-movers" style="display:none">
  <div class="sec">Top gainers (24h)</div>
  <div class="mv" id="gainers"></div>
  <div class="sec">Top losers (24h)</div>
  <div class="mv" id="losers"></div>
  <div class="msg">Tap a coin to analyze signal.</div>
</div>

<div id="tab-account" style="display:none">
  <div class="sig">
    <div class="prices">
      <div><em>Start</em><strong id="aStart">—</strong></div>
      <div><em>Balance</em><strong id="aBal">—</strong></div>
      <div><em>Equity</em><strong id="aEq">—</strong></div>
      <div><em>Realized</em><strong id="aReal">—</strong></div>
    </div>
    <div class="rowbtns">
      <button class="btn g" onclick="resetAcc(10000)">Reset 10k</button>
      <button class="btn g" onclick="resetAcc(100000)">Reset 100k</button>
    </div>
  </div>
</div>

<div id="toast"></div>
<div class="modal-bg" id="modalBg">
  <div class="modal">
    <h3 id="mTitle">Open Trade</h3>
    <input type="hidden" id="m_symbol">
    <label>Side</label><select id="m_side"><option>LONG</option><option>SHORT</option></select>
    <label>Entry</label><input type="number" id="m_entry" step="any">
    <label>Stop Loss</label><input type="number" id="m_sl" step="any">
    <label>TP1</label><input type="number" id="m_tp1" step="any">
    <label>TP2</label><input type="number" id="m_tp2" step="any">
    <label>Leverage <span id="mLevHint"></span></label>
    <input type="number" id="m_lev" value="10" min="1" max="150">
    <label>Risk % balance</label><input type="number" id="m_risk" value="1" min="0.25" max="5" step="0.25">
    <div class="btns">
      <button class="btn g" onclick="closeModal()">Cancel</button>
      <button class="btn ok" onclick="submitTrade()">Open</button>
    </div>
  </div>
</div>

<script>
let soundOn=true, style='all', lastSig='', notifOK=false;
if('Notification'in window){if(Notification.permission==='granted')notifOK=true;else if(Notification.permission==='default')Notification.requestPermission().then(p=>notifOK=p==='granted');}
function beep(n=2){if(!soundOn)return;try{const c=new(window.AudioContext||window.webkitAudioContext);for(let i=0;i<n;i++)setTimeout(()=>{const o=c.createOscillator(),g=c.createGain();o.connect(g);g.connect(c.destination);o.type='square';o.frequency.value=i%2?720:960;g.gain.value=.1;o.start();g.gain.exponentialRampToValueAtTime(.001,c.currentTime+.18);o.stop(c.currentTime+.18);},i*240);}catch(e){}}
function toast(m,ms=2800){const t=document.getElementById('toast');t.textContent=m;t.style.display='block';setTimeout(()=>t.style.display='none',ms);}
function notify(t,b){beep(4);if(notifOK){try{new Notification(t,{body:b,requireInteraction:true});}catch(e){}}toast(t+' — '+b,6000);}
function toggleSound(){soundOn=!soundOn;document.getElementById('soundBtn').textContent=soundOn?'Sound':'Muted';}
function showTab(n){['signals','trades','movers','account'].forEach(x=>{const el=document.getElementById('tab-'+x);if(el)el.style.display=x===n?'block':'none';});document.querySelectorAll('.nav button').forEach(b=>b.classList.toggle('on',b.dataset.t===n));if(n==='trades')loadTrades();if(n==='movers')loadMovers();if(n==='account')loadAccount();}
function setStyle(s){style=s;document.querySelectorAll('.chip').forEach(c=>c.classList.toggle('on',c.dataset.s===s));loadSignals();}
function fmt(n){return Number(n).toLocaleString(undefined,{maximumFractionDigits:2});}
function setAcc(a){if(!a)return;document.getElementById('sBal').textContent='$'+fmt(a.balance);document.getElementById('sEq').textContent='$'+fmt(a.equity);const r=document.getElementById('sReal');r.textContent=(a.realized_pnl>=0?'+':'')+'$'+fmt(a.realized_pnl);r.className=a.realized_pnl>=0?'up':'dn';const x=id=>document.getElementById(id);if(x('aStart')){x('aStart').textContent='$'+fmt(a.start_balance);x('aBal').textContent='$'+fmt(a.balance);x('aEq').textContent='$'+fmt(a.equity);x('aReal').textContent=(a.realized_pnl>=0?'+':'')+'$'+fmt(a.realized_pnl);}}

function tvSymbol(sym){return 'BINANCE:'+sym+'.P';}
function cardHTML(s){
  const L=s.side==='LONG';
  const q=s.quality||'B';
  const mp=s.market_price??s.entry;
  const lev=s.max_leverage||20;
  const added=s.already_added;
  const styles=(s.styles||[]).join(' · ');
  return `<div class="sig ${L?'L':'S'}">
    <div class="top">
      <div><span class="sd ${L?'L':'S'}">${s.side}</span><span class="nm">${s.symbol}</span>
        <span class="tag ${q==='A+'||q==='A'?'a':'b'}">${q}</span>
        <span class="tag">${s.liquidity||'MED'}</span>
        <span class="tag">max ${lev}x</span></div>
      <div class="sc">${s.score}</div>
    </div>
    <div class="meta">${s.setup||'CONFLUENCE'} · ${styles||'day'} · 1D ${s.bias_1d||'—'} · 4H ${s.bias_4h||'—'} · ADX ${s.adx??'—'} · RSI ${s.rsi??'—'}</div>
    <div class="prices">
      <div><em>Entry</em><strong>${s.entry}</strong></div>
      <div><em>Market</em><strong>${mp}</strong></div>
      <div><em>SL</em><strong>${s.stop_loss}</strong> <span style="color:var(--m);font-size:10px">(${s.risk_pct}%)</span></div>
      <div><em>TP1</em><strong>${s.tp1}</strong></div>
      <div><em>TP2</em><strong>${s.tp2}</strong></div>
      <div><em>R:R</em><strong>1:${s.rr}</strong></div>
    </div>
    <div class="plan">${s.plan||''}</div>
    <div class="meta">${(s.reasons||[]).join(' · ')}</div>
    <iframe class="chart" loading="lazy" src="https://s.tradingview.com/widgetembed/?frameElementId=tv&symbol=${tvSymbol(s.symbol)}&interval=15&hidesidetoolbar=1&hidetoptoolbar=1&symboledit=0&saveimage=0&toolbarbg=0f1628&theme=dark&style=1&timezone=Etc%2FUTC"></iframe>
    <div class="rowbtns">
      ${added?'<button class="btn g" disabled>Added</button>':`<button class="btn ok" onclick='openModal(${JSON.stringify(s)})'>Add Trade</button>`}
    </div>
  </div>`;
}

async function loadSignals(){
  try{
    const d=await(await fetch('/api/signals?style='+style)).json();
    setAcc(d.account);
    const st=document.getElementById('status');
    if(d.scanning)st.textContent='Scanning…';
    else if(d.error)st.textContent='⚠️ '+d.error;
    else st.textContent=`${d.count||0} signals · ${d.scanned_count||0} pairs · ${d.last_updated||'—'}`;
    const list=document.getElementById('signalList');
    if(!d.signals||!d.signals.length){list.innerHTML='<div class="empty">No signals. Scan or Analyze a coin.</div>';return;}
    const h=d.signals.map(s=>s.symbol+s.side+s.score).join('|');
    if(lastSig&&h!==lastSig){beep(2);toast('Signals updated');}
    lastSig=h;
    list.innerHTML=d.signals.map(cardHTML).join('');
    window._lastMovers={gainers:d.gainers||[],losers:d.losers||[]};
  }catch(e){document.getElementById('status').textContent='Load failed';}
}
function runScan(){toast('Scan started');location.href='/scan?top=28';}
async function analyzeManual(){
  let s=(document.getElementById('symIn').value||'').trim().toUpperCase().replace('/','').replace('-','');
  if(!s){toast('Enter symbol');return;}
  if(!s.endsWith('USDT'))s+='USDT';
  toast('Analyzing '+s);
  const d=await(await fetch('/api/analyze/'+s)).json();
  const list=document.getElementById('signalList');
  if(!d.ok){list.innerHTML=`<div class="empty">${d.error||'Failed'}</div>`;return;}
  if(!d.signal){list.innerHTML=`<div class="sig"><div class="nm">${d.symbol}</div><div class="meta">Market ${d.market_price??'—'} · max ${d.max_leverage||'—'}x</div><div class="empty">${d.message||'No signal'}</div>
    <iframe class="chart" src="https://s.tradingview.com/widgetembed/?symbol=${tvSymbol(d.symbol)}&interval=15&hidesidetoolbar=1&hidetoptoolbar=1&theme=dark&style=1"></iframe></div>`;return;}
  list.innerHTML=cardHTML(d.signal);
}
function openModal(s){
  document.getElementById('mTitle').textContent=s.symbol+' · max '+(s.max_leverage||20)+'x';
  document.getElementById('m_symbol').value=s.symbol;
  document.getElementById('m_side').value=s.side;
  document.getElementById('m_entry').value=s.market_price||s.entry;
  document.getElementById('m_sl').value=s.stop_loss;
  document.getElementById('m_tp1').value=s.tp1;
  document.getElementById('m_tp2').value=s.tp2||'';
  const ml=s.max_leverage||20;
  document.getElementById('m_lev').value=Math.min(10,ml);
  document.getElementById('m_lev').max=ml;
  document.getElementById('mLevHint').textContent='(max '+ml+'x)';
  document.getElementById('modalBg').style.display='flex';
}
function closeModal(){document.getElementById('modalBg').style.display='none';}
async function submitTrade(){
  const body={symbol:m_symbol.value,side:m_side.value,entry:+m_entry.value,stop_loss:+m_sl.value,tp1:+m_tp1.value,tp2:+m_tp2.value||null,leverage:+m_lev.value||10,risk_pct:+m_risk.value||1};
  const d=await(await fetch('/api/trades',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
  if(d.ok){closeModal();toast(d.duplicate?'Already open':'Trade opened');setAcc(d.account);loadSignals();showTab('trades');loadTrades();}
  else toast(d.error||'Failed');
}
async function loadTrades(){
  const d=await(await fetch('/api/trades')).json();setAcc(d.account);
  const open=(d.trades||[]).filter(t=>t.status==='OPEN');
  const closed=(d.trades||[]).filter(t=>t.status==='CLOSED');
  document.getElementById('sOpen').textContent=open.length;
  let h='';
  if(open.length){h+='<div class="sec">Open</div>';h+=open.map(t=>{
    const L=t.side==='LONG';const up=(t.unrealized_pnl||0)>=0;
    return `<div class="sig ${L?'L':'S'}${t.hit?' hit':''}">
      <div class="top"><div><span class="sd ${L?'L':'S'}">${t.side}</span><span class="nm">${t.symbol}</span>
        <span class="tag">${t.leverage}x</span>${t.hit?`<span class="tag" style="background:#fbbf24;color:#111">${t.hit}</span>`:''}</div>
        <div class="sc" style="color:${up?'var(--gn)':'var(--rd)'}">${up?'+':''}$${fmt(t.unrealized_pnl||0)}</div></div>
      <div class="prices">
        <div><em>Entry</em><strong>${t.entry}</strong></div>
        <div><em>Market</em><strong>${t.current_price??'—'}</strong></div>
        <div><em>SL</em><strong>${t.stop_loss}</strong></div>
        <div><em>TP1</em><strong>${t.tp1}</strong></div>
        <div><em>Margin</em><strong>$${fmt(t.margin)}</strong></div>
        <div><em>PnL%</em><strong>${t.pnl_pct||0}%</strong></div>
      </div>
      <div class="rowbtns"><button class="btn rd" onclick="closeTrade('${t.id}')">Close</button></div>
    </div>`;}).join('');}
  if(closed.length){h+='<div class="sec">Closed</div>';h+=closed.slice(-8).reverse().map(t=>`
    <div class="sig" style="opacity:.7"><div class="top">
      <div><span class="sd ${t.side==='LONG'?'L':'S'}">${t.side}</span><span class="nm">${t.symbol}</span> <span class="tag">${t.hit||'CLOSED'}</span></div>
      <div>${(t.realized_pnl||0)>=0?'+':''}$${fmt(t.realized_pnl||0)}</div></div></div>`).join('');}
  document.getElementById('tradeList').innerHTML=h||'<div class="empty">No trades</div>';
}
async function closeTrade(id){if(!confirm('Close trade?'))return;const d=await(await fetch('/api/trades/'+id+'/close',{method:'POST'})).json();if(d.ok){toast('Closed');setAcc(d.account);document.getElementById('alertBar').style.display='none';loadTrades();loadSignals();}}
function loadMovers(){
  const m=window._lastMovers||{gainers:[],losers:[]};
  const mk=(arr,up)=>arr.map(x=>`<div class="item" onclick="document.getElementById('symIn').value='${x.symbol}';showTab('signals');analyzeManual()">
    <div class="s">${x.symbol.replace('USDT','')}</div>
    <div class="c ${up?'up':'dn'}">${x.change>=0?'+':''}${Number(x.change).toFixed(2)}%</div>
    <div class="meta">${x.price}</div></div>`).join('')||'<div class="empty">Scan first</div>';
  document.getElementById('gainers').innerHTML=mk(m.gainers||[],true);
  document.getElementById('losers').innerHTML=mk(m.losers||[],false);
}
async function loadAccount(){const a=await(await fetch('/api/account')).json();setAcc(a);document.getElementById('sOpen').textContent=a.open_trades||0;}
async function resetAcc(bal){if(!confirm('Reset to $'+bal+'?'))return;const d0=await(await fetch('/api/trades')).json();for(const t of(d0.trades||[]).filter(x=>x.status==='OPEN'))await fetch('/api/trades/'+t.id+'/close',{method:'POST'});const d=await(await fetch('/api/account/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({balance:bal})})).json();if(d.ok){setAcc(d.account);toast('Reset');}}
async function checkAlerts(){try{const d=await(await fetch('/api/alerts')).json();const bar=document.getElementById('alertBar');if(d.count>0){const msg=d.alerts.map(t=>`${t.hit} ${t.symbol}`).join(' · ');bar.textContent='ALERT · '+msg+' · Open Trades to Close';bar.style.display='block';notify('Trade alert',msg);}else bar.style.display='none';}catch(e){}}
loadSignals();
setInterval(loadSignals,50000);
setInterval(checkAlerts,12000);
setInterval(()=>{if(document.getElementById('tab-trades').style.display!=='none')loadTrades();},12000);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("api:app", host="0.0.0.0", port=port, reload=False)
