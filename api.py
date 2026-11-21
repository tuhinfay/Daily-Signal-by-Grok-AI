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
_price_monitor_status: Dict[str, Any] = {
    "state": "starting", "connected": False, "symbols": [],
    "last_ws_update": None, "last_rest_update": None, "last_error": None,
}
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
        print(f"[binance] cooldown persistence failed: {e}")


class TradeCreate(BaseModel):
    symbol: str
    side: str
    entry: float
    stop_loss: float
    tp1: float
    tp2: Optional[float] = None
    order_type: str = "MARKET"
    limit_price: Optional[float] = None
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
    trailing_pct: Optional[float] = None  # legacy callback rate %
    trailing_activation_price: Optional[float] = None
    callback_rate: Optional[float] = None


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



def _estimate_liquidation_price(trade: Dict[str, Any]) -> Optional[float]:
    """Portfolio-aware paper estimate; exchange tiers and fees are approximated."""
    try:
        side = str(trade.get("side", "")).upper()
        qty = float(trade.get("qty") or trade.get("quantity") or 0)
        entry = float(trade.get("entry") or 0)
        mmr = float(trade.get("maintenance_margin_rate") or 0.005)
        maintenance_amount = float(trade.get("maintenance_amount") or 0)
        if side not in ("LONG", "SHORT") or qty <= 0 or entry <= 0 or not (0 <= mmr < 1):
            return None
        if str(trade.get("margin_mode", "CROSS")).upper() == "ISOLATED":
            wallet_equity = float(trade.get("margin") or 0)
            other_maintenance = 0.0
        else:
            isolated_locked = sum(float(x.get("margin") or 0) for x in _trades.values() if x.get("status") == "OPEN" and str(x.get("margin_mode", "CROSS")).upper() == "ISOLATED")
            wallet_equity = float(_account.get("balance", 0) or 0) - isolated_locked
            other_maintenance = 0.0
            for other in _trades.values():
                if other is trade or other.get("status") != "OPEN" or str(other.get("margin_mode", "CROSS")).upper() != "CROSS":
                    continue
                oq=float(other.get("qty") or 0); oe=float(other.get("entry") or 0); op=float(other.get("current_price") or oe)
                if oq <= 0 or oe <= 0: continue
                oside=str(other.get("side", "")).upper()
                wallet_equity += ((op-oe)*oq) if oside == "LONG" else ((oe-op)*oq)
                other_maintenance += max(0.0, op*oq*float(other.get("maintenance_margin_rate") or 0.005)-float(other.get("maintenance_amount") or 0))
        if side == "LONG":
            denominator=qty*(1-mmr)
            price=(entry*qty+other_maintenance-maintenance_amount-wallet_equity)/denominator
        else:
            denominator=qty*(1+mmr)
            price=(entry*qty+wallet_equity-other_maintenance+maintenance_amount)/denominator
        return float(price) if denominator > 0 and price > 0 else None
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _update_trade_liquidation_fields(trade: Dict[str, Any]) -> None:
    """Attach an explicitly estimated price and a distance-based risk warning."""
    price = _estimate_liquidation_price(trade)
    trade["liquidation_price"] = round(price, 8) if price is not None else None
    trade["liquidation_price_type"] = ("PORTFOLIO_ESTIMATE" if str(trade.get("margin_mode", "CROSS")).upper() == "CROSS" else "ISOLATED_ESTIMATE") if price is not None else None
    if str(trade.get("margin_mode", "CROSS")).upper() == "CROSS":
        trade["cross_wallet_equity_est"] = round(float(_account.get("balance", 0) or 0) + sum(float(x.get("unrealized_pnl") or 0) for x in _trades.values() if x.get("status") == "OPEN" and str(x.get("margin_mode", "CROSS")).upper() == "CROSS"), 2)
        trade["cross_maintenance_est"] = round(sum(max(0.0, float(x.get("current_price") or x.get("entry") or 0)*float(x.get("qty") or 0)*float(x.get("maintenance_margin_rate") or 0.005)-float(x.get("maintenance_amount") or 0)) for x in _trades.values() if x.get("status") == "OPEN" and str(x.get("margin_mode", "CROSS")).upper() == "CROSS"), 2)
    else:
        trade["cross_wallet_equity_est"] = None
        trade["cross_maintenance_est"] = None
    if price is None:
        trade["liquidation_risk"] = "UNKNOWN"
        trade["liquidation_distance_pct"] = None
        return
    try:
        current = float(trade.get("current_price") or trade.get("entry") or 0)
        if current <= 0:
            raise ValueError("missing current price")
        distance = abs(current - price) / current * 100
        trade["liquidation_distance_pct"] = round(distance, 2)
        trade["liquidation_risk"] = "HIGH" if distance <= 5 else ("MEDIUM" if distance <= 10 else "LOW")
    except (TypeError, ValueError):
        trade["liquidation_distance_pct"] = None
        trade["liquidation_risk"] = "UNKNOWN"


