#!/usr/bin/env python3
"""Daily Signal by Grok AI — auto-scan, SQLite, live trades"""

from fastapi import FastAPI, Query, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone
from pydantic import BaseModel, Field
import uvicorn, os, json, traceback, uuid, threading, time as time_mod

from scanner.binance_client import BinanceClient
from scanner.signal_engine import SignalEngine
from scanner import db
import config

app = FastAPI(title="Daily Signal by Grok AI", version="4.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_cache: Dict[str, Any] = {
    "signals": [], "last_updated": None, "scanning": False,
    "error": None, "scanned_count": 0,
    "gainers": [], "losers": [], "prices": {},
    "auto_scan": True,
}
_trades: Dict[str, Dict] = {}
_account: Dict[str, float] = {}
_monitor_running = False
_scan_lock = threading.Lock()
_scan_retry_after = 0.0


def _pause_scans_for_rate_limit(client):
    global _scan_retry_after
    until = float(getattr(client, "rate_limit_until", 0.0) or 0.0)
    remaining = max(300.0, until - time_mod.time())
    _scan_retry_after = max(_scan_retry_after, time_mod.monotonic() + remaining)
    try:
        if until > 0:
            db.set_meta("binance_ban_until", str(max(until, time_mod.time() + remaining)))
    except Exception as e:
        print(f"[binance] could not persist cooldown: {e}")


class TradeCreate(BaseModel):
    symbol: str
    side: str
    entry: float
    stop_loss: float
    tp1: float
    tp2: Optional[float] = None
    leverage: int = Field(default=20, ge=1, le=150)
    risk_pct: Optional[float] = None
    margin_usd: Optional[float] = None
    amount_usd: Optional[float] = None  # total notional
    quantity: Optional[float] = None
    margin_mode: str = "CROSS"  # CROSS | ISOLATED
    note: Optional[str] = ""
    scale_mode: Optional[str] = None  # flat | ascending
    scale_orders: Optional[int] = None


class TradeUpdate(BaseModel):
    stop_loss: Optional[float] = None
    tp1: Optional[float] = None
    tp2: Optional[float] = None
    trailing_pct: Optional[float] = None  # trail distance %


class DcaBody(BaseModel):
    amount_usd: Optional[float] = None
    margin_usd: Optional[float] = None
    quantity: Optional[float] = None
    price: Optional[float] = None


class AccountReset(BaseModel):
    balance: float = 10000


def _persist():
    db.save_account(_account)
    db.save_all_trades(_trades)


def _calc_equity():
    u = sum((t.get("unrealized_pnl") or 0) for t in _trades.values() if t.get("status") == "OPEN")
    _account["equity"] = round(_account["balance"] + u, 2)


def _estimate_liquidation_price(trade: Dict[str, Any]) -> Optional[float]:
    """Paper-trade estimate using Binance USDT-M maintenance-margin mechanics.

    The exact Binance result depends on the live symbol/position bracket and,
    for cross margin, the real Futures wallet and all account positions. This
    simulator uses a conservative base-tier MMR estimate and labels it as such.
    """
    try:
        qty = abs(float(trade.get("qty") or 0))
        entry = float(trade.get("entry") or 0)
        margin = max(0.0, float(trade.get("margin") or 0))
        if qty <= 0 or entry <= 0 or margin <= 0:
            return None
        mmr = float(trade.get("maintenance_margin_rate") or 0.005)
        cum = float(trade.get("maintenance_amount") or 0.0)
        side = str(trade.get("side") or "").upper()
        if side == "LONG":
            if str(trade.get("margin_mode") or "CROSS").upper() == "ISOLATED":
                wallet = margin
                price = (qty * entry - wallet - cum) / (qty * (1 - mmr))
            else:
                wallet = float(_account.get("balance") or 0)
                other = [x for x in _trades.values()
                         if x.get("status") == "OPEN" and x.get("id") != trade.get("id")]
                other_upnl = sum(float(x.get("unrealized_pnl") or 0) for x in other)
                other_mm = sum(max(0.0, abs(float(x.get("qty") or 0)) *
                                   float(x.get("current_price") or x.get("entry") or 0) * mmr)
                               for x in other)
                price = (qty * entry - wallet + other_mm - other_upnl - cum) / (qty * (1 - mmr))
        elif side == "SHORT":
            if str(trade.get("margin_mode") or "CROSS").upper() == "ISOLATED":
                wallet = margin
                price = (qty * entry + wallet + cum) / (qty * (1 + mmr))
            else:
                wallet = float(_account.get("balance") or 0)
                other = [x for x in _trades.values()
                         if x.get("status") == "OPEN" and x.get("id") != trade.get("id")]
                other_upnl = sum(float(x.get("unrealized_pnl") or 0) for x in other)
                other_mm = sum(max(0.0, abs(float(x.get("qty") or 0)) *
                                   float(x.get("current_price") or x.get("entry") or 0) * mmr)
                               for x in other)
                price = (qty * entry + wallet - other_mm + other_upnl + cum) / (qty * (1 + mmr))
        else:
            return None
        return round(price, 10) if price > 0 else None
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _update_trade_liquidation_fields(trade: Dict[str, Any]):
    price = _estimate_liquidation_price(trade)
    trade["liquidation_price"] = price
    trade["liquidation_price_type"] = "ESTIMATED"
    trade["maintenance_margin_rate"] = float(trade.get("maintenance_margin_rate") or 0.005)
    if price and trade.get("current_price"):
        current = float(trade["current_price"])
        trade["liquidation_distance_pct"] = round(
            abs(current - price) / current * 100, 3
        ) if current > 0 else None
        distance = trade["liquidation_distance_pct"]
        trade["liquidation_risk"] = "HIGH" if distance < 5 else "ELEVATED" if distance < 10 else "NORMAL"
    else:
        trade["liquidation_distance_pct"] = None
        trade["liquidation_risk"] = "UNKNOWN"


def _stats() -> Dict[str, Any]:
    closed = [t for t in _trades.values() if t.get("status") == "CLOSED" and t.get("hit") != "RESET"]
    wins = [t for t in closed if (t.get("realized_pnl") or 0) > 0]
    losses = [t for t in closed if (t.get("realized_pnl") or 0) < 0]
    total_pnl = sum(t.get("realized_pnl") or 0 for t in closed)
    wr = (len(wins) / len(closed) * 100) if closed else 0
    return {
        "total_trades": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(wr, 1),
        "total_pnl": round(total_pnl, 2),
        "open": sum(1 for t in _trades.values() if t.get("status") == "OPEN"),
    }


