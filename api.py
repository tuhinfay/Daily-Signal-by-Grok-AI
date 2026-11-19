#!/usr/bin/env python3
"""Daily Signal by Grok AI — Premium terminal + paper trading"""

from fastapi import FastAPI, Query, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional, Dict, Any
from datetime import datetime, timezone
from pydantic import BaseModel, Field
import uvicorn, os, traceback, uuid, threading, time as time_mod

from scanner.binance_client import BinanceClient
from scanner.signal_engine import SignalEngine
import config

app = FastAPI(title="Daily Signal by Grok AI", version="3.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_cache: Dict[str, Any] = {
    "signals": [], "last_updated": None, "scanning": False,
    "error": None, "scanned_count": 0, "new_signal": False,
}
_trades: Dict[str, Dict] = {}
_account: Dict[str, Any] = {
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
    leverage: int = Field(default=10, ge=1, le=25)
    margin_usd: Optional[float] = None  # if set, use this margin
    risk_pct: Optional[float] = None    # % of balance to risk
    note: Optional[str] = ""


class AccountReset(BaseModel):
    balance: float = 10000


def run_scan(top_n: int = 25):
    global _cache
    _cache["scanning"] = True
    _cache["error"] = None
    try:
        client = BinanceClient(futures=True)
        engine = SignalEngine()
        pairs = client.get_top_usdt_pairs(top_n=top_n, min_volume=config.MIN_VOLUME_USDT)
        signals = []
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
                if r:
                    signals.append(r)
            except Exception:
                continue
        signals.sort(key=lambda x: x["score"], reverse=True)
        _cache["signals"] = signals
        _cache["last_updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        _cache["scanned_count"] = len(pairs)
        _cache["new_signal"] = bool(signals)
    except Exception as e:
        _cache["error"] = str(e)
        traceback.print_exc()
    finally:
        _cache["scanning"] = False


def _calc_equity():
    unrealized = 0.0
    for t in _trades.values():
        if t.get("status") == "OPEN":
            unrealized += t.get("unrealized_pnl", 0) or 0
    _account["equity"] = round(_account["balance"] + unrealized, 2)


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
                side = t["side"].upper()
                entry = t["entry"]
                qty = t.get("qty", 0) or 0

                if side == "LONG":
                    pnl = (price - entry) * qty
                    t["pnl_pct"] = round(((price - entry) / entry) * 100 * t.get("leverage", 1), 2)
                    if price <= t["stop_loss"]:
                        t["hit"] = "SL"; t["needs_alert"] = True
                    elif t.get("tp2") and price >= t["tp2"]:
                        t["hit"] = "TP2"; t["needs_alert"] = True
                    elif price >= t["tp1"]:
                        t["hit"] = "TP1"; t["needs_alert"] = True
                else:
                    pnl = (entry - price) * qty
                    t["pnl_pct"] = round(((entry - price) / entry) * 100 * t.get("leverage", 1), 2)
                    if price >= t["stop_loss"]:
                        t["hit"] = "SL"; t["needs_alert"] = True
                    elif t.get("tp2") and price <= t["tp2"]:
                        t["hit"] = "TP2"; t["needs_alert"] = True
                    elif price <= t["tp1"]:
                        t["hit"] = "TP1"; t["needs_alert"] = True

                t["unrealized_pnl"] = round(pnl, 2)
                t["alert_at"] = datetime.now(timezone.utc).isoformat() if t.get("needs_alert") else t.get("alert_at")

            _calc_equity()
        except Exception as e:
            print("monitor", e)
        time_mod.sleep(10)


@app.on_event("startup")
def startup():
    global _monitor_running
    _monitor_running = True
    threading.Thread(target=lambda: run_scan(20), daemon=True).start()
    threading.Thread(target=price_monitor_loop, daemon=True).start()


@app.on_event("shutdown")
def shutdown():
    global _monitor_running
    _monitor_running = False


@app.get("/api/signals")
async def api_signals():
    open_keys = {(t["symbol"], t["side"]) for t in _trades.values() if t.get("status") == "OPEN"}
    signals = []
    for s in _cache["signals"]:
        s2 = dict(s)
        s2["already_added"] = (s["symbol"], s["side"]) in open_keys
        signals.append(s2)
    return {
        "last_updated": _cache["last_updated"],
        "scanning": _cache["scanning"],
        "error": _cache["error"],
        "scanned_count": _cache["scanned_count"],
        "count": len(signals),
        "signals": signals,
        "account": _account,
    }


@app.get("/api/account")
async def api_account():
    _calc_equity()
    open_n = sum(1 for t in _trades.values() if t["status"] == "OPEN")
    return {**_account, "open_trades": open_n}


@app.post("/api/account/reset")
async def reset_account(body: AccountReset):
    global _account, _trades
    _trades = {k: v for k, v in _trades.items() if v.get("status") == "CLOSED"}
    # close all open conceptually
    for t in list(_trades.values()):
        pass
    # wipe open
    _trades = {k: v for k, v in _trades.items() if v.get("status") != "OPEN"}
    # actually clear opens
    opens = [k for k, v in _trades.items() if v.get("status") == "OPEN"]
    for k in opens:
        del _trades[k]
    # simpler: mark open as closed void
    for t in list(_trades.values()):
        if t.get("status") == "OPEN":
            t["status"] = "CLOSED"
            t["hit"] = "RESET"
    _account = {
        "balance": float(body.balance),
        "equity": float(body.balance),
        "realized_pnl": 0.0,
        "start_balance": float(body.balance),
    }
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
    lev = max(1, min(int(body.leverage), getattr(config, "MAX_LEVERAGE", 25)))

    for t in _trades.values():
        if t.get("status") == "OPEN" and t["symbol"] == sym and t["side"] == side:
            return {"ok": True, "trade": t, "duplicate": True}

    # Position sizing from risk
    risk_pct = body.risk_pct if body.risk_pct is not None else getattr(config, "RISK_PER_TRADE_PCT", 1.0)
    risk_pct = max(0.25, min(risk_pct, 5.0))
    risk_usd = _account["balance"] * (risk_pct / 100.0)

    sl_dist = abs(body.entry - body.stop_loss)
    if sl_dist <= 0:
        return JSONResponse({"ok": False, "error": "invalid SL"}, status_code=400)

    # qty such that loss at SL ≈ risk_usd
    qty = risk_usd / sl_dist
    notional = qty * body.entry
    margin = notional / lev

    if body.margin_usd and body.margin_usd > 0:
        margin = min(body.margin_usd, _account["balance"] * 0.95)
        notional = margin * lev
        qty = notional / body.entry
        risk_usd = qty * sl_dist

    if margin > _account["balance"] * 0.95:
        return JSONResponse({"ok": False, "error": "insufficient demo balance"}, status_code=400)

    tid = str(uuid.uuid4())[:8]
    trade = {
        "id": tid,
        "symbol": sym,
        "side": side,
        "entry": body.entry,
        "stop_loss": body.stop_loss,
        "tp1": body.tp1,
        "tp2": body.tp2,
        "leverage": lev,
        "qty": round(qty, 6),
        "margin": round(margin, 2),
        "notional": round(qty * body.entry, 2),
        "risk_usd": round(risk_usd, 2),
        "note": body.note or "",
        "status": "OPEN",
        "hit": None,
        "needs_alert": False,
        "current_price": body.entry,
        "pnl_pct": 0.0,
        "unrealized_pnl": 0.0,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "mode": "DEMO",
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

    # Realize PnL into balance
    pnl = t.get("unrealized_pnl") or 0.0
    # if no price update yet, pnl 0
    _account["balance"] = round(_account["balance"] + pnl, 2)
    _account["realized_pnl"] = round(_account["realized_pnl"] + pnl, 2)
    t["status"] = "CLOSED"
    t["needs_alert"] = False
    t["closed_at"] = datetime.now(timezone.utc).isoformat()
    t["hit"] = t.get("hit") or "MANUAL"
    t["realized_pnl"] = round(pnl, 2)
    _calc_equity()
    return {"ok": True, "trade": t, "account": _account}


@app.get("/scan")
async def trigger_scan(background_tasks: BackgroundTasks, top: int = Query(25, ge=10, le=40)):
    if not _cache["scanning"]:
        background_tasks.add_task(run_scan, top_n=top)
    return RedirectResponse(url="/", status_code=303)


@app.get("/health")
async def health():
    return {"status": "ok", "scanning": _cache["scanning"], "equity": _account.get("equity")}


@app.get("/", response_class=HTMLResponse)
async def home():
    return HTMLResponse(PAGE)


PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<meta name="theme-color" content="#070b14">
<title>Daily Signal · Premium</title>
<style>
:root{--bg:#070b14;--card:#0f1628;--line:#1c2740;--text:#e8eefc;--mut:#8b9bb8;--blue:#3b82f6;--cyan:#22d3ee;--green:#10b981;--red:#f43f5e;--gold:#fbbf24;--purple:#a78bfa}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:Inter,system-ui,-apple-system,sans-serif;background:var(--bg);color:var(--text);font-size:13px;line-height:1.4}
.top{position:sticky;top:0;z-index:60;background:linear-gradient(180deg,#0b1220ee,#0b1220cc);backdrop-filter:blur(12px);border-bottom:1px solid var(--line);padding:10px 12px}
.brand{display:flex;justify-content:space-between;align-items:center;gap:8px}
.brand h1{font-size:15px;font-weight:800;letter-spacing:-.02em;background:linear-gradient(90deg,var(--cyan),var(--blue));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.pill{font-size:10px;font-weight:700;padding:3px 8px;border-radius:999px;background:#12203a;color:var(--cyan);border:1px solid #1e3a5f}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin-top:10px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px}
.stat .l{color:var(--mut);font-size:9px;text-transform:uppercase;letter-spacing:.04em}
.stat .v{font-weight:700;font-size:12px;margin-top:2px}
.stat .v.up{color:var(--green)}.stat .v.dn{color:var(--red)}
.tabs{display:flex;gap:6px;padding:8px 12px;position:sticky;top:88px;z-index:50;background:var(--bg)}
.tab{flex:1;border:1px solid var(--line);background:var(--card);color:var(--mut);border-radius:10px;padding:9px 6px;font-weight:700;font-size:11px;cursor:pointer}
.tab.on{background:linear-gradient(135deg,#1d4ed8,#0ea5e9);color:#fff;border-color:transparent}
.bar{display:flex;gap:6px;padding:0 12px 8px;flex-wrap:wrap}
.btn{border:0;border-radius:10px;padding:9px 12px;font-weight:700;font-size:11px;cursor:pointer;color:#fff;background:linear-gradient(135deg,#2563eb,#0891b2)}
.btn.sec{background:#1e293b;border:1px solid var(--line)}
.btn.ok{background:linear-gradient(135deg,#059669,#10b981)}
.btn.danger{background:linear-gradient(135deg,#e11d48,#f43f5e)}
.btn.ghost{background:transparent;border:1px solid var(--line);color:var(--mut)}
.btn:disabled,.btn.added{opacity:.55;pointer-events:none}
.status{margin:0 12px 8px;padding:9px 11px;border-radius:10px;background:var(--card);border:1px solid var(--line);color:var(--mut);font-size:11px}
.card{margin:0 10px 9px;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:12px;position:relative;overflow:hidden}
.card.long{box-shadow:inset 3px 0 0 var(--green)}
.card.short{box-shadow:inset 3px 0 0 var(--red)}
.card.hit{box-shadow:inset 3px 0 0 var(--gold);animation:pulse 1.1s infinite}
@keyframes pulse{50%{border-color:#fbbf2444}}
.row{display:flex;justify-content:space-between;align-items:center;gap:8px}
.side{font-weight:900;font-size:11px;letter-spacing:.04em}
.side.L{color:var(--green)}.side.S{color:var(--red)}
.sym{font-weight:800;font-size:14px;margin-left:4px}
.badge{font-size:9px;font-weight:800;padding:2px 6px;border-radius:6px;margin-left:4px}
.badge.qAP{background:#064e3b;color:#6ee7b7}.badge.qA{background:#1e3a5f;color:#93c5fd}.badge.qB{background:#27272a;color:#d4d4d8}
.badge.liq{background:#172554;color:#93c5fd}
.badge.hit{background:var(--gold);color:#111}
.score{font-weight:900;font-size:16px;color:var(--cyan)}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin-top:10px}
.cell{background:#0a1020;border:1px solid var(--line);border-radius:10px;padding:7px 8px}
.cell .l{color:var(--mut);font-size:9px;text-transform:uppercase}
.cell .v{font-weight:700;margin-top:2px;font-size:12px}
.plan{margin-top:8px;padding:8px;border-radius:10px;background:#0a1020;border:1px dashed #243353;color:#b6c4e0;font-size:11px;white-space:pre-wrap}
.reasons{margin-top:7px;color:var(--mut);font-size:11px}
.actions{display:flex;gap:6px;margin-top:10px}
.actions .btn{flex:1}
.empty{text-align:center;padding:36px 12px;color:var(--mut)}
.pnl.up{color:var(--green)}.pnl.dn{color:var(--red)}
#toast{position:fixed;bottom:18px;left:12px;right:12px;z-index:120;display:none;padding:12px 14px;border-radius:12px;background:linear-gradient(135deg,#1d4ed8,#0ea5e9);color:#fff;font-weight:700;text-align:center;box-shadow:0 10px 30px #0008}
#alertBar{position:fixed;top:0;left:0;right:0;z-index:200;display:none;padding:12px;background:linear-gradient(90deg,#be123c,#f43f5e);color:#fff;font-weight:800;text-align:center;cursor:pointer}
.modal-bg{position:fixed;inset:0;background:#000b;z-index:150;display:none;align-items:flex-end;justify-content:center}
.modal{width:100%;max-width:440px;background:#0f1628;border:1px solid var(--line);border-radius:18px 18px 0 0;padding:16px}
.modal h3{font-size:14px;margin-bottom:8px}
.modal label{display:block;color:var(--mut);font-size:10px;margin:8px 0 3px;text-transform:uppercase}
.modal input,.modal select{width:100%;background:#0a1020;border:1px solid var(--line);color:var(--text);border-radius:10px;padding:10px;font-size:13px}
.modal .btns{display:flex;gap:8px;margin-top:14px}
.modal .btns .btn{flex:1;padding:12px}
.hint{color:var(--mut);font-size:11px;margin-top:6px}
.section{padding:12px 12px 4px;color:var(--mut);font-size:10px;text-transform:uppercase;letter-spacing:.06em;font-weight:700}
</style>
</head>
<body>
<div id="alertBar" onclick="showTab('trades')"></div>
<div class="top">
  <div class="brand">
    <h1>Daily Signal · Grok AI</h1>
    <span class="pill">PREMIUM DEMO</span>
  </div>
  <div class="stats">
    <div class="stat"><div class="l">Balance</div><div class="v" id="sBal">—</div></div>
    <div class="stat"><div class="l">Equity</div><div class="v" id="sEq">—</div></div>
    <div class="stat"><div class="l">Realized</div><div class="v" id="sReal">—</div></div>
    <div class="stat"><div class="l">Open</div><div class="v" id="sOpen">0</div></div>
  </div>
</div>

<div class="tabs">
  <button class="tab on" data-t="signals" onclick="showTab('signals')">Signals</button>
  <button class="tab" data-t="trades" onclick="showTab('trades')">Demo Trades</button>
  <button class="tab" data-t="account" onclick="showTab('account')">Account</button>
</div>

<div id="tab-signals">
  <div class="bar">
    <button class="btn" onclick="runScan()">Scan Market</button>
    <button class="btn sec" onclick="loadSignals()">Refresh</button>
    <button class="btn sec" id="soundBtn" onclick="toggleSound()">Sound ON</button>
  </div>
  <div class="status" id="status">Loading…</div>
  <div class="section">Pro plans · Multi-TF · Demo leverage</div>
  <div id="signalList"></div>
</div>

<div id="tab-trades" style="display:none">
  <div class="bar"><button class="btn sec" onclick="loadTrades()">Refresh</button></div>
  <div class="status">TP/SL hit → continuous alert until you press Close.</div>
  <div id="tradeList"></div>
</div>

<div id="tab-account" style="display:none">
  <div class="section">Paper trading account</div>
  <div class="card">
    <div class="grid" style="grid-template-columns:1fr 1fr">
      <div class="cell"><div class="l">Start</div><div class="v" id="aStart">—</div></div>
      <div class="cell"><div class="l">Balance</div><div class="v" id="aBal">—</div></div>
      <div class="cell"><div class="l">Equity</div><div class="v" id="aEq">—</div></div>
      <div class="cell"><div class="l">Realized PnL</div><div class="v" id="aReal">—</div></div>
    </div>
    <div class="hint" style="margin-top:10px">Default risk 1% per trade. Leverage 1–25x. This is DEMO only — not real Binance orders.</div>
    <div class="actions">
      <button class="btn sec" onclick="resetAcc(10000)">Reset $10,000</button>
      <button class="btn sec" onclick="resetAcc(100000)">Reset $100,000</button>
    </div>
  </div>
</div>

<div id="toast"></div>

<div class="modal-bg" id="modalBg">
  <div class="modal">
    <h3 id="mTitle">Open Demo Trade</h3>
    <input type="hidden" id="m_symbol">
    <label>Side</label>
    <select id="m_side"><option>LONG</option><option>SHORT</option></select>
    <label>Entry</label><input type="number" id="m_entry" step="any">
    <label>Stop Loss</label><input type="number" id="m_sl" step="any">
    <label>TP1</label><input type="number" id="m_tp1" step="any">
    <label>TP2</label><input type="number" id="m_tp2" step="any">
    <label>Leverage (1–25)</label><input type="number" id="m_lev" value="10" min="1" max="25">
    <label>Risk % of balance</label><input type="number" id="m_risk" value="1" min="0.25" max="5" step="0.25">
    <p class="hint" id="mHint">Position size auto-calculated from risk % and SL distance.</p>
    <div class="btns">
      <button class="btn sec" onclick="closeModal()">Cancel</button>
      <button class="btn ok" onclick="submitTrade()">Open Demo</button>
    </div>
  </div>
</div>

<script>
let soundOn=true, lastSig='', lastAlert='', notifOK=false;
if('Notification'in window){if(Notification.permission==='granted')notifOK=true;else if(Notification.permission==='default')Notification.requestPermission().then(p=>notifOK=p==='granted');}

function beep(n=2){if(!soundOn)return;try{const c=new(window.AudioContext||window.webkitAudioContext);for(let i=0;i<n;i++)setTimeout(()=>{const o=c.createOscillator(),g=c.createGain();o.connect(g);g.connect(c.destination);o.type='square';o.frequency.value=i%2?700:980;g.gain.value=.11;o.start();g.gain.exponentialRampToValueAtTime(.001,c.currentTime+.2);o.stop(c.currentTime+.2);},i*250);}catch(e){}}
function toast(m,ms=3000){const t=document.getElementById('toast');t.textContent=m;t.style.display='block';setTimeout(()=>t.style.display='none',ms);}
function notify(title,body){beep(4);if(notifOK){try{new Notification(title,{body,requireInteraction:true});}catch(e){}}toast(title+' — '+body,6500);}
function toggleSound(){soundOn=!soundOn;document.getElementById('soundBtn').textContent=soundOn?'Sound ON':'Sound OFF';}
function showTab(n){['signals','trades','account'].forEach(x=>{document.getElementById('tab-'+x).style.display=x===n?'block':'none';});document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('on',t.dataset.t===n));if(n==='trades')loadTrades();if(n==='account')loadAccount();}
function fmt(n){return Number(n).toLocaleString(undefined,{maximumFractionDigits:2});}
function fmtVol(v){if(!v)return'—';if(v>=1e9)return(v/1e9).toFixed(1)+'B';if(v>=1e6)return(v/1e6).toFixed(0)+'M';return(v/1e3).toFixed(0)+'K';}
function setAcc(a){if(!a)return;const up=a.realized_pnl>=0;document.getElementById('sBal').textContent='$'+fmt(a.balance);document.getElementById('sEq').textContent='$'+fmt(a.equity);const r=document.getElementById('sReal');r.textContent=(a.realized_pnl>=0?'+':'')+'$'+fmt(a.realized_pnl);r.className='v '+(up?'up':'dn');document.getElementById('aStart').textContent='$'+fmt(a.start_balance);document.getElementById('aBal').textContent='$'+fmt(a.balance);document.getElementById('aEq').textContent='$'+fmt(a.equity);document.getElementById('aReal').textContent=(a.realized_pnl>=0?'+':'')+'$'+fmt(a.realized_pnl);}

async function loadSignals(){
  try{
    const d=await(await fetch('/api/signals')).json();
    setAcc(d.account);
    const st=document.getElementById('status');
    if(d.scanning)st.textContent='Scanning liquid pairs…';
    else if(d.error)st.textContent='⚠️ '+d.error;
    else st.textContent=`${d.count||0} signals · scanned ${d.scanned_count||0} · ${d.last_updated||'—'}`;
    const list=document.getElementById('signalList');
    if(!d.signals||!d.signals.length){list.innerHTML='<div class="empty">No signals. Tap Scan Market.</div>';return;}
    const h=d.signals.map(s=>s.symbol+s.side+s.score).join('|');
    if(lastSig&&h!==lastSig){beep(2);toast('Signals updated');}
    lastSig=h;
    list.innerHTML=d.signals.map(s=>{
      const L=s.side==='LONG';const q=s.quality||'B';const qc=q==='A+'?'qAP':('q'+q);
      const btn=s.already_added?'<button class="btn added">Added</button>':`<button class="btn ok" onclick='openModal(${JSON.stringify(s)})'>Add Demo Trade</button>`;
      return `<div class="card ${L?'long':'short'}">
        <div class="row">
          <div><span class="side ${L?'L':'S'}">${s.side}</span><span class="sym">${s.symbol}</span>
            <span class="badge ${qc}">${q}</span><span class="badge liq">${s.liquidity||'MED'}</span></div>
          <div class="score">${s.score}</div>
        </div>
        <div class="reasons" style="margin-top:6px">${s.setup||'CONFLUENCE'} · 1D ${s.bias_1d||'—'} · 4H ${s.bias_4h||'—'} · ADX ${s.adx??'—'}</div>
        <div class="grid">
          <div class="cell"><div class="l">Entry</div><div class="v">${s.entry}</div></div>
          <div class="cell"><div class="l">SL</div><div class="v">${s.stop_loss} <span style="color:var(--mut)">(${s.risk_pct}%)</span></div></div>
          <div class="cell"><div class="l">TP1</div><div class="v">${s.tp1} <span style="color:var(--mut)">(${s.tp1_pct}%)</span></div></div>
        </div>
        <div class="grid" style="grid-template-columns:repeat(4,1fr)">
          <div class="cell"><div class="l">TP2</div><div class="v">${s.tp2}</div></div>
          <div class="cell"><div class="l">R:R</div><div class="v">1:${s.rr}</div></div>
          <div class="cell"><div class="l">RSI</div><div class="v">${s.rsi??'—'}</div></div>
          <div class="cell"><div class="l">Vol</div><div class="v">${fmtVol(s.volume_24h)}</div></div>
        </div>
        <div class="plan">${s.plan||''}</div>
        <div class="reasons">${(s.reasons||[]).join(' · ')}</div>
        <div class="actions">${btn}</div>
      </div>`;
    }).join('');
  }catch(e){document.getElementById('status').textContent='Load failed';}
}
function runScan(){toast('Scan started');location.href='/scan?top=25';}
function openModal(s){
  document.getElementById('mTitle').textContent='Demo · '+s.symbol;
  document.getElementById('m_symbol').value=s.symbol;
  document.getElementById('m_side').value=s.side;
  document.getElementById('m_entry').value=s.entry;
  document.getElementById('m_sl').value=s.stop_loss;
  document.getElementById('m_tp1').value=s.tp1;
  document.getElementById('m_tp2').value=s.tp2||'';
  document.getElementById('m_lev').value=10;
  document.getElementById('m_risk').value=1;
  document.getElementById('modalBg').style.display='flex';
}
function closeModal(){document.getElementById('modalBg').style.display='none';}
async function submitTrade(){
  const body={symbol:document.getElementById('m_symbol').value,side:document.getElementById('m_side').value,entry:+document.getElementById('m_entry').value,stop_loss:+document.getElementById('m_sl').value,tp1:+document.getElementById('m_tp1').value,tp2:+document.getElementById('m_tp2').value||null,leverage:+document.getElementById('m_lev').value||10,risk_pct:+document.getElementById('m_risk').value||1};
  const d=await(await fetch('/api/trades',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
  if(d.ok){closeModal();toast(d.duplicate?'Already open':'Demo trade opened');setAcc(d.account);loadSignals();showTab('trades');loadTrades();}
  else toast(d.error||'Failed');
}
async function loadTrades(){
  const d=await(await fetch('/api/trades')).json();
  setAcc(d.account);
  const open=(d.trades||[]).filter(t=>t.status==='OPEN');
  const closed=(d.trades||[]).filter(t=>t.status==='CLOSED');
  document.getElementById('sOpen').textContent=open.length;
  const list=document.getElementById('tradeList');
  if(!open.length&&!closed.length){list.innerHTML='<div class="empty">No demo trades yet.</div>';return;}
  let h='';
  if(open.length){h+='<div class="section">Open positions</div>';h+=open.map(t=>{
    const L=t.side==='LONG';const up=(t.unrealized_pnl||0)>=0;
    return `<div class="card ${L?'long':'short'}${t.hit?' hit':''}">
      <div class="row"><div><span class="side ${L?'L':'S'}">${t.side}</span><span class="sym">${t.symbol}</span>
        <span class="badge liq">${t.leverage}x</span>${t.hit?`<span class="badge hit">${t.hit} HIT</span>`:''}</div>
        <div class="pnl ${up?'up':'dn'}">${up?'+':''}$${fmt(t.unrealized_pnl||0)} <span style="opacity:.7">(${t.pnl_pct||0}%)</span></div></div>
      <div class="grid">
        <div class="cell"><div class="l">Entry</div><div class="v">${t.entry}</div></div>
        <div class="cell"><div class="l">Mark</div><div class="v">${t.current_price??'—'}</div></div>
        <div class="cell"><div class="l">Margin</div><div class="v">$${fmt(t.margin)}</div></div>
      </div>
      <div class="grid">
        <div class="cell"><div class="l">SL</div><div class="v">${t.stop_loss}</div></div>
        <div class="cell"><div class="l">TP1</div><div class="v">${t.tp1}</div></div>
        <div class="cell"><div class="l">Qty</div><div class="v">${t.qty}</div></div>
      </div>
      <div class="actions"><button class="btn danger" onclick="closeTrade('${t.id}')">Close Position</button></div>
    </div>`;}).join('');}
  if(closed.length){h+='<div class="section">Closed</div>';h+=closed.slice(-10).reverse().map(t=>`
    <div class="card" style="opacity:.7"><div class="row">
      <div><span class="side ${t.side==='LONG'?'L':'S'}">${t.side}</span><span class="sym">${t.symbol}</span>
        <span class="badge">${t.hit||'CLOSED'}</span></div>
      <div class="pnl ${(t.realized_pnl||0)>=0?'up':'dn'}">${(t.realized_pnl||0)>=0?'+':''}$${fmt(t.realized_pnl||0)}</div>
    </div></div>`).join('');}
  list.innerHTML=h;
}
async function closeTrade(id){
  if(!confirm('Close this demo position?'))return;
  const d=await(await fetch('/api/trades/'+id+'/close',{method:'POST'})).json();
  if(d.ok){toast('Closed');setAcc(d.account);document.getElementById('alertBar').style.display='none';loadTrades();loadSignals();}
}
async function loadAccount(){const a=await(await fetch('/api/account')).json();setAcc(a);document.getElementById('sOpen').textContent=a.open_trades||0;}
async function resetAcc(bal){
  if(!confirm('Reset demo account to $'+bal+'? Open trades will be cleared.'))return;
  // close opens first client-side via API
  const d0=await(await fetch('/api/trades')).json();
  for(const t of(d0.trades||[]).filter(x=>x.status==='OPEN')){await fetch('/api/trades/'+t.id+'/close',{method:'POST'});}
  const d=await(await fetch('/api/account/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({balance:bal})})).json();
  if(d.ok){setAcc(d.account);toast('Account reset');loadTrades();}
}
async function checkAlerts(){
  try{
    const d=await(await fetch('/api/alerts')).json();
    const bar=document.getElementById('alertBar');
    if(d.count>0){
      const msg=d.alerts.map(t=>`${t.hit} ${t.symbol} ${t.side} $${fmt(t.unrealized_pnl||0)}`).join(' · ');
      bar.textContent='ALERT · '+msg+' · Tap to close in Demo Trades';
      bar.style.display='block';
      notify('Trade alert',msg);
    }else bar.style.display='none';
  }catch(e){}
}
loadSignals();loadAccount();
setInterval(loadSignals,55000);
setInterval(checkAlerts,12000);
setInterval(()=>{if(document.getElementById('tab-trades').style.display!=='none')loadTrades();},12000);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("api:app", host="0.0.0.0", port=port, reload=False)