def run_scan(top_n: int = 28):
    global _scan_retry_after
    cooldown_remaining = int(_scan_retry_after - time_mod.monotonic())
    if cooldown_remaining > 0:
        minutes, seconds = divmod(cooldown_remaining, 60)
        _cache["error"] = f"Binance Futures IP cooldown active. Scan will retry in {minutes}m {seconds}s."
        _cache["scanning"] = False
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
                max_leverage = client.get_max_leverage(symbol)
                if px:
                    prices[symbol] = px
                for style_name in ("scalp", "day", "swing"):
                    r = engine.analyze_pair(symbol, data, volume_24h=vol, style=style_name)
                    if not r:
                        continue
                    r["market_price"] = px or r["entry"]
                    r["max_leverage"] = max_leverage
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
        if t.get("status") in ("OPEN", "PENDING") and t.get("symbol")
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
        if trade_symbol != symbol:
            continue
        if t.get("status") == "PENDING":
            limit_px=float(t.get("limit_price") or t.get("entry") or 0)
            pending_side=str(t.get("side", "")).upper()
            crossed=(price <= limit_px) if pending_side == "LONG" else (price >= limit_px)
            if not crossed: continue
            matching = next((x for x in _trades.values() if x is not t and x.get("status") == "OPEN" and x.get("symbol") == symbol and x.get("side") == pending_side), None)
            if matching:
                old_qty = float(matching.get("qty") or 0)
                add_qty = float(t.get("qty") or 0)
                total_qty = old_qty + add_qty
                old_entry = float(matching.get("entry") or limit_px)
                matching["entry"] = round((old_entry * old_qty + limit_px * add_qty) / total_qty, 8)
                matching["qty"] = round(total_qty, 8)
                matching["notional"] = round(matching["entry"] * total_qty, 2)
                matching["margin"] = round(float(matching.get("margin") or 0) + float(t.get("margin") or 0), 2)
                matching["dca_count"] = int(matching.get("dca_count") or 0) + 1
                matching.setdefault("dca_history", []).append({"price": limit_px, "qty": add_qty, "notional": round(limit_px * add_qty, 2), "margin": t.get("margin", 0), "created_at": datetime.now(timezone.utc).isoformat()})
                matching["current_price"] = price
                if pending_side == "LONG":
                    matching["unrealized_pnl"] = round((price - matching["entry"]) * total_qty, 4)
                else:
                    matching["unrealized_pnl"] = round((matching["entry"] - price) * total_qty, 4)
                matching["position_mode"] = "HEDGE"
                _update_trade_liquidation_fields(matching)
                t.update({"status": "MERGED", "hit": "MERGED", "merged_into": matching.get("id"), "merged_at": datetime.now(timezone.utc).isoformat()})
                db.save_trade(t)
                db.save_trade(matching)
                print(f"[orders] limit filled and auto-merged {t.get('id')} into {matching.get('id')}")
                changed = True
                continue
            t["status"]="OPEN"; t["entry"]=limit_px; t["current_price"]=None
            t["filled_at"]=datetime.now(timezone.utc).isoformat(); t["unrealized_pnl"]=0.0
            t["position_mode"] = "HEDGE"
            print(f"[orders] limit filled {t.get('id')} {symbol} @ {limit_px}")
        if t.get("status") != "OPEN": continue
        if t.get("current_price") == price: continue
        t["current_price"] = price
        side = t["side"].upper()
        entry = float(t.get("entry") or 0)
        qty = float(t.get("qty") or 0)
        lev = float(t.get("leverage") or 1)
        if entry <= 0 or qty <= 0:
            continue

        callback = float(t.get("callback_rate") or t.get("trailing_pct") or 0)
        activation = float(t.get("trailing_activation_price") or 0)
        active = bool(t.get("trailing_active"))
        if callback > 0:
            if not active:
                active = (price >= activation if side == "LONG" else price <= activation) if activation > 0 else (price >= entry if side == "LONG" else price <= entry)
                if active:
                    t["trailing_active"] = True
                    t["peak_price"] = price
                    t["trough_price"] = price
            if active:
                if side == "LONG":
                    peak = max(float(t.get("peak_price") or price), price)
                    t["peak_price"] = peak
                    trail_sl = peak * (1 - callback / 100)
                    if trail_sl > float(t.get("stop_loss") or 0): t["stop_loss"] = round(trail_sl, 8)
                else:
                    trough = min(float(t.get("trough_price") or price), price)
                    t["trough_price"] = trough
                    trail_sl = trough * (1 + callback / 100)
                    if trail_sl < float(t.get("stop_loss") or float("inf")): t["stop_loss"] = round(trail_sl, 8)

        previous_hit = t.get("hit")
        if side == "LONG":
            pnl = (price - entry) * qty
            t["pnl_pct"] = round(((price - entry) / entry) * 100 * lev, 3)
            trigger = "SL" if price <= float(t.get("stop_loss") or 0) else (
                "TP2" if t.get("tp2") and price >= float(t["tp2"]) else (
                    "TP1" if t.get("tp1") and price >= float(t["tp1"]) and not t.get("tp1_filled") else None
                )
            )
        else:
            pnl = (entry - price) * qty
            t["pnl_pct"] = round(((entry - price) / entry) * 100 * lev, 3)
            trigger = "SL" if price >= float(t.get("stop_loss") or float("inf")) else (
                "TP2" if t.get("tp2") and price <= float(t["tp2"]) else (
                    "TP1" if t.get("tp1") and price <= float(t["tp1"]) and not t.get("tp1_filled") else None
                )
            )

        if trigger == "TP1":
            close_pct = max(1.0, min(float(t.get("tp1_close_pct") or 50.0), 99.0))
            close_qty = qty * close_pct / 100.0
            realized_partial = (price - entry) * close_qty if side == "LONG" else (entry - price) * close_qty
            remaining_qty = max(0.0, qty - close_qty)
            _account["balance"] = round(float(_account.get("balance", 0)) + realized_partial, 2)
            _account["realized_pnl"] = round(float(_account.get("realized_pnl", 0)) + realized_partial, 2)
            t["realized_pnl"] = round(float(t.get("realized_pnl") or 0) + realized_partial, 4)
            t["qty"] = round(remaining_qty, 8)
            t["margin"] = round(float(t.get("margin") or 0) * (remaining_qty / qty), 2)
            t["notional"] = round(entry * remaining_qty, 2)
            t["tp1_filled"] = True
            t["hit"] = "TP1"
            pnl = (price - entry) * remaining_qty if side == "LONG" else (entry - price) * remaining_qty
            t["unrealized_pnl"] = round(pnl, 4)
        elif trigger in ("SL", "TP2"):
            _account["balance"] = round(float(_account.get("balance", 0)) + pnl, 2)
            _account["realized_pnl"] = round(float(_account.get("realized_pnl", 0)) + pnl, 2)
            t["realized_pnl"] = round(float(t.get("realized_pnl") or 0) + pnl, 4)
            t.update({
                "status": "CLOSED", "hit": trigger, "unrealized_pnl": 0.0,
                "needs_alert": True, "closed_at": datetime.now(timezone.utc).isoformat(),
            })
        else:
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
        for position in _trades.values():
            if position.get("status") == "OPEN": _update_trade_liquidation_fields(position)
    return changed