def run_scan(top_n: int = 28):
    global _scan_retry_after
    if time_mod.monotonic() < _scan_retry_after:
        return
    if not _scan_lock.acquire(blocking=False):
        return
    _cache["scanning"] = True
    _cache["error"] = None
    try:
        client = BinanceClient(futures=True)
        engine = SignalEngine()
        pairs = client.get_top_usdt_pairs(top_n=top_n, min_volume=config.MIN_VOLUME_USDT)
        try:
            mv = client.get_movers(top_n=12, min_volume=3_000_000)
            _cache["gainers"] = mv.get("gainers", [])
            _cache["losers"] = mv.get("losers", [])
        except Exception as e:
            print("movers", e)

        if client.rate_limited:
            _cache["error"] = "Binance Futures rate limit active; scans will retry automatically after a cooldown."
            _pause_scans_for_rate_limit(client)
            return

        signals, prices = [], {}
        for symbol, vol in pairs:
            data = {}
            for tf in ("5m", "15m", "1h", "4h", "1d"):
                if client.rate_limited:
                    break
                df = client.fetch_ohlcv(symbol, timeframe=tf, limit=config.CANDLE_LIMIT.get(tf, 100))
                if df is not None and len(df) > 30:
                    data[tf] = df
            if client.rate_limited:
                _cache["error"] = "Binance Futures rate limit active; remaining pairs paused and will retry after cooldown."
                _pause_scans_for_rate_limit(client)
                break
            if len(data) < 4:
                continue
            try:
                px = client.get_current_price(symbol)
                if client.rate_limited:
                    _cache["error"] = "Binance Futures rate limit active; remaining symbols paused."
                    _pause_scans_for_rate_limit(client)
                    break
                max_leverage = client.get_max_leverage(symbol)
                if px:
                    prices[symbol] = px
                for style_name in ("scalp", "day", "swing"):
                    r = engine.analyze_pair(symbol, data, volume_24h=vol, style=style_name)
                    if not r:
                        continue
                    r["market_price"] = px or r["entry"]
                    r["max_leverage"] = max_leverage
                    r["quote_volume_24h"] = vol
                    r["styles"] = [style_name]
                    r["summary"] = f"{r.get('setup','')} · {style_name.upper()} · R:R 1:{r['rr']} · SL {r['risk_pct']}% · TP1 {r['tp1_pct']}%"
                    signals.append(r)
            except Exception as e:
                print(f"scan {symbol}: {e}")
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
        _scan_lock.release()


def auto_scan_loop():
    while _monitor_running:
        if _cache.get("auto_scan"):
            try:
                run_scan(26)
            except Exception as e:
                print("auto scan", e)
            # wait ~3 min between scans
            for _ in range(18):
                if not _monitor_running or not _cache.get("auto_scan"):
                    break
                time_mod.sleep(10)
        else:
            time_mod.sleep(3)


def _open_trade_symbols():
    return sorted({
        str(t.get("symbol", "")).upper().replace("/", "").split(":")[0]
        for t in _trades.values()
        if t.get("status") == "OPEN" and t.get("symbol")
    })


def _apply_trade_price(symbol: str, price: float) -> bool:
    changed = False
    symbol = str(symbol).upper().replace("/", "").split(":")[0]
    try:
        price = float(price)
    except Exception:
        return False
    if price <= 0:
        return False

    for t in list(_trades.values()):
        trade_symbol = str(t.get("symbol", "")).upper().replace("/", "").split(":")[0]
        if t.get("status") != "OPEN" or trade_symbol != symbol:
            continue
        if t.get("current_price") == price:
            continue
        t["current_price"] = price
        side = t["side"].upper()
        entry = float(t.get("entry") or 0)
        qty = float(t.get("qty") or 0)
        lev = float(t.get("leverage") or 1)
        if entry <= 0 or qty <= 0:
            continue

        trail = float(t.get("trailing_pct") or 0)
        if trail > 0:
            if side == "LONG":
                peak = max(float(t.get("peak_price") or entry), price)
                t["peak_price"] = peak
                trail_sl = peak * (1 - trail / 100)
                if trail_sl > float(t.get("stop_loss") or 0):
                    t["stop_loss"] = round(trail_sl, 8)
            else:
                trough = min(float(t.get("trough_price") or entry), price)
                t["trough_price"] = trough
                trail_sl = trough * (1 + trail / 100)
                if trail_sl < float(t.get("stop_loss") or float("inf")):
                    t["stop_loss"] = round(trail_sl, 8)

        previous_hit = t.get("hit")
        if side == "LONG":
            pnl = (price - entry) * qty
            t["pnl_pct"] = round(((price - entry) / entry) * 100 * lev, 3)
            if price <= float(t.get("stop_loss") or 0):
                t["hit"] = "SL"
            elif t.get("tp2") and price >= float(t["tp2"]):
                t["hit"] = "TP2"
            elif t.get("tp1") and price >= float(t["tp1"]):
                t["hit"] = "TP1"
        else:
            pnl = (entry - price) * qty
            t["pnl_pct"] = round(((entry - price) / entry) * 100 * lev, 3)
            if price >= float(t.get("stop_loss") or float("inf")):
                t["hit"] = "SL"
            elif t.get("tp2") and price <= float(t["tp2"]):
                t["hit"] = "TP2"
            elif t.get("tp1") and price <= float(t["tp1"]):
                t["hit"] = "TP1"

        t["unrealized_pnl"] = round(pnl, 4)
        _update_trade_liquidation_fields(t)
        t["updated_at"] = datetime.now(timezone.utc).isoformat()
        if t.get("hit") and t.get("hit") != previous_hit:
            t["needs_alert"] = True
            t["alert_text"] = f"{t['symbol']} {t['hit']} hit. PnL {t['pnl_pct']}%"
        _cache["prices"][symbol] = price
        changed = True

    if changed:
        _calc_equity()
    return changed


