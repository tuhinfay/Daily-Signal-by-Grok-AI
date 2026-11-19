#!/usr/bin/env python3
"""Daily Signal by Grok AI"""

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

app = FastAPI(title="Daily Signal by Grok AI", version="3.5.0")
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
    risk_pct: Optional[float] = None
    margin_usd: Optional[float] = None
    quantity: Optional[float] = None
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
        try:
            mv = client.get_movers(top_n=10)
            _cache["gainers"] = mv.get("gainers", [])
            _cache["losers"] = mv.get("losers", [])
        except Exception:
            pass
        signals, prices = [], {}
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
                px = client.get_current_price(symbol)
                r["market_price"] = px or r["entry"]
                if px:
                    prices[symbol] = px
                r["max_leverage"] = client.get_max_leverage(symbol)
                styles = []
                if r.get("adx", 0) >= 22 and r.get("score", 0) >= 58:
                    styles.append("day")
                if r.get("score", 0) >= 55:
                    styles.append("scalp")
                if r.get("bias_1d") == r.get("bias_4h") and r.get("bias_1d") != "neutral":
                    styles.append("swing")
                r["styles"] = styles or ["day"]
                # short reason line only — no bulky plan box
                r["summary"] = f"{r.get('setup','')} · R:R 1:{r['rr']} · SL {r['risk_pct']}% · TP1 {r['tp1_pct']}%"
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
                prev_hit = t.get("hit")
                if side == "LONG":
                    pnl = (price - entry) * qty
                    t["pnl_pct"] = round(((price - entry) / entry) * 100 * lev, 2)
                    if price <= t["stop_loss"]:
                        t["hit"] = "SL"
                    elif t.get("tp2") and price >= t["tp2"]:
                        t["hit"] = "TP2"
                    elif price >= t["tp1"]:
                        t["hit"] = "TP1"
                else:
                    pnl = (entry - price) * qty
                    t["pnl_pct"] = round(((entry - price) / entry) * 100 * lev, 2)
                    if price >= t["stop_loss"]:
                        t["hit"] = "SL"
                    elif t.get("tp2") and price <= t["tp2"]:
                        t["hit"] = "TP2"
                    elif price <= t["tp1"]:
                        t["hit"] = "TP1"
                t["unrealized_pnl"] = round(pnl, 2)
                if t.get("hit") and t.get("hit") != prev_hit:
                    t["needs_alert"] = True
                    t["alert_text"] = f"{t['symbol']} {t['hit']} hit. PnL {t['pnl_pct']}%"
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
        if s2["symbol"] in _cache.get("prices", {}):
            s2["market_price"] = _cache["prices"][s2["symbol"]]
        if style != "all" and style not in (s2.get("styles") or []):
            continue
        out.append(s2)
    return {
        "last_updated": _cache["last_updated"], "scanning": _cache["scanning"],
        "error": _cache["error"], "scanned_count": _cache["scanned_count"],
        "count": len(out), "signals": out,
        "gainers": _cache.get("gainers", []), "losers": _cache.get("losers", []),
        "account": _account,
    }


@app.get("/api/analyze/{symbol}")
async def analyze_one(symbol: str):
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
        return JSONResponse({"ok": False, "error": "No data for " + sym}, status_code=404)
    r = engine.analyze_pair(sym, data, volume_24h=0)
    px = client.get_current_price(sym)
    lev = client.get_max_leverage(sym)
    if not r:
        return {"ok": True, "signal": None, "symbol": sym, "market_price": px, "max_leverage": lev,
                "message": "No confluence signal now"}
    r["market_price"] = px or r["entry"]
    r["max_leverage"] = lev
    r["styles"] = ["day", "scalp", "swing"]
    r["summary"] = f"{r.get('setup','')} · R:R 1:{r['rr']} · SL {r['risk_pct']}% · TP1 {r['tp1_pct']}%"
    return {"ok": True, "signal": r}