def _persist_live_prices(symbols):
    for t in list(_trades.values()):
        trade_symbol = str(t.get("symbol", "")).upper().replace("/", "").split(":")[0]
        if t.get("status") in ("OPEN", "CLOSED") and trade_symbol in symbols:
            db.save_trade(t)
    _calc_equity()
    db.save_account(_account)


def price_monitor_loop():
    """Track open trades from Binance mark-price streams, with a quiet-stream REST watchdog."""
    try:
        import websocket
    except Exception as e:
        websocket = None
        print(f"[price-ws] websocket-client unavailable; REST fallback only: {e}")

    client = BinanceClient(futures=True)
    last_rest_poll = 0.0
    while _monitor_running:
        symbols = _open_trade_symbols()
        _price_monitor_status["symbols"] = symbols
        if not symbols:
            _price_monitor_status.update({"state": "idle", "connected": False})
            time_mod.sleep(1)
            continue

        dirty = set()
        ws = None
        ws_last_message = time_mod.monotonic()
        last_persist = time_mod.monotonic()
        try:
            if websocket is None:
                raise RuntimeError("websocket-client unavailable")
            streams = "/".join(f"{symbol.lower()}@markPrice@1s" for symbol in symbols)
            url = f"wss://fstream.binance.com/market/stream?streams={streams}"
            ws = websocket.create_connection(url, timeout=2)
            _price_monitor_status.update({
                "state": "connected", "connected": True, "last_error": None,
                "symbols": symbols,
            })
            print(f"[price-ws] connected for {len(symbols)} open symbols")
            while _monitor_running and symbols == _open_trade_symbols():
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    raw = None

                if raw:
                    try:
                        message = json.loads(raw)
                        event = message.get("data", message)
                        if isinstance(event, str):
                            event = json.loads(event)
                        if isinstance(event, dict) and event.get("e") == "markPriceUpdate":
                            symbol = event.get("s")
                            price = event.get("p")
                            if symbol and price:
                                ws_last_message = time_mod.monotonic()
                                _price_monitor_status["last_ws_update"] = datetime.now(timezone.utc).isoformat()
                                if _apply_trade_price(symbol, price):
                                    dirty.add(str(symbol).upper())
                    except (ValueError, TypeError, AttributeError) as parse_error:
                        print(f"[price-ws] ignored malformed message: {parse_error}")

                now = time_mod.monotonic()
                # A connected socket can go quiet without disconnecting. Do not let
                # that suppress all price updates: use a conservative REST fallback.
                if now - ws_last_message >= 8 and now - last_rest_poll >= 10:
                    for symbol in symbols:
                        price = client.get_mark_price(symbol)
                        if price is not None and _apply_trade_price(symbol, price):
                            dirty.add(symbol)
                    last_rest_poll = now
                    _price_monitor_status["last_rest_update"] = datetime.now(timezone.utc).isoformat()
                    _price_monitor_status["state"] = "connected-rest-watchdog"
                if dirty and now - last_persist >= 3:
                    _persist_live_prices(dirty)
                    dirty.clear()
                    last_persist = now

        except Exception as e:
            _price_monitor_status.update({
                "state": "reconnecting", "connected": False, "last_error": str(e)[:240],
            })
            print(f"[price-ws] connection/reconnect: {e}")
        finally:
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass
            if dirty:
                try:
                    _persist_live_prices(dirty)
                except Exception as persist_error:
                    print(f"[price-ws] persistence failed: {persist_error}")

        # If the socket cannot connect, continue a low-frequency REST fallback.
        if _monitor_running and time_mod.monotonic() - last_rest_poll >= 10:
            current_symbols = _open_trade_symbols()
            rest_dirty = set()
            for symbol in current_symbols:
                price = client.get_mark_price(symbol)
                if price is not None and _apply_trade_price(symbol, price):
                    rest_dirty.add(symbol)
            if rest_dirty:
                try:
                    _persist_live_prices(rest_dirty)
                except Exception as persist_error:
                    print(f"[price-ws] REST persistence failed: {persist_error}")
            if current_symbols:
                last_rest_poll = time_mod.monotonic()
                _price_monitor_status["last_rest_update"] = datetime.now(timezone.utc).isoformat()
        time_mod.sleep(2)


@app.on_event("startup")
def startup():
    global _monitor_running, _account, _trades
    db.init_db(getattr(config, "DEMO_BALANCE", 10000))
    _account = db.load_account()
    _trades = db.load_trades()
    # Restore the user's saved preference; auto-scan state must not be reset on deploy.
    try:
        _cache["auto_scan"] = db.get_meta("auto_scan", "1") == "1"
        saved_ban_until = float(db.get_meta("binance_ban_until", "0") or 0)
        if saved_ban_until > time_mod.time():
            global _scan_retry_after
            _scan_retry_after = time_mod.monotonic() + (saved_ban_until - time_mod.time())
            print(f"[binance] persisted cooldown active for {int(saved_ban_until - time_mod.time())}s")
    except Exception as e:
        print(f"[startup] preference/cooldown restore failed: {e}")
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
        return JSONResponse({"ok": False, "error": "Binance Futures IP cooldown active; manual analysis is paused."}, status_code=429, headers={"Retry-After": str(retry_after)})
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
    r = engine.analyze_pair(sym, data, volume_24h=0, style=selected_style)
    px = client.get_current_price(sym)
    lev = client.get_max_leverage(sym)
    liq = client.get_market_liquidity(sym)
    if not r:
        return {"ok": True, "signal": None, "symbol": sym, "market_price": px,
                "max_leverage": lev, "liquidity": liq, "message": f"No {selected_style} confluence signal now"}
    r["market_price"] = px or r["entry"]
    r["max_leverage"] = lev
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


@app.get("/api/position-mode")
async def api_position_mode():
    return {"mode": "HEDGE", "engine": "SIMULATED", "allows_opposite_positions": True}