def _persist_live_prices(symbols):
    for t in list(_trades.values()):
        trade_symbol = str(t.get("symbol", "")).upper().replace("/", "").split(":")[0]
        if t.get("status") == "OPEN" and trade_symbol in symbols:
            db.save_trade(t)
    _calc_equity()
    db.save_account(_account)


def price_monitor_loop():
    try:
        import websocket
    except Exception as e:
        websocket = None
        print(f"[price-ws] websocket-client unavailable; REST fallback only: {e}")

    client = BinanceClient(futures=True)
    while _monitor_running:
        symbols = _open_trade_symbols()
        if not symbols:
            time_mod.sleep(1)
            continue

        if websocket is not None:
            # Binance mark-price stream is the price used by the Futures risk engine.
            streams = "/".join(f"{symbol.lower()}@markPrice@1s" for symbol in symbols)
            url = f"wss://fstream.binance.com/market/stream?streams={streams}"
            ws = None
            dirty = set()
            last_persist = time_mod.monotonic()
            try:
                ws = websocket.create_connection(url, timeout=2)
                print(f"[price-ws] connected for {len(symbols)} open symbols")
                while _monitor_running and symbols == _open_trade_symbols():
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        raw = None
                    if raw:
                        message = json.loads(raw)
                        event = message.get("data", message)
                        if event.get("e") == "markPriceUpdate":
                            symbol = event.get("s")
                            price = event.get("p")
                            if symbol and price and _apply_trade_price(symbol, price):
                                dirty.add(symbol)
                    now = time_mod.monotonic()
                    if dirty and now - last_persist >= 3:
                        _persist_live_prices(dirty)
                        dirty.clear()
                        last_persist = now
            except Exception as e:
                print(f"[price-ws] connection/reconnect: {e}")
            finally:
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
                if dirty:
                    _persist_live_prices(dirty)

        # Short REST fallback keeps PnL moving while the stream reconnects.
        for _ in range(3):
            if not _monitor_running:
                break
            current_symbols = _open_trade_symbols()
            if not current_symbols:
                break
            dirty = set()
            for symbol in current_symbols:
                price = client.get_mark_price(symbol)
                if price is not None and _apply_trade_price(symbol, price):
                    dirty.add(symbol)
            if dirty:
                _persist_live_prices(dirty)
            time_mod.sleep(2)


@app.on_event("startup")
def startup():
    global _monitor_running, _account, _trades
    db.init_db(getattr(config, "DEMO_BALANCE", 10000))
    _account = db.load_account()
    _trades = db.load_trades()
    for existing_trade in _trades.values():
        if existing_trade.get("status") == "OPEN":
            _update_trade_liquidation_fields(existing_trade)
    # Restore the user's saved preference; auto-scan state must not be reset on deploy.
    try:
        _cache["auto_scan"] = db.get_meta("auto_scan", "1") == "1"
        saved_ban_until = float(db.get_meta("binance_ban_until", "0") or 0)
        if saved_ban_until > time_mod.time():
            global _scan_retry_after
            _scan_retry_after = time_mod.monotonic() + (saved_ban_until - time_mod.time())
            print(f"[binance] persisted cooldown active for {int(saved_ban_until - time_mod.time())}s")
    except Exception as e:
        print(f"[startup] scan preference/cooldown restore: {e}")
        _cache["auto_scan"] = True
    _monitor_running = True
    threading.Thread(target=auto_scan_loop, daemon=True).start()
    threading.Thread(target=price_monitor_loop, daemon=True).start()


@app.on_event("shutdown")
def shutdown():
    global _monitor_running
    _monitor_running = False
    _persist()


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
    _calc_equity()
    return {
        "last_updated": _cache["last_updated"], "scanning": _cache["scanning"],
        "auto_scan": _cache["auto_scan"], "error": _cache["error"],
        "scanned_count": _cache["scanned_count"], "count": len(out),
        "signals": out, "gainers": _cache.get("gainers", []), "losers": _cache.get("losers", []),
        "account": _account, "stats": _stats(),
    }


@app.post("/api/auto-scan")
async def set_auto_scan(enabled: bool = Query(True)):
    _cache["auto_scan"] = bool(enabled)
    db.set_meta("auto_scan", "1" if enabled else "0")
    if enabled and not _cache["scanning"]:
        threading.Thread(target=lambda: run_scan(26), daemon=True).start()
    return {"ok": True, "auto_scan": _cache["auto_scan"]}


