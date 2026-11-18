#!/usr/bin/env python3
"""
Daily Signal by Grok AI - FastAPI Web Server
Mobile-first UI + Sound alerts + Trade tracker
"""

from fastapi import FastAPI, Query, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Optional, Dict, Any
from datetime import datetime, timezone
from pydantic import BaseModel
import uvicorn
import os
import traceback
import uuid
import threading
import time as time_mod

from scanner.binance_client import BinanceClient
from scanner.signal_engine import SignalEngine
import config

app = FastAPI(title="Daily Signal by Grok AI", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_cache: Dict[str, Any] = {
    "signals": [],
    "last_updated": None,
    "scanning": False,
    "error": None,
    "scanned_count": 0,
    "new_signal": False,
}

# In-memory open trades (per process; good enough for free tier single instance)
_trades: Dict[str, Dict] = {}
_price_cache: Dict[str, float] = {}
_monitor_running = False


class TradeCreate(BaseModel):
    symbol: str
    side: str
    entry: float
    stop_loss: float
    tp1: float
    tp2: Optional[float] = None
    note: Optional[str] = ""


def run_scan(top_n: int = 20):
    global _cache
    _cache["scanning"] = True
    _cache["error"] = None
    _cache["new_signal"] = False

    try:
        client = BinanceClient(futures=True)
        engine = SignalEngine()
        pairs = client.get_top_usdt_pairs(top_n=top_n, min_volume=config.MIN_VOLUME_USDT)

        signals = []
        timeframes = ["15m", "1h", "4h", "1d"]

        for symbol, vol in pairs:
            data = {}
            for tf in timeframes:
                limit = config.CANDLE_LIMIT.get(tf, 100)
                df = client.fetch_ohlcv(symbol, timeframe=tf, limit=limit)
                if df is not None and len(df) > 30:
                    data[tf] = df

            if len(data) < 3:
                continue

            try:
                result = engine.analyze_pair(symbol, data, volume_24h=vol)
                if result:
                    signals.append(result)
            except Exception:
                continue

        signals.sort(key=lambda x: x["score"], reverse=True)
        prev_count = len(_cache["signals"])
        _cache["signals"] = signals
        _cache["last_updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        _cache["scanned_count"] = len(pairs)
        if signals and (prev_count == 0 or len(signals) > 0):
            _cache["new_signal"] = True

    except Exception as e:
        _cache["error"] = str(e)
        traceback.print_exc()
    finally:
        _cache["scanning"] = False


def price_monitor_loop():
    """Background loop: update prices for open trades and flag TP/SL hits."""
    global _monitor_running, _price_cache, _trades
    client = BinanceClient(futures=True)
    while _monitor_running:
        try:
            open_trades = [t for t in _trades.values() if t.get("status") == "OPEN"]
            for t in open_trades:
                price = client.get_current_price(t["symbol"])
                if price is None:
                    continue
                _price_cache[t["symbol"]] = price
                t["current_price"] = price

                side = t["side"].upper()
                hit = None
                if side == "LONG":
                    if price <= t["stop_loss"]:
                        hit = "SL"
                    elif price >= t["tp1"]:
                        hit = "TP1"
                    elif t.get("tp2") and price >= t["tp2"]:
                        hit = "TP2"
                else:  # SHORT
                    if price >= t["stop_loss"]:
                        hit = "SL"
                    elif price <= t["tp1"]:
                        hit = "TP1"
                    elif t.get("tp2") and price <= t["tp2"]:
                        hit = "TP2"

                if hit and not t.get("alert_sent"):
                    t["hit"] = hit
                    t["alert_sent"] = True
                    t["alert_at"] = datetime.now(timezone.utc).isoformat()
                    # keep status OPEN until user confirms close

                # PnL %
                if side == "LONG":
                    t["pnl_pct"] = round(((price - t["entry"]) / t["entry"]) * 100, 2)
                else:
                    t["pnl_pct"] = round(((t["entry"] - price) / t["entry"]) * 100, 2)

        except Exception as e:
            print(f"Monitor error: {e}")

        time_mod.sleep(12)  # every 12s


@app.on_event("startup")
def startup():
    global _monitor_running
    _monitor_running = True
    threading.Thread(target=lambda: run_scan(top_n=15), daemon=True).start()
    threading.Thread(target=price_monitor_loop, daemon=True).start()


@app.on_event("shutdown")
def shutdown():
    global _monitor_running
    _monitor_running = False


# ===================== API =====================

@app.get("/api/signals")
async def api_signals():
    return {
        "last_updated": _cache["last_updated"],
        "scanning": _cache["scanning"],
        "error": _cache["error"],
        "scanned_count": _cache["scanned_count"],
        "new_signal": _cache["new_signal"],
        "count": len(_cache["signals"]),
        "signals": _cache["signals"],
        "min_volume": config.MIN_VOLUME_USDT,
        "timeframes": "15m entry · 1H momentum · 4H trend · 1D regime",
    }


@app.get("/api/trades")
async def api_trades():
    return {"trades": list(_trades.values())}


@app.post("/api/trades")
async def create_trade(body: TradeCreate):
    tid = str(uuid.uuid4())[:8]
    trade = {
        "id": tid,
        "symbol": body.symbol.upper().replace("/", ""),
        "side": body.side.upper(),
        "entry": body.entry,
        "stop_loss": body.stop_loss,
        "tp1": body.tp1,
        "tp2": body.tp2,
        "note": body.note or "",
        "status": "OPEN",
        "hit": None,
        "alert_sent": False,
        "current_price": body.entry,
        "pnl_pct": 0.0,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _trades[tid] = trade
    return {"ok": True, "trade": trade}


@app.post("/api/trades/{tid}/close")
async def close_trade(tid: str):
    if tid not in _trades:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    _trades[tid]["status"] = "CLOSED"
    _trades[tid]["closed_at"] = datetime.now(timezone.utc).isoformat()
    _trades[tid]["hit"] = _trades[tid].get("hit") or "MANUAL"
    return {"ok": True, "trade": _trades[tid]}


@app.post("/api/trades/{tid}/ack")
async def ack_alert(tid: str):
    """Acknowledge alert but keep trade open (user saw the alert)."""
    if tid not in _trades:
        return JSONResponse({"ok": False}, status_code=404)
    _trades[tid]["alert_sent"] = False  # allow re-alert if price moves again? better keep True
    _trades[tid]["alert_acked"] = True
    return {"ok": True}


@app.get("/scan")
async def trigger_scan(background_tasks: BackgroundTasks, top: int = Query(20, ge=10, le=40)):
    if not _cache["scanning"]:
        background_tasks.add_task(run_scan, top_n=top)
    return RedirectResponse(url="/", status_code=303)


@app.get("/health")
async def health():
    return {"status": "ok", "scanning": _cache["scanning"], "open_trades": sum(1 for t in _trades.values() if t["status"]=="OPEN")}


# ===================== UI =====================

@app.get("/", response_class=HTMLResponse)
async def home():
    return HTMLResponse(PAGE_HTML)


PAGE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<meta name="theme-color" content="#0f172a">
<title>Daily Signal by Grok AI</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,-apple-system,sans-serif;background:#0b1220;color:#e2e8f0;font-size:13px;line-height:1.35}
.header{position:sticky;top:0;z-index:50;background:#0b1220ee;backdrop-filter:blur(8px);padding:10px 12px 8px;border-bottom:1px solid #1e293b}
.header h1{font-size:1.05rem;color:#38bdf8;font-weight:700}
.header .meta{color:#64748b;font-size:11px;margin-top:2px}
.tabs{display:flex;gap:6px;padding:8px 12px;background:#0f172a;border-bottom:1px solid #1e293b;position:sticky;top:52px;z-index:40}
.tab{flex:1;text-align:center;padding:8px 4px;border-radius:8px;background:#1e293b;color:#94a3b8;font-weight:600;font-size:12px;border:none;cursor:pointer}
.tab.active{background:#0ea5e9;color:#fff}
.toolbar{display:flex;gap:6px;padding:8px 12px;flex-wrap:wrap}
.btn{background:#0ea5e9;color:#fff;border:none;border-radius:8px;padding:8px 12px;font-weight:600;font-size:12px;cursor:pointer}
.btn:active{opacity:.85}
.btn.sec{background:#334155}
.btn.danger{background:#dc2626}
.btn.ok{background:#16a34a}
.status{margin:0 12px 8px;padding:8px 10px;background:#1e293b;border-radius:8px;color:#94a3b8;font-size:11px}
.card{margin:0 10px 8px;background:#1e293b;border-radius:10px;padding:10px 12px;border:1px solid #334155}
.card.long{border-left:3px solid #22c55e}
.card.short{border-left:3px solid #ef4444}
.card.hit{border-left:3px solid #f59e0b;animation:pulse 1.2s infinite}
@keyframes pulse{0%,100%{box-shadow:0 0 0 0 #f59e0b44}50%{box-shadow:0 0 0 6px #f59e0b11}}
.row{display:flex;justify-content:space-between;align-items:center;gap:6px}
.side{font-weight:800;font-size:12px}
.side.L{color:#22c55e}.side.S{color:#ef4444}
.sym{font-weight:700;font-size:13px}
.score{background:#0f172a;padding:2px 7px;border-radius:6px;font-weight:700;font-size:12px}
.grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;margin-top:8px}
.cell{background:#0f172a;border-radius:7px;padding:6px 8px}
.cell .l{color:#64748b;font-size:10px;text-transform:uppercase}
.cell .v{font-weight:600;font-size:12px;margin-top:1px}
.reasons{margin-top:7px;color:#94a3b8;font-size:11px;line-height:1.4}
.liq{font-size:10px;padding:1px 6px;border-radius:4px;font-weight:600}
.liq.VH,.liq.HIGH{background:#14532d;color:#86efac}
.liq.MED{background:#1e3a5f;color:#93c5fd}
.liq.LOW{background:#3f3f46;color:#a1a1aa}
.actions{display:flex;gap:6px;margin-top:8px}
.actions .btn{flex:1;padding:7px}
.empty{text-align:center;padding:28px 12px;color:#64748b}
.badge{display:inline-block;background:#f59e0b;color:#000;font-size:10px;font-weight:800;padding:2px 6px;border-radius:4px;margin-left:4px}
.pnl.pos{color:#22c55e}.pnl.neg{color:#ef4444}
#toast{position:fixed;bottom:16px;left:12px;right:12px;background:#0ea5e9;color:#fff;padding:12px 14px;border-radius:10px;font-weight:600;text-align:center;display:none;z-index:100;box-shadow:0 8px 24px #0008}
.modal-bg{position:fixed;inset:0;background:#000a;z-index:90;display:none;align-items:flex-end;justify-content:center}
.modal{background:#1e293b;width:100%;max-width:420px;border-radius:16px 16px 0 0;padding:16px;border:1px solid #334155}
.modal h3{margin-bottom:10px;font-size:14px}
.modal label{display:block;color:#94a3b8;font-size:11px;margin:8px 0 3px}
.modal input,.modal select{width:100%;background:#0f172a;border:1px solid #334155;color:#e2e8f0;border-radius:8px;padding:9px 10px;font-size:13px}
.modal .btns{display:flex;gap:8px;margin-top:14px}
.modal .btns .btn{flex:1;padding:11px}
.section-title{padding:10px 12px 4px;color:#64748b;font-size:11px;text-transform:uppercase;letter-spacing:.04em;font-weight:600}
</style>
</head>
<body>
<div class="header">
  <h1>Daily Signal by Grok AI</h1>
  <div class="meta" id="metaLine">Loading…</div>
</div>

<div class="tabs">
  <button class="tab active" data-tab="signals" onclick="showTab('signals')">Signals</button>
  <button class="tab" data-tab="trades" onclick="showTab('trades')">My Trades <span id="tradeBadge"></span></button>
</div>

<div id="tab-signals">
  <div class="toolbar">
    <button class="btn" onclick="runScan()">🔄 Scan</button>
    <button class="btn sec" onclick="loadSignals()">↻ Refresh</button>
    <button class="btn sec" id="soundBtn" onclick="toggleSound()">🔊 Sound ON</button>
  </div>
  <div class="status" id="status">—</div>
  <div class="section-title">High liquidity · 15M+ USDT vol · 15m entry</div>
  <div id="signalList"></div>
</div>

<div id="tab-trades" style="display:none">
  <div class="toolbar">
    <button class="btn" onclick="openManualTrade()">＋ Add Trade</button>
    <button class="btn sec" onclick="loadTrades()">↻ Refresh</button>
  </div>
  <div class="status">Open trades are monitored every ~12s. You get sound + browser alert on TP/SL. Close only when you confirm.</div>
  <div id="tradeList"></div>
</div>

<div id="toast"></div>

<div class="modal-bg" id="modalBg">
  <div class="modal">
    <h3 id="modalTitle">Set Trade Entry</h3>
    <input type="hidden" id="m_symbol">
    <label>Side</label>
    <select id="m_side"><option value="LONG">LONG</option><option value="SHORT">SHORT</option></select>
    <label>Entry Price</label>
    <input type="number" id="m_entry" step="any">
    <label>Stop Loss</label>
    <input type="number" id="m_sl" step="any">
    <label>Take Profit 1</label>
    <input type="number" id="m_tp1" step="any">
    <label>Take Profit 2 (optional)</label>
    <input type="number" id="m_tp2" step="any" placeholder="optional">
    <label>Note</label>
    <input type="text" id="m_note" placeholder="optional">
    <div class="btns">
      <button class="btn sec" onclick="closeModal()">Cancel</button>
      <button class="btn ok" onclick="submitTrade()">Save Trade</button>
    </div>
  </div>
</div>

<script>
let soundOn = true;
let lastSignalHash = '';
let notifPermission = false;

// Request notification permission
if ('Notification' in window && Notification.permission === 'default') {
  Notification.requestPermission().then(p => notifPermission = p === 'granted');
} else if ('Notification' in window) {
  notifPermission = Notification.permission === 'granted';
}

function beep(times=2) {
  if (!soundOn) return;
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    for (let i=0;i<times;i++) {
      setTimeout(() => {
        const o = ctx.createOscillator();
        const g = ctx.createGain();
        o.connect(g); g.connect(ctx.destination);
        o.frequency.value = i%2===0 ? 880 : 660;
        g.gain.value = 0.15;
        o.start();
        g.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.25);
        o.stop(ctx.currentTime + 0.25);
      }, i*280);
    }
  } catch(e){}
}

function toast(msg, ms=3500) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.style.display = 'block';
  setTimeout(() => t.style.display = 'none', ms);
}

function notify(title, body) {
  beep(3);
  if (notifPermission && document.hidden) {
    try { new Notification(title, { body, icon: '/favicon.ico' }); } catch(e){}
  }
  toast(title + ' — ' + body, 6000);
}

function toggleSound() {
  soundOn = !soundOn;
  document.getElementById('soundBtn').textContent = soundOn ? '🔊 Sound ON' : '🔇 Sound OFF';
}

function showTab(name) {
  document.getElementById('tab-signals').style.display = name==='signals'?'block':'none';
  document.getElementById('tab-trades').style.display = name==='trades'?'block':'none';
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab===name));
  if (name==='trades') loadTrades();
}

function fmtVol(v) {
  if (!v) return '—';
  if (v >= 1e9) return (v/1e9).toFixed(1)+'B';
  if (v >= 1e6) return (v/1e6).toFixed(0)+'M';
  return (v/1e3).toFixed(0)+'K';
}

async function loadSignals() {
  try {
    const r = await fetch('/api/signals');
    const d = await r.json();
    const meta = document.getElementById('metaLine');
    meta.textContent = (d.last_updated ? 'Updated '+d.last_updated : '—') + ' · Min liq 15M USDT · TF: 15m/1H/4H/1D';

    const st = document.getElementById('status');
    if (d.scanning) st.textContent = '🔄 Scanning high-liquidity pairs… wait 20-40s then refresh';
    else if (d.error) st.textContent = '⚠️ '+d.error;
    else st.textContent = '✅ '+(d.count||0)+' signals · scanned '+(d.scanned_count||0)+' pairs · high liquidity only';

    const list = document.getElementById('signalList');
    if (!d.signals || !d.signals.length) {
      list.innerHTML = '<div class="empty">No signals right now.<br>Tap Scan to refresh.</div>';
      return;
    }

    const hash = d.signals.map(s => s.symbol+s.side+s.score).join('|');
    if (lastSignalHash && hash !== lastSignalHash) {
      beep(2);
      toast('New / updated signals');
    }
    lastSignalHash = hash;

    list.innerHTML = d.signals.map(s => {
      const cls = s.side === 'LONG' ? 'long' : 'short';
      const sc = s.side === 'LONG' ? 'L' : 'S';
      return `<div class="card ${cls}">
        <div class="row">
          <div><span class="side ${sc}">${s.side}</span> <span class="sym">${s.symbol}</span>
            <span class="liq ${s.liquidity||'MED'}">${s.liquidity||'MED'}</span></div>
          <span class="score">${s.score}</span>
        </div>
        <div class="grid">
          <div class="cell"><div class="l">Entry</div><div class="v">${s.entry}</div></div>
          <div class="cell"><div class="l">SL (${s.risk_pct>0?'+':''}${s.risk_pct}%)</div><div class="v">${s.stop_loss}</div></div>
          <div class="cell"><div class="l">TP1 (${s.tp1_pct>0?'+':''}${s.tp1_pct}%)</div><div class="v">${s.tp1}</div></div>
        </div>
        <div class="grid" style="grid-template-columns:1fr 1fr 1fr 1fr">
          <div class="cell"><div class="l">TP2</div><div class="v">${s.tp2}</div></div>
          <div class="cell"><div class="l">RR</div><div class="v">1:${s.rr}</div></div>
          <div class="cell"><div class="l">RSI</div><div class="v">${s.rsi??'—'}</div></div>
          <div class="cell"><div class="l">Vol</div><div class="v">${fmtVol(s.volume_24h)}</div></div>
        </div>
        <div class="reasons">${(s.reasons||[]).join(' · ')}</div>
        <div class="reasons" style="margin-top:3px">TF: ${s.timeframe||'15m entry'}</div>
        <div class="actions">
          <button class="btn ok" onclick='takeTrade(${JSON.stringify(s)})'>📌 Take Trade</button>
        </div>
      </div>`;
    }).join('');
  } catch(e) {
    document.getElementById('status').textContent = 'Failed to load signals';
  }
}

function runScan() {
  toast('Scan started…');
  window.location.href = '/scan?top=20';
}

function takeTrade(s) {
  document.getElementById('modalTitle').textContent = 'Take Trade — '+s.symbol;
  document.getElementById('m_symbol').value = s.symbol;
  document.getElementById('m_side').value = s.side;
  document.getElementById('m_entry').value = s.entry;
  document.getElementById('m_sl').value = s.stop_loss;
  document.getElementById('m_tp1').value = s.tp1;
  document.getElementById('m_tp2').value = s.tp2 || '';
  document.getElementById('m_note').value = '';
  document.getElementById('modalBg').style.display = 'flex';
}

function openManualTrade() {
  document.getElementById('modalTitle').textContent = 'Add Manual Trade';
  document.getElementById('m_symbol').value = '';
  document.getElementById('m_side').value = 'LONG';
  document.getElementById('m_entry').value = '';
  document.getElementById('m_sl').value = '';
  document.getElementById('m_tp1').value = '';
  document.getElementById('m_tp2').value = '';
  document.getElementById('m_note').value = '';
  // make symbol editable via note field hack — use entry field focus
  const sym = prompt('Symbol (e.g. BTCUSDT):');
  if (!sym) return;
  document.getElementById('m_symbol').value = sym.toUpperCase().replace('/','');
  document.getElementById('modalBg').style.display = 'flex';
}

function closeModal() {
  document.getElementById('modalBg').style.display = 'none';
}

async function submitTrade() {
  const body = {
    symbol: document.getElementById('m_symbol').value,
    side: document.getElementById('m_side').value,
    entry: parseFloat(document.getElementById('m_entry').value),
    stop_loss: parseFloat(document.getElementById('m_sl').value),
    tp1: parseFloat(document.getElementById('m_tp1').value),
    tp2: parseFloat(document.getElementById('m_tp2').value) || null,
    note: document.getElementById('m_note').value,
  };
  if (!body.symbol || !body.entry || !body.stop_loss || !body.tp1) {
    toast('Fill required fields');
    return;
  }
  const r = await fetch('/api/trades', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
  const d = await r.json();
  if (d.ok) {
    closeModal();
    toast('Trade saved — monitoring for TP/SL');
    showTab('trades');
    loadTrades();
  } else toast('Failed to save');
}

async function loadTrades() {
  const r = await fetch('/api/trades');
  const d = await r.json();
  const open = (d.trades||[]).filter(t => t.status==='OPEN');
  const closed = (d.trades||[]).filter(t => t.status==='CLOSED');
  document.getElementById('tradeBadge').textContent = open.length ? `(${open.length})` : '';

  // Alerts for hits not yet acked/closed
  open.forEach(t => {
    if (t.hit && t.alert_sent && !t.alert_acked) {
      notify(`${t.hit} HIT — ${t.symbol}`, `${t.side} @ ${t.current_price} | PnL ${t.pnl_pct}%`);
    }
  });

  const list = document.getElementById('tradeList');
  if (!open.length && !closed.length) {
    list.innerHTML = '<div class="empty">No trades yet.<br>Take a signal or add manually.</div>';
    return;
  }

  let html = '';
  if (open.length) {
    html += '<div class="section-title">Open</div>';
    html += open.map(t => {
      const hitCls = t.hit ? ' hit' : '';
      const pnlCls = (t.pnl_pct||0) >= 0 ? 'pos' : 'neg';
      return `<div class="card ${t.side==='LONG'?'long':'short'}${hitCls}">
        <div class="row">
          <div><span class="side ${t.side==='LONG'?'L':'S'}">${t.side}</span> <span class="sym">${t.symbol}</span>
            ${t.hit ? `<span class="badge">${t.hit} HIT</span>` : ''}</div>
          <span class="pnl ${pnlCls}">${t.pnl_pct>0?'+':''}${t.pnl_pct||0}%</span>
        </div>
        <div class="grid">
          <div class="cell"><div class="l">Entry</div><div class="v">${t.entry}</div></div>
          <div class="cell"><div class="l">Now</div><div class="v">${t.current_price??'—'}</div></div>
          <div class="cell"><div class="l">SL</div><div class="v">${t.stop_loss}</div></div>
        </div>
        <div class="grid">
          <div class="cell"><div class="l">TP1</div><div class="v">${t.tp1}</div></div>
          <div class="cell"><div class="l">TP2</div><div class="v">${t.tp2||'—'}</div></div>
          <div class="cell"><div class="l">Note</div><div class="v">${t.note||'—'}</div></div>
        </div>
        <div class="actions">
          <button class="btn danger" onclick="closeTrade('${t.id}')">✓ Close Trade (OK)</button>
        </div>
      </div>`;
    }).join('');
  }
  if (closed.length) {
    html += '<div class="section-title">Closed (recent)</div>';
    html += closed.slice(-5).reverse().map(t => `
      <div class="card" style="opacity:.7">
        <div class="row">
          <div><span class="side ${t.side==='LONG'?'L':'S'}">${t.side}</span> <span class="sym">${t.symbol}</span>
            <span class="badge" style="background:#64748b;color:#fff">${t.hit||'CLOSED'}</span></div>
          <span class="pnl ${(t.pnl_pct||0)>=0?'pos':'neg'}">${t.pnl_pct>0?'+':''}${t.pnl_pct||0}%</span>
        </div>
      </div>`).join('');
  }
  list.innerHTML = html;
}

async function closeTrade(id) {
  if (!confirm('Confirm you closed this trade?')) return;
  await fetch('/api/trades/'+id+'/close', {method:'POST'});
  toast('Trade closed');
  loadTrades();
}

// Poll
loadSignals();
setInterval(loadSignals, 45000);
setInterval(() => {
  if (document.getElementById('tab-trades').style.display !== 'none') loadTrades();
  else {
    // still check for trade alerts in background
    fetch('/api/trades').then(r=>r.json()).then(d => {
      (d.trades||[]).filter(t=>t.status==='OPEN'&&t.hit&&t.alert_sent&&!t.alert_acked)
        .forEach(t => notify(t.hit+' HIT — '+t.symbol, t.side+' PnL '+t.pnl_pct+'%'));
    }).catch(()=>{});
  }
}, 15000);
</script>
</body>
</html>
"""