@app.delete("/api/trades/history")
async def clear_trade_history():
    removed = []
    for tid, trade in list(_trades.items()):
        if trade.get("status") not in ("OPEN", "PENDING"):
            removed.append(tid)
            _trades.pop(tid, None)
            db.delete_trade(tid)
    _calc_equity()
    db.save_account(_account)
    return {"ok": True, "deleted": len(removed), "account": _account}


@app.get("/api/trades")
async def api_trades():
    _calc_equity()
    return {"trades": list(_trades.values()), "account": _account, "stats": _stats()}


@app.get("/api/alerts")
async def api_alerts():
    alerts = [t for t in _trades.values() if t.get("needs_alert") and t.get("hit")]
    for trade in alerts:
        trade["needs_alert"] = False
        db.save_trade(trade)
    return {"alerts": alerts, "count": len(alerts)}


@app.post("/api/trades")
async def create_trade(body: TradeCreate):
    sym = body.symbol.upper().replace("/", "")
    side = body.side.upper()
    order_type = (body.order_type or "MARKET").upper()
    if side not in ("LONG", "SHORT"): return JSONResponse({"ok": False, "error": "side must be LONG or SHORT"}, status_code=400)
    if order_type not in ("MARKET", "LIMIT"): return JSONResponse({"ok": False, "error": "order_type must be MARKET or LIMIT"}, status_code=400)
    entry_price = float((body.limit_price or body.entry) if order_type == "LIMIT" else body.entry)
    if entry_price <= 0: return JSONResponse({"ok": False, "error": "invalid entry price"}, status_code=400)
    client = BinanceClient(futures=True)
    max_lev = client.get_max_leverage(sym)
    leverage_cap = max_lev or 20
    lev = max(1, min(int(body.leverage or leverage_cap), leverage_cap, 150))
    margin_mode = (body.margin_mode or "CROSS").upper()
    if margin_mode not in ("CROSS", "ISOLATED"):
        margin_mode = "CROSS"

    existing_trade = next((t for t in _trades.values() if t.get("status") == "OPEN" and t["symbol"] == sym and t["side"] == side), None)

    sl_dist = abs(entry_price - body.stop_loss)
    if sl_dist <= 0:
        return JSONResponse({"ok": False, "error": "invalid SL"}, status_code=400)
    if (side == "LONG" and body.stop_loss >= entry_price) or (side == "SHORT" and body.stop_loss <= entry_price):
        return JSONResponse({"ok": False, "error": "Stop Loss is on the wrong side of entry"}, status_code=400)
    if body.tp1 and ((side == "LONG" and body.tp1 <= entry_price) or (side == "SHORT" and body.tp1 >= entry_price)):
        return JSONResponse({"ok": False, "error": "Take Profit is on the wrong side of entry"}, status_code=400)

    # Priority: quantity > amount_usd (notional) > margin_usd > risk_pct
    if body.quantity and body.quantity > 0:
        qty = float(body.quantity)
        notional = qty * entry_price
        margin = notional / lev
        risk_usd = qty * sl_dist
    elif body.amount_usd and body.amount_usd > 0:
        notional = float(body.amount_usd)
        qty = notional / entry_price
        margin = notional / lev
        risk_usd = qty * sl_dist
    elif body.margin_usd and body.margin_usd > 0:
        margin = min(float(body.margin_usd), _account["balance"] * 0.99)
        notional = margin * lev
        qty = notional / entry_price
        risk_usd = qty * sl_dist
    else:
        risk_pct = max(0.25, min(float(body.risk_pct or 1.0), 100.0))
        risk_usd = _account["balance"] * (risk_pct / 100.0)
        qty = risk_usd / sl_dist
        notional = qty * entry_price
        margin = notional / lev

    if margin > _account["balance"] * 0.99:
        return JSONResponse({"ok": False, "error": "insufficient balance"}, status_code=400)

    # optional scale metadata
    scale_mode = body.scale_mode if body.scale_mode in ("flat", "ascending") else None
    scale_orders = int(body.scale_orders or 0) if scale_mode else 0

    # Always derive liquidity from this contract's current Binance Futures turnover.
    liq = client.get_market_liquidity(sym)

    tid = str(uuid.uuid4())[:8]
    trade = {
        "id": tid, "symbol": sym, "side": side, "position_mode": "HEDGE", "order_type": order_type, "order_price": entry_price if order_type == "LIMIT" else None, "limit_price": entry_price if order_type == "LIMIT" else None,
        "entry": entry_price, "stop_loss": body.stop_loss, "tp1": body.tp1, "tp2": body.tp2,
        "leverage": lev, "max_leverage": max_lev, "leverage_verified": max_lev is not None, "margin_mode": margin_mode,
        "qty": round(qty, 6), "margin": round(margin, 2), "notional": round(notional, 2),
        "risk_usd": round(risk_usd, 2), "liquidity": liq,
        "trailing_pct": None, "callback_rate": None, "trailing_activation_price": None,
        "trailing_active": False, "peak_price": entry_price, "trough_price": entry_price,
        "scale_mode": scale_mode, "scale_orders": scale_orders,
        "dca_history": [{"price": entry_price, "qty": round(qty, 8), "notional": round(notional, 2), "margin": round(margin, 2), "created_at": datetime.now(timezone.utc).isoformat()}],
        "note": body.note or "", "status": "OPEN", "hit": None, "needs_alert": False,
        "current_price": entry_price if order_type == "MARKET" else None, "pnl_pct": 0.0, "unrealized_pnl": 0.0,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "dca_count": 0,
    }
    if order_type == "LIMIT":
        trade["status"] = "PENDING"
        trade["pending_created_at"] = datetime.now(timezone.utc).isoformat()
        trade["dca_history"] = []
        _trades[tid] = trade
        db.save_trade(trade)
        _calc_equity()
        db.save_account(_account)
        return {"ok": True, "trade": trade, "pending": True, "duplicate": False, "account": _account}
    if existing_trade:
        # Same-side entries merge into one hedge-side position and retain its leverage.
        lev = int(existing_trade.get("leverage") or lev)
        margin = notional / lev
        old_qty = float(existing_trade.get("qty") or 0)
        combined_qty = old_qty + qty
        old_entry = float(existing_trade.get("entry") or entry_price)
        existing_trade["entry"] = round((old_entry * old_qty + entry_price * qty) / combined_qty, 8)
        existing_trade["qty"] = round(combined_qty, 8)
        existing_trade["notional"] = round(existing_trade["entry"] * combined_qty, 2)
        existing_trade["margin"] = round(float(existing_trade.get("margin") or 0) + margin, 2)
        existing_trade["dca_count"] = int(existing_trade.get("dca_count") or 0) + 1
        existing_trade.setdefault("dca_history", []).append({"price": body.entry, "qty": round(qty, 8), "notional": round(notional, 2), "margin": round(margin, 2), "created_at": datetime.now(timezone.utc).isoformat()})
        current_mark = float(_cache.get("prices", {}).get(sym) or existing_trade.get("current_price") or entry_price)
        existing_trade["current_price"] = current_mark
        if side == "LONG":
            existing_trade["unrealized_pnl"] = round((current_mark - existing_trade["entry"]) * combined_qty, 4)
            existing_trade["pnl_pct"] = round(((current_mark - existing_trade["entry"]) / existing_trade["entry"]) * 100 * lev, 3)
        else:
            existing_trade["unrealized_pnl"] = round((existing_trade["entry"] - current_mark) * combined_qty, 4)
            existing_trade["pnl_pct"] = round(((existing_trade["entry"] - current_mark) / existing_trade["entry"]) * 100 * lev, 3)
        existing_trade["position_mode"] = "HEDGE"
        for position in _trades.values():
            if position.get("status") == "OPEN": _update_trade_liquidation_fields(position)
        db.save_trade(existing_trade)
        _calc_equity()
        db.save_account(_account)
        return {"ok": True, "trade": existing_trade, "duplicate": True, "auto_dca": True, "account": _account}
    _update_trade_liquidation_fields(trade)
    _trades[tid] = trade
    for position in _trades.values():
        if position.get("status") == "OPEN": _update_trade_liquidation_fields(position)
    db.save_trade(trade)
    print(f"[trades] opened {tid} {sym} {side} entry={entry_price} qty={trade['qty']}")
    _calc_equity()
    db.save_account(_account)
    return {"ok": True, "trade": trade, "duplicate": False, "account": _account}