@app.get("/api/analyze/{symbol}")
async def analyze_one(symbol: str, style: str = Query("day")):
    if time_mod.monotonic() < _scan_retry_after:
        retry_after = max(1, int(_scan_retry_after - time_mod.monotonic()))
        return JSONResponse(
            {"ok": False, "error": "Binance Futures IP cooldown is active; manual analysis is paused to avoid extending the ban."},
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )
    sym = symbol.upper().replace("/", "").replace("-", "")
    if not sym.endswith("USDT"):
        sym += "USDT"
    selected_style = style if style in ("scalp", "day", "swing") else "day"
    client = BinanceClient(futures=True)
    engine = SignalEngine()
    # Fetch only the timeframes required by the selected strategy. This keeps
    # manual analysis independent of Auto Scan and reduces Binance request weight.
    required_tfs = {
        "scalp": ("5m", "15m", "1h", "4h"),
        "day": ("15m", "1h", "4h", "1d"),
        "swing": ("1h", "4h", "1d"),
    }[selected_style]
    data = {}
    for tf in required_tfs:
        df = client.fetch_ohlcv(sym, timeframe=tf, limit=config.CANDLE_LIMIT.get(tf, 100))
        if client.rate_limited:
            _pause_scans_for_rate_limit(client)
            retry_after = max(300, int(float(getattr(client, "rate_limit_until", 0.0) or 0.0) - time_mod.time()))
            return JSONResponse(
                {"ok": False, "error": "Binance Futures has rate-limited this Render IP. Manual analysis will work again after the ban expires; please retry later."},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
        if df is not None and len(df) > 30:
            data[tf] = df
    if len(data) != len(required_tfs):
        missing = [tf for tf in required_tfs if tf not in data]
        return JSONResponse(
            {"ok": False, "error": f"Insufficient Binance Futures candles for {sym}; missing: {', '.join(missing)}. Auto Scan does not control manual analysis."},
            status_code=503,
        )
    quote_volume_24h = client.get_quote_volume_24h(sym) or 0
    if client.rate_limited:
        _pause_scans_for_rate_limit(client)
        return JSONResponse(
            {"ok": False, "error": "Binance Futures IP cooldown is active; manual analysis is paused."},
            status_code=429,
            headers={"Retry-After": str(max(300, int(client.rate_limit_until - time_mod.time())))},
        )
    r = engine.analyze_pair(sym, data, volume_24h=quote_volume_24h, style=selected_style)
    px = client.get_mark_price(sym)
    lev = client.get_max_leverage(sym)
    liq = client.get_market_liquidity(sym)
    if not r:
        return {"ok": True, "signal": None, "symbol": sym, "market_price": px,
                "max_leverage": lev, "liquidity": liq, "message": f"No {selected_style} confluence signal now"}
    r["market_price"] = px or r["entry"]
    r["max_leverage"] = lev
    r["quote_volume_24h"] = quote_volume_24h or None
    r["liquidity"] = liq
    r["styles"] = [selected_style]
    r["summary"] = f"{r.get('setup','')} · {selected_style.upper()} · R:R 1:{r['rr']} · SL {r['risk_pct']}% · TP1 {r['tp1_pct']}%"
    return {"ok": True, "signal": r}


@app.get("/api/account")
async def api_account():
    _calc_equity()
    return {**_account, "open_trades": sum(1 for t in _trades.values() if t["status"] == "OPEN"), "stats": _stats()}


@app.post("/api/account/reset")
async def reset_account(body: AccountReset):
    for t in list(_trades.values()):
        if t.get("status") == "OPEN":
            t["status"] = "CLOSED"; t["hit"] = "RESET"; t["needs_alert"] = False
            db.save_trade(t)
    _account.update({"balance": float(body.balance), "equity": float(body.balance),
                     "realized_pnl": 0.0, "start_balance": float(body.balance)})
    db.save_account(_account)
    return {"ok": True, "account": _account}


@app.get("/api/trades")
async def api_trades():
    _calc_equity()
    return {"trades": list(_trades.values()), "account": _account, "stats": _stats()}


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
    leverage_cap = max_lev or 20
    lev = max(1, min(int(body.leverage or leverage_cap), leverage_cap, 150))
    margin_mode = (body.margin_mode or "CROSS").upper()
    if margin_mode not in ("CROSS", "ISOLATED"):
        margin_mode = "CROSS"

    for t in _trades.values():
        if t.get("status") == "OPEN" and t["symbol"] == sym and t["side"] == side:
            return {"ok": True, "trade": t, "duplicate": True}

    sl_dist = abs(body.entry - body.stop_loss)
    if sl_dist <= 0:
        return JSONResponse({"ok": False, "error": "invalid SL"}, status_code=400)

    # Priority: quantity > amount_usd (notional) > margin_usd > risk_pct
    if body.quantity and body.quantity > 0:
        qty = float(body.quantity)
        notional = qty * body.entry
        margin = notional / lev
        risk_usd = qty * sl_dist
    elif body.amount_usd and body.amount_usd > 0:
        notional = float(body.amount_usd)
        qty = notional / body.entry
        margin = notional / lev
        risk_usd = qty * sl_dist
    elif body.margin_usd and body.margin_usd > 0:
        margin = min(float(body.margin_usd), _account["balance"] * 0.99)
        notional = margin * lev
        qty = notional / body.entry
        risk_usd = qty * sl_dist
    else:
        risk_pct = max(0.25, min(float(body.risk_pct or 1.0), 100.0))
        risk_usd = _account["balance"] * (risk_pct / 100.0)
        qty = risk_usd / sl_dist
        notional = qty * body.entry
        margin = notional / lev

    if margin > _account["balance"] * 0.99:
        return JSONResponse({"ok": False, "error": "insufficient balance"}, status_code=400)

    # optional scale metadata
    scale_mode = body.scale_mode if body.scale_mode in ("flat", "ascending") else None
    scale_orders = int(body.scale_orders or 0) if scale_mode else 0

    # Keep 24h quote turnover separate from liquidation risk.
    quote_volume_24h = client.get_quote_volume_24h(sym)

    tid = str(uuid.uuid4())[:8]
    trade = {
        "id": tid, "symbol": sym, "side": side,
        "entry": body.entry, "stop_loss": body.stop_loss, "tp1": body.tp1, "tp2": body.tp2,
        "leverage": lev, "max_leverage": max_lev, "leverage_verified": max_lev is not None, "margin_mode": margin_mode,
        "qty": round(qty, 6), "margin": round(margin, 2), "notional": round(notional, 2),
        "risk_usd": round(risk_usd, 2), "quote_volume_24h": quote_volume_24h,
        "maintenance_margin_rate": 0.005, "maintenance_amount": 0.0,
        "trailing_pct": None, "peak_price": body.entry, "trough_price": body.entry,
        "scale_mode": scale_mode, "scale_orders": scale_orders,
        "note": body.note or "", "status": "OPEN", "hit": None, "needs_alert": False,
        "current_price": body.entry, "pnl_pct": 0.0, "unrealized_pnl": 0.0,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "dca_count": 0,
    }
    _update_trade_liquidation_fields(trade)
    _trades[tid] = trade
    db.save_trade(trade)
    _calc_equity()
    db.save_account(_account)
    return {"ok": True, "trade": trade, "duplicate": False, "account": _account}


@app.post("/api/trades/{tid}/dca")
async def dca_trade(tid: str, body: DcaBody):
    if tid not in _trades or _trades[tid].get("status") != "OPEN":
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    t = _trades[tid]
    price = body.price or t.get("current_price") or t["entry"]
    lev = t.get("leverage") or 1
    if body.quantity and body.quantity > 0:
        add_qty = float(body.quantity)
    elif body.amount_usd and body.amount_usd > 0:
        add_qty = float(body.amount_usd) / price
    elif body.margin_usd and body.margin_usd > 0:
        add_qty = (float(body.margin_usd) * lev) / price
    else:
        return JSONResponse({"ok": False, "error": "need amount/margin/qty"}, status_code=400)

    old_qty = t["qty"]
    new_qty = old_qty + add_qty
    # weighted avg entry
    t["entry"] = round(((t["entry"] * old_qty) + (price * add_qty)) / new_qty, 6)
    t["qty"] = round(new_qty, 6)
    t["notional"] = round(new_qty * t["entry"], 2)
    t["margin"] = round(t["notional"] / lev, 2)
    t["dca_count"] = (t.get("dca_count") or 0) + 1
    t["current_price"] = price
    _update_trade_liquidation_fields(t)
    db.save_trade(t)
    _calc_equity()
    return {"ok": True, "trade": t, "account": _account}


@app.patch("/api/trades/{tid}")
async def update_trade(tid: str, body: TradeUpdate):
    if tid not in _trades or _trades[tid].get("status") != "OPEN":
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    t = _trades[tid]
    if body.stop_loss is not None:
        t["stop_loss"] = body.stop_loss
    if body.tp1 is not None:
        t["tp1"] = body.tp1
    if body.tp2 is not None:
        t["tp2"] = body.tp2
    if body.trailing_pct is not None:
        t["trailing_pct"] = max(0.0, float(body.trailing_pct))
    _update_trade_liquidation_fields(t)
    db.save_trade(t)
    return {"ok": True, "trade": t}


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
    db.save_trade(t)
    _calc_equity()
    db.save_account(_account)
    return {"ok": True, "trade": t, "account": _account, "stats": _stats()}


@app.get("/scan")
async def trigger_scan(background_tasks: BackgroundTasks, top: int = Query(28, ge=10, le=40)):
    if not _cache["scanning"]:
        background_tasks.add_task(run_scan, top_n=top)
    return RedirectResponse(url="/", status_code=303)


@app.get("/health")
async def health():
    return {"status": "ok", "scanning": _cache["scanning"], "auto_scan": _cache["auto_scan"], "database": db.backend_name()}


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
.top{position:sticky;top:0;z-index:40;background:#060a12f2;backdrop-filter:blur(12px);border-bottom:1px solid var(--line);padding:12px}
.top h1{font-size:16px;font-weight:800;background:linear-gradient(90deg,var(--cyan),var(--blue));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin-top:10px}
.metric{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px}
.metric label{display:block;font-size:9px;color:var(--mut);text-transform:uppercase;font-weight:600}
.metric b{font-size:12px;font-weight:700}
.metric b.up{color:var(--gn)}.metric b.dn{color:var(--rd)}
.tabs{display:flex;gap:6px;padding:8px 12px;position:sticky;top:96px;z-index:30;background:var(--bg)}
.tabs button{flex:1;border:1px solid var(--line);background:var(--card);color:var(--mut);border-radius:9px;padding:9px 4px;font-size:11px;font-weight:700}
.tabs button.on{color:#fff;border:0;background:linear-gradient(135deg,#1d4ed8,#0891b2)}
.bar{display:flex;flex-wrap:wrap;gap:6px;padding:0 12px 8px}
.bar input{flex:1;min-width:100px;background:var(--card);border:1px solid var(--line);color:var(--text);border-radius:9px;padding:9px 10px}
.btn{border:0;border-radius:9px;padding:9px 12px;font-size:11px;font-weight:700;color:#fff;background:linear-gradient(135deg,#2563eb,#0891b2);cursor:pointer}
.btn.dim{background:var(--card);border:1px solid var(--line);color:var(--mut)}
.btn.good{background:linear-gradient(135deg,#059669,#10b981)}
.btn.bad{background:linear-gradient(135deg,#e11d48,#f43f5e)}
.btn:disabled{opacity:.45}
.filters{display:flex;gap:6px;padding:0 12px 8px;flex-wrap:wrap}
.filters button{border:1px solid var(--line);background:var(--card);color:var(--mut);border-radius:999px;padding:5px 11px;font-size:11px;font-weight:600}
.filters button.on{color:var(--cyan);border-color:#164e63;background:#0b1c2e}
.note{margin:0 12px 8px;padding:8px 10px;background:var(--card);border:1px solid var(--line);border-radius:9px;color:var(--mut);font-size:11px}
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
.actions .btn{flex:1;min-width:80px}
.chartbox{display:none;margin-top:8px;border-radius:10px;overflow:hidden;border:1px solid var(--line)}
.chartbox iframe{width:100%;height:340px;border:0;display:block}.chart-loading,.chart-fallback{padding:8px 10px;color:var(--mut);font-size:11px}.chart-fallback a{color:var(--cyan)}
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
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin:0 10px 8px}
.stats .metric{text-align:center}
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
    <button class="btn dim" id="autoBtn" onclick="toggleAuto()">Auto ON</button>
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
  <div class="stats" id="tradeStats"></div>
  <div class="note">Live Binance Futures mark prices and PNL. Liquidation price is an estimate using a base-tier maintenance-margin assumption; actual Binance symbol brackets and cross-wallet positions can differ.. Updates as trades occur.</div>
  <div id="tradeList"></div>
</section>

<section id="tab-movers" style="display:none">
  <div class="sec">Top gainers</div><div class="mv" id="gainers"></div>
  <div class="sec">Top losers</div><div class="mv" id="losers"></div>
</section>

<section id="tab-account" style="display:none">
  <div class="stats" id="accStats"></div>
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
      <div><label>Margin mode</label><select id="m_mm"><option value="CROSS">Cross</option><option value="ISOLATED">Isolated</option></select></div>
    </div>
    <div class="row2">
      <div><label>Leverage <span id="mLevHint"></span></label><input type="number" id="m_lev" value="20" min="1" max="150"></div>
      <div><label>24h USDT Turnover</label><input id="m_liq" readonly style="opacity:.8"></div>
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
      <option value="amount" selected>Amount USDT (notional)</option>
      <option value="margin">Margin USDT</option>
      <option value="risk">Risk % of balance</option>
      <option value="qty">Quantity</option>
    </select>
    <div id="box_amount"><label>Amount USDT</label><input type="number" id="m_amount" step="any" placeholder="e.g. 500"></div>
    <div id="box_margin" style="display:none"><label>Margin USDT</label><input type="number" id="m_margin" step="any"></div>
    <div id="box_risk" style="display:none">
      <label>Risk % <span id="riskVal">1%</span></label>
      <input type="range" id="m_risk" min="0.25" max="100" step="0.25" value="1" oninput="riskVal.textContent=this.value+'%'">
    </div>
    <div id="box_qty" style="display:none"><label>Quantity</label><input type="number" id="m_qty" step="any"></div>
    <div class="row2">
      <div><label>Scale</label><select id="m_scale"><option value="">None</option><option value="flat">Flat</option><option value="ascending">Ascending</option></select></div>
      <div><label>Scale orders</label><input type="number" id="m_scale_n" min="0" max="10" value="0"></div>
    </div>
    <p class="hint">Default: Amount + max leverage + Cross. Scale = split entry style.</p>
    <div class="btns">
      <button class="btn dim" onclick="closeModal()">Cancel</button>
      <button class="btn good" onclick="submitTrade()">Open Trade</button>
    </div>
  </div>
</div>

<script>
let soundOn=true, voiceOn=true, style='all', lastSig='', spokenAlerts=new Set(), pinManual=false, autoScan=true;
if('Notification'in window && Notification.permission==='default') Notification.requestPermission();

function speak(text){if(!voiceOn||!('speechSynthesis'in window))return;try{speechSynthesis.cancel();const u=new SpeechSynthesisUtterance(text);u.rate=1;u.lang='en-US';speechSynthesis.speak(u);}catch(e){}}
function beep(n=2){if(!soundOn)return;try{const c=new(window.AudioContext||window.webkitAudioContext);for(let i=0;i<n;i++)setTimeout(()=>{const o=c.createOscillator(),g=c.createGain();o.connect(g);g.connect(c.destination);o.type='sine';o.frequency.value=i%2?640:880;g.gain.value=.12;o.start();g.gain.exponentialRampToValueAtTime(.001,c.currentTime+.2);o.stop(c.currentTime+.2);},i*220);}catch(e){}}
function toast(m,ms=2800){const t=document.getElementById('toast');t.textContent=m;t.style.display='block';setTimeout(()=>t.style.display='none',ms);}
function toggleSound(){soundOn=!soundOn;voiceOn=soundOn;soundBtn.textContent=soundOn?'Voice ON':'Voice OFF';if(soundOn)speak('Voice on');}
async function toggleAuto(){autoScan=!autoScan;await fetch('/api/auto-scan?enabled='+(autoScan?'true':'false'),{method:'POST'});autoBtn.textContent=autoScan?'Auto ON':'Auto OFF';toast(autoScan?'Auto-scan started':'Auto-scan stopped');if(autoScan)runScan();}
function showTab(n){['signals','trades','movers','account'].forEach(x=>{const el=document.getElementById('tab-'+x);if(el)el.style.display=x===n?'block':'none';});document.querySelectorAll('.tabs button').forEach(b=>b.classList.toggle('on',b.dataset.t===n));if(n==='trades')loadTrades();if(n==='movers')loadMovers();if(n==='account')loadAccount();}
function setStyle(s){style=s;pinManual=false;document.querySelectorAll('.filters button').forEach(b=>b.classList.toggle('on',b.dataset.s===s));loadSignals();}
function fmt(n){return Number(n).toLocaleString(undefined,{maximumFractionDigits:2});}
function setAcc(a){if(!a)return;sBal.textContent='$'+fmt(a.balance);sEq.textContent='$'+fmt(a.equity);sReal.textContent=(a.realized_pnl>=0?'+':'')+'$'+fmt(a.realized_pnl);sReal.className=a.realized_pnl>=0?'up':'dn';if(window.aStart){aStart.textContent='$'+fmt(a.start_balance);aBal.textContent='$'+fmt(a.balance);aEq.textContent='$'+fmt(a.equity);aReal.textContent=(a.realized_pnl>=0?'+':'')+'$'+fmt(a.realized_pnl);}}
function renderStats(st,el){if(!st||!el)return;el.innerHTML=`
  <div class="metric"><label>Trades</label><b>${st.total_trades||0}</b></div>
  <div class="metric"><label>Win / Loss</label><b class="up">${st.wins||0}</b> / <b class="dn">${st.losses||0}</b></div>
  <div class="metric"><label>Win rate</label><b>${st.win_rate||0}%</b></div>
  <div class="metric"><label>Total PnL</label><b class="${(st.total_pnl||0)>=0?'up':'dn'}">${(st.total_pnl||0)>=0?'+':''}$${fmt(st.total_pnl||0)}</b></div>
  <div class="metric"><label>Open</label><b>${st.open||0}</b></div>`;}
function syncSizeMode(){const m=m_mode.value;box_amount.style.display=m==='amount'?'block':'none';box_margin.style.display=m==='margin'?'block':'none';box_risk.style.display=m==='risk'?'block':'none';box_qty.style.display=m==='qty'?'block':'none';}
function tv(sym){return 'BINANCE:'+String(sym).replace('/','').replace(':USDT','')+'.P';}
function toggleChart(id,sym){
  const box=document.getElementById(id);if(!box)return;
  if(box.style.display==='block'){box.style.display='none';box.innerHTML='';return;}
  const symbol=tv(sym);
  const url='https://www.tradingview.com/widgetembed/?symbol='+encodeURIComponent(symbol)+'&interval=15&hidesidetoolbar=1&hidetoptoolbar=1&theme=dark&style=1&withdateranges=1&allow_symbol_change=1';
  box.style.display='block';
  box.innerHTML='<div class="chart-loading">Loading '+symbol+' chart…</div><iframe title="'+symbol+' TradingView chart" src="'+url+'" loading="eager" allowfullscreen referrerpolicy="no-referrer-when-downgrade"></iframe><div class="chart-fallback">Chart not loading? <a target="_blank" rel="noopener" href="https://www.tradingview.com/chart/?symbol='+encodeURIComponent(symbol)+'">Open chart in TradingView</a></div>';
}
function cardHTML(s){
  const L=s.side==='LONG';const mp=s.market_price??s.entry;const lev=s.max_leverage;const cid='ch_'+s.symbol+'_'+(s.style||'day');
  const btn=s.already_added?`<button class="btn dim" disabled>Added</button>`:`<button class="btn good" onclick='openModal(${JSON.stringify(s)})'>Add Trade</button>`;
  return `<article class="card ${L?'L':'S'}">
    <div class="head"><div><span class="side ${L?'L':'S'}">${s.side}</span><span class="sym">${s.symbol}</span>
      <span class="tag q">${s.quality||'B'}</span><span class="tag">24h ${s.quote_volume_24h?fmt(s.quote_volume_24h):'—'}</span><span class="tag">${lev?lev+'x':'Leverage N/A'}</span></div>
      <div class="score">${s.score}</div></div>
    <div class="sub">${s.setup||''} · ${(s.styles||[]).join('/')} · ${s.timeframe||''} · HTF ${s.bias_1d||'—'}/${s.bias_4h||'—'} · ADX ${s.adx??'—'} · RSI ${s.rsi??'—'}</div>
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
    <div class="actions">${btn}<button class="btn dim" onclick="toggleChart('${cid}','${s.symbol}')">Chart</button></div>
    <div class="chartbox" id="${cid}"></div></article>`;
}
async function loadSignals(){
  try{
    const d=await(await fetch('/api/signals?style='+style)).json();
    setAcc(d.account); renderStats(d.stats, tradeStats); renderStats(d.stats, accStats);
    sOpen.textContent=(d.stats&&d.stats.open)||0;
    autoScan=!!d.auto_scan; autoBtn.textContent=autoScan?'Auto ON':'Auto OFF';
    status.textContent=d.scanning?'Scanning…':(d.error?'⚠️ '+d.error:`${d.count||0} signals · ${d.scanned_count||0} pairs · ${d.last_updated||''}${autoScan?' · auto':''}`);
    window._movers={gainers:d.gainers||[],losers:d.losers||[]};
    if(pinManual) return; // keep manual analysis visible
    const list=document.getElementById('signalList');
    if(!d.signals||!d.signals.length){list.innerHTML='<div class="empty">No signals yet. Wait for auto-scan or tap Scan.</div>';return;}
    const h=d.signals.map(s=>s.symbol+s.side+s.score).join('|');
    if(lastSig&&h!==lastSig){beep(2);const neu=d.signals.filter(s=>!lastSig.includes(s.symbol+s.side));if(neu.length)speak(neu.slice(0,3).map(s=>`${s.side} ${s.symbol.replace('USDT','')}`).join(', '));}
    lastSig=h;
    list.innerHTML=d.signals.map(cardHTML).join('');
  }catch(e){status.textContent='Load failed';}
}
function runScan(){pinManual=false;toast('Scan started');fetch('/scan?top=28').then(()=>setTimeout(loadSignals,3000));}
async function analyzeManual(){
  let s=(symIn.value||'').trim().toUpperCase().replace('/','').replace('-','');
  if(!s){toast('Enter symbol');return;} if(!s.endsWith('USDT'))s+='USDT';
  toast('Analyzing '+s); pinManual=true;
  const d=await(await fetch('/api/analyze/'+s+'?style='+(style==='all'?'day':style))).json();
  const list=document.getElementById('signalList');
  if(!d.ok){list.innerHTML=`<div class="empty">${d.error||'Failed'}</div>`;return;}
  if(!d.signal){list.innerHTML=`<div class="card"><div class="sym">${d.symbol}</div><div class="sub">Market ${d.market_price??'—'} · max ${d.max_leverage||'—'}x</div><div class="empty">${d.message||'No signal'}</div>
    <div class="actions"><button class="btn dim" onclick="pinManual=false;loadSignals()">Back to list</button><button class="btn dim" onclick="toggleChart('chm','${d.symbol}')">Chart</button></div><div class="chartbox" id="chm"></div></div>`;return;}
  list.innerHTML=cardHTML(d.signal)+`<div class="actions" style="padding:0 10px"><button class="btn dim" onclick="pinManual=false;loadSignals()">Back to list</button></div>`;
  speak(`${d.signal.side} ${d.signal.symbol.replace('USDT','')}`);
}
function openModal(s){
  mTitle.textContent=s.symbol+' · 24h turnover';
  m_symbol.value=s.symbol; m_side.value=s.side; m_mm.value='CROSS';
  m_entry.value=s.market_price||s.entry; m_mkt.value=s.market_price||s.entry;
  m_sl.value=s.stop_loss; m_tp1.value=s.tp1; m_tp2.value=s.tp2||'';
  const ml=s.max_leverage||20; m_lev.value=ml; m_lev.max=ml; mLevHint.textContent=s.max_leverage?'verified max '+ml+'x':'safe 20x cap · verified max unavailable';
  m_liq.value=s.quote_volume_24h?'
}
function closeModal(){modalBg.style.display='none';}
async function submitTrade(){
  const mode=m_mode.value;
  const body={symbol:m_symbol.value,side:m_side.value,entry:+m_entry.value,stop_loss:+m_sl.value,tp1:+m_tp1.value,tp2:+m_tp2.value||null,leverage:+m_lev.value||20,margin_mode:m_mm.value,scale_mode:m_scale.value||null,scale_orders:+m_scale_n.value||0};
  if(mode==='amount') body.amount_usd=+m_amount.value||0;
  if(mode==='margin') body.margin_usd=+m_margin.value||0;
  if(mode==='risk') body.risk_pct=+m_risk.value||1;
  if(mode==='qty') body.quantity=+m_qty.value||0;
  const d=await(await fetch('/api/trades',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
  if(d.ok){closeModal();toast(d.duplicate?'Already open':'Trade opened');speak(m_symbol.value.replace('USDT','')+' opened');setAcc(d.account);pinManual=false;loadSignals();showTab('trades');loadTrades();}
  else toast(d.error||'Failed');
}
async function loadTrades(){
  const d=await(await fetch('/api/trades')).json(); setAcc(d.account); renderStats(d.stats,tradeStats); sOpen.textContent=(d.stats&&d.stats.open)||0;
  const open=(d.trades||[]).filter(t=>t.status==='OPEN');
  const closed=(d.trades||[]).filter(t=>t.status==='CLOSED');
  let h='';
  if(open.length){h+='<div class="sec">Open</div>';h+=open.map(t=>{
    const L=t.side==='LONG';const up=(t.unrealized_pnl||0)>=0;
    return `<article class="card ${L?'L':'S'}${t.hit?' hit':''}">
      <div class="head"><div><span class="side ${L?'L':'S'}">${t.side}</span><span class="sym">${t.symbol}</span>
        <span class="tag">${t.leverage}x</span><span class="tag">${t.margin_mode||'CROSS'}</span>
        <span class="tag">24h ${t.quote_volume_24h?fmt(t.quote_volume_24h):'—'}</span>${t.hit?`<span class="tag" style="background:#fbbf24;color:#111">${t.hit}</span>`:''}
        ${(t.dca_count||0)?`<span class="tag">DCA ${t.dca_count}</span>`:''}</div>
        <div class="score" style="color:${up?'var(--gn)':'var(--rd)'}">${up?'+':''}$${fmt(t.unrealized_pnl||0)}</div></div>
      <div class="vals">
        <div><em>Entry</em><strong>${t.entry}</strong></div>
        <div><em>Market</em><strong>${t.current_price??'—'}</strong></div>
        <div><em>SL</em><strong>${t.stop_loss}</strong></div>
        <div><em>TP1</em><strong>${t.tp1}</strong></div>
        <div><em>Margin</em><strong>$${fmt(t.margin)}</strong></div>
        <div><em>Qty</em><strong>${t.qty}</strong></div>
        <div><em>PnL%</em><strong>${t.pnl_pct||0}%</strong></div>
        <div><em>Est. Liquidation</em><strong>${t.liquidation_price??'—'}</strong></div>
        <div><em>Liq. Risk / Distance</em><strong>${t.liquidation_distance_pct!=null?t.liquidation_distance_pct+'%':'—'}</strong></div>
        <div><em>Trail</em><strong>${t.trailing_pct?t.trailing_pct+'%':'—'}</strong></div>
      </div>
      <div class="actions">
        <button class="btn bad" onclick="closeTrade('${t.id}')">Close</button>
        <button class="btn dim" onclick="dcaPrompt('${t.id}')">DCA</button>
        <button class="btn dim" onclick="trailPrompt('${t.id}',${t.trailing_pct||0})">Trail</button>
        <button class="btn dim" onclick="editLevels('${t.id}',${t.stop_loss},${t.tp1},${t.tp2||0})">SL/TP</button>
        <button class="btn dim" onclick="toggleChart('ct_${t.id}','${t.symbol}')">Chart</button>
      </div>
      <div class="chartbox" id="ct_${t.id}"></div></article>`;}).join('');}
  if(closed.length){h+='<div class="sec">Closed</div><div class="card">';h+=closed.slice(-12).reverse().map(t=>`<div class="list-row"><span><b class="side ${t.side==='LONG'?'L':'S'}">${t.side}</b> ${t.symbol} · ${t.hit||'CLOSED'}</span>
    <span style="color:${(t.realized_pnl||0)>=0?'var(--gn)':'var(--rd)'}">${(t.realized_pnl||0)>=0?'+':''}$${fmt(t.realized_pnl||0)}</span></div>`).join('');h+='</div>';}
  tradeList.innerHTML=h||'<div class="empty">No trades</div>';
}
async function closeTrade(id){if(!confirm('Close trade?'))return;const d=await(await fetch('/api/trades/'+id+'/close',{method:'POST'})).json();if(d.ok){toast('Closed');speak('Trade closed');setAcc(d.account);alertBar.style.display='none';loadTrades();loadSignals();}}
async function dcaPrompt(id){const v=prompt('DCA Amount USDT (notional)');if(!v)return;const d=await(await fetch('/api/trades/'+id+'/dca',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({amount_usd:+v})})).json();if(d.ok){toast('DCA added');loadTrades();}else toast(d.error||'DCA failed');}
async function trailPrompt(id,cur){const v=prompt('Trailing stop % (e.g. 1.5). 0 to disable',cur||'1');if(v===null)return;const d=await(await fetch('/api/trades/'+id,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({trailing_pct:+v})})).json();if(d.ok){toast('Trailing updated');loadTrades();}}
async function editLevels(id,sl,tp1,tp2){const nsl=prompt('Stop loss',sl);if(nsl===null)return;const n1=prompt('TP1',tp1);if(n1===null)return;const n2=prompt('TP2 (optional)',tp2||'');const body={stop_loss:+nsl,tp1:+n1};if(n2)body.tp2=+n2;const d=await(await fetch('/api/trades/'+id,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();if(d.ok){toast('Levels updated');loadTrades();}}
function loadMovers(){const m=window._movers||{gainers:[],losers:[]};const mk=(arr,up)=>arr.map(x=>`<div class="i" onclick="symIn.value='${x.symbol}';showTab('signals');analyzeManual()"><b>${x.symbol.replace('USDT','')}</b><span class="${up?'up':'dn'}">${x.change>=0?'+':''}${Number(x.change).toFixed(2)}%</span></div>`).join('')||'<div class="empty">Waiting for scan…</div>';gainers.innerHTML=mk(m.gainers||[],1);losers.innerHTML=mk(m.losers||[],0);}
async function loadAccount(){const a=await(await fetch('/api/account')).json();setAcc(a);renderStats(a.stats,accStats);sOpen.textContent=a.open_trades||0;}
async function resetAcc(bal){if(!confirm('Reset to $'+bal+'?'))return;const d0=await(await fetch('/api/trades')).json();for(const t of(d0.trades||[]).filter(x=>x.status==='OPEN'))await fetch('/api/trades/'+t.id+'/close',{method:'POST'});const d=await(await fetch('/api/account/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({balance:bal})})).json();if(d.ok){setAcc(d.account);toast('Reset');}}
async function checkAlerts(){try{const d=await(await fetch('/api/alerts')).json();if(d.count>0){const msg=d.alerts.map(t=>`${t.hit} ${t.symbol} ${t.pnl_pct||0}%`).join(' · ');alertBar.textContent='ALERT · '+msg;alertBar.style.display='block';d.alerts.forEach(t=>{const key=t.id+t.hit;if(!spokenAlerts.has(key)){spokenAlerts.add(key);speak(`${t.symbol.replace('USDT','')} ${t.hit} hit. PnL ${t.pnl_pct} percent`);beep(4);}});}else alertBar.style.display='none';}catch(e){}}
loadSignals();
setInterval(()=>{if(!pinManual)loadSignals();},20000);
setInterval(checkAlerts,10000);
setInterval(()=>{loadTrades();},1000); // UI follows Binance trade stream updates
setInterval(async()=>{const a=await(await fetch('/api/account')).json();setAcc(a);sOpen.textContent=a.open_trades||0;},8000);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("api:app", host="0.0.0.0", port=port, reload=False)