@app.get("/api/account")
async def api_account():
    _calc_equity()
    return {**_account, "open_trades": sum(1 for t in _trades.values() if t["status"] == "OPEN")}


@app.post("/api/account/reset")
async def reset_account(body: AccountReset):
    for t in list(_trades.values()):
        if t.get("status") == "OPEN":
            t["status"] = "CLOSED"; t["hit"] = "RESET"; t["needs_alert"] = False
    _account.update({"balance": float(body.balance), "equity": float(body.balance),
                     "realized_pnl": 0.0, "start_balance": float(body.balance)})
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

    sl_dist = abs(body.entry - body.stop_loss)
    if sl_dist <= 0:
        return JSONResponse({"ok": False, "error": "invalid SL"}, status_code=400)

    # Flexible sizing: quantity > margin_usd > risk_pct
    if body.quantity and body.quantity > 0:
        qty = float(body.quantity)
        margin = (qty * body.entry) / lev
        risk_usd = qty * sl_dist
    elif body.margin_usd and body.margin_usd > 0:
        margin = min(float(body.margin_usd), _account["balance"] * 0.95)
        notional = margin * lev
        qty = notional / body.entry
        risk_usd = qty * sl_dist
    else:
        risk_pct = max(0.25, min(float(body.risk_pct or 1.0), 5.0))
        risk_usd = _account["balance"] * (risk_pct / 100.0)
        qty = risk_usd / sl_dist
        margin = (qty * body.entry) / lev

    if margin > _account["balance"] * 0.95:
        return JSONResponse({"ok": False, "error": "insufficient balance"}, status_code=400)

    tid = str(uuid.uuid4())[:8]
    trade = {
        "id": tid, "symbol": sym, "side": side,
        "entry": body.entry, "stop_loss": body.stop_loss, "tp1": body.tp1, "tp2": body.tp2,
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
<meta name="theme-color" content="#060a12">
<title>Daily Signal by Grok AI</title>
<style>
:root{--bg:#060a12;--card:#0c1322;--line:#1a2336;--text:#eef3ff;--mut:#7f8eaa;--cyan:#22d3ee;--blue:#3b82f6;--gn:#10b981;--rd:#f43f5e;--gd:#fbbf24}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:Inter,system-ui,sans-serif;background:var(--bg);color:var(--text);font-size:13px}
.top{position:sticky;top:0;z-index:40;background:#060a12ee;backdrop-filter:blur(12px);border-bottom:1px solid var(--line);padding:12px}
.top h1{font-size:16px;font-weight:800;letter-spacing:-.02em;background:linear-gradient(90deg,var(--cyan),var(--blue));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin-top:10px}
.metric{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px}
.metric label{display:block;font-size:9px;color:var(--mut);text-transform:uppercase;font-weight:600}
.metric b{font-size:12px;font-weight:700}
.metric b.up{color:var(--gn)}.metric b.dn{color:var(--rd)}
.tabs{display:flex;gap:6px;padding:8px 12px;position:sticky;top:96px;z-index:30;background:var(--bg)}
.tabs button{flex:1;border:1px solid var(--line);background:var(--card);color:var(--mut);border-radius:9px;padding:9px 4px;font-size:11px;font-weight:700}
.tabs button.on{color:#fff;border:0;background:linear-gradient(135deg,#1d4ed8,#0891b2)}
.bar{display:flex;flex-wrap:wrap;gap:6px;padding:0 12px 8px}
.bar input{flex:1;min-width:110px;background:var(--card);border:1px solid var(--line);color:var(--text);border-radius:9px;padding:9px 10px}
.btn{border:0;border-radius:9px;padding:9px 12px;font-size:11px;font-weight:700;color:#fff;background:linear-gradient(135deg,#2563eb,#0891b2);cursor:pointer}
.btn.dim{background:var(--card);border:1px solid var(--line);color:var(--mut)}
.btn.good{background:linear-gradient(135deg,#059669,#10b981)}
.btn.bad{background:linear-gradient(135deg,#e11d48,#f43f5e)}
.btn:disabled{opacity:.45}
.filters{display:flex;gap:6px;padding:0 12px 8px;flex-wrap:wrap}
.filters button{border:1px solid var(--line);background:var(--card);color:var(--mut);border-radius:999px;padding:5px 11px;font-size:11px;font-weight:600}
.filters button.on{color:var(--cyan);border-color:#164e63;background:#0b1c2e}
.note{margin:0 12px 8px;padding:8px 10px;background:var(--card);border:1px solid var(--line);border-radius:9px;color:var(--mut);font-size:11px}
/* clean signal card — single layer */
.card{margin:0 10px 8px;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px;border-left:3px solid var(--line)}
.card.L{border-left-color:var(--gn)}.card.S{border-left-color:var(--rd)}.card.hit{border-left-color:var(--gd)}
.head{display:flex;justify-content:space-between;align-items:center;gap:8px}
.side{font-size:11px;font-weight:800}.side.L{color:var(--gn)}.side.S{color:var(--rd)}
.sym{font-size:14px;font-weight:800;margin-left:4px}
.score{font-size:16px;font-weight:900;color:var(--cyan)}
.tag{display:inline-block;font-size:9px;font-weight:700;padding:2px 6px;border-radius:5px;margin-left:4px;background:#152033;color:var(--mut)}
.tag.q{background:#0b3b2e;color:#6ee7b7}
.sub{margin-top:5px;color:var(--mut);font-size:11px;line-height:1.4}
.vals{display:flex;flex-wrap:wrap;gap:12px 16px;margin-top:10px}
.vals div em{display:block;font-style:normal;font-size:9px;color:var(--mut);text-transform:uppercase}
.vals div strong{font-size:12px;font-weight:700}
.actions{display:flex;gap:6px;margin-top:10px;flex-wrap:wrap}
.actions .btn{flex:1;min-width:88px}
.chartbox{display:none;margin-top:8px;border-radius:10px;overflow:hidden;border:1px solid var(--line)}
.chartbox iframe{width:100%;height:210px;border:0;display:block}
.empty{text-align:center;padding:30px 12px;color:var(--mut)}
.list-row{display:flex;justify-content:space-between;align-items:center;padding:10px 0;border-bottom:1px solid var(--line)}
.list-row:last-child{border-bottom:0}
#toast{position:fixed;bottom:16px;left:12px;right:12px;z-index:90;display:none;padding:12px;border-radius:12px;background:linear-gradient(135deg,#1d4ed8,#0891b2);color:#fff;font-weight:700;text-align:center}
#alertBar{position:fixed;top:0;left:0;right:0;z-index:100;display:none;padding:11px;background:linear-gradient(90deg,#be123c,#f43f5e);color:#fff;font-weight:800;text-align:center;font-size:12px}
.modal-bg{position:fixed;inset:0;background:#000c;z-index:80;display:none;align-items:flex-end;justify-content:center}
.modal{width:100%;max-width:430px;background:var(--card);border:1px solid var(--line);border-radius:16px 16px 0 0;padding:14px 14px 20px;max-height:92vh;overflow:auto}
.modal h3{font-size:14px;margin-bottom:6px}
.modal label{display:block;margin:8px 0 3px;font-size:10px;color:var(--mut);text-transform:uppercase}
.modal input,.modal select{width:100%;background:#080e1a;border:1px solid var(--line);color:var(--text);border-radius:9px;padding:10px;font-size:13px}
.modal input[type=range]{padding:0;height:28px}
.modal .row2{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.modal .btns{display:flex;gap:8px;margin-top:12px}
.modal .btns .btn{flex:1;padding:12px}
.hint{font-size:11px;color:var(--mut);margin-top:4px}
.sec{padding:10px 12px 4px;font-size:10px;color:var(--mut);text-transform:uppercase;letter-spacing:.05em;font-weight:700}
.mv{display:flex;gap:8px;overflow-x:auto;padding:0 12px 10px}
.mv .i{min-width:108px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px}
.mv .i b{display:block;font-size:12px}.mv .up{color:var(--gn)}.mv .dn{color:var(--rd)}
</style>
</head>
<body>
<div id="alertBar" onclick="showTab('trades')"></div>
<header class="top">
  <h1>Daily Signal by Grok AI</h1>
  <div class="metrics">
    <div class="metric"><label>Balance</label><b id="sBal">—</b></div>
    <div class="metric"><label>Equity</label><b id="sEq">—</b></div>
    <div class="metric"><label>PnL</label><b id="sReal">—</b></div>
    <div class="metric"><label>Open</label><b id="sOpen">0</b></div>
  </div>
</header>
<nav class="tabs">
  <button class="on" data-t="signals" onclick="showTab('signals')">Signals</button>
  <button data-t="trades" onclick="showTab('trades')">My Trades</button>
  <button data-t="movers" onclick="showTab('movers')">Movers</button>
  <button data-t="account" onclick="showTab('account')">Account</button>
</nav>

<section id="tab-signals">
  <div class="bar">
    <input id="symIn" placeholder="BTC / ETHUSDT" />
    <button class="btn" onclick="analyzeManual()">Analyze</button>
    <button class="btn dim" onclick="runScan()">Scan</button>
    <button class="btn dim" id="soundBtn" onclick="toggleSound()">Voice ON</button>
  </div>
  <div class="filters">
    <button class="on" data-s="all" onclick="setStyle('all')">All</button>
    <button data-s="scalp" onclick="setStyle('scalp')">Scalp</button>
    <button data-s="day" onclick="setStyle('day')">Day</button>
    <button data-s="swing" onclick="setStyle('swing')">Swing</button>
  </div>
  <div class="note" id="status">Loading…</div>
  <div id="signalList"></div>
</section>

<section id="tab-trades" style="display:none">
  <div class="note">Hit alert speaks coin name. Close to stop alerts.</div>
  <div id="tradeList"></div>
</section>

<section id="tab-movers" style="display:none">
  <div class="sec">Top gainers</div><div class="mv" id="gainers"></div>
  <div class="sec">Top losers</div><div class="mv" id="losers"></div>
</section>

<section id="tab-account" style="display:none">
  <div class="card">
    <div class="vals">
      <div><em>Start</em><strong id="aStart">—</strong></div>
      <div><em>Balance</em><strong id="aBal">—</strong></div>
      <div><em>Equity</em><strong id="aEq">—</strong></div>
      <div><em>Realized</em><strong id="aReal">—</strong></div>
    </div>
    <div class="actions">
      <button class="btn dim" onclick="resetAcc(10000)">Reset 10k</button>
      <button class="btn dim" onclick="resetAcc(100000)">Reset 100k</button>
    </div>
  </div>
</section>

<div id="toast"></div>
<div class="modal-bg" id="modalBg">
  <div class="modal">
    <h3 id="mTitle">Open Trade</h3>
    <input type="hidden" id="m_symbol">
    <div class="row2">
      <div><label>Side</label><select id="m_side"><option>LONG</option><option>SHORT</option></select></div>
      <div><label>Leverage <span id="mLevHint"></span></label><input type="number" id="m_lev" value="10" min="1" max="150"></div>
    </div>
    <div class="row2">
      <div><label>Entry</label><input type="number" id="m_entry" step="any"></div>
      <div><label>Market</label><input type="number" id="m_mkt" step="any" readonly style="opacity:.7"></div>
    </div>
    <div class="row2">
      <div><label>Stop Loss</label><input type="number" id="m_sl" step="any"></div>
      <div><label>TP1</label><input type="number" id="m_tp1" step="any"></div>
    </div>
    <label>TP2</label><input type="number" id="m_tp2" step="any">
    <label>Size mode</label>
    <select id="m_mode" onchange="syncSizeMode()">
      <option value="risk">Risk % of balance</option>
      <option value="margin">Margin USDT</option>
      <option value="qty">Quantity</option>
    </select>
    <div id="box_risk">
      <label>Risk % <span id="riskVal">1%</span></label>
      <input type="range" id="m_risk" min="0.25" max="5" step="0.25" value="1" oninput="riskVal.textContent=this.value+'%'">
    </div>
    <div id="box_margin" style="display:none">
      <label>Margin USDT</label><input type="number" id="m_margin" step="any" placeholder="e.g. 50">
    </div>
    <div id="box_qty" style="display:none">
      <label>Quantity</label><input type="number" id="m_qty" step="any" placeholder="e.g. 0.01">
    </div>
    <p class="hint">Coin max leverage auto-capped. Flexible size: % · margin · qty.</p>
    <div class="btns">
      <button class="btn dim" onclick="closeModal()">Cancel</button>
      <button class="btn good" onclick="submitTrade()">Open Trade</button>
    </div>
  </div>
</div>

<script>
let soundOn=true, voiceOn=true, style='all', lastSig='', spokenAlerts=new Set();
if('Notification'in window && Notification.permission==='default') Notification.requestPermission();

function speak(text){
  if(!voiceOn||!('speechSynthesis'in window)) return;
  try{
    window.speechSynthesis.cancel();
    const u=new SpeechSynthesisUtterance(text);
    u.rate=1; u.lang='en-US';
    window.speechSynthesis.speak(u);
  }catch(e){}
}
function beep(n=2){
  if(!soundOn) return;
  try{
    const c=new(window.AudioContext||window.webkitAudioContext);
    for(let i=0;i<n;i++) setTimeout(()=>{
      const o=c.createOscillator(),g=c.createGain();
      o.connect(g); g.connect(c.destination);
      o.type='sine'; o.frequency.value=i%2?640:880;
      g.gain.value=.12; o.start();
      g.gain.exponentialRampToValueAtTime(.001,c.currentTime+.2);
      o.stop(c.currentTime+.2);
    }, i*220);
  }catch(e){}
}
function toast(m,ms=2800){const t=document.getElementById('toast');t.textContent=m;t.style.display='block';setTimeout(()=>t.style.display='none',ms);}
function toggleSound(){
  soundOn=!soundOn; voiceOn=soundOn;
  document.getElementById('soundBtn').textContent=soundOn?'Voice ON':'Voice OFF';
  if(soundOn) speak('Voice alerts on');
}
function showTab(n){
  ['signals','trades','movers','account'].forEach(x=>{
    const el=document.getElementById('tab-'+x); if(el) el.style.display=x===n?'block':'none';
  });
  document.querySelectorAll('.tabs button').forEach(b=>b.classList.toggle('on',b.dataset.t===n));
  if(n==='trades') loadTrades();
  if(n==='movers') loadMovers();
  if(n==='account') loadAccount();
}
function setStyle(s){style=s;document.querySelectorAll('.filters button').forEach(b=>b.classList.toggle('on',b.dataset.s===s));loadSignals();}
function fmt(n){return Number(n).toLocaleString(undefined,{maximumFractionDigits:2});}
function setAcc(a){
  if(!a) return;
  sBal.textContent='$'+fmt(a.balance); sEq.textContent='$'+fmt(a.equity);
  sReal.textContent=(a.realized_pnl>=0?'+':'')+'$'+fmt(a.realized_pnl);
  sReal.className=a.realized_pnl>=0?'up':'dn';
  if(aStart){aStart.textContent='$'+fmt(a.start_balance); aBal.textContent='$'+fmt(a.balance);
    aEq.textContent='$'+fmt(a.equity); aReal.textContent=(a.realized_pnl>=0?'+':'')+'$'+fmt(a.realized_pnl);}
}
function syncSizeMode(){
  const m=m_mode.value;
  box_risk.style.display=m==='risk'?'block':'none';
  box_margin.style.display=m==='margin'?'block':'none';
  box_qty.style.display=m==='qty'?'block':'none';
}
function tv(sym){return 'BINANCE:'+sym+'.P';}
function toggleChart(id,sym){
  const box=document.getElementById(id);
  if(!box) return;
  if(box.style.display==='block'){box.style.display='none'; box.innerHTML=''; return;}
  box.style.display='block';
  box.innerHTML=`<iframe src="https://s.tradingview.com/widgetembed/?symbol=${tv(sym)}&interval=15&hidesidetoolbar=1&hidetoptoolbar=1&theme=dark&style=1&timezone=Etc%2FUTC" loading="lazy"></iframe>`;
}
function cardHTML(s){
  const L=s.side==='LONG';
  const mp=s.market_price??s.entry;
  const lev=s.max_leverage||20;
  const cid='ch_'+s.symbol;
  const btn=s.already_added
    ? `<button class="btn dim" disabled>Added</button>`
    : `<button class="btn good" onclick='openModal(${JSON.stringify(s)})'>Add Trade</button>`;
  return `<article class="card ${L?'L':'S'}">
    <div class="head">
      <div><span class="side ${L?'L':'S'}">${s.side}</span><span class="sym">${s.symbol}</span>
        <span class="tag q">${s.quality||'B'}</span><span class="tag">${lev}x</span></div>
      <div class="score">${s.score}</div>
    </div>
    <div class="sub">${s.setup||''} · ${(s.styles||[]).join('/')} · 1D ${s.bias_1d||'—'} · 4H ${s.bias_4h||'—'} · ADX ${s.adx??'—'} · RSI ${s.rsi??'—'}</div>
    <div class="vals">
      <div><em>Entry</em><strong>${s.entry}</strong></div>
      <div><em>Market</em><strong>${mp}</strong></div>
      <div><em>SL</em><strong>${s.stop_loss}</strong></div>
      <div><em>TP1</em><strong>${s.tp1}</strong></div>
      <div><em>TP2</em><strong>${s.tp2}</strong></div>
      <div><em>R:R</em><strong>1:${s.rr}</strong></div>
    </div>
    <div class="sub" style="margin-top:8px">${s.summary||''}</div>
    <div class="sub">${(s.reasons||[]).slice(0,4).join(' · ')}</div>
    <div class="actions">
      ${btn}
      <button class="btn dim" onclick="toggleChart('${cid}','${s.symbol}')">Chart</button>
    </div>
    <div class="chartbox" id="${cid}"></div>
  </article>`;
}
async function loadSignals(){
  try{
    const d=await(await fetch('/api/signals?style='+style)).json();
    setAcc(d.account);
    status.textContent=d.scanning?'Scanning…':(d.error?'⚠️ '+d.error:`${d.count||0} signals · ${d.scanned_count||0} pairs · ${d.last_updated||''}`);
    const list=document.getElementById('signalList');
    if(!d.signals||!d.signals.length){list.innerHTML='<div class="empty">No signals. Scan or Analyze.</div>';return;}
    const h=d.signals.map(s=>s.symbol+s.side+s.score).join('|');
    if(lastSig && h!==lastSig){
      beep(2);
      const neu=d.signals.filter(s=>!lastSig.includes(s.symbol+s.side));
      if(neu.length) speak(neu.slice(0,3).map(s=>`${s.side} ${s.symbol.replace('USDT','')}`).join(', '));
      else speak('Signals updated');
    }
    lastSig=h;
    list.innerHTML=d.signals.map(cardHTML).join('');
    window._movers={gainers:d.gainers||[],losers:d.losers||[]};
  }catch(e){status.textContent='Load failed';}
}
function runScan(){toast('Scan started');location.href='/scan?top=28';}
async function analyzeManual(){
  let s=(symIn.value||'').trim().toUpperCase().replace('/','').replace('-','');
  if(!s){toast('Enter symbol');return;}
  if(!s.endsWith('USDT')) s+='USDT';
  toast('Analyzing '+s);
  const d=await(await fetch('/api/analyze/'+s)).json();
  const list=document.getElementById('signalList');
  if(!d.ok){list.innerHTML=`<div class="empty">${d.error||'Failed'}</div>`;return;}
  if(!d.signal){list.innerHTML=`<div class="card"><div class="sym">${d.symbol}</div><div class="sub">Market ${d.market_price??'—'} · max ${d.max_leverage||'—'}x</div><div class="empty">${d.message||'No signal'}</div>
    <div class="actions"><button class="btn dim" onclick="toggleChart('chm','${d.symbol}')">Chart</button></div><div class="chartbox" id="chm"></div></div>`;return;}
  list.innerHTML=cardHTML(d.signal);
  speak(`${d.signal.side} ${d.signal.symbol.replace('USDT','')}`);
}
function openModal(s){
  mTitle.textContent=s.symbol;
  m_symbol.value=s.symbol; m_side.value=s.side;
  m_entry.value=s.market_price||s.entry; m_mkt.value=s.market_price||s.entry;
  m_sl.value=s.stop_loss; m_tp1.value=s.tp1; m_tp2.value=s.tp2||'';
  const ml=s.max_leverage||20; m_lev.value=Math.min(10,ml); m_lev.max=ml; mLevHint.textContent='max '+ml+'x';
  m_mode.value='risk'; syncSizeMode();
  modalBg.style.display='flex';
}
function closeModal(){modalBg.style.display='none';}
async function submitTrade(){
  const mode=m_mode.value;
  const body={symbol:m_symbol.value,side:m_side.value,entry:+m_entry.value,stop_loss:+m_sl.value,tp1:+m_tp1.value,tp2:+m_tp2.value||null,leverage:+m_lev.value||10};
  if(mode==='risk') body.risk_pct=+m_risk.value||1;
  if(mode==='margin') body.margin_usd=+m_margin.value||0;
  if(mode==='qty') body.quantity=+m_qty.value||0;
  const d=await(await fetch('/api/trades',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
  if(d.ok){closeModal();toast(d.duplicate?'Already open':'Trade opened');speak(m_symbol.value.replace('USDT','')+' trade opened');setAcc(d.account);loadSignals();showTab('trades');loadTrades();}
  else toast(d.error||'Failed');
}
async function loadTrades(){
  const d=await(await fetch('/api/trades')).json(); setAcc(d.account);
  const open=(d.trades||[]).filter(t=>t.status==='OPEN');
  const closed=(d.trades||[]).filter(t=>t.status==='CLOSED');
  sOpen.textContent=open.length;
  let h='';
  if(open.length){
    h+='<div class="sec">Open</div>';
    h+=open.map(t=>{
      const L=t.side==='LONG'; const up=(t.unrealized_pnl||0)>=0;
      return `<article class="card ${L?'L':'S'}${t.hit?' hit':''}">
        <div class="head">
          <div><span class="side ${L?'L':'S'}">${t.side}</span><span class="sym">${t.symbol}</span>
            <span class="tag">${t.leverage}x</span>${t.hit?`<span class="tag" style="background:#fbbf24;color:#111">${t.hit}</span>`:''}</div>
          <div class="score" style="color:${up?'var(--gn)':'var(--rd)'}">${up?'+':''}$${fmt(t.unrealized_pnl||0)}</div>
        </div>
        <div class="vals">
          <div><em>Entry</em><strong>${t.entry}</strong></div>
          <div><em>Market</em><strong>${t.current_price??'—'}</strong></div>
          <div><em>SL</em><strong>${t.stop_loss}</strong></div>
          <div><em>TP1</em><strong>${t.tp1}</strong></div>
          <div><em>Margin</em><strong>$${fmt(t.margin)}</strong></div>
          <div><em>Qty</em><strong>${t.qty}</strong></div>
          <div><em>PnL%</em><strong>${t.pnl_pct||0}%</strong></div>
        </div>
        <div class="actions"><button class="btn bad" onclick="closeTrade('${t.id}')">Close</button>
          <button class="btn dim" onclick="toggleChart('ct_${t.id}','${t.symbol}')">Chart</button></div>
        <div class="chartbox" id="ct_${t.id}"></div>
      </article>`;
    }).join('');
  }
  if(closed.length){
    h+='<div class="sec">Closed</div><div class="card">';
    h+=closed.slice(-10).reverse().map(t=>`<div class="list-row"><span><b class="side ${t.side==='LONG'?'L':'S'}">${t.side}</b> ${t.symbol} · ${t.hit||'CLOSED'}</span>
      <span style="color:${(t.realized_pnl||0)>=0?'var(--gn)':'var(--rd)'}">${(t.realized_pnl||0)>=0?'+':''}$${fmt(t.realized_pnl||0)}</span></div>`).join('');
    h+='</div>';
  }
  tradeList.innerHTML=h||'<div class="empty">No trades yet</div>';
}
async function closeTrade(id){
  if(!confirm('Close this trade?')) return;
  const d=await(await fetch('/api/trades/'+id+'/close',{method:'POST'})).json();
  if(d.ok){toast('Closed');speak('Trade closed');setAcc(d.account);alertBar.style.display='none';loadTrades();loadSignals();}
}
function loadMovers(){
  const m=window._movers||{gainers:[],losers:[]};
  const mk=(arr,up)=>arr.map(x=>`<div class="i" onclick="symIn.value='${x.symbol}';showTab('signals');analyzeManual()">
    <b>${x.symbol.replace('USDT','')}</b><span class="${up?'up':'dn'}">${x.change>=0?'+':''}${Number(x.change).toFixed(2)}%</span></div>`).join('')||'<div class="empty">Scan first</div>';
  gainers.innerHTML=mk(m.gainers||[],1); losers.innerHTML=mk(m.losers||[],0);
}
async function loadAccount(){const a=await(await fetch('/api/account')).json();setAcc(a);sOpen.textContent=a.open_trades||0;}
async function resetAcc(bal){
  if(!confirm('Reset account to $'+bal+'?')) return;
  const d0=await(await fetch('/api/trades')).json();
  for(const t of(d0.trades||[]).filter(x=>x.status==='OPEN')) await fetch('/api/trades/'+t.id+'/close',{method:'POST'});
  const d=await(await fetch('/api/account/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({balance:bal})})).json();
  if(d.ok){setAcc(d.account);toast('Reset done');}
}
async function checkAlerts(){
  try{
    const d=await(await fetch('/api/alerts')).json();
    if(d.count>0){
      const msg=d.alerts.map(t=>`${t.hit} ${t.symbol} ${t.pnl_pct||0}%`).join(' · ');
      alertBar.textContent='ALERT · '+msg+' · Tap for My Trades';
      alertBar.style.display='block';
      d.alerts.forEach(t=>{
        const key=t.id+t.hit;
        if(!spokenAlerts.has(key)){
          spokenAlerts.add(key);
          const name=t.symbol.replace('USDT','');
          const kind=t.hit==='SL'?'stop loss':'take profit '+t.hit;
          speak(`${name} ${kind} hit. PnL ${t.pnl_pct} percent`);
          beep(4);
        }
      });
    } else alertBar.style.display='none';
  }catch(e){}
}
loadSignals();
setInterval(loadSignals,50000);
setInterval(checkAlerts,12000);
setInterval(()=>{if(tab-trades.style.display!=='none')loadTrades();},12000);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("api:app", host="0.0.0.0", port=port, reload=False)