@app.delete("/api/trades/{tid}")
async def cancel_pending_trade(tid: str):
    if tid not in _trades or _trades[tid].get("status") != "PENDING":
        return JSONResponse({"ok": False, "error": "pending order not found"}, status_code=404)
    t = _trades[tid]
    t.update({"status": "CANCELED", "canceled_at": datetime.now(timezone.utc).isoformat()})
    db.save_trade(t)
    return {"ok": True, "trade": t}


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
    t.setdefault("dca_history", []).append({"price": price, "qty": round(add_qty, 8), "notional": round(price * add_qty, 2), "margin": round(price * add_qty / lev, 2), "created_at": datetime.now(timezone.utc).isoformat()})
    t["current_price"] = price
    _update_trade_liquidation_fields(t)
    for position in _trades.values():
        if position.get("status") == "OPEN": _update_trade_liquidation_fields(position)
    db.save_trade(t)
    _calc_equity()
    return {"ok": True, "trade": t, "account": _account}


@app.patch("/api/trades/{tid}")
async def update_trade(tid: str, body: TradeUpdate):
    if tid not in _trades or _trades[tid].get("status") != "OPEN":
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    t = _trades[tid]
    new_sl = body.stop_loss if body.stop_loss is not None else t.get("stop_loss")
    new_tp1 = body.tp1 if body.tp1 is not None else t.get("tp1")
    new_tp2 = body.tp2 if "tp2" in body.model_fields_set else t.get("tp2")
    entry = float(t.get("current_price") or t.get("entry") or 0)
    side = str(t.get("side", "")).upper()
    if new_sl is not None and entry > 0 and ((side == "LONG" and float(new_sl) >= entry) or (side == "SHORT" and float(new_sl) <= entry)):
        return JSONResponse({"ok": False, "error": "Stop Loss must be below LONG / above SHORT mark price"}, status_code=400)
    if new_tp1 is not None and entry > 0 and ((side == "LONG" and float(new_tp1) <= entry) or (side == "SHORT" and float(new_tp1) >= entry)):
        return JSONResponse({"ok": False, "error": "Take Profit must be above LONG / below SHORT mark price"}, status_code=400)
    if body.stop_loss is not None:
        t["stop_loss"] = body.stop_loss
    if body.tp1 is not None:
        t["tp1"] = body.tp1
    if "tp2" in body.model_fields_set:
        t["tp2"] = body.tp2
    if body.trailing_pct is not None or body.callback_rate is not None:
        rate = body.callback_rate if body.callback_rate is not None else body.trailing_pct
        rate = max(0.0, min(float(rate), 5.0))
        if 0 < rate < 0.1:
            return JSONResponse({"ok": False, "error": "Callback rate must be 0 or between 0.1% and 5%"}, status_code=400)
        t["callback_rate"] = rate or None
        t["trailing_pct"] = rate or None
        t["trailing_active"] = False
        t["peak_price"] = float(t.get("current_price") or t.get("entry") or 0)
        t["trough_price"] = float(t.get("current_price") or t.get("entry") or 0)
    if body.trailing_activation_price is not None:
        t["trailing_activation_price"] = max(0.0, float(body.trailing_activation_price)) or None
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
    total_realized = round(float(t.get("realized_pnl") or 0) + pnl, 2)
    t.update({"status": "CLOSED", "needs_alert": False,
              "closed_at": datetime.now(timezone.utc).isoformat(),
              "hit": t.get("hit") or "MANUAL", "realized_pnl": total_realized, "unrealized_pnl": 0.0})
    for position in _trades.values():
        if position.get("status") == "OPEN": _update_trade_liquidation_fields(position)
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

@app.get("/api/price-monitor")
async def api_price_monitor():
    return dict(_price_monitor_status)


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
.side-picker{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:5px}
.side-picker .side-btn{padding:13px 8px;border:2px solid #334155;border-radius:10px;background:#101827;color:#94a3b8;font-weight:900;font-size:13px;cursor:pointer;opacity:.72}
.side-picker .side-btn.long.selected{background:#064e3b;border-color:#10b981;color:#d1fae5;opacity:1;box-shadow:0 0 0 2px #10b98133}
.side-picker .side-btn.short.selected{background:#881337;border-color:#f43f5e;color:#ffe4e6;opacity:1;box-shadow:0 0 0 2px #f43f5e33}
.trade-details{margin-top:8px;border-top:1px solid var(--line);padding-top:7px}
.trade-details summary{cursor:pointer;color:var(--cyan);font-size:11px;font-weight:800;padding:5px 0;list-style:none}
.trade-details summary::-webkit-details-marker{display:none}
.trade-details summary:before{content:'＋ ';font-weight:900}
.trade-details[open] summary:before{content:'− '}
.position-main{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin-top:8px}
.position-main>div{background:#080e1a;border:1px solid var(--line);border-radius:8px;padding:7px}
.position-main em{display:block;font-size:9px;color:var(--mut);font-style:normal}
.position-main strong{display:block;font-size:11px;margin-top:3px;overflow-wrap:anywhere}
.manage-modal{width:100%;max-width:430px;background:var(--card);border:1px solid var(--line);border-radius:16px 16px 0 0;padding:14px;max-height:85vh;overflow:auto}
.manage-modal label{display:block;margin:9px 0 3px;font-size:10px;color:var(--mut);text-transform:uppercase}
.manage-modal input{width:100%;background:#080e1a;border:1px solid var(--line);color:var(--text);border-radius:9px;padding:11px;font-size:13px}
@media(max-width:420px){.position-main{grid-template-columns:repeat(2,1fr)}}
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
  <div class="note">HEDGE MODE · SIMULATED — একই symbol-এ LONG ও SHORT আলাদা position. Live mark price, estimated liquidation ও PnL. <button class="btn dim" style="float:right;padding:4px 8px" onclick="clearTradeHistory()">Clear History</button></div>
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
      <div><label>Side</label><select id="m_side"><option value="LONG">LONG</option><option value="SHORT">SHORT</option></select></div>
      <div><label>Margin mode</label><select id="m_mm"><option value="CROSS">Cross</option><option value="ISOLATED">Isolated</option></select></div>
    </div>
    <div class="row2">
      <div><label>Order Type</label><select id="m_order_type" onchange="toggleOrderType()"><option value="MARKET">Market</option><option value="LIMIT">Limit</option></select></div>
      <div><label>Leverage <span id="mLevHint"></span></label><input type="number" id="m_lev" value="20" min="1" max="150" oninput="updateOrderPreview()"></div>
    </div>
    <div><label>Liquidity</label><input id="m_liq" readonly style="opacity:.8"></div>
    <div class="row2">
      <div><label id="m_entry_label">Entry</label><input type="number" id="m_entry" step="any" oninput="updateOrderPreview()"></div>
      <div><label>Market</label><input type="number" id="m_mkt" step="any" readonly style="opacity:.7"></div>
    </div>
    <div id="limit_hint" class="hint" style="display:none">Limit order remains pending until mark price reaches your limit.</div>
    <div class="row2">
      <div><label>Stop Loss</label><input type="number" id="m_sl" step="any" oninput="updateOrderPreview()"></div>
      <div><label>TP1</label><input type="number" id="m_tp1" step="any"></div>
    </div>
    <label>TP2</label><input type="number" id="m_tp2" step="any">
    <label>Size mode</label>
    <select id="m_mode" onchange="syncSizeMode();updateOrderPreview()">
      <option value="amount" selected>Amount USDT (notional)</option>
      <option value="margin">Margin USDT</option>
      <option value="risk">Risk % of balance</option>
      <option value="qty">Quantity</option>
    </select>
    <div id="box_amount"><label>Amount USDT</label><input type="number" id="m_amount" step="any" placeholder="e.g. 500" oninput="updateOrderPreview()"></div>
    <div id="box_margin" style="display:none"><label>Margin USDT</label><input type="number" id="m_margin" step="any" oninput="updateOrderPreview()"></div>
    <div id="box_risk" style="display:none">
      <label>Risk % <span id="riskVal">1%</span></label>
      <input type="range" id="m_risk" min="0.25" max="100" step="0.25" value="1" oninput="riskVal.textContent=this.value+'%';updateOrderPreview()">
    </div>
    <div id="box_qty" style="display:none"><label>Quantity</label><input type="number" id="m_qty" step="any" oninput="updateOrderPreview()"></div>
    <div class="row2">
      <div><label>Scaled Order</label><select id="m_scale"><option value="">None</option><option value="flat">Equal Split</option><option value="ascending">Ascending Size</option></select></div>
      <div><label>Number of Orders</label><input type="number" id="m_scale_n" min="0" max="10" value="0"></div>
    </div>
    <div id="order_preview" style="display:none"></div>
    <p class="hint">Default: Amount + Cross margin. Scaled Order splits your entry into multiple orders.</p>
    <div class="btns">
      <button class="btn dim" onclick="closeModal()">Cancel</button>
      <button class="btn good" onclick="submitTrade()">Open Trade</button>
    </div>
  </div>
</div>
<div class="modal-bg" id="manageBg"><div class="manage-modal"><h3 id="manageTitle">Manage Position</h3><input type="hidden" id="manageTradeId"><div id="manageTrailFields" style="display:none"><label>Activation Price (blank = from entry)</label><input type="number" id="manageTrailActivation" step="any" placeholder="Optional"><label>Callback Rate (%) · 0.1–5</label><input type="number" id="manageTrailRate" min="0" max="5" step="0.1" value="1"><p class="hint">Trailing follows favorable mark-price movement after activation. Set 0 to disable.</p></div><div id="manageLevelsFields" style="display:none"><label>Stop Loss</label><input type="number" id="manageSL" step="any"><label>Take Profit 1</label><input type="number" id="manageTP1" step="any"><label>Take Profit 2 (optional)</label><input type="number" id="manageTP2" step="any"></div><div class="btns"><button class="btn dim" onclick="closeManage()">Cancel</button><button class="btn good" onclick="saveManage()">Confirm</button></div></div></div>

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
function syncSizeMode(){const m=m_mode.value;box_amount.style.display=m==='amount'?'block':'none';box_margin.style.display=m==='margin'?'block':'none';box_risk.style.display=m==='risk'?'block':'none';box_qty.style.display=m==='qty'?'block':'none';updateOrderPreview();}
function setSide(side){m_side.value=side;const isLong=side==='LONG';sideLong.classList.toggle('selected',isLong);sideShort.classList.toggle('selected',!isLong);sideLong.setAttribute('aria-pressed',String(isLong));sideShort.setAttribute('aria-pressed',String(!isLong));updateOrderPreview();}
function toggleOrderType(){const lim=m_order_type.value==='LIMIT';m_entry_label.textContent=lim?'Limit Price':'Market Entry';limit_hint.style.display=lim?'block':'none';m_entry.readOnly=!lim;if(!lim)m_entry.value=m_mkt.value||m_entry.value;updateOrderPreview();}
function updateOrderPreview(){
 const px=+(m_entry.value||m_mkt.value||0),lev=+m_lev.value||1,mode=m_mode.value;
 let notional=0;
 if(mode==='amount')notional=+m_amount.value||0;
 else if(mode==='margin')notional=(+m_margin.value||0)*lev;
 else if(mode==='qty')notional=(+m_qty.value||0)*px;
 else if(mode==='risk'){const bal=+(sBal.textContent||'').replace(/[^0-9.]/g,'')||10000;const risk=(+m_risk.value||1)/100*bal,dist=Math.abs(px-(+m_sl.value||px));notional=dist>0?risk/dist*px:0;}
 const qty=px>0?notional/px:0,margin=lev>0?notional/lev:0;
 order_preview.innerHTML='<div><em>Position Value</em><strong>$'+fmt(notional)+' USDT</strong></div><div><em>Initial Margin</em><strong>$'+fmt(margin)+' USDT</strong></div><div><em>Quantity</em><strong>'+qty.toFixed(6)+'</strong></div>';
}
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
  const btn=`<button class="btn good" onclick='openModal(${JSON.stringify(s)})'>${s.already_added?"Add / DCA":"Add Trade"}</button>`;
  return `<article class="card ${L?'L':'S'}">
    <div class="head"><div><span class="side ${L?'L':'S'}">${s.side}</span><span class="sym">${s.symbol}</span>
      <span class="tag q">${s.quality||'B'}</span><span class="tag">${s.liquidity||'UNKNOWN'}</span><span class="tag">${lev?lev+'x':'Max leverage unavailable'}</span></div>
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
  mTitle.textContent=s.symbol+' · '+(s.liquidity||'UNKNOWN');
  m_symbol.value=s.symbol; m_side.value=s.side; m_mm.value='CROSS'; m_order_type.value='MARKET';
  m_entry.value=s.market_price||s.entry; m_mkt.value=s.market_price||s.entry;
  m_sl.value=s.stop_loss; m_tp1.value=s.tp1; m_tp2.value=s.tp2||'';
  const ml=s.max_leverage||20; m_lev.value=ml; m_lev.max=ml; mLevHint.textContent=s.max_leverage?'verified max '+ml+'x':'safe 20x cap · verified max unavailable';
  m_liq.value=s.liquidity||'UNKNOWN'; m_mode.value='amount'; m_amount.value=''; m_margin.value=''; m_qty.value=''; toggleOrderType(); syncSizeMode();
  modalBg.style.display='flex'; updateOrderPreview();
}
function closeModal(){modalBg.style.display='none';}
async function submitTrade(){
  const mode=m_mode.value;
  const typ=m_order_type.value,px=+m_entry.value;
  const body={symbol:m_symbol.value,side:m_side.value,order_type:typ,entry:px,limit_price:typ==='LIMIT'?px:null,stop_loss:+m_sl.value,tp1:+m_tp1.value,tp2:+m_tp2.value||null,leverage:+m_lev.value||20,margin_mode:m_mm.value,scale_mode:m_scale.value||null,scale_orders:+m_scale_n.value||0};
  if(mode==='amount') body.amount_usd=+m_amount.value||0;
  if(mode==='margin') body.margin_usd=+m_margin.value||0;
  if(mode==='risk') body.risk_pct=+m_risk.value||1;
  if(mode==='qty') body.quantity=+m_qty.value||0;
  const d=await(await fetch('/api/trades',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
  if(d.ok){closeModal();toast(d.pending?'Limit order placed':(d.auto_dca?'Auto DCA added':(d.duplicate?'Already open':'Trade opened')));speak(m_symbol.value.replace('USDT','')+' opened');setAcc(d.account);pinManual=false;loadSignals();showTab('trades');loadTrades();}
  else toast(d.error||'Failed');
}
async function loadTrades(){
  const d=await(await fetch('/api/trades')).json(); setAcc(d.account); renderStats(d.stats,tradeStats); sOpen.textContent=(d.stats&&d.stats.open)||0;
  const open=(d.trades||[]).filter(t=>t.status==='OPEN');
  const pending=(d.trades||[]).filter(t=>t.status==='PENDING');
  const closed=(d.trades||[]).filter(t=>t.status==='CLOSED');
  let h='';
  if(pending.length){h+='<div class="sec">Pending Orders</div><div class="card">';h+=pending.map(t=>'<div class="list-row"><span><b class="side '+(t.side==='LONG'?'L':'S')+'">'+t.side+'</b> '+t.symbol+' · LIMIT '+t.limit_price+' · '+t.leverage+'x</span><button class="btn dim" onclick="cancelPending(\''+t.id+'\')">Cancel</button></div>').join('');h+='</div>';}
  if(open.length){h+='<div class="sec">Open</div>';h+=open.map(t=>{
    const L=t.side==='LONG';const up=(t.unrealized_pnl||0)>=0;
    const trail=t.callback_rate?(t.callback_rate+'%'):((t.trailing_pct||0)?(t.trailing_pct+'%'):'—');
    return `<article class="card ${L?'L':'S'}${t.hit?' hit':''}">
      <div class="head"><div><span class="side ${L?'L':'S'}">${t.side}</span><span class="sym">${t.symbol}</span>
        <span class="tag">${t.leverage}x</span><span class="tag">${t.margin_mode||'CROSS'}</span>
        ${t.hit?`<span class="tag" style="background:#fbbf24;color:#111">${t.hit}</span>`:''}
        ${(t.dca_count||0)?`<span class="tag">DCA ${t.dca_count}</span>`:''}</div>
        <div class="score" style="color:${up?'var(--gn)':'var(--rd)'}">${up?'+':''}$${fmt(t.unrealized_pnl||0)}</div></div>
      <div class="vals">
        <div><em>Average Entry</em><strong>${fmt(t.entry)}</strong></div>
        <div><em>Mark Price</em><strong>${t.current_price??'—'}</strong></div>
        <div><em>Position Value</em><strong>${fmt((t.current_price||t.entry)*(t.qty||0))}</strong></div>
        <div><em>Est. Liq. Price</em><strong>${t.liquidation_price?("~"+fmt(t.liquidation_price)):"—"}</strong></div>
        <div><em>SL</em><strong>${t.stop_loss}</strong></div>
        <div><em>TP1</em><strong>${t.tp1}</strong></div>
        <div><em>Margin</em><strong>$${fmt(t.margin)}</strong></div>
        <div><em>Qty</em><strong>${t.qty}</strong></div>
        <div><em>PnL%</em><strong>${t.pnl_pct||0}%</strong></div>
        <div><em>Liq. Distance</em><strong>${t.liquidation_distance_pct==null?"—":fmt(t.liquidation_distance_pct)+"%"}</strong></div>
        <div><em>Liq. Risk</em><strong>${t.liquidation_risk||"UNKNOWN"}</strong></div>
        <div><em>Trail</em><strong>${trail}${t.trailing_active?" · ACTIVE":""}</strong></div>
      </div>
      <div class="actions">
        <button class="btn bad" onclick="closeTrade('${t.id}')">Close</button>
        <button class="btn dim" onclick="trailPrompt('${t.id}',${t.callback_rate||t.trailing_pct||0},${t.trailing_activation_price||0})">Trailing Stop</button>
        <button class="btn dim" onclick="editLevels('${t.id}',${t.stop_loss},${t.tp1},${t.tp2||0})">SL/TP</button>
        <button class="btn dim" onclick="toggleChart('ct_${t.id}','${t.symbol}')">Chart</button>
      </div>
      <div class="chartbox" id="ct_${t.id}"></div></article>`;}).join('');}
  if(closed.length){h+='<div class="sec">Closed</div><div class="card">';h+=closed.slice(-12).reverse().map(t=>`<div class="list-row"><span><b class="side ${t.side==='LONG'?'L':'S'}">${t.side}</b> ${t.symbol} · ${t.hit||'CLOSED'}</span>
    <span style="color:${(t.realized_pnl||0)>=0?'var(--gn)':'var(--rd)'}">${(t.realized_pnl||0)>=0?'+':''}$${fmt(t.realized_pnl||0)}</span></div>`).join('');h+='</div>';}
  tradeList.innerHTML=h||'<div class="empty">No trades</div>';
}
async function cancelPending(id){if(!confirm('Cancel pending limit order?'))return;const d=await(await fetch('/api/trades/'+id,{method:'DELETE'})).json();if(d.ok){toast('Limit order canceled');loadTrades();}else toast(d.error||'Cancel failed');}
async function closeTrade(id){if(!confirm('Close trade?'))return;const d=await(await fetch('/api/trades/'+id+'/close',{method:'POST'})).json();if(d.ok){toast('Closed');speak('Trade closed');setAcc(d.account);alertBar.style.display='none';loadTrades();loadSignals();}}
function dcaPrompt(id){manageTradeId.value=id;manageTitle.textContent='Add to Position · DCA';manageDcaFields.style.display='block';manageTrailFields.style.display='none';manageDcaAmount.value='';manageBg.style.display='flex';}
function trailPrompt(id,cur,activation){manageTradeId.value=id;manageTitle.textContent='Trailing Stop';manageLevelsFields.style.display='none';manageTrailFields.style.display='block';manageTrailActivation.value=activation||'';manageTrailRate.value=cur||1;manageBg.style.display='flex';}
function closeManage(){manageBg.style.display='none';}
async function saveManage(){const id=manageTradeId.value;let body={};if(manageLevelsFields.style.display!=='none'){const sl=+manageSL.value,tp1=+manageTP1.value,tp2=manageTP2.value.trim()?+manageTP2.value:null;if(!(sl>0&&tp1>0)){toast('Enter valid Stop Loss and Take Profit');return;}body={stop_loss:sl,tp1:tp1,tp2:tp2};}else{const rate=+manageTrailRate.value;if(!Number.isFinite(rate)||rate<0||rate>5||(rate>0&&rate<0.1)){toast('Callback must be 0 or 0.1–5%');return;}body={callback_rate:rate};if(manageTrailActivation.value.trim()){const p=+manageTrailActivation.value;if(!Number.isFinite(p)||p<=0){toast('Invalid activation price');return;}body.trailing_activation_price=p;}else{body.trailing_activation_price=0;}}const d=await(await fetch('/api/trades/'+id,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();if(d.ok){closeManage();toast('Position protection updated');loadTrades();}else toast(d.error||'Update failed');}
async function editLevels(id,sl,tp1,tp2){manageTradeId.value=id;manageTitle.textContent='Stop Loss / Take Profit';manageTrailFields.style.display='none';manageLevelsFields.style.display='block';manageSL.value=sl;manageTP1.value=tp1;manageTP2.value=tp2||'';manageBg.style.display='flex';}
async function clearTradeHistory(){if(!confirm('Delete closed/canceled trade history? Open positions will remain.'))return;const d=await(await fetch('/api/trades/history',{method:'DELETE'})).json();if(d.ok){toast(d.deleted+' history records cleared');loadTrades();}}
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