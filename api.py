#!/usr/bin/env python3
"""Daily Signal by Tuhinfay — auto-scan, SQLite, live trades"""

from fastapi import FastAPI, Query, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone
from pydantic import BaseModel, Field
import uvicorn, os, json, traceback, uuid, threading, time as time_mod, secrets

from scanner.binance_client import BinanceClient
from scanner.liquidation_feed import start_liquidation_feed, get_liquidation_summary
from scanner.multi_exchange_data import get_multi_exchange_context
from scanner.signal_engine import SignalEngine
from scanner.v5_analysis import qualify_v5_signal
from scanner.risk_sizing import calculate_position_size
from scanner.forward_validation import evaluate_forward_trades
from scanner.indicators import add_indicators, get_latest
from scanner import db
import config

app = FastAPI(title="Daily Signal by Tuhinfay", version="4.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Backend-only HTTP Basic authentication. Set APP_PASSWORD in the deployment environment.
_APP_PASSWORD = os.environ.get("APP_PASSWORD", "4980")
_APP_USERNAME = os.environ.get("APP_USERNAME", "admin")

@app.middleware("http")
async def require_dashboard_password(request, call_next):
    if request.method == "OPTIONS":
        return await call_next(request)
    from fastapi.responses import Response
    from starlette.status import HTTP_401_UNAUTHORIZED
    supplied_user = supplied_password = ""
    auth = request.headers.get("authorization", "")
    if auth.startswith("Basic "):
        import base64
        try:
            decoded = base64.b64decode(auth[6:], validate=True).decode("utf-8")
            supplied_user, supplied_password = decoded.split(":", 1)
        except Exception:
            pass
    valid = secrets.compare_digest(supplied_user, _APP_USERNAME) and secrets.compare_digest(supplied_password, _APP_PASSWORD)
    if not valid:
        return Response("Authentication required", status_code=HTTP_401_UNAUTHORIZED,
            headers={"WWW-Authenticate": 'Basic realm="Daily Signal", charset="UTF-8"'})
    return await call_next(request)


@app.middleware("http")
async def order_request_timing(request, call_next):
    started = time_mod.perf_counter()
    response = await call_next(request)
    if request.method == "POST" and request.url.path == "/api/trades":
        elapsed_ms = (time_mod.perf_counter() - started) * 1000
        print(f"[order-timing] POST /api/trades status={response.status_code} elapsed_ms={elapsed_ms:.1f}")
    return response

_cache: Dict[str, Any] = {
    "signals": [], "last_updated": None, "scanning": False,
    "error": None, "scanned_count": 0,
    "gainers": [], "losers": [], "prices": {},
    "auto_scan": True,
}
_trades: Dict[str, Dict] = {}
_account: Dict[str, float] = {}
_monitor_running = False
_modal_price_cache: Dict[str, Dict[str, Any]] = {}
_price_monitor_status: Dict[str, Any] = {
    "state": "starting", "connected": False, "symbols": [],
    "last_ws_update": None, "last_rest_update": None, "last_error": None,
}
_scan_lock = threading.Lock()
_scan_retry_after = 0.0
_scan_max_leverage: Dict[str, Optional[int]] = {}
_daily_open_cache: Dict[str, Dict[str, float]] = {}
_symbol_price_precision: Dict[str, int] = {}
_market_structure_cache: Dict[str, Dict[str, Any]] = {}
_market_structure_refresh_at: Dict[str, float] = {}
_market_structure_rotation = 0
_daily_market_context_cache: Dict[str, Dict[str, Any]] = {}


def _freeze_trade_market_context(trade: Dict[str, Any]) -> None:
    """Freeze the latest available market snapshot at the moment a trade closes."""
    symbol = _market_symbol_key(str(trade.get("symbol") or ""))
    snapshot = dict(_market_structure_cache.get(symbol) or {})
    snapshot.update(_daily_market_context_cache.get(symbol) or {})
    if snapshot:
        trade["market_structure"] = snapshot
        for key in ("daily_open", "daily_high", "daily_low", "change_7d_pct", "change_30d_pct", "change_1y_pct"):
            if snapshot.get(key) is not None:
                trade[key] = snapshot[key]


def _nearest_support_resistance(df, price):
    """Nearest confirmed local pivot support/resistance from the strategy timeframe."""
    result = {"support": None, "resistance": None}
    try:
        if df is None or len(df) < 8 or float(price or 0) <= 0:
            return result
        lows, highs = [], []
        start = max(2, len(df) - 100)
        for i in range(start, len(df) - 2):
            low, high = float(df["low"].iloc[i]), float(df["high"].iloc[i])
            if low <= float(df["low"].iloc[i-1]) and low <= float(df["low"].iloc[i-2]) and low < float(df["low"].iloc[i+1]) and low < float(df["low"].iloc[i+2]):
                if low < float(price):
                    lows.append(low)
            if high >= float(df["high"].iloc[i-1]) and high >= float(df["high"].iloc[i-2]) and high > float(df["high"].iloc[i+1]) and high > float(df["high"].iloc[i+2]):
                if high > float(price):
                    highs.append(high)
        result["support"] = max(lows) if lows else None
        result["resistance"] = min(highs) if highs else None
    except (TypeError, ValueError, KeyError, IndexError, OverflowError):
        pass
    return result


def _timeframe_trend_map(data: Dict[str, Any]) -> Dict[str, str]:
    """EMA9/EMA21 trend labels for available signal timeframes."""
    trends = {}
    for tf in ("15m", "1h", "4h", "1d"):
        try:
            frame = data.get(tf)
            if frame is None or len(frame) < 25:
                continue
            latest = get_latest(add_indicators(frame))
            close, fast, slow = (float(latest.get(k) or 0) for k in ("close", "ema9", "ema21"))
            if close > 0 and fast > 0 and slow > 0:
                trends[tf] = "UP" if close > fast > slow else "DOWN" if close < fast < slow else "SIDE"
        except (TypeError, ValueError, KeyError, IndexError):
            continue
    return trends


def _market_symbol_key(symbol: Any) -> str:
    """Canonical key shared by scanner cache and trade API (e.g. BTC/USDT:USDT -> BTCUSDT)."""
    return str(symbol or "").upper().replace("/", "").split(":")[0]
_bot_config = {"enabled": False, "symbols": "", "min_score": 75, "min_quality": "A", "amount_usd": 50.0, "max_open": 2, "trailing_pct": 0.8, "exit_on_opposite": True, "trade_style": "day", "cooldown_minutes": 30, "max_symbol_trades_day": 3, "max_daily_loss_pct": 2.0, "min_rr": 1.5, "direction": "both", "advanced_enabled": False, "advanced_sl_pct": 0.0, "advanced_tp1_pct": 0.0, "advanced_tp2_pct": 0.0, "advanced_limit_offset_pct": 0.0, "advanced_profit_lock": False, "advanced_profit_activation_pct": 0.8, "advanced_profit_callback_pct": 0.5, "dca_range_min_pct": 0.0, "dca_range_max_pct": 0.0}

def _utc_daily_change_pct(df, current_price=None):
    """Calculate change from Binance Futures' active UTC 1d candle open (BD 06:00)."""
    if df is None or len(df) == 0:
        return None
    try:
        daily_open = float(df["open"].iloc[-1])
        daily_price = float(current_price or df["close"].iloc[-1])
        if daily_open <= 0 or daily_price <= 0:
            return None
        return round((daily_price / daily_open - 1.0) * 100.0, 2)
    except (KeyError, TypeError, ValueError, IndexError, OverflowError):
        return None


def _daily_change_from_open(daily_open, current_price):
    try:
        daily_open = float(daily_open or 0)
        current_price = float(current_price or 0)
        if daily_open <= 0 or current_price <= 0:
            return None
        return round((current_price / daily_open - 1.0) * 100.0, 2)
    except (TypeError, ValueError, OverflowError):
        return None


def _symbol_leverage_cap(symbol: str) -> int:
    """Use a verified symbol tier; keep a conservative 20x cap when unknown."""
    value = _scan_max_leverage.get(str(symbol).upper().replace("/", "").split(":")[0])
    try:
        return max(1, min(int(value), 150)) if value is not None else 20
    except (TypeError, ValueError):
        return 20



def _classify_btc_regime(df):
    """Classify broad BTC 4h conditions from completed candles only."""
    if df is None or len(df) < 55:
        return "UNKNOWN"
    try:
        enriched = add_indicators(df.iloc[:-1].copy())
        row = get_latest(enriched)
        close = float(row.get("close") or 0)
        ema50 = float(row.get("ema50") or 0)
        ema200 = float(row.get("ema200") or 0)
        adx = float(row.get("adx") or 0)
        if not all(map(lambda x: x == x and abs(x) != float("inf"), (close, ema50, ema200, adx))):
            return "UNKNOWN"
        if close > ema50 and ema50 > ema200 and adx >= 18:
            return "BULL"
        if close < ema50 and ema50 < ema200 and adx >= 18:
            return "BEAR"
        return "RANGE"
    except (TypeError, ValueError, KeyError):
        return "UNKNOWN"


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
    trade_style: Optional[str] = None
    note: Optional[str] = ""
    scale_mode: Optional[str] = None  # flat | ascending
    scale_orders: Optional[int] = None
    scale_lower_price: Optional[float] = None
    scale_upper_price: Optional[float] = None
    advanced_enabled: bool = False
    dca_enabled: bool = False
    dca_gap_pct: Optional[float] = None
    dca_max_orders: Optional[int] = None
    dca_multiplier: Optional[float] = None
    dca_lower_price: Optional[float] = None
    dca_upper_price: Optional[float] = None
    sl_pct: Optional[float] = None
    tp1_pct: Optional[float] = None
    tp2_pct: Optional[float] = None
    limit_offset_pct: Optional[float] = None
    profit_lock_enabled: bool = False
    profit_activation_pct: Optional[float] = None
    profit_callback_pct: Optional[float] = None
    signal_adx: Optional[float] = None
    signal_rsi: Optional[float] = None
    timeframe_trends: Optional[Dict[str, str]] = None
    bias_1d: Optional[str] = None
    bias_4h: Optional[str] = None


class TradeUpdate(BaseModel):
    stop_loss: Optional[float] = None
    tp1: Optional[float] = None
    tp2: Optional[float] = None
    trailing_pct: Optional[float] = None  # legacy callback rate %
    trailing_activation_price: Optional[float] = None
    callback_rate: Optional[float] = None
    profit_lock_enabled: Optional[bool] = None
    profit_activation_pct: Optional[float] = None
    profit_callback_pct: Optional[float] = None


class DcaBody(BaseModel):
    amount_usd: Optional[float] = None
    margin_usd: Optional[float] = None
    quantity: Optional[float] = None
    price: Optional[float] = None


class FundsAdjustment(BaseModel):
    action: str
    amount: float


class ResetDataBody(BaseModel):
    password: str


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
    open_trades = [t for t in _trades.values() if t.get("status") == "OPEN"]
    unrealized_pnl = sum(float(t.get("unrealized_pnl") or 0) for t in open_trades)
    wr = (len(wins) / len(closed) * 100) if closed else 0
    return {
        "total_trades": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(wr, 1),
        "total_pnl": round(total_pnl, 2),
        "open": len(open_trades),
        "unrealized_pnl": round(unrealized_pnl, 2),
    }




def _trade_opened_at(trade: Dict[str, Any]) -> Optional[str]:
    """Resolve the actual open/fill timestamp, with legacy DCA fallbacks."""
    value = trade.get("filled_at") or trade.get("created_at")
    if not value:
        history = trade.get("dca_history") or []
        stamps = [x.get("created_at") for x in history if isinstance(x, dict) and x.get("created_at")]
        value = min(stamps) if stamps else None
    return str(value) if value else None


def _trade_duration_seconds(trade: Dict[str, Any]) -> Optional[int]:
    """Return elapsed whole seconds between fill/open and close timestamps."""
    opened = _trade_opened_at(trade)
    closed = trade.get("closed_at")
    if not opened or not closed:
        return None
    try:
        start = datetime.fromisoformat(str(opened).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(closed).replace("Z", "+00:00"))
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        return max(0, int((end - start).total_seconds()))
    except (TypeError, ValueError, OverflowError):
        return None


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
        trade["liquidation_distance_pct"] = None
        return
    try:
        current = float(trade.get("current_price") or trade.get("entry") or 0)
        if current <= 0:
            raise ValueError("missing current price")
        distance = abs(current - price) / current * 100
        trade["liquidation_distance_pct"] = round(distance, 2)
        trade["liquidation_risk"] = "HIGH" if distance <= 5 else "MEDIUM" if distance <= 10 else "LOW"
    except (TypeError, ValueError):
        trade["liquidation_distance_pct"] = None
        trade["liquidation_risk"] = "UNKNOWN"
 

def _run_paper_auto_bot(signals):
    cfg = _bot_config
    if not cfg.get("enabled"): return
    quality = {"B": 1, "A": 2, "A+": 3}
    min_quality = quality.get(str(cfg.get("min_quality", "A")).upper(), 2)
    symbols = {x.strip().upper().replace("/", "") for x in str(cfg.get("symbols") or "").split(",") if x.strip()}
    strategy = str(cfg.get("trade_style") or "day").lower()
    if strategy not in ("scalp", "day", "swing"): strategy = "day"
    now = datetime.now(timezone.utc)
    today = now.date()
    cooldown = max(0, min(int(cfg.get("cooldown_minutes") or 0), 1440))
    max_symbol_day = max(1, min(int(cfg.get("max_symbol_trades_day") or 3), 30))
    max_daily_loss = max(0.0, float(_account.get("start_balance") or _account.get("balance") or 0) * max(0.0, min(float(cfg.get("max_daily_loss_pct") or 0), 20.0)) / 100.0)
    try:
        opened = [t for t in _trades.values() if t.get("status") in ("OPEN", "PENDING")]
        if cfg.get("advanced_enabled") and cfg.get("dca_enabled") and strategy != "hybrid":
            gap = max(0.1, min(float(cfg.get("dca_gap_pct") or 1.5), 20.0)) / 100.0
            max_adds = max(1, min(int(cfg.get("dca_max_orders") or 3), 5))
            multiplier = max(1.0, min(float(cfg.get("dca_multiplier") or 2.0), 3.0))
            for position in list(opened):
                if not position.get("auto_trade") or position.get("status") != "OPEN": continue
                adds = int(position.get("dca_count") or 0)
                if adds >= max_adds: continue
                mark = float(_cache.get("prices", {}).get(position.get("symbol")) or position.get("current_price") or 0)
                entry0 = float(position.get("entry") or 0)
                if mark <= 0 or entry0 <= 0: continue
                history = position.get("dca_history") or []
                anchor = float(history[-1].get("price") or entry0) if history else entry0
                side0 = str(position.get("side") or "").upper()
                triggered = mark <= anchor * (1-gap) if side0 == "LONG" else mark >= anchor * (1+gap)
                dca_reference = float(position.get("dca_reference_entry") or entry0)
                adverse_pct = ((dca_reference-mark)/dca_reference*100.0) if side0 == "LONG" and dca_reference > 0 else ((mark-dca_reference)/dca_reference*100.0) if dca_reference > 0 else 0.0
                range_min = max(0.0, float(cfg.get("dca_range_min_pct") or 0)); range_max = max(0.0, float(cfg.get("dca_range_max_pct") or 0))
                if not triggered or adverse_pct < range_min or (range_max and adverse_pct > range_max): continue
                base_notional = float(position.get("auto_dca_base_usd") or cfg.get("amount_usd") or 50)
                add_notional = base_notional * (multiplier ** adds)
                lev0 = max(1, int(position.get("leverage") or 1))
                add_margin = add_notional / lev0
                if add_margin > max(0.0, float(_account.get("balance") or 0)): continue
                old_qty = float(position.get("qty") or 0)
                add_qty = add_notional / mark
                new_qty = old_qty + add_qty
                if new_qty <= 0: continue
                position["entry"] = round((entry0 * old_qty + mark * add_qty) / new_qty, 8)
                position["qty"] = round(new_qty, 8)
                avg_entry = float(position["entry"])
                if float(position.get("sl_pct_setting") or 0) > 0:
                    p = float(position["sl_pct_setting"]) / 100.0
                    position["stop_loss"] = avg_entry * (1-p if position.get("side") == "LONG" else 1+p)
                if float(position.get("tp1_pct_setting") or 0) > 0:
                    p = float(position["tp1_pct_setting"]) / 100.0
                    position["tp1"] = avg_entry * (1+p if position.get("side") == "LONG" else 1-p)
                if float(position.get("tp2_pct_setting") or 0) > 0:
                    p = float(position["tp2_pct_setting"]) / 100.0
                    position["tp2"] = avg_entry * (1+p if position.get("side") == "LONG" else 1-p)
                position["notional"] = round(float(position.get("notional") or 0) + add_notional, 2)
                position["margin"] = round(float(position.get("margin") or 0) + add_margin, 2)
                position["dca_count"] = adds + 1
                position["auto_dca_base_usd"] = base_notional
                position.setdefault("dca_history", []).append({"price": mark, "qty": round(add_qty, 8), "notional": round(add_notional, 2), "margin": round(add_margin, 2), "created_at": now.isoformat(), "auto": True})
                position["current_price"] = mark
                _update_trade_liquidation_fields(position)
                db.save_trade(position)
        bot_closed_today = [t for t in _trades.values() if t.get("auto_trade") and t.get("status") == "CLOSED" and str(t.get("closed_at") or "").startswith(today.isoformat())]
        daily_bot_pnl = sum(float(t.get("realized_pnl") or 0) for t in bot_closed_today)
        if max_daily_loss and daily_bot_pnl <= -max_daily_loss:
            print(f"[auto-bot] daily loss guard active: {daily_bot_pnl:.2f} USDT")
            return
        max_open = min(int(cfg.get("max_open") or 2), int(getattr(config, "V5_MAX_OPEN_PAPER_TRADES", 2))) if strategy == "hybrid" else int(cfg.get("max_open") or 2)
        for sig in sorted(signals or [], key=lambda x: int(x.get("score") or 0), reverse=True):
            if len(opened) >= max_open: break
            sym, side = str(sig.get("symbol") or "").upper(), str(sig.get("side") or "").upper()
            if not sym or (symbols and sym not in symbols): continue
            direction = str(cfg.get("direction") or "both").lower()
            if direction == "long" and side != "LONG": continue
            if direction == "short" and side != "SHORT": continue
            if strategy not in (sig.get("styles") or []): continue
            if int(sig.get("score") or 0) < int(cfg.get("min_score") or 75): continue
            if float(sig.get("rr") or 0) < float(cfg.get("min_rr") or 1.5): continue
            symbol_history = [t for t in _trades.values() if t.get("auto_trade") and t.get("symbol") == sym]
            symbol_today = [t for t in symbol_history if str(t.get("created_at") or "").startswith(today.isoformat())]
            if len(symbol_today) >= max_symbol_day: continue
            recent_close = max((str(t.get("closed_at") or "") for t in symbol_history if t.get("closed_at")), default="")
            if cooldown and recent_close:
                try:
                    if (now - datetime.fromisoformat(recent_close.replace("Z", "+00:00"))).total_seconds() < cooldown * 60: continue
                except ValueError: pass
            if quality.get(str(sig.get("quality") or "B").upper(), 0) < min_quality: continue
            if any(t.get("status") in ("OPEN", "PENDING") and t.get("symbol") == sym for t in _trades.values()): continue
            entry = float((sig.get("mark_price") if strategy == "hybrid" else None) or sig.get("market_price") or sig.get("entry") or 0); sl = float(sig.get("stop_loss") or 0); tp = float(sig.get("tp1") or 0)
            if entry <= 0 or sl <= 0 or tp <= 0: continue
            if (side == "LONG" and not sl < entry < tp) or (side == "SHORT" and not tp < entry < sl): continue
            before = set(_trades)
            quantity = None
            if strategy == "hybrid":
                if not sig.get("qty_step") or sig.get("min_qty") is None or sig.get("min_notional") is None:
                    continue
                sizing = calculate_position_size(
                    balance=float(_account.get("balance") or 0),
                    risk_pct=config.V5_RISK_PER_TRADE_PCT, entry=entry, stop_loss=sl,
                    qty_step=float(sig["qty_step"]), min_qty=float(sig["min_qty"]),
                    min_notional=float(sig["min_notional"]),
                    max_qty=float(sig["max_qty"]) if sig.get("max_qty") is not None else None,
                )
                if not sizing.get("ok"):
                    continue
                quantity = sizing["quantity"]
            advanced = bool(cfg.get("advanced_enabled")) and strategy != "hybrid"
            sl_pct = float(cfg.get("advanced_sl_pct") or 0) if advanced else 0
            tp1_pct = float(cfg.get("advanced_tp1_pct") or 0) if advanced else 0
            tp2_pct = float(cfg.get("advanced_tp2_pct") or 0) if advanced else 0
            offset_pct = float(cfg.get("advanced_limit_offset_pct") or 0) if advanced else 0
            if sl_pct > 0: sl = entry * (1 - sl_pct/100 if side == "LONG" else 1 + sl_pct/100)
            if tp1_pct > 0: tp = entry * (1 + tp1_pct/100 if side == "LONG" else 1 - tp1_pct/100)
            tp2 = sig.get("tp2")
            if tp2_pct > 0: tp2 = entry * (1 + tp2_pct/100 if side == "LONG" else 1 - tp2_pct/100)
            limit_px = entry * (1 - offset_pct/100 if side == "LONG" else 1 + offset_pct/100) if offset_pct > 0 else None
            req = TradeCreate(
                symbol=sym, side=side, entry=limit_px or entry, stop_loss=sl, tp1=tp, tp2=tp2,
                order_type="LIMIT" if limit_px else "MARKET", limit_price=limit_px,
                leverage=min(_symbol_leverage_cap(sym), int(sig.get("max_leverage") or 20)),
                quantity=quantity,
                amount_usd=None if strategy == "hybrid" else float(cfg.get("amount_usd") or 50),
                risk_pct=None, advanced_enabled=advanced,
                dca_enabled=bool(cfg.get("dca_enabled")) if advanced else False,
                dca_gap_pct=cfg.get("dca_gap_pct"), dca_max_orders=cfg.get("dca_max_orders"), dca_multiplier=cfg.get("dca_multiplier"),
                dca_lower_price=None, dca_upper_price=None,
                sl_pct=sl_pct, tp1_pct=tp1_pct, tp2_pct=tp2_pct,
                profit_lock_enabled=bool(cfg.get("advanced_profit_lock")) if advanced else False,
                profit_activation_pct=cfg.get("advanced_profit_activation_pct"), profit_callback_pct=cfg.get("advanced_profit_callback_pct"),
                note="V5 · exchange-step risk-sized simulated paper trade" if strategy == "hybrid" else "Auto Bot · simulated paper trade",
            )
            result = create_trade(req)
            if isinstance(result, dict) and result.get("ok"):
                for tid in set(_trades) - before:
                    trade = _trades.get(tid)
                    if trade:
                        trade["auto_trade"] = True
                        trade["trade_style"] = strategy
                        trade["entry_source"] = "BOT"
                        trade["auto_dca_base_usd"] = float(cfg.get("amount_usd") or 50)
                        rate = float(cfg.get("trailing_pct") or 0)
                        if rate >= 0.1: trade["callback_rate"] = min(rate, 5.0)
                        if advanced and cfg.get("advanced_profit_lock"):
                            trade["callback_rate"] = float(cfg.get("advanced_profit_callback_pct") or 0.5)
                            trade["profit_lock_enabled"] = True
                            trade["profit_activation_pct"] = float(cfg.get("advanced_profit_activation_pct") or 0.8)
                        db.save_trade(trade); opened.append(trade)
                        break
        if cfg.get("exit_on_opposite"):
            latest = {str(x.get("symbol")): x for x in signals or []}
            for trade in list(_trades.values()):
                sig = latest.get(str(trade.get("symbol")))
                if trade.get("status") != "OPEN" or not trade.get("auto_trade") or not sig or sig.get("side") == trade.get("side") or int(sig.get("score") or 0) < int(cfg.get("min_score") or 75): continue
                price = float(_cache.get("prices", {}).get(trade.get("symbol")) or trade.get("current_price") or trade.get("entry") or 0); qty = float(trade.get("qty") or 0); entry = float(trade.get("entry") or 0)
                pnl = (price-entry)*qty if trade.get("side") == "LONG" else (entry-price)*qty
                _account["balance"] = round(float(_account.get("balance") or 0)+pnl, 2); _account["realized_pnl"] = round(float(_account.get("realized_pnl") or 0)+pnl, 2)
                _freeze_trade_market_context(trade)
                trade.update({"status":"CLOSED", "hit":"AUTO_SIGNAL_EXIT", "close_price":price, "realized_pnl":round(float(trade.get("realized_pnl") or 0)+pnl,4), "unrealized_pnl":0.0, "closed_at":datetime.now(timezone.utc).isoformat(), "needs_alert":True}); db.save_trade(trade)
        _calc_equity(); db.save_account(_account)
    except Exception as exc: print("[auto-bot] safe cycle error:", exc)

def run_scan(top_n: int = 12):
    global _scan_retry_after, _market_structure_rotation
    top_n = max(5, min(int(top_n or 26), 26))
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
        for pair_symbol, _ in pairs:
            precision = client.get_price_precision(pair_symbol)
            if precision is not None:
                _symbol_price_precision[pair_symbol] = precision
        for existing_trade in _trades.values():
            trade_symbol = str(existing_trade.get("symbol") or "")
            if existing_trade.get("status") in ("OPEN", "CLOSED") and trade_symbol and trade_symbol not in _symbol_price_precision:
                precision = client.get_price_precision(trade_symbol)
                if precision is not None:
                    _symbol_price_precision[trade_symbol] = precision
        try:
            mv = client.get_movers(top_n=1000, min_volume=0)
            _cache["gainers"] = mv.get("gainers", [])
            _cache["losers"] = mv.get("losers", [])
        except Exception as e:
            print("movers", e)

        if client.rate_limited:
            _cache["error"] = "Binance Futures rate limit active; scans will retry automatically after a cooldown."
            _pause_scans_for_rate_limit(client)
            return

        signals, prices = [], {}
        # BTC 24h relative-strength baseline is fetched once per scan.
        btc_df = client.fetch_ohlcv("BTCUSDT", timeframe="1h", limit=25)
        btc_regime_df = client.fetch_ohlcv("BTCUSDT", timeframe="4h", limit=220)
        btc_regime = _classify_btc_regime(btc_regime_df)
        btc_return = None
        if btc_df is not None and len(btc_df) >= 25 and float(btc_df["close"].iloc[-25]) > 0:
            btc_return = (float(btc_df["close"].iloc[-1]) / float(btc_df["close"].iloc[-25]) - 1.0) * 100.0
        for symbol, vol in pairs:
            data = {}
            scan_timeframes = ("15m", "1h", "4h", "1d") if getattr(config, "V5_ENABLED", False) else ("5m", "15m", "1h", "4h", "1d")
            for tf in scan_timeframes:
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
                _scan_max_leverage[symbol] = max_leverage
                if px:
                    prices[symbol] = px
                for style_name in (("hybrid",) if getattr(config, "V5_ENABLED", False) else ("scalp", "day", "swing")):
                    r = engine.analyze_pair(symbol, data, volume_24h=vol, style=style_name)
                    if not r: continue
                    r["timeframe_trends"] = _timeframe_trend_map(data)
                    r["entry_timeframe"] = {"scalp": "5m", "day": "15m", "swing": "1h", "hybrid": "15m"}.get(style_name, "15m")
                    r["market_price"] = px or r["entry"]
                    daily_change_pct = client.get_24h_change_pct(symbol)
                    if daily_change_pct is not None:
                        r["daily_change_pct"] = daily_change_pct
                    r["max_leverage"] = max_leverage
                    # Compare the coin's 24h return with BTC to avoid buying relative laggards.
                    coin_df = data.get("1h")
                    relative_strength = None
                    if btc_return is not None and coin_df is not None and len(coin_df) >= 25:
                        base_close = float(coin_df["close"].iloc[-25])
                        if base_close > 0:
                            coin_return = (float(coin_df["close"].iloc[-1]) / base_close - 1.0) * 100.0
                            relative_strength = coin_return - btc_return
                            r["coin_return_24h_pct"] = round(coin_return, 2)
                            r["btc_relative_strength_pct"] = round(relative_strength, 2)
                            if r["side"] == "LONG" and relative_strength < -1.5:
                                r["score"] = max(0, int(r["score"]) - 5)
                                r["reasons"].append("BTC relative lag")
                            elif r["side"] == "SHORT" and relative_strength > 1.5:
                                r["score"] = max(0, int(r["score"]) - 5)
                                r["reasons"].append("BTC relative strength")
                            elif (r["side"] == "LONG" and relative_strength > 1.5) or (r["side"] == "SHORT" and relative_strength < -1.5):
                                r["score"] = min(100, int(r["score"]) + 3)
                                r["reasons"].append("BTC relative strength aligned")
                    r["styles"] = [style_name]
                    r["summary"] = f"{r.get('setup','')} · {style_name.upper()} · RR TP1 1:{r['rr']} / TP2 1:{r.get('rr_tp2', 0)} · SL {r['risk_pct']}% · TP1 {r['tp1_pct']}%"
                    signals.append(r)
            except Exception as e:
                print(f"scan {symbol}: {e}")
                continue

        # Query derivatives context only for the highest-ranked unique symbols to
        # keep public API request weight bounded; funding is a mild crowding filter.
        for item in signals:
            item["market_regime"] = btc_regime
            if (btc_regime == "BULL" and item["side"] == "SHORT") or (btc_regime == "BEAR" and item["side"] == "LONG"):
                item["score"] = max(0, int(item["score"]) - 7)
                item["reasons"].append(f"Against BTC {btc_regime} regime")
            elif (btc_regime == "BULL" and item["side"] == "LONG") or (btc_regime == "BEAR" and item["side"] == "SHORT"):
                item["score"] = min(100, int(item["score"]) + 2)
                item["reasons"].append(f"Aligned BTC {btc_regime} regime")

        checked_derivatives = set()
        priority_symbols = [_market_symbol_key(t.get("symbol")) for t in _trades.values() if t.get("status") == "OPEN"]
        # Rotate the signal universe so lower-ranked coins also receive market-data refreshes.
        signal_symbols = list(dict.fromkeys(_market_symbol_key(x.get("symbol")) for x in sorted(signals, key=lambda x: x["score"], reverse=True)))
        if signal_symbols:
            start_at = _market_structure_rotation % len(signal_symbols)
            signal_symbols = signal_symbols[start_at:] + signal_symbols[:start_at]
            _market_structure_rotation = (start_at + 10) % len(signal_symbols)
        priority_symbols += signal_symbols
        for sym in dict.fromkeys(priority_symbols):
            if not sym or len(checked_derivatives) >= 10 or client.rate_limited:
                continue
            checked_derivatives.add(sym)
            context = client.get_global_derivatives_context(sym)
            book = client.get_order_book_context(sym) if not client.rate_limited else {}
            structure = client.get_market_structure_context(sym) if not client.rate_limited else {}
            market_pressure = client.get_market_pressure_context(sym) if not client.rate_limited else {"timeframes": {}}
            multi_exchange = get_multi_exchange_context(sym)
            market_pressure["multi_exchange"] = multi_exchange
            mark_price = (context.get("mark_price") or client.get_mark_price(sym)) if getattr(config, "V5_ENABLED", False) and not client.rate_limited else None
            constraints = client.get_order_constraints(sym) if getattr(config, "V5_ENABLED", False) and not client.rate_limited else {}
            funding = context.get("funding_rate")
            oi_change = context.get("oi_change_pct")
            # Cache for My Trades independently of whether this symbol produced a signal.
            daily_context = client.get_daily_market_context(sym, prices.get(sym))
            _daily_market_context_cache[_market_symbol_key(sym)] = daily_context
            first_signal = next((x for x in signals if x["symbol"] == sym), {})
            sr_context = {}
            sr_df = None
            if first_signal and not client.rate_limited:
                strategy_tf = {"scalp": "15m", "day": "1h", "swing": "4h", "hybrid": "1h"}.get((first_signal.get("styles") or ["day"])[0], "1h")
                sr_df = client.fetch_ohlcv(sym, timeframe=strategy_tf, limit=120)
                sr_context = _nearest_support_resistance(sr_df, prices.get(sym) or first_signal.get("market_price") or first_signal.get("entry"))
            structure_snapshot = {
                **book, **structure, **daily_context, **sr_context,
                "market_pressure": market_pressure,
                "liquidations": get_liquidation_summary(sym),
                "funding_rate": funding,
                "oi_change_1h_pct": round(oi_change, 2) if oi_change is not None else None,
                "open_interest": context.get("open_interest"),
                "open_interest_usd": context.get("open_interest_usd"),
                "oi_change_24h_pct": context.get("oi_change_24h_pct"),
                "oi_updated_at": context.get("oi_updated_at"),
                "mark_price": mark_price,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            _market_structure_cache[_market_symbol_key(sym)] = structure_snapshot
            _market_structure_refresh_at[_market_symbol_key(sym)] = time_mod.monotonic()
            # Keep running-trade market context live; closed trades are intentionally immutable.
            for position in _trades.values():
                if position.get("status") == "OPEN" and _market_symbol_key(position.get("symbol")) == _market_symbol_key(sym):
                    position["market_structure"] = dict(structure_snapshot)
                    for field in ("daily_open", "daily_high", "daily_low", "change_7d_pct", "change_30d_pct", "change_1y_pct"):
                        if structure_snapshot.get(field) is not None:
                            position[field] = structure_snapshot[field]
                    db.save_trade(position)
            imbalance = book.get("book_imbalance_pct")
            spread = book.get("spread_pct")
            for item in signals:
                if item["symbol"] != sym:
                    continue
                item.update(daily_context)
                strategy_tf = {"scalp": "15m", "day": "1h", "swing": "4h", "hybrid": "1h"}.get((item.get("styles") or ["day"])[0], "1h")
                item.update(sr_context if sr_context else _nearest_support_resistance(sr_df, prices.get(sym) or item.get("market_price") or item.get("entry")))
                item["funding_rate"] = funding
                item["oi_sources"] = context.get("oi_sources") or ["Binance"]
                item["oi_coverage"] = context.get("oi_coverage", 1)
                item["open_interest"] = context.get("open_interest")
                item["oi_change_1h_pct"] = round(oi_change, 2) if oi_change is not None else None
                item["orderbook_imbalance_pct"] = round(imbalance, 2) if imbalance is not None else None
                item["spread_pct"] = round(spread, 4) if spread is not None else None
                item["market_structure"] = structure_snapshot
                item["price_precision"] = _symbol_price_precision.get(sym)
                item["mark_price"] = mark_price
                item["qty_step"] = constraints.get("qty_step")
                item["min_qty"] = constraints.get("min_qty")
                item["max_qty"] = constraints.get("max_qty")
                item["min_notional"] = constraints.get("min_notional")
                item["market_regime"] = btc_regime
                momentum = float(item.get("coin_return_24h_pct") or 0)
                if funding is not None and funding >= 0.0005 and item["side"] == "LONG":
                    item["score"] = max(0, int(item["score"]) - 4)
                    item["reasons"].append("Crowded long funding")
                elif funding is not None and funding <= -0.0005 and item["side"] == "SHORT":
                    item["score"] = max(0, int(item["score"]) - 4)
                    item["reasons"].append("Crowded short funding")
                if oi_change is not None and oi_change >= 3:
                    if (item["side"] == "LONG" and momentum > 0) or (item["side"] == "SHORT" and momentum < 0):
                        item["score"] = min(100, int(item["score"]) + 2)
                        item["reasons"].append("OI rising with price momentum")
                    elif (item["side"] == "LONG" and momentum < 0) or (item["side"] == "SHORT" and momentum > 0):
                        item["score"] = max(0, int(item["score"]) - 2)
                        item["reasons"].append("OI/price divergence")
                if imbalance is not None:
                    if (item["side"] == "LONG" and imbalance <= -25) or (item["side"] == "SHORT" and imbalance >= 25):
                        item["score"] = max(0, int(item["score"]) - 4)
                        item["reasons"].append("Order-book pressure against signal")
                if spread is not None and spread >= 0.10:
                    item["score"] = max(0, int(item["score"]) - 4)
                    item["reasons"].append("Wide order-book spread")

        if client.rate_limited:
            _cache["error"] = "Binance Futures rate limit active during derivatives checks; remaining checks paused."
            _pause_scans_for_rate_limit(client)
        # V5 uses its independent 100-point evidence model and hard execution filters.
        filtered = []
        for item in signals:
            if "hybrid" in (item.get("styles") or []):
                item, qualified, reason = qualify_v5_signal(item)
                if not qualified:
                    continue
            else:
                if int(item.get("score", 0)) < config.MIN_SCORE:
                    continue
                aligned = ((item.get("side") == "LONG" and item.get("bias_1d") == "bull" and item.get("bias_4h") == "bull") or
                           (item.get("side") == "SHORT" and item.get("bias_1d") == "bear" and item.get("bias_4h") == "bear"))
                item["quality"] = "A+" if item["score"] >= 72 and aligned else ("A" if item["score"] >= 62 or aligned else "B")
            filtered.append(item)
        signals = filtered
        signals.sort(key=lambda x: x["score"], reverse=True)
        # Advisory-only position review from the latest completed signal analysis.
        latest_by_symbol = {str(x.get("symbol")): x for x in signals}
        for position in list(_trades.values()):
            if position.get("status") != "OPEN":
                continue
            candidate = latest_by_symbol.get(str(position.get("symbol")))
            if not candidate:
                continue
            mark = float(prices.get(position.get("symbol")) or position.get("current_price") or 0)
            entry = float(position.get("entry") or 0)
            pnl = float(position.get("unrealized_pnl") or 0)
            opposite = str(candidate.get("side") or "").upper() != str(position.get("side") or "").upper()
            score = int(candidate.get("score") or 0)
            advice = None
            if opposite and score >= 75:
                advice = "OPPOSITE SIGNAL · Review closing or reducing this position"
                side_now = str(position.get("side") or "").upper()
                profitable_side = mark > entry if side_now == "LONG" else (mark < entry if side_now == "SHORT" else False)
                if pnl > 0 and profitable_side and entry > 0:
                    old_sl = float(position.get("stop_loss") or 0)
                    improves = old_sl < entry if side_now == "LONG" else (old_sl > entry if side_now == "SHORT" else False)
                    if improves:
                        position["stop_loss"] = entry
                        position["auto_sl_protected"] = True
                        advice = "PROFIT PROTECTED · SL moved to breakeven after opposite signal"
                        db.save_trade(position)
            elif not opposite and score >= 75 and pnl < 0:
                advice = "DCA WATCH · Directional analysis still agrees; review risk before adding"
            elif pnl > 0 and entry > 0 and mark > 0:
                side_now = str(position.get("side") or "").upper()
                favorable = mark > entry if side_now == "LONG" else mark < entry
                profit_pct = abs(mark-entry)/entry*100 if favorable else 0
                if favorable and profit_pct >= 0.5:
                    callback_pct = max(0.2, min(1.5, profit_pct * 0.25))
                    activation_pct = max(0.5, min(profit_pct * 0.5, profit_pct))
                    position["trailing_recommendation"] = {"activation_profit_pct": round(activation_pct,2), "callback_pct": round(callback_pct,2), "based_on_profit_pct": round(profit_pct,2), "mark_price": mark}
                    advice = f"PROFIT TRAIL SUGGESTION · Activate +{activation_pct:.2f}% · Callback {callback_pct:.2f}% (review before applying)"
            if advice:
                position["analysis_alert"] = advice
                position["analysis_alert_at"] = datetime.now(timezone.utc).isoformat()
                db.save_trade(position)
            elif position.get("analysis_alert") or position.get("trailing_recommendation"):
                position["analysis_alert"] = None
                position["trailing_recommendation"] = None
                position["analysis_alert_at"] = datetime.now(timezone.utc).isoformat()
                db.save_trade(position)
        _cache["signals"] = signals
        _cache["prices"] = prices
        _run_paper_auto_bot(signals)
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
            # Wait 90 seconds after each completed scan; the scan lock prevents overlap.
            for _ in range(9):
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
                if t.get("advanced_enabled"):
                    for key in ("advanced_enabled","advanced_dca_enabled","dca_gap_pct","dca_max_orders","dca_multiplier","dca_lower_price","dca_upper_price","auto_dca_base_usd","dca_reference_entry","sl_pct_setting","tp1_pct_setting","tp2_pct_setting","profit_lock_enabled","profit_activation_pct","profit_callback_pct","callback_rate","trailing_pct"):
                        if key in t: matching[key] = t[key]
                    avg = float(matching.get("entry") or 0); s0 = str(matching.get("side") or "").upper()
                    if float(matching.get("sl_pct_setting") or 0)>0:
                        p=float(matching["sl_pct_setting"])/100; matching["stop_loss"]=avg*(1-p if s0=="LONG" else 1+p)
                    if float(matching.get("tp1_pct_setting") or 0)>0:
                        p=float(matching["tp1_pct_setting"])/100; matching["tp1"]=avg*(1+p if s0=="LONG" else 1-p)
                    if float(matching.get("tp2_pct_setting") or 0)>0:
                        p=float(matching["tp2_pct_setting"])/100; matching["tp2"]=avg*(1+p if s0=="LONG" else 1-p)
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

        # Optional per-position DCA: adverse move from latest fill, bounded by count and optional range.
        if t.get("advanced_dca_enabled") and not t.get("auto_trade") and int(t.get("dca_count") or 0) < int(t.get("dca_max_orders") or 3):
            adds = int(t.get("dca_count") or 0); hist = t.get("dca_history") or []
            anchor = float(hist[-1].get("price") or entry) if hist else entry
            gap = max(0.001, min(float(t.get("dca_gap_pct") or 1.5), 20.0)) / 100.0
            adverse = price <= anchor * (1-gap) if side == "LONG" else price >= anchor * (1+gap)
            lower, upper = float(t.get("dca_lower_price") or 0), float(t.get("dca_upper_price") or 0)
            in_range = (not lower or price >= lower) and (not upper or price <= upper)
            base = float(t.get("auto_dca_base_usd") or t.get("notional") or 0)
            add_notional = base * (max(1.0, min(float(t.get("dca_multiplier") or 1.5), 3.0)) ** adds)
            add_margin = add_notional / max(1, int(t.get("leverage") or 1))
            if adverse and in_range and add_notional > 0 and add_margin <= max(0.0, float(_account.get("balance") or 0)):
                add_qty = add_notional / price; old_qty = float(t.get("qty") or 0); new_qty = old_qty + add_qty
                old_entry = float(t.get("entry") or entry)
                if new_qty > 0:
                    t["entry"] = round((old_entry*old_qty + price*add_qty)/new_qty, 8); t["qty"] = round(new_qty, 8)
                    t["notional"] = round(float(t.get("notional") or 0)+add_notional, 2); t["margin"] = round(float(t.get("margin") or 0)+add_margin, 2)
                    t["dca_count"] = adds+1; t.setdefault("dca_history", []).append({"price":price,"qty":round(add_qty,8),"notional":round(add_notional,2),"margin":round(add_margin,2),"created_at":datetime.now(timezone.utc).isoformat(),"auto":True})
                    entry = float(t["entry"]); qty = float(t["qty"])
                    if float(t.get("sl_pct_setting") or 0)>0:
                        p=float(t["sl_pct_setting"])/100; t["stop_loss"]=entry*(1-p if side=="LONG" else 1+p)
                    if float(t.get("tp1_pct_setting") or 0)>0:
                        p=float(t["tp1_pct_setting"])/100; t["tp1"]=entry*(1+p if side=="LONG" else 1-p)
                    if float(t.get("tp2_pct_setting") or 0)>0:
                        p=float(t["tp2_pct_setting"])/100; t["tp2"]=entry*(1+p if side=="LONG" else 1-p)
                    print(f"[trade-dca] {t.get('symbol')} add {adds+1} @ {price}")
        callback = float(t.get("callback_rate") or t.get("trailing_pct") or 0)
        activation = float(t.get("trailing_activation_price") or 0)
        if t.get("profit_lock_enabled") and float(t.get("profit_activation_pct") or 0)>0:
            ap=float(t.get("profit_activation_pct") or 0.8)/100
            activation=entry*(1+ap if side=="LONG" else 1-ap)
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
            _freeze_trade_market_context(t)
            t.update({
                "status": "CLOSED", "hit": trigger, "unrealized_pnl": 0.0,
                "close_price": float(t.get("current_price") or t.get("entry") or 0),
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


def daily_open_cache_loop():
    """Refresh rolling 24h ticker change off-request so My Trades stays responsive."""
    client = None
    while _monitor_running:
        try:
            symbols = sorted({
                str(t.get("symbol") or "").upper().replace("/", "").replace(":USDT", "")
                for t in _trades.values()
                if t.get("status") == "OPEN" and t.get("symbol")
            })
            now = time_mod.time()
            for symbol in symbols:
                cached = _daily_open_cache.get(symbol)
                if cached and now - float(cached.get("ts", 0)) < 60:
                    continue
                try:
                    client = client or BinanceClient(futures=True)
                    daily_change = client.get_24h_change_pct(symbol)
                    if daily_change is not None:
                        _daily_open_cache[symbol] = {"change": daily_change, "ts": now}
                except Exception as exc:
                    print(f"[trade-daily-change] {symbol}: {exc}")
        except Exception as exc:
            print(f"[trade-daily-cache] cycle failed: {exc}")
        time_mod.sleep(15)


@app.on_event("startup")
def startup():
    global _monitor_running, _account, _trades
    db.init_db(getattr(config, "DEMO_BALANCE", 10000))
    _account = db.load_account()
    _trades = db.load_trades()
    # Restore the user's saved preference; auto-scan state must not be reset on deploy.
    try:
        _cache["auto_scan"] = db.get_meta("auto_scan", "1") == "1"
        try:
            _bot_config.update(json.loads(db.get_meta("auto_trade_config", "{}") or "{}"))
        except Exception: pass
        saved_ban_until = float(db.get_meta("binance_ban_until", "0") or 0)
        if saved_ban_until > time_mod.time():
            global _scan_retry_after
            _scan_retry_after = time_mod.monotonic() + (saved_ban_until - time_mod.time())
            print(f"[binance] persisted cooldown active for {int(saved_ban_until - time_mod.time())}s")
    except Exception as e:
        print(f"[startup] preference/cooldown restore failed: {e}")
        _cache["auto_scan"] = True
    _monitor_running = True
    start_liquidation_feed()
    threading.Thread(target=auto_scan_loop, daemon=True).start()
    threading.Thread(target=price_monitor_loop, daemon=True).start()
    threading.Thread(target=daily_open_cache_loop, daemon=True).start()


@app.on_event("shutdown")
def shutdown():
    global _monitor_running
    _monitor_running = False
    _persist()


@app.get("/api/market-structure/{symbol}")
def api_market_structure(symbol: str):
    """Refresh per-symbol market context at a bounded cadence for the live Signals view."""
    clean = _market_symbol_key(symbol)
    now = time_mod.monotonic()
    cached = _market_structure_cache.get(clean) or {}
    if cached and now - _market_structure_refresh_at.get(clean, 0.0) < 30.0:
        return {"ok": True, "market_structure": cached, "cached": True}
    client = BinanceClient(futures=True)
    if client.rate_limited:
        return {"ok": bool(cached), "market_structure": cached, "cached": True,
                "warning": "Binance rate limit active; showing last available snapshot"}
    derivatives = client.get_derivatives_context(clean)
    book = client.get_order_book_context(clean)
    crowd = client.get_market_structure_context(clean)
    pressure = client.get_market_pressure_context(clean)
    pressure["multi_exchange"] = get_multi_exchange_context(clean)
    daily_context = client.get_daily_market_context(clean, client.get_current_price(clean))
    snapshot = {
        **book, **crowd, **daily_context,
        "market_pressure": pressure,
        "liquidations": get_liquidation_summary(clean),
        "funding_rate": derivatives.get("funding_rate"),
        "oi_change_1h_pct": derivatives.get("oi_change_pct"),
        "open_interest": derivatives.get("open_interest"),
        "open_interest_usd": derivatives.get("open_interest_usd"),
        "oi_change_24h_pct": derivatives.get("oi_change_24h_pct"),
        "oi_updated_at": derivatives.get("oi_updated_at"),
        "mark_price": derivatives.get("mark_price"),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _market_structure_cache[clean] = snapshot
    _market_structure_refresh_at[clean] = now
    return {"ok": True, "market_structure": snapshot, "cached": False}


@app.get("/api/signals")
async def api_signals(style: str = Query("all")):
    open_keys = {(t["symbol"], t["side"]) for t in _trades.values() if t.get("status") == "OPEN"}
    out = []
    for s in _cache["signals"]:
        s2 = dict(s)
        s2["already_added"] = (s["symbol"], s["side"]) in open_keys
        s2["market_structure"] = dict(_market_structure_cache.get(_market_symbol_key(s2.get("symbol"))) or s2.get("market_structure") or {})
        s2["market_structure"]["liquidations"] = get_liquidation_summary(s2.get("symbol"))
        s2["price_precision"] = _symbol_price_precision.get(str(s2.get("symbol") or ""))
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


@app.get("/api/auto-trade")
async def get_auto_trade():
    return {"config": _bot_config, "engine": "SIMULATED_PAPER_ONLY"}

@app.post("/api/auto-trade")
async def set_auto_trade(body: Dict[str, Any]):
    try:
        _bot_config.update({"advanced_enabled": bool(body.get("advanced_enabled", False)), "advanced_sl_pct": max(0.0,min(float(body.get("advanced_sl_pct",0)),50.0)), "advanced_tp1_pct": max(0.0,min(float(body.get("advanced_tp1_pct",0)),100.0)), "advanced_tp2_pct": max(0.0,min(float(body.get("advanced_tp2_pct",0)),200.0)), "advanced_limit_offset_pct": max(0.0,min(float(body.get("advanced_limit_offset_pct",0)),20.0)), "advanced_profit_lock": bool(body.get("advanced_profit_lock",False)), "advanced_profit_activation_pct": max(0.1,min(float(body.get("advanced_profit_activation_pct",0.8)),50.0)), "advanced_profit_callback_pct": max(0.1,min(float(body.get("advanced_profit_callback_pct",0.5)),5.0)), "dca_range_min_pct": max(0.0,min(float(body.get("dca_range_min_pct",0)),50.0)), "dca_range_max_pct": max(0.0,min(float(body.get("dca_range_max_pct",0)),100.0)), "enabled": bool(body.get("enabled", False)), "symbols": str(body.get("symbols") or "").upper().replace(" ", "")[:500], "min_score": max(60,min(int(body.get("min_score",75)),100)), "min_quality": str(body.get("min_quality") or "A").upper(), "amount_usd": max(5.0,min(float(body.get("amount_usd",50)),1000000.0)), "max_open": max(1,min(int(body.get("max_open",2)),10)), "trailing_pct": max(0.0,min(float(body.get("trailing_pct",0.8)),5.0)), "exit_on_opposite": bool(body.get("exit_on_opposite",True)), "trade_style": str(body.get("trade_style") or "day").lower(), "cooldown_minutes": max(0,min(int(body.get("cooldown_minutes",30)),1440)), "max_symbol_trades_day": max(1,min(int(body.get("max_symbol_trades_day",3)),30)), "max_daily_loss_pct": max(0.0,min(float(body.get("max_daily_loss_pct",2.0)),20.0)), "min_rr": max(1.0,min(float(body.get("min_rr",1.5)),10.0)), "direction": str(body.get("direction") or "both").lower(), "dca_enabled": bool(body.get("dca_enabled", False)), "dca_gap_pct": max(0.1,min(float(body.get("dca_gap_pct",1.5)),20.0)), "dca_max_orders": max(1,min(int(body.get("dca_max_orders",3)),5)), "dca_multiplier": max(1.0,min(float(body.get("dca_multiplier",2.0)),3.0))})
        if _bot_config["dca_range_max_pct"] and _bot_config["dca_range_max_pct"] < _bot_config["dca_range_min_pct"]:
            return JSONResponse({"ok":False,"error":"DCA range maximum must be greater than minimum"},status_code=400)
        if _bot_config["trade_style"] not in ("scalp","day","swing"): _bot_config["trade_style"] = "day"
        if _bot_config["min_quality"] not in ("B","A","A+"): _bot_config["min_quality"] = "A"
        if _bot_config["direction"] not in ("both","long","short"): _bot_config["direction"] = "both"
    except (TypeError,ValueError): return JSONResponse({"ok":False,"error":"Invalid Auto Trade settings"},status_code=400)
    db.set_meta("auto_trade_config", json.dumps(_bot_config))
    return {"ok":True,"config":_bot_config,"engine":"SIMULATED_PAPER_ONLY"}

@app.post("/api/auto-scan")
async def set_auto_scan(enabled: bool = Query(True)):
    _cache["auto_scan"] = bool(enabled)
    db.set_meta("auto_scan", "1" if enabled else "0")
    if enabled and not _cache["scanning"]:
        threading.Thread(target=lambda: run_scan(26), daemon=True).start()
    return {"ok": True, "auto_scan": _cache["auto_scan"]}


@app.get("/api/analyze/{symbol}")
def analyze_one(symbol: str, style: str = Query("day")):
    if time_mod.monotonic() < _scan_retry_after:
        retry_after = max(1, int(_scan_retry_after - time_mod.monotonic()))
        return JSONResponse({"ok": False, "error": "Binance Futures IP cooldown active; manual analysis is paused."}, status_code=429, headers={"Retry-After": str(retry_after)})
    sym = symbol.upper().replace("/", "").replace("-", "")
    if not sym.endswith("USDT"):
        sym += "USDT"
    selected_style = style if style in ("scalp", "day", "swing", "hybrid") else "day"
    client = BinanceClient(futures=True)
    engine = SignalEngine()
    # Fetch only the timeframes required by the selected strategy. This keeps
    # manual analysis independent of Auto Scan and reduces Binance request weight.
    required_tfs = {
        "scalp": ("5m", "15m", "1h", "4h", "1d"),
        "day": ("15m", "1h", "4h", "1d"),
        "swing": ("1h", "4h", "1d"),
        "hybrid": ("15m", "1h", "4h", "1d"),
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
    volume_24h = client.get_quote_volume_24h(sym) or 0
    r = engine.analyze_pair(sym, data, volume_24h=volume_24h, style=selected_style)
    if r and selected_style == "hybrid":
        derivatives = client.get_derivatives_context(sym)
        book = client.get_order_book_context(sym) if not client.rate_limited else {}
        r["funding_rate"] = derivatives.get("funding_rate")
        r["open_interest"] = derivatives.get("open_interest")
        r["oi_change_1h_pct"] = derivatives.get("oi_change_pct")
        r["orderbook_imbalance_pct"] = book.get("book_imbalance_pct")
        r["spread_pct"] = book.get("spread_pct")
        mark_price = derivatives.get("mark_price") or client.get_mark_price(sym)
        r, qualified, reason = qualify_v5_signal(r, {
            "funding_rate": r.get("funding_rate"),
            "oi_change_pct": r.get("oi_change_1h_pct"),
            "book_imbalance_pct": r.get("orderbook_imbalance_pct"),
            "spread_pct": r.get("spread_pct"),
            "mark_price": mark_price,
        })
        if not qualified:
            r = None
    px = client.get_current_price(sym)
    lev = client.get_max_leverage(sym)
    _scan_max_leverage[sym] = lev
    liq = ("VERY HIGH" if volume_24h >= 1e8 else "HIGH" if volume_24h >= 4e7 else "MED" if volume_24h >= 1.2e7 else "LOW" if volume_24h > 0 else "UNKNOWN")
    if not r:
        entry_tf = {"scalp": "5m", "day": "15m", "swing": "1h", "hybrid": "15m"}[selected_style]
        latest = get_latest(add_indicators(data[entry_tf]))
        indicators = {"adx": round(float(latest.get("adx") or 0), 1), "rsi": round(float(latest.get("rsi") or 0), 1)}
        trends = _timeframe_trend_map(data)
        return {"ok": True, "signal": None, "symbol": sym, "market_price": px,
                "max_leverage": lev, "liquidity": liq, "indicators": indicators,
                "timeframe_trends": trends, "bias_1d": trends.get("1d", "SIDE"),
                "bias_4h": trends.get("4h", "SIDE"),
                "message": f"NO TRADE — no {selected_style} confluence now"}
    r["market_price"] = (r.get("mark_price") if selected_style == "hybrid" else None) or px or r["entry"]
    r["timeframe_trends"] = _timeframe_trend_map(data)
    r["price_precision"] = client.get_price_precision(sym)
    if r["price_precision"] is not None:
        _symbol_price_precision[sym] = r["price_precision"]
    daily_context = client.get_daily_market_context(sym, px)
    _daily_market_context_cache[_market_symbol_key(sym)] = daily_context
    strategy_tf = {"scalp": "15m", "day": "1h", "swing": "4h", "hybrid": "1h"}[selected_style]
    r.update(daily_context)
    r.update(_nearest_support_resistance(data.get(strategy_tf), px or r.get("entry")))
    r["market_structure"] = {**daily_context, **client.get_order_book_context(sym), **client.get_market_structure_context(sym), **client.get_derivatives_context(sym)}
    r["market_structure"].update({k: r.get(k) for k in ("support", "resistance")})
    r["market_structure"]["market_pressure"] = client.get_market_pressure_context(sym)
    multi_exchange = get_multi_exchange_context(sym)
    r["market_structure"]["market_pressure"]["multi_exchange"] = multi_exchange
    r["market_structure"]["liquidations"] = get_liquidation_summary(sym)
    _market_structure_cache[_market_symbol_key(sym)] = r["market_structure"]
    # Match Binance USDⓈ-M rolling 24h ticker percentage, not the UTC 1d candle open.
    change_24h_pct = client.get_24h_change_pct(sym)
    if change_24h_pct is not None:
        r["daily_change_pct"] = change_24h_pct
    r["max_leverage"] = lev
    r["liquidity"] = liq
    r["styles"] = [selected_style]
    r["summary"] = f"{r.get('setup','')} · {selected_style.upper()} · RR TP1 1:{r['rr']} / TP2 1:{r.get('rr_tp2', 0)} · SL {r['risk_pct']}% · TP1 {r['tp1_pct']}%"
    return {"ok": True, "signal": r}


@app.get("/api/v5/forward-validation")
def api_v5_forward_validation(start: Optional[str] = None):
    """Report elapsed V5 paper-forward evidence; never labels an immature sample as validated."""
    end = datetime.now(timezone.utc)
    if start:
        try:
            start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return JSONResponse({"ok": False, "error": "start must be an ISO-8601 datetime"}, status_code=400)
    else:
        from datetime import timedelta
        start_dt = end - timedelta(days=30)
    result = evaluate_forward_trades(_trades.values(), start=start_dt, end=end, minimum_days=30)
    return {"ok": True, "validation": result}


@app.get("/api/account")
async def api_account():
    _calc_equity()
    return {**_account, "open_trades": sum(1 for t in _trades.values() if t["status"] == "OPEN"), "stats": _stats()}


@app.post("/api/account/funds")
async def adjust_account_funds(body: FundsAdjustment):
    action = str(body.action or "").strip().lower()
    amount = float(body.amount or 0)
    if action not in ("add", "remove"):
        return JSONResponse({"ok": False, "error": "Choose Add or Remove funds"}, status_code=400)
    if not (amount > 0 and amount < float("inf")):
        return JSONResponse({"ok": False, "error": "Enter a valid amount greater than zero"}, status_code=400)
    delta = amount if action == "add" else -amount
    if _account["balance"] + delta < 0:
        return JSONResponse({"ok": False, "error": "Insufficient account balance to remove that amount"}, status_code=400)
    _account["balance"] = round(float(_account["balance"]) + delta, 2)
    _account["start_balance"] = round(float(_account.get("start_balance") or 0) + delta, 2)
    _calc_equity()
    for position in _trades.values():
        if position.get("status") == "OPEN":
            _update_trade_liquidation_fields(position)
            db.save_trade(position)
    db.save_account(_account)
    return {"ok": True, "action": action, "amount": amount, "account": _account}


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


@app.delete("/api/account/data")
async def clear_all_account_data(body: ResetDataBody):
    if not secrets.compare_digest(str(body.password or ""), _APP_PASSWORD):
        return JSONResponse({"ok": False, "error": "Incorrect password"}, status_code=403)
    _trades.clear()
    db.save_all_trades({})
    start = float(getattr(config, "DEMO_BALANCE", 10000.0))
    _account.update({"balance": start, "equity": start, "realized_pnl": 0.0, "start_balance": start})
    db.save_account(_account)
    return {"ok": True, "deleted": True, "account": _account}


@app.get("/api/trades")
async def api_trades():
    # This endpoint must remain local/fast: never make Binance network calls while
    # rendering the trade list. Daily-open context is best-effort from background
    # cache only; live mark prices are maintained by price_monitor_loop().
    _calc_equity()
    trades = [dict(trade) for trade in _trades.values()]
    for trade in trades:
        symbol = _market_symbol_key(trade.get("symbol"))
        trade["price_precision"] = _symbol_price_precision.get(symbol)
        trade["market_structure"] = dict(_market_structure_cache.get(symbol) or {})
        trade.update(_daily_market_context_cache.get(symbol) or {})
        trade["market_structure"].update(_daily_market_context_cache.get(symbol) or {})
        trade["market_structure"]["liquidations"] = get_liquidation_summary(symbol)
        trade["opened_at"] = _trade_opened_at(trade)
        trade["duration_seconds"] = _trade_duration_seconds(trade)
        if trade.get("status") != "OPEN":
            continue
        cached = _daily_open_cache.get(symbol)
        if cached and cached.get("change") is not None:
            trade["daily_change_pct"] = cached["change"]
    return JSONResponse(
        {"trades": trades, "account": _account, "stats": _stats()},
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


@app.get("/api/market-price/{symbol}")
def api_market_price(symbol: str):
    sym = symbol.upper().replace("/", "").replace(":USDT", "")
    now = time_mod.time()
    cached = _modal_price_cache.get(sym)
    if cached and now - cached.get("ts", 0) < 8:
        return {"symbol": sym, "price": cached.get("price")}
    price = _cache.get("prices", {}).get(sym)
    try:
        client = BinanceClient(futures=True)
        fresh = client.get_current_price(sym)
        if fresh:
            price = fresh
            _cache.setdefault("prices", {})[sym] = fresh
    except Exception as e:
        print(f"[market-price] {sym}: {e}")
    if not price:
        price = next((t.get("current_price") for t in _trades.values() if t.get("symbol") == sym and t.get("current_price")), None)
    _modal_price_cache[sym] = {"price": price, "ts": now}
    return {"symbol": sym, "price": price}


@app.post("/api/trade-preview")
async def api_trade_preview(body: TradeCreate):
    sym = body.symbol.upper().replace("/", "").split(":")[0].replace(":USDT", "")
    side = body.side.upper()
    if side not in ("LONG", "SHORT"):
        return JSONResponse({"ok": False, "error": "Choose LONG or SHORT"}, status_code=400)
    cached = _modal_price_cache.get(sym) or {}
    market = cached.get("price") or _cache.get("prices", {}).get(sym)
    entry = float((body.limit_price or body.entry) if body.order_type == "LIMIT" else (market or body.entry or 0))
    if body.advanced_enabled and body.order_type == "LIMIT" and body.limit_offset_pct and market:
        offset = min(float(body.limit_offset_pct), 20.0) / 100.0
        entry = float(market) * (1-offset if side == "LONG" else 1+offset)
    if body.advanced_enabled and body.sl_pct and body.sl_pct > 0:
        p = min(float(body.sl_pct), 50.0) / 100.0
        body.stop_loss = entry * (1-p if side == "LONG" else 1+p)
    if entry <= 0:
        return {"ok": False, "error": "Live price unavailable"}
    # Preview must be local and fast; verified leverage metadata is optional here.
    leverage_cap = _symbol_leverage_cap(sym)
    requested_leverage = int(body.leverage or 20)
    if requested_leverage > leverage_cap:
        return {"ok": False, "error": f"{sym} leverage is capped at {leverage_cap}x by available Binance tier data."}
    lev = max(1, min(requested_leverage, leverage_cap, 150))
    sl_dist = abs(entry - float(body.stop_loss or 0))
    if body.quantity and body.quantity > 0:
        qty = float(body.quantity)
    elif body.amount_usd and body.amount_usd > 0:
        qty = float(body.amount_usd) / entry
    elif body.margin_usd and body.margin_usd > 0:
        qty = float(body.margin_usd) * lev / entry
    elif body.risk_pct and sl_dist > 0:
        qty = (_account["balance"] * max(0.25, min(float(body.risk_pct), 100.0)) / 100.0) / sl_dist
    else:
        return {"ok": False, "error": "Enter an amount to calculate liquidation"}
    existing = next((t for t in _trades.values() if t.get("status") == "OPEN" and t.get("symbol") == sym and t.get("side") == side), None)
    if existing and body.order_type != "LIMIT":
        old_qty = float(existing.get("qty") or 0)
        total_qty = old_qty + qty
        entry = ((float(existing.get("entry") or entry) * old_qty) + entry * qty) / total_qty
        qty = total_qty
    candidate = {
        "symbol": sym, "side": side, "entry": entry, "qty": qty,
        "margin": qty * entry / lev, "leverage": lev,
        "margin_mode": (body.margin_mode or "CROSS").upper(),
        "current_price": float(market or entry), "maintenance_margin_rate": 0.005,
        "maintenance_amount": 0, "status": "OPEN",
    }
    liq = _estimate_liquidation_price(candidate)
    current = float(market or entry)
    distance = abs(current-liq)/current*100 if liq and current > 0 else None
    risk = ("HIGH" if distance <= 5 else "MEDIUM" if distance <= 10 else "LOW") if distance is not None else "UNKNOWN"
    no_liquidation_price = liq is None and str(body.margin_mode or "CROSS").upper() == "CROSS"
    if no_liquidation_price:
        risk = "SAFE"
    return {"ok": True, "entry": round(entry, 8), "market_price": market,
            "quantity": round(qty, 8), "notional": round(qty*entry, 2),
            "margin": round(qty*entry/lev, 2), "leverage": lev,
            "liquidation_price": round(liq, 8) if liq else None,
            "liquidation_distance_pct": round(distance, 2) if distance is not None else None,
            "liquidation_risk": risk, "no_liquidation_price": no_liquidation_price, "estimate": True}


@app.get("/api/alerts")
async def api_alerts():
    alerts = [t for t in _trades.values() if t.get("needs_alert") and t.get("hit")]
    for trade in alerts:
        trade["needs_alert"] = False
        db.save_trade(trade)
    return {"alerts": alerts, "count": len(alerts)}


@app.post("/api/trades")
def create_trade(body: TradeCreate):
    # Canonicalize symbols exactly like scanner/cache keys (e.g. BTC/USDT:USDT -> BTCUSDT).
    sym = _market_symbol_key(body.symbol)
    if not sym:
        return JSONResponse({"ok": False, "error": "Invalid trade symbol"}, status_code=400)
    side = body.side.upper()
    order_type = (body.order_type or "MARKET").upper()
    if side not in ("LONG", "SHORT"): return JSONResponse({"ok": False, "error": "side must be LONG or SHORT"}, status_code=400)
    if order_type not in ("MARKET", "LIMIT"): return JSONResponse({"ok": False, "error": "order_type must be MARKET or LIMIT"}, status_code=400)
    scale_low = float(body.scale_lower_price or 0)
    scale_high = float(body.scale_upper_price or 0)
    has_scale_range = bool(scale_low or scale_high)
    if has_scale_range:
        if not (scale_low > 0 and scale_high > scale_low):
            return JSONResponse({"ok": False, "error": "Set a valid Lower Range and Upper Range"}, status_code=400)
        entry_price = (scale_low + scale_high) / 2
    else:
        entry_price = float((body.limit_price or body.entry) if order_type == "LIMIT" else body.entry)
    modal_market = _modal_price_cache.get(sym) or {}
    market_reference = float(modal_market.get("price") or _cache.get("prices", {}).get(sym) or body.entry or 0)
    if body.advanced_enabled and order_type == "LIMIT" and body.limit_offset_pct and body.limit_offset_pct > 0 and market_reference > 0:
        offset = min(float(body.limit_offset_pct), 20.0) / 100.0
        entry_price = market_reference * (1 - offset if side == "LONG" else 1 + offset)
    if body.advanced_enabled:
        if body.sl_pct and body.sl_pct > 0:
            p = min(float(body.sl_pct), 50.0) / 100.0
            body.stop_loss = entry_price * (1 - p if side == "LONG" else 1 + p)
        if body.tp1_pct and body.tp1_pct > 0:
            p = min(float(body.tp1_pct), 99.0 if side == "SHORT" else 100.0) / 100.0
            body.tp1 = entry_price * (1 + p if side == "LONG" else 1 - p)
        if body.tp2_pct and body.tp2_pct > 0:
            p = min(float(body.tp2_pct), 99.0 if side == "SHORT" else 200.0) / 100.0
            body.tp2 = entry_price * (1 + p if side == "LONG" else 1 - p)
    if body.advanced_enabled and body.dca_enabled:
        lower, upper = float(body.dca_lower_price or 0), float(body.dca_upper_price or 0)
        if lower and upper and upper <= lower:
            return JSONResponse({"ok": False, "error": "DCA upper price must be greater than lower price"}, status_code=400)
    if entry_price <= 0: return JSONResponse({"ok": False, "error": "invalid entry price"}, status_code=400)
    # No Binance client/network I/O in the order-submit path.
    # The modal/scanner refreshes market prices in the background.
    max_lev = _scan_max_leverage.get(sym)
    leverage_cap = max(1, min(int(max_lev), 150)) if max_lev is not None else 20
    requested_leverage = int(body.leverage or min(20, leverage_cap))
    if requested_leverage > leverage_cap:
        return JSONResponse({"ok": False, "error": f"{sym} leverage is capped at {leverage_cap}x. Exact higher tiers are unavailable; refresh symbol analysis first."}, status_code=400)
    lev = max(1, min(requested_leverage, leverage_cap, 150))
    margin_mode = (body.margin_mode or "CROSS").upper()
    if margin_mode not in ("CROSS", "ISOLATED"):
        margin_mode = "CROSS"

    existing_trade = next((t for t in _trades.values() if t.get("status") == "OPEN" and t["symbol"] == sym and t["side"] == side), None)

    sl_dist = abs(entry_price - body.stop_loss)
    if sl_dist <= 0:
        return JSONResponse({"ok": False, "error": "invalid SL"}, status_code=400)
    check_low = scale_low if has_scale_range else entry_price
    check_high = scale_high if has_scale_range else entry_price
    if (side == "LONG" and body.stop_loss >= check_low) or (side == "SHORT" and body.stop_loss <= check_high):
        return JSONResponse({"ok": False, "error": "Stop Loss is on the wrong side of the entry range"}, status_code=400)
    if body.tp1 and ((side == "LONG" and body.tp1 <= check_high) or (side == "SHORT" and body.tp1 >= check_low)):
        return JSONResponse({"ok": False, "error": "Take Profit is on the wrong side of the entry range"}, status_code=400)

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

    # Liquidity is not used by order execution or the trade card; avoid a
    # blocking Binance ticker request during order placement.
    liq = None

    tid = str(uuid.uuid4())[:8]
    trade = {
        "id": tid, "symbol": sym, "side": side, "position_mode": "HEDGE", "order_type": order_type, "order_price": entry_price if order_type == "LIMIT" else None, "limit_price": entry_price if order_type == "LIMIT" else None,
        "entry": entry_price, "stop_loss": body.stop_loss, "tp1": body.tp1, "tp2": body.tp2,
        "leverage": lev, "max_leverage": max_lev, "leverage_verified": max_lev is not None, "margin_mode": margin_mode,
        "trade_style": (str(body.trade_style).lower() if str(body.trade_style or "").lower() in ("scalp", "day", "swing") else "day"),
        "signal_adx": body.signal_adx, "signal_rsi": body.signal_rsi,
        "timeframe_trends": dict(body.timeframe_trends or {}), "bias_1d": body.bias_1d, "bias_4h": body.bias_4h,
        "qty": round(qty, 6), "margin": round(margin, 2), "notional": round(notional, 2),
        "risk_usd": round(risk_usd, 2), "liquidity": liq,
        "trailing_pct": (max(0.1, min(float(body.profit_callback_pct or 0.5), 5.0)) if body.advanced_enabled and body.profit_lock_enabled else None), "callback_rate": (max(0.1, min(float(body.profit_callback_pct or 0.5), 5.0)) if body.advanced_enabled and body.profit_lock_enabled else None), "trailing_activation_price": None,
        "trailing_active": False, "peak_price": entry_price, "trough_price": entry_price,
        "advanced_enabled": bool(body.advanced_enabled),
        "dca_reference_entry": entry_price,
        "advanced_dca_enabled": bool(body.advanced_enabled and body.dca_enabled),
        "dca_gap_pct": max(0.1, min(float(body.dca_gap_pct or 1.5), 20.0)),
        "dca_max_orders": max(1, min(int(body.dca_max_orders or 3), 5)),
        "dca_multiplier": max(1.0, min(float(body.dca_multiplier or 1.5), 3.0)),
        "dca_lower_price": float(body.dca_lower_price or 0) or None,
        "dca_upper_price": float(body.dca_upper_price or 0) or None,
        "auto_dca_base_usd": round(notional, 2),
        "sl_pct_setting": min(50.0, float(body.sl_pct or 0)) if body.advanced_enabled else 0,
        "tp1_pct_setting": min(99.0 if side == "SHORT" else 100.0, float(body.tp1_pct or 0)) if body.advanced_enabled else 0,
        "tp2_pct_setting": min(99.0 if side == "SHORT" else 200.0, float(body.tp2_pct or 0)) if body.advanced_enabled else 0,
        "profit_lock_enabled": bool(body.advanced_enabled and body.profit_lock_enabled),
        "profit_activation_pct": max(0.1, min(float(body.profit_activation_pct or 0.8), 50.0)),
        "profit_callback_pct": max(0.1, min(float(body.profit_callback_pct or 0.5), 5.0)),
        "scale_mode": scale_mode, "scale_orders": scale_orders,
        "dca_history": [{"price": entry_price, "qty": round(qty, 8), "notional": round(notional, 2), "margin": round(margin, 2), "created_at": datetime.now(timezone.utc).isoformat()}],
        "note": body.note or "", "status": "OPEN", "hit": None, "needs_alert": False,
        "current_price": entry_price if order_type == "MARKET" else None, "pnl_pct": 0.0, "unrealized_pnl": 0.0,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "dca_count": 0,
    }
    if order_type == "LIMIT" and scale_mode and scale_orders >= 2:
        cached = _modal_price_cache.get(sym) or {}
        market_price = float(cached.get("price") or _cache.get("prices", {}).get(sym) or 0)
        if market_price <= 0:
            return JSONResponse({"ok": False, "error": "Live market price unavailable; wait for the market price to load and retry"}, status_code=503)
        if has_scale_range:
            if (side == "LONG" and scale_high >= market_price) or (side == "SHORT" and scale_low <= market_price):
                return JSONResponse({"ok": False, "error": "LONG range must be below market; SHORT range must be above market"}, status_code=400)
            child_prices = [
                (scale_high - (scale_high - scale_low) * i / (scale_orders - 1))
                if side == "LONG" else
                (scale_low + (scale_high - scale_low) * i / (scale_orders - 1))
                for i in range(scale_orders)
            ]
        else:
            if (side == "LONG" and entry_price >= market_price) or (side == "SHORT" and entry_price <= market_price):
                return JSONResponse({"ok": False, "error": "Scaled LONG limit must be below market; SHORT limit must be above market"}, status_code=400)
            child_prices = [market_price + (entry_price - market_price) * (i + 1) / scale_orders for i in range(scale_orders)]
        weights = [1.0 / scale_orders] * scale_orders
        if scale_mode == "ascending":
            total_weight = scale_orders * (scale_orders + 1) / 2
            weights = [(i + 1) / total_weight for i in range(scale_orders)]
        if any((side == "LONG" and body.stop_loss >= p) or (side == "SHORT" and body.stop_loss <= p) for p in child_prices):
            return JSONResponse({"ok": False, "error": "Stop Loss conflicts with a scaled entry price"}, status_code=400)
        created = []
        for i, weight in enumerate(weights):
            child_price = child_prices[i]
            child_notional = notional * weight
            child_qty = round(child_notional / child_price, 6)
            if child_qty <= 0:
                continue
            child = dict(trade)
            child_id = str(uuid.uuid4())[:8]
            child_margin = round(child_notional / lev, 2)
            child.update({
                "id": child_id, "entry": round(child_price, 8),
                "order_price": round(child_price, 8), "limit_price": round(child_price, 8),
                "qty": child_qty, "margin": child_margin,
                "notional": round(child_notional, 2),
                "risk_usd": round(child_qty * abs(child_price - body.stop_loss), 2),
                "current_price": None, "status": "PENDING",
                "scale_order_index": i + 1, "scale_orders": scale_orders,
                "dca_history": [], "pending_created_at": datetime.now(timezone.utc).isoformat(),
            })
            _trades[child_id] = child
            created.append(child)
        _calc_equity()
        db.save_trades_and_account(created, _account)
        return {"ok": True, "trades": created, "pending": True, "scaled": True, "account": _account}
    if order_type == "LIMIT":
        trade["status"] = "PENDING"
        trade["pending_created_at"] = datetime.now(timezone.utc).isoformat()
        trade["dca_history"] = []
        _trades[tid] = trade
        _calc_equity()
        db.save_trades_and_account([trade], _account)
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
        if body.advanced_enabled:
            existing_trade.update({"advanced_enabled": True, "advanced_dca_enabled": bool(body.dca_enabled), "dca_gap_pct": max(0.1,min(float(body.dca_gap_pct or 1.5),20.0)), "dca_max_orders": max(1,min(int(body.dca_max_orders or 3),5)), "dca_multiplier": max(1.0,min(float(body.dca_multiplier or 1.5),3.0)), "dca_lower_price": float(body.dca_lower_price or 0) or None, "dca_upper_price": float(body.dca_upper_price or 0) or None, "auto_dca_base_usd": round(notional,2), "sl_pct_setting": min(50.0,float(body.sl_pct or 0)), "tp1_pct_setting": min(99.0 if side == "SHORT" else 100.0,float(body.tp1_pct or 0)), "tp2_pct_setting": min(99.0 if side == "SHORT" else 200.0,float(body.tp2_pct or 0)), "profit_lock_enabled": bool(body.profit_lock_enabled), "profit_activation_pct": float(body.profit_activation_pct or 0.8), "profit_callback_pct": float(body.profit_callback_pct or 0.5)})
            if existing_trade["profit_lock_enabled"]:
                existing_trade["callback_rate"] = existing_trade["profit_callback_pct"]
                existing_trade["trailing_pct"] = existing_trade["profit_callback_pct"]
                existing_trade["trailing_active"] = False
                existing_trade["peak_price"] = float(existing_trade.get("current_price") or existing_trade.get("entry") or 0)
                existing_trade["trough_price"] = existing_trade["peak_price"]
            avg = float(existing_trade["entry"]); side_avg = str(existing_trade.get("side") or side).upper()
            if existing_trade["sl_pct_setting"] > 0:
                p = existing_trade["sl_pct_setting"]/100; existing_trade["stop_loss"] = avg*(1-p if side_avg=="LONG" else 1+p)
            if existing_trade["tp1_pct_setting"] > 0:
                p = existing_trade["tp1_pct_setting"]/100; existing_trade["tp1"] = avg*(1+p if side_avg=="LONG" else 1-p)
            if existing_trade["tp2_pct_setting"] > 0:
                p = existing_trade["tp2_pct_setting"]/100; existing_trade["tp2"] = avg*(1+p if side_avg=="LONG" else 1-p)
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
        _calc_equity()
        db.save_trades_and_account([existing_trade], _account)
        return {"ok": True, "trade": existing_trade, "duplicate": True, "auto_dca": True, "account": _account}
    _update_trade_liquidation_fields(trade)
    _trades[tid] = trade
    for position in _trades.values():
        if position.get("status") == "OPEN": _update_trade_liquidation_fields(position)
    print(f"[trades] opened {tid} {sym} {side} entry={entry_price} qty={trade['qty']}")
    _calc_equity()
    db.save_trades_and_account([trade], _account)
    return {"ok": True, "trade": trade, "duplicate": False, "account": _account}


@app.delete("/api/trades/pending")
async def cancel_all_pending_trades():
    canceled_at = datetime.now(timezone.utc).isoformat()
    canceled = []
    for trade in list(_trades.values()):
        if trade.get("status") == "PENDING":
            trade.update({"status": "CANCELED", "canceled_at": canceled_at})
            db.save_trade(trade)
            canceled.append(trade["id"])
    return {"ok": True, "canceled": len(canceled), "ids": canceled}


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
    entry = float(t.get("entry") or 0)
    mark = float(t.get("current_price") or entry)
    side = str(t.get("side", "")).upper()
    # Validate only fields the user actually changed. A trailing stop may
    # legitimately sit beyond breakeven while remaining on the safe side of mark.
    if body.stop_loss is not None and mark > 0 and ((side == "LONG" and float(body.stop_loss) >= mark) or (side == "SHORT" and float(body.stop_loss) <= mark)):
        return JSONResponse({"ok": False, "error": "Stop Loss must remain below LONG / above SHORT mark price"}, status_code=400)
    if body.tp1 is not None and entry > 0 and ((side == "LONG" and float(body.tp1) <= entry) or (side == "SHORT" and float(body.tp1) >= entry)):
        return JSONResponse({"ok": False, "error": "Take Profit 1 must be above LONG / below SHORT average entry"}, status_code=400)
    if "tp2" in body.model_fields_set and body.tp2 is not None and entry > 0 and ((side == "LONG" and float(body.tp2) <= entry) or (side == "SHORT" and float(body.tp2) >= entry)):
        return JSONResponse({"ok": False, "error": "Take Profit 2 must be above LONG / below SHORT average entry"}, status_code=400)
    if body.stop_loss is not None:
        t["stop_loss"] = body.stop_loss
    if body.tp1 is not None:
        t["tp1"] = body.tp1
        t["tp1_filled"] = False
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
    if body.profit_lock_enabled is not None:
        t["profit_lock_enabled"] = bool(body.profit_lock_enabled)
    if body.profit_activation_pct is not None:
        t["profit_activation_pct"] = max(0.1, min(float(body.profit_activation_pct), 50.0))
    if body.profit_callback_pct is not None:
        t["profit_callback_pct"] = max(0.1, min(float(body.profit_callback_pct), 5.0))
        t["callback_rate"] = t["profit_callback_pct"]
        t["trailing_pct"] = t["profit_callback_pct"]
        t["trailing_active"] = False
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
    symbol_key = str(t.get("symbol") or "").upper().replace("/", "").replace(":USDT", "")
    daily_snapshot = _daily_open_cache.get(symbol_key) or {}
    if daily_snapshot.get("change") is not None:
        t["daily_change_pct"] = daily_snapshot["change"]
    pnl = t.get("unrealized_pnl") or 0.0
    _account["balance"] = round(_account["balance"] + pnl, 2)
    _account["realized_pnl"] = round(_account["realized_pnl"] + pnl, 2)
    total_realized = round(float(t.get("realized_pnl") or 0) + pnl, 2)
    _freeze_trade_market_context(t)
    t.update({"status": "CLOSED", "needs_alert": False,
              "closed_at": datetime.now(timezone.utc).isoformat(),
              "close_price": float(t.get("current_price") or t.get("entry") or 0),
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
        background_tasks.add_task(run_scan, top_n=min(top, 26))
    return RedirectResponse(url="/", status_code=303)


@app.get("/health")
async def health():
    return {"status": "ok", "scanning": _cache["scanning"], "auto_scan": _cache["auto_scan"], "database": db.backend_name()}

@app.get("/api/price-monitor")
async def api_price_monitor():
    return dict(_price_monitor_status)


@app.get("/", response_class=HTMLResponse)
async def home():
    # Dashboard JavaScript is inline; prevent browsers/proxies serving stale UI.
    return HTMLResponse(
        PAGE,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<meta name="theme-color" content="#060a12">
<title>Daily Signal by Tuhinfay</title>
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
.trade-section{display:block;width:100%;margin:0 0 10px;padding:0;background:transparent;border:0;border-radius:0;box-sizing:border-box}
.trade-section>summary{display:block;width:100%;box-sizing:border-box;cursor:pointer;list-style:none;margin:0 0 8px;padding:11px 12px;background:var(--card);border:1px solid var(--line);border-radius:10px;font-weight:800}
.trade-section>summary::-webkit-details-marker{display:none}
.trade-section>.card{display:block;width:100%;box-sizing:border-box;margin:0 0 8px}
.trade-section>.history-trade{width:100%;box-sizing:border-box;margin:0;padding:12px 10px;border-bottom:1px solid var(--line)}
.trade-section>.history-trade:last-child{border-bottom:0}
.card.L{border-left-color:var(--gn)}.card.S{border-left-color:var(--rd)}.card.hit{border-left-color:var(--gd)}
.head{display:flex;justify-content:space-between;align-items:center;gap:8px}
.side{font-size:11px;font-weight:800}.side.L{color:var(--gn)}.side.S{color:var(--rd)}
.sym{font-size:14px;font-weight:800;margin-left:4px}
.score{font-size:16px;font-weight:900;color:var(--cyan)}
.tag{display:inline-block;font-size:9px;font-weight:700;padding:2px 6px;border-radius:5px;margin-left:4px;background:#152033;color:var(--mut)}
.tag.q{background:#0b3b2e;color:#6ee7b7}
.tag.up{background:#064e3b;color:#6ee7b7}.tag.dn{background:#881337;color:#fecdd3}
.sub{margin-top:5px;color:var(--mut);font-size:11px;line-height:1.4}
.tf-strip{display:flex;flex-wrap:wrap;align-items:center;gap:5px;margin-top:7px}
.tf-chip{display:inline-flex;align-items:center;gap:4px;padding:4px 7px;border:1px solid var(--line);border-radius:6px;background:#101a2b;font-size:10px;font-weight:800;white-space:nowrap}
.tf-chip.up{color:var(--gn);border-color:#14543f;background:#09251e}.tf-chip.dn{color:var(--rd);border-color:#64263a;background:#2b101b}.tf-chip.side{color:var(--gd);border-color:#66501d;background:#251e0d}
.market-summary{display:flex;flex-wrap:wrap;align-items:center;gap:5px 9px;margin-top:7px;padding:7px 8px;border:1px solid var(--line);border-radius:8px;background:#080f1c;font-size:10px;line-height:1.5}
.market-summary .ms-label{color:var(--mut);font-weight:800}.market-summary .open{color:var(--cyan);font-weight:800}.market-summary .high,.market-summary .support{color:var(--gn);font-weight:800}.market-summary .low,.market-summary .resistance{color:var(--rd);font-weight:800}.market-summary .up{color:var(--gn);font-weight:800}.market-summary .dn{color:var(--rd);font-weight:800}
@media(max-width:360px){.market-summary{gap:4px 7px}}
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
 .hint{font-size:11px;color:var(--mut);margin-top:4px}.fund-tools{margin-top:14px;padding-top:12px;border-top:1px solid var(--line)}.fund-tools label{display:block;color:var(--mut);font-size:11px;margin-bottom:6px}.fund-tools input{width:100%;padding:11px;border-radius:9px;background:#080e1a;border:1px solid var(--line);color:var(--text);font-size:14px}.fund-tools .actions{margin-top:8px}.share-bg{display:none;position:fixed;inset:0;background:#000b;z-index:120;align-items:center;justify-content:center;padding:14px}.share-modal{width:min(100%,480px);max-height:94vh;overflow:auto;background:var(--card);border:1px solid var(--line);border-radius:16px;padding:14px}.share-modal h3{font-size:15px;margin-bottom:10px}.share-preview{background:#070b14;border-radius:12px;padding:8px;text-align:center}.share-preview canvas{display:block;width:100%;height:auto;border-radius:8px}.share-modal .actions{display:flex;gap:7px;margin-top:10px}.share-modal .btn{flex:1}.history-trade{padding:12px 0;border-bottom:1px solid var(--line)}.history-trade:last-child{border-bottom:0}.history-trade .vals{margin-top:8px;column-gap:10px;row-gap:8px}.history-trade .vals .trade-time{min-width:0}.history-trade .vals .trade-time strong{font-size:12px;letter-spacing:0}.history-trade .vals>div:last-child{min-width:0;padding:0;border:0;border-radius:0;background:transparent}.history-trade .vals>div:last-child strong{color:var(--text)}.order-preview{margin-top:9px;padding:9px 10px;background:#080e1a;border:1px solid var(--line);border-radius:8px;color:var(--text);font-size:11px;font-weight:700;line-height:1.5;overflow-wrap:anywhere}.liq-preview{margin-top:6px;padding:7px 9px;border:1px solid var(--line);border-radius:8px;background:#080e1a;font-size:10px;line-height:1.4;color:var(--mut)}.scale-preview{margin-top:8px;border:1px solid var(--line);border-radius:9px;overflow:hidden}.scale-row{display:grid;grid-template-columns:30px repeat(4,minmax(0,1fr));gap:4px;padding:8px 6px;border-bottom:1px solid var(--line);font-size:9px;overflow-wrap:anywhere}.scale-row:last-child{border:0}.scale-head{color:var(--cyan);font-weight:800;background:#101827}
.sec{padding:10px 12px 4px;font-size:10px;color:var(--mut);text-transform:uppercase;letter-spacing:.05em;font-weight:700}
.mv{display:flex;gap:8px;overflow-x:auto;padding:0 12px 10px}
.mv .i{min-width:108px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px}
.mv .i b{display:block;font-size:12px}.mv .up{color:var(--gn)}.mv .dn{color:var(--rd)}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin:0 10px 8px}
.stats .metric{text-align:center}

.bot-panel{margin:14px 0;border:1px solid #29364a;border-radius:18px;overflow:hidden;background:linear-gradient(145deg,rgba(24,37,57,.98),rgba(12,20,34,.98));box-shadow:0 12px 32px rgba(0,0,0,.18)}
.bot-summary{list-style:none;display:flex;align-items:center;gap:12px;padding:16px;cursor:pointer}.bot-summary::-webkit-details-marker{display:none}.bot-mark{width:40px;height:40px;border-radius:13px;display:grid;place-items:center;background:linear-gradient(135deg,#173d4b,#1d2b52);color:#63d8cf;font-size:22px}.bot-summary-copy{display:grid;gap:3px;flex:1}.bot-summary-copy b{font-size:16px}.bot-summary-copy small,.bot-enable-row small,.bot-style small,.bot-help{display:block;color:var(--muted,#8995a8);font-size:11px;font-weight:400}.bot-status{font-size:11px;font-weight:800;letter-spacing:.08em;padding:6px 10px;border-radius:20px;background:#303b4d;color:#aab5c6}.bot-chevron{color:#98a7ba;font-size:20px}.bot-panel[open] .bot-chevron{transform:rotate(180deg)}.bot-content{padding:0 16px 16px;border-top:1px solid #29364a}.bot-enable-row{display:flex;justify-content:space-between;align-items:center;padding:14px 0;border-bottom:1px solid #29364a}.bot-enable-row small{margin-top:4px}.bot-section{padding:15px 0;border-bottom:1px solid #29364a}.bot-section-title{font-size:13px;font-weight:750;margin-bottom:12px}.bot-section-title span{color:#59cfc2;margin-right:8px;font-size:11px}.bot-style-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin-bottom:14px}.bot-style{display:flex;align-items:flex-start;gap:6px;padding:10px 8px;border:1px solid #35445a;border-radius:12px;background:#111d2d;min-width:0}.bot-style:has(input:checked){border-color:#45bfb4;background:rgba(31,112,108,.2);box-shadow:inset 0 0 0 1px rgba(69,191,180,.2)}.bot-style input{margin:2px 0 0;accent-color:#4bd0c2}.bot-style b{font-size:11px;display:block}.bot-style small{font-size:9px;line-height:1.35;margin-top:4px}.bot-content .row2{gap:10px;margin:10px 0}.bot-content label{font-size:12px;color:#c2ccda}.bot-content input:not([type=checkbox]):not([type=radio]),.bot-content select{margin-top:6px;border:1px solid #34445a;border-radius:10px;background:#0d1828;color:#eef4ff;padding:11px 12px;min-height:42px;width:100%;box-sizing:border-box}.bot-content> .bot-section>label{display:block;margin-top:12px}.bot-help{display:inline;font-size:10px;margin-left:4px}.bot-safety{padding:14px 0}.bot-safety label{display:flex;gap:9px;align-items:center;font-size:12px}.bot-safety input{accent-color:#4bd0c2}.bot-safety small{display:block;color:#8995a8;font-size:10px;line-height:1.5;margin:8px 0 0 24px}.bot-footer{display:flex;align-items:center;justify-content:space-between;gap:10px}.bot-paper-note{font-size:9px;color:#55c8b4;letter-spacing:.07em;font-weight:800}.bot-footer .btn{border-radius:11px;padding:11px 16px}.bot-panel[open] .bot-status{background:rgba(32,180,132,.16);color:#53d5aa}
</style>
</head>
<body>
<div id="alertBar" onclick="showTab('trades')"></div>
<header class="top">
  <h1>Daily Signal by Tuhinfay</h1>
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
  <details class="bot-panel" id="botPanel">
    <summary class="bot-summary">
      <span class="bot-mark">✦</span>
      <span class="bot-summary-copy"><b>Auto Trade</b><small>Strategy & risk controls</small></span>
      <span id="botState" class="bot-status">OFF</span>
      <span class="bot-chevron">⌄</span>
    </summary>
    <div class="bot-content">
      <div class="bot-enable-row">
        <div><b>Bot engine</b><small>Automatically enter qualified signals</small></div>
        <select id="botEnabled" aria-label="Bot status"><option value="false">OFF</option><option value="true">ON</option></select>
      </div>
      <div class="bot-section">
        <div class="bot-section-title"><span>01</span> Strategy</div>
        <div class="bot-style-grid">
          <label class="bot-style"><input type="radio" name="botStyleChoice" value="scalp"><span><b>Scalping</b><small>Short moves · 5m–15m</small></span></label>
          <label class="bot-style"><input type="radio" name="botStyleChoice" value="day" checked><span><b>Day trade</b><small>Intraday · 15m–4h</small></span></label>
          <label class="bot-style"><input type="radio" name="botStyleChoice" value="swing"><span><b>Swing</b><small>Multi-session · 4h–1d</small></span></label>
          
        </div>
        <input type="hidden" id="botStyle" value="day">
        <div class="row2"><div><label>Trade direction</label><select id="botDirection"><option value="both">Long + Short</option><option value="long">Long only</option><option value="short">Short only</option></select></div><div><label>Minimum quality</label><select id="botQuality"><option>A+</option><option selected>A</option><option>B</option></select></div></div>
      </div>
      <div class="bot-section">
        <div class="bot-section-title"><span>02</span> Entry filters</div>
        <div class="row2"><div><label>Minimum signal score</label><input id="botScore" type="number" min="60" max="100" value="75"></div><div><label>Minimum TP1 R:R</label><input id="botMinRR" type="number" min="1" max="10" step="0.1" value="1.5"></div></div>
        <label>Coin watchlist <small class="bot-help">Blank means all eligible coins</small></label><input id="botSymbols" placeholder="BTCUSDT, ETHUSDT, SOLUSDT">
      </div>
      <div class="bot-section">
        <div class="bot-section-title"><span>03</span> Advanced Option</div>
        <label>Advanced settings <select id="botAdvanced"><option value="false">OFF</option><option value="true">ON</option></select></label>
        <div id="botAdvancedFields" style="display:none">
        <div class="row2"><div><label>SL % override (0 = signal)</label><input id="botAdvSL" type="number" min="0" max="50" step="0.1" value="0"></div><div><label>TP1 % override (0 = signal)</label><input id="botAdvTP1" type="number" min="0" max="100" step="0.1" value="0"></div></div>
        <div class="row2"><div><label>TP2 % override (0 = signal)</label><input id="botAdvTP2" type="number" min="0" max="200" step="0.1" value="0"></div><div><label>Limit Entry Offset % (0 = market)</label><input id="botAdvOffset" type="number" min="0" max="20" step="0.1" value="0"></div></div>
        <div class="row2"><div><label>Profit-lock activation %</label><input id="botAdvActivation" type="number" min="0.1" max="50" step="0.1" value="0.8"></div><div><label>Profit-lock callback %</label><input id="botAdvCallback" type="number" min="0.1" max="5" step="0.1" value="0.5"></div></div>
        <label><input id="botAdvProfitLock" type="checkbox"> Enable profit-lock trailing</label>
        <div class="bot-section-title" style="margin-top:14px">DCA & Scaled Entry</div>
        <div class="row2"><div><label>Auto DCA / Scale</label><select id="botDcaEnabled"><option value="false">OFF</option><option value="true">ON</option></select></div><div><label>Entry gap (%)</label><input id="botDcaGap" type="number" min="0.1" max="20" step="0.1" value="1.5"></div></div>
        <div class="row2"><div><label>Maximum add orders</label><input id="botDcaMax" type="number" min="1" max="5" value="3"></div><div><label>Next order multiplier</label><input id="botDcaMultiplier" type="number" min="1" max="3" step="0.1" value="2"></div></div>
        <div class="row2"><div><label>DCA range from %</label><input id="botDcaMinPct" type="number" min="0" max="50" step="0.1" value="0"></div><div><label>DCA range up to % (0 = no cap)</label><input id="botDcaMaxPct" type="number" min="0" max="100" step="0.1" value="0"></div></div>
        <small class="bot-help">Adds to an existing bot position after each adverse price move by the configured gap. Adds are capped by order count and multiplier.</small>
        </div>
      </div>
      <div class="bot-section">
        <div class="bot-section-title"><span>04</span> Position & limits</div>
        <div class="row2"><div><label>Position value (USDT)</label><input id="botAmount" type="number" min="5" max="1000000" step="any" value="50"></div><div><label>Max open bot trades</label><input id="botMaxOpen" type="number" min="1" max="10" value="2"></div></div>
        <div class="row2"><div><label>Max trades / coin / day</label><input id="botMaxSymbolDay" type="number" min="1" max="30" value="3"></div><div><label>Coin cooldown (minutes)</label><input id="botCooldown" type="number" min="0" max="1440" value="30"></div></div>
        <div class="row2"><div><label>Daily loss limit (%)</label><input id="botDailyLoss" type="number" min="0" max="20" step="0.1" value="2"></div><div><label>Trailing stop (%) <small class="bot-help">0 = off</small></label><input id="botTrail" type="number" min="0" max="5" step="0.1" value="0.8"></div></div>
      </div>
      <div class="bot-safety"><label><input id="botOpposite" type="checkbox" checked> Exit if a strong opposite signal appears</label><small>Daily loss guard pauses new entries after the configured bot loss limit. Existing positions still follow their SL/TP and trailing rules.</small></div>
      <div class="bot-footer"><button class="btn good" onclick="saveBotSettings()">Save bot settings</button></div>
    </div>
  </details>
  <div class="note" id="status">Loading…</div>
  <div id="signalList"></div>
</section>

<section id="tab-trades" style="display:none">
  <div class="stats" id="tradeStats"></div>
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
    <div class="fund-tools">
      <input id="fundAmount" type="number" min="0.01" step="any" placeholder="Enter USDT amount">
      <div class="actions"><button class="btn good" onclick="adjustFunds('add')">＋ Add Funds</button><button class="btn bad" onclick="adjustFunds('remove')">− Remove Funds</button></div>
    </div>
  </div>
</section>

<div id="toast"></div>
<div class="share-bg" id="shareBg" onclick="if(event.target===this)closeShare()"><div class="share-modal"><h3>Share Trade</h3><div class="share-preview"><canvas id="shareCanvas" width="900" height="1080"></canvas></div><div class="actions"><button class="btn dim" onclick="closeShare()">Close</button><button class="btn good" onclick="saveShareCard()">Save Image</button></div><p class="hint">Save the trade card image to your device.</p></div></div>
<div class="modal-bg" id="modalBg">
  <div class="modal">
    <h3 id="mTitle">Open Trade</h3>
    <input type="hidden" id="m_symbol">
    <div class="row2">
      <div><label>Side</label><select id="m_side"><option value="LONG">LONG</option><option value="SHORT">SHORT</option></select></div>
      <div><label>Margin mode</label><select id="m_mm"><option value="CROSS">Cross</option><option value="ISOLATED">Isolated</option></select></div>
    </div>
    <div class="row2">
      <div><label>Order Type</label><select id="m_order_type" onchange="toggleOrderType()"><option value="MARKET">Market</option><option value="LIMIT">Limit</option><option value="SCALED_FLAT">Scaled · Equal Split</option><option value="SCALED_ASCENDING">Scaled · Ascending</option></select></div>
      <div><label>Leverage</label><input type="number" id="m_lev" value="20" min="1" max="150" oninput="updateOrderPreview()"></div>
    </div>
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
    <input type="hidden" id="m_scale" value="">
    <div id="scaledOrderFields" style="display:none">
    <div class="row2"><div><label>Number of Orders</label><input type="number" id="m_scale_n" min="2" max="50" placeholder="2–50" oninput="updateOrderPreview()"></div><div></div></div>
    <div class="row2" id="scaleRangeFields" style="display:none">
      <div><label>Lower Range Price</label><input type="number" id="m_scale_low" min="0" step="any" placeholder="Range low" oninput="updateOrderPreview()"></div>
      <div><label>Upper Range Price</label><input type="number" id="m_scale_high" min="0" step="any" placeholder="Range high" oninput="updateOrderPreview()"></div>
    </div>
    </div>
    <details class="trade-details" id="addAdvanced"><summary>Advanced Option</summary>
      <label><input type="checkbox" id="m_adv_dca"> Enable DCA</label>
      <div class="row2"><div><label>DCA gap %</label><input type="number" id="m_dca_gap" min="0.1" max="20" step="0.1" value="1.5"></div><div><label>Maximum DCA orders</label><input type="number" id="m_dca_max" min="1" max="5" value="3"></div></div>
      <div class="row2"><div><label>DCA multiplier</label><input type="number" id="m_dca_mult" min="1" max="3" step="0.1" value="1.5"></div><div><label>Limit Entry Offset %</label><input type="number" id="m_limit_offset" min="0" max="20" step="0.1" placeholder="Optional"></div></div>
      <div class="row2"><div><label>DCA lower price (optional)</label><input type="number" id="m_dca_low" min="0" step="any"></div><div><label>DCA upper price (optional)</label><input type="number" id="m_dca_high" min="0" step="any"></div></div>
      <div class="row2"><div><label>SL % override</label><input type="number" id="m_sl_pct" min="0" max="50" step="0.1" placeholder="Optional"></div><div><label>TP1 % override</label><input type="number" id="m_tp1_pct" min="0" max="100" step="0.1" placeholder="Optional"></div></div>
      <div class="row2"><div><label>TP2 % override</label><input type="number" id="m_tp2_pct" min="0" max="200" step="0.1" placeholder="Optional"></div><div><label>Profit-lock activation %</label><input type="number" id="m_profit_activation" min="0.1" max="50" step="0.1" value="0.8"></div></div>
      <label>Profit-lock callback %</label><input type="number" id="m_profit_callback" min="0.1" max="5" step="0.1" value="0.5">
      <label><input type="checkbox" id="m_profit_lock"> Enable profit-lock trailing</label>
      <p class="hint">DCA adds exposure. Maximum orders and order size are capped; use conservative values.</p>
    </details>
    <div id="order_preview" class="order-preview">Value 0 USDT · Margin 0 USDT · Qty 0.000000</div><div id="liq_preview" class="liq-preview" aria-live="polite">Add amount + SL for liquidation estimate.</div><div id="scale_preview" class="scale-preview" style="display:none"></div>
    
    <div class="btns">
      <button class="btn dim" onclick="closeModal()">Cancel</button>
      <button class="btn good" onclick="submitTrade()">Open Trade</button>
    </div>
  </div>
</div>
<div class="modal-bg" id="manageBg"><div class="manage-modal"><h3 id="manageTitle">Manage Position</h3><input type="hidden" id="manageTradeId"><div id="manageTrailFields" style="display:none"><label>Activation Price (blank = from entry)</label><input type="number" id="manageTrailActivation" step="any" placeholder="Optional"><label>Callback Rate (%) · 0.1–5</label><input type="number" id="manageTrailRate" min="0" max="5" step="0.1" value="1"><p class="hint">Trailing follows favorable mark-price movement after activation. Set 0 to disable.</p></div><div id="manageLevelsFields" style="display:none"><label>Stop Loss Price</label><input type="number" id="manageSL" step="any" oninput="syncLevelPct('sl','price')"><label>SL PNL % (loss magnitude)</label><input type="number" id="manageSLPct" step="any" min="0" placeholder="Optional" oninput="syncLevelPct('sl','pct')"><label>Take Profit 1 Price</label><input type="number" id="manageTP1" step="any" oninput="syncLevelPct('tp','price')"><label>TP1 PNL %</label><input type="number" id="manageTP1Pct" step="any" min="0" placeholder="Optional" oninput="syncLevelPct('tp','pct')"><label>Take Profit 2 (optional)</label><input type="number" id="manageTP2" step="any" oninput="syncLevelPct('tp2','price')"><label>TP2 PNL %</label><input type="number" id="manageTP2Pct" step="any" min="0" placeholder="Optional" oninput="syncLevelPct('tp2','pct')"></div><div class="btns"><button class="btn dim" onclick="closeManage()">Cancel</button><button class="btn good" onclick="saveManage()">Confirm</button></div></div></div>

<script>
let soundOn=true, voiceOn=true, style='all', lastSig='', spokenAlerts=new Set(), pinManual=false, autoScan=true;
async function loadBotSettings(){try{const d=await(await fetch('/api/auto-trade')).json(),c=d.config||{};botEnabled.value=String(!!c.enabled);botScore.value=c.min_score??75;botQuality.value=c.min_quality||'A';botStyle.value=['scalp','day','swing'].includes(c.trade_style)?c.trade_style:'day';document.querySelectorAll('input[name="botStyleChoice"]').forEach(x=>x.checked=x.value===botStyle.value);botDirection.value=c.direction||'both';botAmount.value=c.amount_usd??50;botMaxOpen.value=c.max_open??2;botTrail.value=c.trailing_pct??0.8;botMinRR.value=c.min_rr??1.5;botCooldown.value=c.cooldown_minutes??30;botMaxSymbolDay.value=c.max_symbol_trades_day??3;botDailyLoss.value=c.max_daily_loss_pct??2;botSymbols.value=c.symbols||'';botDcaEnabled.value=String(!!c.dca_enabled);botDcaGap.value=c.dca_gap_pct??1.5;botDcaMax.value=c.dca_max_orders??3;botDcaMultiplier.value=c.dca_multiplier??2;botDcaMinPct.value=c.dca_range_min_pct??0;botDcaMaxPct.value=c.dca_range_max_pct??0;botAdvanced.value=String(!!c.advanced_enabled);botAdvancedFields.style.display=c.advanced_enabled?'block':'none';botAdvSL.value=c.advanced_sl_pct??0;botAdvTP1.value=c.advanced_tp1_pct??0;botAdvTP2.value=c.advanced_tp2_pct??0;botAdvOffset.value=c.advanced_limit_offset_pct??0;botAdvActivation.value=c.advanced_profit_activation_pct??0.8;botAdvCallback.value=c.advanced_profit_callback_pct??0.5;botAdvProfitLock.checked=!!c.advanced_profit_lock;botOpposite.checked=c.exit_on_opposite!==false;botState.textContent=c.enabled?'ON':'OFF';botState.style.color=c.enabled?'#53d5aa':'';}catch(e){}}
document.addEventListener('change',e=>{if(e.target.matches('input[name="botStyleChoice"]'))botStyle.value=e.target.value;});
document.getElementById('botAdvanced').addEventListener('change',()=>{botAdvancedFields.style.display=botAdvanced.value==='true'?'block':'none';});
async function saveBotSettings(){const p={advanced_enabled:botAdvanced.value==='true',advanced_sl_pct:+botAdvSL.value,advanced_tp1_pct:+botAdvTP1.value,advanced_tp2_pct:+botAdvTP2.value,advanced_limit_offset_pct:+botAdvOffset.value,advanced_profit_lock:botAdvProfitLock.checked,advanced_profit_activation_pct:+botAdvActivation.value,advanced_profit_callback_pct:+botAdvCallback.value,enabled:botEnabled.value==='true',min_score:+botScore.value,min_quality:botQuality.value,trade_style:botStyle.value,direction:botDirection.value,amount_usd:+botAmount.value,max_open:+botMaxOpen.value,trailing_pct:+botTrail.value,min_rr:+botMinRR.value,cooldown_minutes:+botCooldown.value,max_symbol_trades_day:+botMaxSymbolDay.value,max_daily_loss_pct:+botDailyLoss.value,symbols:botSymbols.value,dca_enabled:botDcaEnabled.value==='true',dca_gap_pct:+botDcaGap.value,dca_max_orders:+botDcaMax.value,dca_multiplier:+botDcaMultiplier.value,dca_range_min_pct:+botDcaMinPct.value||0,dca_range_max_pct:+botDcaMaxPct.value||0,exit_on_opposite:botOpposite.checked};try{const r=await fetch('/api/auto-trade',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p)}),d=await r.json();if(!r.ok||!d.ok){toast(d.error||'Bot settings failed');return;}botState.textContent=d.config.enabled?'ON':'OFF';botState.style.color=d.config.enabled?'#53d5aa':'';toast(d.config.enabled?'Auto Trade enabled · paper mode':'Auto Trade settings saved');}catch(e){toast('Could not save Auto Trade settings');}}
if('Notification'in window && Notification.permission==='default') Notification.requestPermission();

function speak(text){if(!voiceOn||!('speechSynthesis'in window))return;try{speechSynthesis.cancel();const u=new SpeechSynthesisUtterance(text);u.rate=1;u.lang='en-US';speechSynthesis.speak(u);}catch(e){}}
function beep(n=2){if(!soundOn)return;try{const c=new(window.AudioContext||window.webkitAudioContext);for(let i=0;i<n;i++)setTimeout(()=>{const o=c.createOscillator(),g=c.createGain();o.connect(g);g.connect(c.destination);o.type='sine';o.frequency.value=i%2?640:880;g.gain.value=.12;o.start();g.gain.exponentialRampToValueAtTime(.001,c.currentTime+.2);o.stop(c.currentTime+.2);},i*220);}catch(e){}}
function toast(m,ms=2800){const t=document.getElementById('toast');t.textContent=m;t.style.display='block';setTimeout(()=>t.style.display='none',ms);}
function toggleSound(){soundOn=!soundOn;voiceOn=soundOn;soundBtn.textContent=soundOn?'Voice ON':'Voice OFF';if(soundOn)speak('Voice on');}
async function toggleAuto(){autoScan=!autoScan;await fetch('/api/auto-scan?enabled='+(autoScan?'true':'false'),{method:'POST'});autoBtn.textContent=autoScan?'Auto ON':'Auto OFF';toast(autoScan?'Auto-scan started':'Auto-scan stopped');if(autoScan)runScan();}
function showTab(n){['signals','trades','movers','account'].forEach(x=>{const el=document.getElementById('tab-'+x);if(el)el.style.display=x===n?'block':'none';});document.querySelectorAll('.tabs button').forEach(b=>b.classList.toggle('on',b.dataset.t===n));if(n==='trades')loadTrades();if(n==='movers')loadMovers();if(n==='account')loadAccount();}
function setStyle(s){style=s;pinManual=false;document.querySelectorAll('.filters button').forEach(b=>b.classList.toggle('on',b.dataset.s===s));loadSignals();}
function fmt(n){return Number(n).toLocaleString(undefined,{maximumFractionDigits:2});}
function fmtPrice(n,precision){const v=Number(n);if(!Number.isFinite(v))return '—';const p=precision!=null&&Number.isInteger(Number(precision))?Math.max(0,Math.min(Number(precision),12)):8;return p===0?v.toFixed(0):v.toFixed(p).replace(/0+$/,'').replace(/\.$/,'');}
function fmtSymbolPrice(symbol,n,precision){return fmtPrice(n,precision);}
function durationShort(start,end,seconds){let s=Number.isFinite(Number(seconds))&&seconds!=null?Math.max(0,Math.floor(Number(seconds))):null;if(s===null){const a=Date.parse(start||''),b=Date.parse(end||'');if(!Number.isFinite(a)||!Number.isFinite(b)||b<a)return '—';s=Math.floor((b-a)/1000);}const d=Math.floor(s/86400);s%=86400;const h=Math.floor(s/3600);s%=3600;const m=Math.floor(s/60);s%=60;return [[d,'D'],[h,'H'],[m,'M'],[s,'S']].filter(x=>x[0]>0).map(x=>x[0]+x[1]).join(' ')||'0S';}
function marketStructureHTML(m,p){
 if(!m)return '';
 const pressure=m.market_pressure||{},tfs=pressure.timeframes||{},multi=pressure.multi_exchange||{};
 const localLiq=(m.liquidations||{}).timeframes||{},ml=multi.marketlight||{},remoteLiq=ml.liquidations||{};
 const oiUsd=m.open_interest_usd??ml.open_interest_usd,oi24h=m.oi_change_24h_pct??ml.oi_change_24h_pct;
 const labels=['5m','15m','1h','4h','1d'];
 const fmt=(v,d=1)=>v==null||!Number.isFinite(Number(v))?'—':Number(v).toFixed(d);
 const compact=v=>{if(v==null||!Number.isFinite(Number(v)))return '—';const n=Number(v),a=Math.abs(n);return a>=1e9?(n/1e9).toFixed(1)+'B':a>=1e6?(n/1e6).toFixed(1)+'M':a>=1e3?(n/1e3).toFixed(1)+'K':n.toFixed(0);};
 const cell=tf=>{const x=tfs[tf]||{},buy=Number(x.buy_usdt),sell=Number(x.sell_usdt),long=x.long_pct,short=x.short_pct;const bc=Number.isFinite(buy)&&Number.isFinite(sell)?(buy>=sell?'var(--gn)':'var(--rd)'):'var(--text)',sc=Number.isFinite(buy)&&Number.isFinite(sell)?(sell>=buy?'var(--rd)':'var(--gn)'):'var(--text)';const flow=(v,pct)=>v==null||!Number.isFinite(Number(v))?'—':compact(v)+(pct!=null?' '+fmt(pct)+'%':'');return '<tr><td style="font-weight:800;text-align:left;padding:2px 1px">'+tf+'</td><td style="text-align:left;color:var(--gn);font-weight:800;padding:2px 0">'+(long==null?'—':fmt(long)+'%')+'</td><td style="text-align:left;color:var(--rd);font-weight:800;padding:2px 0">'+(short==null?'—':fmt(short)+'%')+'</td><td style="padding:2px 0;white-space:nowrap;font-size:9px"><div style="display:grid;grid-template-columns:1fr auto 1fr;text-align:center;gap:2px"><span style="color:'+bc+';font-weight:700">'+flow(x.buy_usdt,x.buy_pct)+'</span><span style="opacity:.5">/</span><span style="color:'+sc+';font-weight:700">'+flow(x.sell_usdt,x.sell_pct)+'</span></div></td><td style="text-align:right;font-weight:800;color:'+bc+';padding:2px 1px">'+compact(x.total_volume_usdt)+'</td></tr>';};
 const hasRows=labels.some(tf=>tfs[tf]&&Object.values(tfs[tf]).some(v=>v!=null));
 const liqRow=tf=>{const remote=remoteLiq[tf]||{},local=localLiq[tf]||{},hasRemote=remote.long_usdt!=null||remote.short_usdt!=null,localReady=!!(m.liquidations&&m.liquidations.connected&&m.liquidations.collected_since),x=hasRemote?remote:(localReady?local:{});return '<span style="white-space:nowrap"><b>'+tf.toUpperCase()+'</b> <span style="color:var(--gn)">L '+compact(x.long_usdt)+'</span> / <span style="color:var(--rd)">S '+compact(x.short_usdt)+'</span></span>';};
 const box='margin-top:4px;padding:4px 4px;border:1px solid var(--bd,#263449);border-radius:7px;font-size:9px;line-height:1.25;min-width:0;width:100%;max-width:100%;box-sizing:border-box;overflow:hidden';
 const oiColor='var(--cyan)';
 const book=multi.book_imbalance_pct,bookColor=book==null?'var(--mut)':Number(book)>0?'var(--gn)':Number(book)<0?'var(--rd)':'var(--mut)';
 return '<div class="market-pressure" style="'+box+'"><b style="font-size:10px;letter-spacing:.04em">MARKET STRUCTURE</b>'+
 '<div style="width:100%;min-width:0;overflow:hidden"><table style="width:100%;min-width:0;table-layout:fixed;border-collapse:collapse;margin-top:2px;font-size:10px"><colgroup><col style="width:9%"><col style="width:15%"><col style="width:16%"><col style="width:40%"><col style="width:20%"></colgroup><thead><tr style="color:var(--mut);white-space:nowrap"><th style="text-align:left;padding:2px 1px;white-space:nowrap">TF</th><th style="text-align:left;padding:2px 0;white-space:nowrap">LONG %</th><th style="text-align:left;padding:2px 0;white-space:nowrap">SHORT %</th><th style="padding:2px 0;white-space:nowrap"><div style="display:grid;grid-template-columns:1fr auto 1fr;text-align:center;gap:2px"><b style="color:var(--gn)">BUY</b><span></span><b style="color:var(--rd)">SELL</b></div></th><th style="text-align:right;padding:2px 1px;white-space:nowrap">VOL</th></tr></thead><tbody>'+labels.map(cell).join('')+'</tbody></table></div>'+
 '<div style="display:flex;align-items:center;gap:6px 12px;flex-wrap:wrap;margin-top:7px;padding-top:6px;border-top:1px solid var(--bd,#263449)"><b>LIQ</b>'+['1h','4h','1d'].map(liqRow).join('')+'</div>'+
 '<div style="display:flex;gap:5px 12px;flex-wrap:wrap;margin-top:6px;padding-top:5px;border-top:1px solid var(--bd,#263449);font-size:10px"><span>OI <b style="color:var(--text)">'+compact(oiUsd)+' USDT</b></span><span>OI 24H <b style="color:'+oiColor+'">'+(oi24h==null?'—':(Number(oi24h)>0?'+':'')+fmt(oi24h)+'%')+'</b></span><span>Book <b style="color:'+bookColor+'">'+(book==null?'—':(Number(book)>0?'+':'')+fmt(book)+'%')+'</b></span></div>'+
 (hasRows?'':'<div style="opacity:.7;margin-top:4px">No market data yet</div>')+
 '</div>';
}
function timeframeHTML(s){
 const trends=s.timeframe_trends||{},items=['15m','1h','4h','1d'].filter(tf=>trends[tf]).map(tf=>({label:tf.toUpperCase(),trend:trends[tf]}));
 const bias=v=>{const x=String(v||'').toLowerCase();return x==='bull'?'Bull':x==='bear'?'Bear':x?x[0].toUpperCase()+x.slice(1):'—';};
 const cls=v=>String(v||'').toLowerCase()==='bull'?'up':String(v||'').toLowerCase()==='bear'?'dn':'side';
 const htf='<span class="tf-chip '+(String(s.bias_1d).toLowerCase()==='bear'?'dn':String(s.bias_1d).toLowerCase()==='bull'?'up':'side')+'">HTF '+bias(s.bias_1d)+'/'+bias(s.bias_4h)+'</span>';
 const entry=s.entry_timeframe||({'scalp':'5m','day':'15m','swing':'1h','hybrid':'15m'}[String((s.styles||[s.style||'day'])[0]).toLowerCase()]||'15m');
 return '<div class="tf-strip">'+(items.map(x=>'<span class="tf-chip '+(x.trend==='UP'?'up':x.trend==='DOWN'?'dn':'side')+'">'+x.label+' '+x.trend+'</span>').join('')||'<span class="tf-chip side">TF —</span>')+htf+'</div>';
}
function marketSummaryHTML(t){
 const m=t.market_structure||t,price=(v)=>v==null?'—':fmtSymbolPrice(t.symbol,v,t.price_precision);
 const pct=(v)=>'<span class="'+(Number(v)>=0?'up':'dn')+'">'+(v==null?'—':(Number(v)>0?'+':'')+fmt(v)+'%')+'</span>';
 return '<div class="market-summary"><span class="ms-label">24H</span><span>O: <b class="open">'+price(t.daily_open)+'</b></span><span>H: <b class="high">'+price(t.daily_high)+'</b></span><span>L: <b class="low">'+price(t.daily_low)+'</b></span><span>7D '+pct(t.change_7d_pct)+'</span><span>30D '+pct(t.change_30d_pct)+'</span><span>1Y '+pct(t.change_1y_pct)+'</span><span>S: <b class="support">'+price(m.support)+'</b></span><span>R: <b class="resistance">'+price(m.resistance)+'</b></span></div>';
}
function setAcc(a){if(!a)return;sBal.textContent='$'+fmt(a.balance);sEq.textContent='$'+fmt(a.equity);sReal.textContent=(a.realized_pnl>=0?'+':'')+'$'+fmt(a.realized_pnl);sReal.className=a.realized_pnl>=0?'up':'dn';if(window.aStart){aStart.textContent='$'+fmt(a.start_balance);aBal.textContent='$'+fmt(a.balance);aEq.textContent='$'+fmt(a.equity);aReal.textContent=(a.realized_pnl>=0?'+':'')+'$'+fmt(a.realized_pnl);}}
function renderStats(st,el){if(!st||!el)return;el.innerHTML=`
  <div class="metric"><label>Trades</label><b>${st.total_trades||0}</b></div>
  <div class="metric"><label>Win / Loss</label><b class="up">${st.wins||0}</b> / <b class="dn">${st.losses||0}</b></div>
  <div class="metric"><label>Win rate</label><b>${st.win_rate||0}%</b></div>
  <div class="metric"><label>Total PnL</label><b class="${(st.total_pnl||0)>=0?'up':'dn'}">${(st.total_pnl||0)>=0?'+':''}$${fmt(st.total_pnl||0)}</b></div>
  <div class="metric"><label>Open</label><b>${st.open||0}</b></div>
  <div class="metric"><label>Open Unrealized PnL</label><b class="${(st.unrealized_pnl||0)>=0?'up':'dn'}">${(st.unrealized_pnl||0)>=0?'+':''}${fmt(st.unrealized_pnl||0)}</b></div>`;}
function syncSizeMode(){const m=m_mode.value;box_amount.style.display=m==='amount'?'block':'none';box_margin.style.display=m==='margin'?'block':'none';box_risk.style.display=m==='risk'?'block':'none';box_qty.style.display=m==='qty'?'block':'none';updateOrderPreview();}
function setSide(side){m_side.value=side;const isLong=side==='LONG';sideLong.classList.toggle('selected',isLong);sideShort.classList.toggle('selected',!isLong);sideLong.setAttribute('aria-pressed',String(isLong));sideShort.setAttribute('aria-pressed',String(!isLong));updateOrderPreview();}
function actualOrderType(){return m_order_type.value==='MARKET'?'MARKET':'LIMIT';}
function toggleOrderType(){const selected=m_order_type.value;const lim=selected!=='MARKET';m_scale.value=selected==='SCALED_FLAT'?'flat':selected==='SCALED_ASCENDING'?'ascending':'';m_entry_label.textContent=lim?'Limit Price':'Market Entry';limit_hint.style.display=lim?'block':'none';m_entry.readOnly=!lim;if(!lim)m_entry.value=m_mkt.value||m_entry.value;document.getElementById('scaledOrderFields').style.display=m_scale.value?'block':'none';updateOrderPreview();}
function updateOrderPreview(){
 const endSpan='<'+'/span>',endDiv='<'+'/div>',endB='<'+'/b>';
 let px=+(m_entry.value||m_mkt.value||0);
 const lev=+m_lev.value||1,mode=m_mode.value;
 const rangeLow=+m_scale_low.value||0,rangeHigh=+m_scale_high.value||0;
 if(m_scale.value&&rangeLow>0&&rangeHigh>rangeLow)px=(rangeLow+rangeHigh)/2;
 document.getElementById('scaledOrderFields').style.display=m_scale.value?'block':'none';
 document.getElementById('scaleRangeFields').style.display=m_scale.value?'grid':'none';
 let notional=0;
 if(mode==='amount')notional=+m_amount.value||0;
 else if(mode==='margin')notional=(+m_margin.value||0)*lev;
 else if(mode==='qty')notional=(+m_qty.value||0)*px;
 else if(mode==='risk'){const bal=+(sBal.textContent||'').replace(/[^0-9.]/g,'')||10000;const risk=(+m_risk.value||1)/100*bal,dist=Math.abs(px-(+m_sl.value||px));notional=dist>0?risk/dist*px:0;}
 const qty=px>0?notional/px:0,margin=lev>0?notional/lev:0;
 order_preview.textContent='Value '+fmt(notional)+' USDT · Margin '+fmt(margin)+' USDT · Qty '+qty.toFixed(6);
 const n=parseInt(m_scale_n.value||'',10),sm=m_scale.value,sp=document.getElementById('scale_preview');
 if(sm&&Number.isInteger(n)&&n>=2&&n<=50&&notional>0&&px>0){
   const market=+(m_mkt.value||0),rows=[];let totalQty=0;
   if(actualOrderType()!=='LIMIT'){
     sp.style.display='block';sp.innerHTML='<div class="hint">Scaled orders are available for Limit orders only.</div>';
   }else if(!(rangeLow>0&&rangeHigh>rangeLow)){
     sp.style.display='block';sp.innerHTML='<div class="hint">Enter a valid Lower Range and Upper Range to preview the order ladder.</div>';
   }else if(!(market>0)||((m_side.value==='LONG'&&rangeHigh>=market)||(m_side.value==='SHORT'&&rangeLow<=market))){
     sp.style.display='block';sp.innerHTML='<div class="hint">'+(market>0?'LONG range must be below market; SHORT range must be above market.':'Waiting for live market price.')+'</div>';
   }else{
     let cumulativeQty=0,cumulativeCost=0;
     for(let i=0;i<n;i++){
       const weight=sm==='ascending'?(i+1)/(n*(n+1)/2):1/n;
       const price=m_side.value==='LONG'?rangeHigh-(rangeHigh-rangeLow)*i/(n-1):rangeLow+(rangeHigh-rangeLow)*i/(n-1);
       const amount=notional*weight;
       const childQty=Math.round((amount/price)*1e6)/1e6;
       totalQty+=childQty;cumulativeQty+=childQty;cumulativeCost+=price*childQty;
       const avgAfter=cumulativeQty>0?cumulativeCost/cumulativeQty:0;
       rows.push('<div class="scale-row"><b>#'+(i+1)+endB+'<span>'+fmtPrice(price)+endSpan+'<span>$'+fmt(amount)+endSpan+'<span>'+childQty.toFixed(6)+endSpan+'<b>'+fmtPrice(avgAfter)+endB+endDiv);
     }
     const finalAverage=totalQty>0?cumulativeCost/totalQty:0;
     sp.style.display='block';
     sp.innerHTML='<div class="scale-row scale-head"><b>Step'+endB+'<span>Order Price'+endSpan+'<span>Amount'+endSpan+'<span>Qty'+endSpan+'<span>Avg Entry After Fill'+endSpan+endDiv+rows.join('')+'<div class="scale-row"><b>Total'+endB+'<span>—'+endSpan+'<b>$'+fmt(notional)+endB+'<b>'+totalQty.toFixed(6)+endB+'<b>'+fmtPrice(finalAverage)+endB+endDiv+'<div class="hint">Final average entry assumes every limit order fills at its preview price.</div>';
   }
 }else{sp.style.display='none';sp.innerHTML='';}
 clearTimeout(window._liqPreviewTimer);
 window._liqPreviewTimer=setTimeout(async()=>{
   try{
     if(!(notional>0&&px>0&&+m_sl.value>0)){liq_preview.textContent='Add amount + SL for liquidation estimate.';return;}
     const pb={symbol:m_symbol.value,side:m_side.value,order_type:actualOrderType(),entry:px,limit_price:actualOrderType()==='LIMIT'?px:null,stop_loss:+m_sl.value,tp1:+m_tp1.value||0,leverage:lev,margin_mode:m_mm.value};
     if(mode==='amount')pb.amount_usd=+m_amount.value||0;
     else if(mode==='margin')pb.margin_usd=+m_margin.value||0;
     else if(mode==='qty')pb.quantity=+m_qty.value||0;
     else pb.risk_pct=+m_risk.value||1;
     const res=await fetch('/api/trade-preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(pb)});
     const data=await res.json();
     liq_preview.textContent=data.ok?(data.liquidation_price?'Liq. est. '+fmt(data.liquidation_price)+' · '+data.liquidation_risk+' · '+(data.liquidation_distance_pct==null?'—':data.liquidation_distance_pct+'%')+' away · est.':(data.no_liquidation_price?'No liq. price at current cross equity · SAFE':'Liq. estimate unavailable · check size')):(data.error||'Liquidation preview unavailable');
   }catch(e){liq_preview.textContent='Liquidation preview unavailable';}
 },350);
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
  // Keep signal payload out of inline HTML/JS: reasons or setup text may contain apostrophes.
  window._signalCache=window._signalCache||Object.create(null);
  window._signalCache[String(s.symbol)+'|'+String(s.side)]=s;
  const btn=`<button class="btn good" onclick="openSignalTrade('${String(s.symbol)}','${String(s.side)}')">Add Trade</button>`;
  const v5Details=s.v5_score_breakdown?'<div class="sub">V5 Score · '+Object.entries(s.v5_score_breakdown).map(([k,v])=>k.replaceAll('_',' ')+' '+v.points+'/'+v.weight).join(' · ')+'</div>':'';
  const v5Structure=s.v5_structure?`<div class="sub">Structure · ${s.v5_structure.bias||'neutral'} · BOS ${s.v5_structure.bullish_bos?'↑':s.v5_structure.bearish_bos?'↓':'—'} · Sweep ${s.v5_structure.bullish_sweep?'bullish':s.v5_structure.bearish_sweep?'bearish':'—'} · Swing ${s.v5_structure.higher_highs&&s.v5_structure.higher_lows?'HH/HL':s.v5_structure.lower_highs&&s.v5_structure.lower_lows?'LH/LL':'mixed'}</div>`:'';
  return `<article class="card ${L?'L':'S'}">
    <div class="head"><div><span class="side ${L?'L':'S'}">${s.side}</span><span class="sym">${s.symbol}</span>
      <span class="tag q">${s.quality||'B'}</span><span class="tag">${lev?lev+'x max':'20x max'}</span><span class="tag ${(Number(s.daily_change_pct)||0)>=0?'up':'dn'}">${s.daily_change_pct==null?'—':(Number(s.daily_change_pct)>=0?'+':'')+Number(s.daily_change_pct).toFixed(2)+'% 24H'}</span></div>
      <div class="score">${s.score}</div></div>
      <div class="sub">${s.setup||''} · ${(s.styles||[]).map(x=>({scalp:'Scalp',day:'Day',swing:'Swing',hybrid:'Hybrid'}[String(x).toLowerCase()]||String(x))).join('/')} · Entry ${s.entry_timeframe||({'scalp':'5m','day':'15m','swing':'1h','hybrid':'15m'}[String((s.styles||['day'])[0]).toLowerCase()]||'15m')} · ADX ${s.adx??'—'} · RSI ${s.rsi??'—'}</div>
      ${timeframeHTML(s)}
      ${marketSummaryHTML(s)}
    <div class="vals">
      <div><em>Entry</em><strong>${fmtSymbolPrice(s.symbol,s.entry,s.price_precision)}</strong></div>
      <div><em>Market</em><strong>${fmtSymbolPrice(s.symbol,mp,s.price_precision)}</strong></div>
      <div><em>SL</em><strong>${fmtSymbolPrice(s.symbol,s.stop_loss,s.price_precision)}</strong></div>
      <div><em>TP1</em><strong>${fmtSymbolPrice(s.symbol,s.tp1,s.price_precision)}</strong></div>
      <div><em>TP2</em><strong>${fmtSymbolPrice(s.symbol,s.tp2,s.price_precision)}</strong></div>
      <div><em>TP1 RR</em><strong>1:${s.rr}</strong></div>
      <div><em>TP2 RR</em><strong>1:${(s.rr_tp2??'—')}</strong></div>
      <div><em>SL %</em><strong>${s.risk_pct==null?'—':fmt(s.risk_pct)+'%'}</strong></div>
      <div><em>TP1 %</em><strong>${s.tp1_pct==null?'—':fmt(s.tp1_pct)+'%'}</strong></div>
      <div><em>TP2 %</em><strong>${s.tp2_pct==null?'—':fmt(s.tp2_pct)+'%'}</strong></div>
    </div>
    ${v5Details}
    ${v5Structure}
    <div id="ms_${cid}">${marketStructureHTML(s.market_structure,s.price_precision)}</div>
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
    if(!d.signals||!d.signals.length){list.innerHTML='<div class="empty">'+(d.error?('Signal scanner paused: '+d.error):('No matching signals in '+(d.scanned_count||0)+' scanned pairs. Wait for the next scan or tap Scan.'))+'</div>';return;}
    const h=d.signals.map(s=>s.symbol+s.side+s.score).join('|');
    if(lastSig&&h!==lastSig){beep(2);const neu=d.signals.filter(s=>!lastSig.includes(s.symbol+s.side));if(neu.length)speak(neu.slice(0,3).map(s=>`${s.side} ${s.symbol.replace('USDT','')}`).join(', '));}
    lastSig=h;
    list.innerHTML=d.signals.map(cardHTML).join('');
    refreshSignalMarketStructures(d.signals);
  }catch(e){status.textContent='Load failed';}
}
function runScan(){pinManual=false;toast('Scan started');fetch('/scan?top=26').then(()=>setTimeout(loadSignals,3000));}
async function analyzeManual(){
  let s=(symIn.value||'').trim().toUpperCase().replace('/','').replace('-','');
  if(!s){toast('Enter symbol');return;} if(!s.endsWith('USDT'))s+='USDT';
  toast('Analyzing '+s); pinManual=true;
  const d=await(await fetch('/api/analyze/'+s+'?style='+(style==='all'?'day':style))).json();
  const list=document.getElementById('signalList');
  if(!d.ok){list.innerHTML=`<div class="empty">${d.error||'Failed'}</div>`;return;}
  if(!d.signal){const tr=d.timeframe_trends||{},ind=d.indicators||{};list.innerHTML=`<div class="card"><div class="sym">${d.symbol}</div><div class="sub">Market ${d.market_price??'—'} · max ${d.max_leverage||'—'}x</div><div class="sub">ADX ${ind.adx??'—'} · RSI ${ind.rsi??'—'}</div><div class="sub">15M ${tr['15m']||'—'} · 1H ${tr['1h']||'—'} · 4H ${tr['4h']||'—'} · 1D ${tr['1d']||'—'} · HTF ${d.bias_1d||'—'}/${d.bias_4h||'—'}</div><div class="empty">${d.message||'NO TRADE — wait for confirmation'}</div>
    <div class="actions"><button class="btn dim" onclick="pinManual=false;loadSignals()">Back to list</button><button class="btn dim" onclick="toggleChart('chm','${d.symbol}')">Chart</button></div><div class="chartbox" id="chm"></div></div>`;return;}
  list.innerHTML=cardHTML(d.signal)+`<div class="actions" style="padding:0 10px"><button class="btn dim" onclick="pinManual=false;loadSignals()">Back to list</button></div>`;
  speak(`${d.signal.side} ${d.signal.symbol.replace('USDT','')}`);
}
async function refreshSignalMarketStructures(signals){
  for(const s of (signals||[]).slice(0,3)){
    const cid='ch_'+s.symbol+'_'+(s.style||'day');
    const target=document.getElementById('ms_'+cid);
    if(!target)continue;
    try{
      const response=await fetch('/api/market-structure/'+encodeURIComponent(s.symbol));
      if(!response.ok)continue;
      const data=await response.json();
      if(data.ok&&data.market_structure)target.innerHTML=marketStructureHTML(data.market_structure,s.price_precision);
    }catch(e){}
  }
}
function openSignalTrade(symbol,side){
  const signal=window._signalCache&&window._signalCache[String(symbol)+'|'+String(side)];
  if(!signal){toast('Signal details expired. Refresh signals and try again.',5000);return;}
  openModal(signal);
}
function openModal(s){
  window._selectedSignal=s;
  window._selectedTradeStrategy=(style!=='all'?style:((s.styles||[]).find(x=>['scalp','day','swing'].includes(x))||'day'));
  mTitle.textContent=s.symbol;
  m_symbol.value=s.symbol; m_side.value=s.side; m_mm.value='CROSS'; m_order_type.value='MARKET';
  m_entry.value=s.market_price||s.entry; m_mkt.value=s.market_price||s.entry;
  m_sl.value=s.stop_loss; m_tp1.value=s.tp1; m_tp2.value=s.tp2||'';
  m_scale_low.value='';m_scale_high.value='';document.getElementById('scaleRangeFields').style.display='none';document.getElementById('addAdvanced').open=false;['m_adv_dca','m_profit_lock'].forEach(id=>document.getElementById(id).checked=false);['m_dca_gap','m_dca_max','m_dca_mult','m_limit_offset','m_dca_low','m_dca_high','m_sl_pct','m_tp1_pct','m_tp2_pct'].forEach(id=>document.getElementById(id).value='');m_profit_activation.value='0.8';m_profit_callback.value='0.5';
  const ml=s.max_leverage||20; m_lev.value=ml; m_lev.max=ml; m_mode.value='amount'; m_amount.value=''; m_margin.value=''; m_qty.value=''; m_scale.value=''; m_scale_n.value=''; toggleOrderType(); syncSizeMode();
  modalBg.style.display='flex'; updateOrderPreview();
}
function closeModal(){modalBg.style.display='none';}
async function refreshModalMarket(){if(modalBg.style.display!=='flex'||!m_symbol.value)return;try{const d=await(await fetch('/api/market-price/'+encodeURIComponent(m_symbol.value))).json();if(d.price>0){m_mkt.value=d.price;if(m_order_type.value==='MARKET')m_entry.value=d.price;updateOrderPreview();}}catch(e){}}
async function submitTrade(){
  const submitBtn=document.querySelector('#modalBg .btns .btn.good');
  if(!submitBtn){toast('Open Trade button not found. Refresh the page and try again.',5000);return;}
  if(submitBtn.disabled)return;
  const mode=m_mode.value,typ=actualOrderType();
  let px=typ==='MARKET'?(+m_mkt.value||+m_entry.value):(+m_entry.value);
  const rangeLow=+m_scale_low.value||0,rangeHigh=+m_scale_high.value||0;
  if(m_scale.value&&rangeLow>0&&rangeHigh>rangeLow)px=(rangeLow+rangeHigh)/2;
  const size=mode==='amount'?+m_amount.value:mode==='margin'?+m_margin.value:mode==='qty'?+m_qty.value:+m_risk.value;
  if(!(px>0)){toast('Live market / limit price is not available');return;}
  if(!(size>0)){toast('Enter a valid order amount');return;}
  if(!(+m_sl.value>0)){toast('Enter a valid Stop Loss');return;}
  if(!(+m_tp1.value>0)){toast('Enter a valid TP1');return;}
  const adv=document.getElementById('addAdvanced').open,offset=adv?(+m_limit_offset.value||0):0;
  if(offset>0&&typ!=='LIMIT'){toast('Choose Limit order to use Limit Entry Offset');return;}
  if(offset>0&&m_scale.value){toast('Limit offset cannot be combined with scaled entry');return;}
  const signalMeta=window._selectedSignal||{};
  const body={symbol:m_symbol.value,side:m_side.value,trade_style:window._selectedTradeStrategy||'day',signal_adx:signalMeta.adx??null,signal_rsi:signalMeta.rsi??null,timeframe_trends:signalMeta.timeframe_trends||{},bias_1d:signalMeta.bias_1d||null,bias_4h:signalMeta.bias_4h||null,order_type:typ,entry:px,limit_price:typ==='LIMIT'?px:null,stop_loss:+m_sl.value,tp1:+m_tp1.value,tp2:+m_tp2.value||null,leverage:+m_lev.value||20,margin_mode:m_mm.value,scale_mode:m_scale.value||null,scale_orders:+m_scale_n.value||0,scale_lower_price:rangeLow||null,scale_upper_price:rangeHigh||null,advanced_enabled:adv,dca_enabled:adv&&m_adv_dca.checked,dca_gap_pct:+m_dca_gap.value||1.5,dca_max_orders:+m_dca_max.value||3,dca_multiplier:+m_dca_mult.value||1.5,dca_lower_price:+m_dca_low.value||null,dca_upper_price:+m_dca_high.value||null,limit_offset_pct:offset,sl_pct:adv?(+m_sl_pct.value||null):null,tp1_pct:adv?(+m_tp1_pct.value||null):null,tp2_pct:adv?(+m_tp2_pct.value||null):null,profit_lock_enabled:adv&&m_profit_lock.checked,profit_activation_pct:+m_profit_activation.value||0.8,profit_callback_pct:+m_profit_callback.value||0.5};
  if(mode==='amount')body.amount_usd=+m_amount.value;
  if(mode==='margin')body.margin_usd=+m_margin.value;
  if(mode==='risk')body.risk_pct=+m_risk.value||1;
  if(mode==='qty')body.quantity=+m_qty.value;
  if(body.scale_mode&&typ!=='LIMIT'){toast('Scaled orders require Limit order type');return;}
  if(body.scale_mode&&(!Number.isInteger(body.scale_orders)||body.scale_orders<2||body.scale_orders>50)){toast('Set number of scaled orders (2–50)');return;}
  if(body.scale_mode&&(!(rangeLow>0&&rangeHigh>rangeLow))){toast('Set a valid Lower Range and Upper Range');return;}
  if(body.scale_mode&&((m_side.value==='LONG'&&rangeHigh>=(+m_mkt.value||0))||(m_side.value==='SHORT'&&rangeLow<=(+m_mkt.value||0)))){toast('LONG range must be below market; SHORT range must be above market');return;}
  if(body.scale_mode&&body.scale_orders>=2&&!confirm('Place '+body.scale_orders+' separate pending orders?'))return;
  submitBtn.disabled=true;submitBtn.textContent='Placing…';
  try{
    const response=await fetch('/api/trades',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const d=await response.json();
    if(response.ok&&d.ok){closeModal();toast(d.pending?'Limit order placed':(d.auto_dca?'Auto DCA added':(d.duplicate?'Position updated':'Trade opened')));speak(m_symbol.value.replace('USDT','')+' opened');if(d.account)setAcc(d.account);pinManual=false;showTab('trades');await loadTrades();loadSignals();}
    else{const detail=Array.isArray(d.detail)?d.detail.map(x=>x.msg||x.loc?.join('.')).join(', '):d.detail;toast(d.error||detail||('Order failed · HTTP '+response.status),5000);}
  }catch(e){console.error('submitTrade',e);toast('Order request failed: '+(e.message||'network error'),5000);}
  finally{submitBtn.disabled=false;submitBtn.textContent='Open Trade';}
}
async function loadTrades(){
  if(window._loadingTrades)return;window._loadingTrades=true;
  const controller=new AbortController();
  const timeout=setTimeout(()=>controller.abort(),8000);
  try{const scrollY=window.scrollY;const openCharts=[...document.querySelectorAll('#tradeList .chartbox[style*="display: block"]')];const pendingExpanded=document.getElementById('pendingOrdersDetails')?.open||false;const openExpanded=document.getElementById('openTradesDetails')?.open??true;const closedExpanded=document.getElementById('closedTradesDetails')?.open??false;
  const response=await fetch('/api/trades',{cache:'no-store',signal:controller.signal});
  if(!response.ok)throw new Error('Trade list HTTP '+response.status);
  const d=await response.json(); setAcc(d.account); renderStats(d.stats,tradeStats); sOpen.textContent=(d.stats&&d.stats.open)||0;
  window._tradeCache=Object.fromEntries((d.trades||[]).map(t=>[String(t.id),t]));
  if(Date.now()-(window._lastTradeScrollAt||0)<1500)return;
  const selection=window.getSelection();
  if(selection&&!selection.isCollapsed){window._tradeCache=Object.fromEntries((d.trades||[]).map(t=>[String(t.id),t]));return;}
  const chartIsOpen=!!document.querySelector('#tradeList .chartbox[style*="display: block"]');
  const manageIsOpen=document.getElementById('manageBg')?.style.display==='flex';
  if(chartIsOpen||manageIsOpen)return;
  const open=(d.trades||[]).filter(t=>t.status==='OPEN');
  const pending=(d.trades||[]).filter(t=>t.status==='PENDING');
  const closed=(d.trades||[]).filter(t=>t.status==='CLOSED');
  let h='';
  if(pending.length){h+='<details id="pendingOrdersDetails" class="trade-section" '+(pendingExpanded?'open':'')+'><summary class="sec" style="cursor:pointer">Pending Orders ('+pending.length+') <button class="btn bad" style="float:right" onclick="cancelAllPending(event)">Cancel All</button></summary>';h+=pending.map(t=>`<div class="list-row"><span><b class="side ${t.side==='LONG'?'L':'S'}">${t.side}</b> ${t.symbol} · LIMIT ${fmtSymbolPrice(t.symbol,t.limit_price,t.price_precision)} · ${fmt(t.notional)} · Qty ${t.qty} · ${t.leverage}x</span><button class="btn dim" onclick="cancelPending('${t.id}')">Cancel</button></div>`).join('');h+='</details>';}
  window._tradeCache=Object.fromEntries((d.trades||[]).map(t=>[String(t.id),t]));
  if(open.length){h+='<details id="openTradesDetails" class="trade-section" '+(openExpanded?'open':'')+'><summary class="sec" style="cursor:pointer">Running Trades ('+open.length+')</summary>';h+=open.map(t=>{
    const L=t.side==='LONG';const up=(t.unrealized_pnl||0)>=0;
    const trail=t.callback_rate?(t.callback_rate+'%'):((t.trailing_pct||0)?(t.trailing_pct+'%'):'—');
    return `<article class="card ${L?'L':'S'}${t.hit?' hit':''}">
      <div class="head"><div><span class="side ${L?'L':'S'}">${t.side}</span><span class="sym">${t.symbol}</span>
        <span class="tag">${t.leverage}x</span><span class="tag">${t.margin_mode||'CROSS'}</span><span class="tag">${({scalp:'Scalp',day:'Day',swing:'Swing'}[String(t.trade_style||'day').toLowerCase()]||'Day')}</span>
        <span class="tag ${t.daily_change_pct==null?'':(Number(t.daily_change_pct)>=0?'up':'dn')}">${t.daily_change_pct==null?'—':(Number(t.daily_change_pct)>=0?'+':'')+Number(t.daily_change_pct).toFixed(2)+'% 24H'}</span>
        ${t.auto_trade?'<span class="tag" style="background:#164e3b;color:#6ee7b7">BOT</span>':''}
        ${t.hit?`<span class="tag" style="background:#fbbf24;color:#111">${t.hit}</span>`:''}
        ${(t.dca_count||0)?`<span class="tag">DCA ${t.dca_count}</span>`:''}</div>
        <div class="score" style="color:${up?'var(--gn)':'var(--rd)'}">${up?'+':''}$${fmt(t.unrealized_pnl||0)}</div></div>
${t.analysis_alert?'<div class="hint" style="border:1px solid #d9a441;padding:8px;margin:8px 0;border-radius:8px">⚠ '+t.analysis_alert+'</div>':''}
      ${t.trailing_recommendation?'<div class="hint" style="border:1px solid #22d3ee;padding:8px;margin:8px 0;border-radius:8px">Suggested trailing: activate at +'+t.trailing_recommendation.activation_profit_pct+'% profit · callback '+t.trailing_recommendation.callback_pct+'% <button class="btn dim" onclick="applySuggestedTrail(\''+t.id+'\','+t.trailing_recommendation.activation_profit_pct+','+t.trailing_recommendation.callback_pct+')">Apply</button></div>':''}
      ${marketSummaryHTML(t)}
      <div class="sub">ADX ${t.signal_adx??"—"} · RSI ${t.signal_rsi??"—"} · 15M ${(t.timeframe_trends||{})["15m"]||"—"} · 1H ${(t.timeframe_trends||{})["1h"]||"—"} · 4H ${(t.timeframe_trends||{})["4h"]||"—"} · 1D ${(t.timeframe_trends||{})["1d"]||"—"} · HTF ${t.bias_1d||"—"}/${t.bias_4h||"—"}</div>
      <div class="vals">
        <div><em>Average Entry</em><strong>${fmtSymbolPrice(t.symbol,t.entry,t.price_precision)}</strong></div>
        <div><em>Mark Price</em><strong>${t.current_price==null?'—':fmtSymbolPrice(t.symbol,t.current_price,t.price_precision)}</strong></div>
        <div><em>Position Value</em><strong>${fmt((t.current_price||t.entry)*(t.qty||0))}</strong></div>
<div><em>Liquidation Price</em><strong>${t.liquidation_price?(t.liquidation_risk==="LOW"?"✓ Safe · ":"⚠ ")+fmtSymbolPrice(t.symbol,t.liquidation_price,t.price_precision):"—"}</strong></div>
        <div><em>SL %</em><strong>${fmtSymbolPrice(t.symbol,t.stop_loss,t.price_precision)} · ${Number(t.entry)>0?Math.abs((Number(t.stop_loss)-Number(t.entry))/Number(t.entry)*100).toFixed(2):'—'}%</strong></div>
        <div><em>TP1 %</em><strong>${fmtSymbolPrice(t.symbol,t.tp1,t.price_precision)} · ${Number(t.entry)>0?((t.side==='LONG'?(Number(t.tp1)-Number(t.entry)):(Number(t.entry)-Number(t.tp1)))/Number(t.entry)*100).toFixed(2):'—'}%</strong></div>
                <div><em>Margin</em><strong>${fmt(t.margin)}</strong></div>
        <div><em>Qty</em><strong>${t.qty}</strong></div>
        <div><em>Trade Amount</em><strong>${fmt(t.notional||((Number(t.entry)||0)*(Number(t.qty)||0)))}</strong></div>
        <div><em>PnL%</em><strong class="${up?'up':'dn'}">${(Number(t.margin)>0?((Number(t.unrealized_pnl)||0)/Number(t.margin))*100:(Number(t.pnl_pct)||0)).toFixed(2)}%</strong></div>
        <div><em>Liq. Distance</em><strong>${t.liquidation_distance_pct==null?"—":fmt(t.liquidation_distance_pct)+"%"}</strong></div>

        <div><em>Trail</em><strong>${trail}${t.trailing_active?" · ACTIVE":""}</strong></div>
        <div class="trade-time"><em>Opened</em><strong>${(t.opened_at||t.filled_at||t.created_at)?new Date(t.opened_at||t.filled_at||t.created_at).toLocaleString():'—'}</strong></div>
      </div>
      ${marketStructureHTML(t.market_structure,t.price_precision)}
      <div class="actions">
        <button class="btn good" onclick="addDcaTrade('${t.id}')">Add Trade / DCA</button>
        <button class="btn bad" onclick="closeTrade('${t.id}')">Close</button>
        <button class="btn dim" onclick="trailPrompt('${t.id}',${t.callback_rate||t.trailing_pct||0},${t.trailing_activation_price||0})">Trailing Stop</button>
        <button class="btn dim" onclick="editLevels('${t.id}',${t.stop_loss},${t.tp1},${t.tp2||0})">SL/TP</button>
        <button class="btn dim" onclick="shareTrade('${t.id}')">Share</button>
        <button class="btn dim" onclick="toggleChart('ct_${t.id}','${t.symbol}')">Chart</button>
      </div>
      <div class="chartbox" id="ct_${t.id}"></div></article>`;}).join('');h+='</details>';}
  if(closed.length){h+='<details id="closedTradesDetails" class="trade-section" '+(closedExpanded?'open':'')+'><summary class="sec" style="cursor:pointer">Closed Trade History ('+closed.length+')</summary>';h+=closed.slice().sort((a,b)=>String(b.closed_at||'').localeCompare(String(a.closed_at||''))).slice(0,50).map(t=>{
    const pnl=Number(t.realized_pnl)||0,exit=Number(t.close_price)||Number(t.current_price)||0;
    const openedStamp=t.opened_at||t.filled_at||t.created_at;const openedAt=openedStamp?new Date(openedStamp).toLocaleString():'—';
    const closedAt=t.closed_at?new Date(t.closed_at).toLocaleString():'—';
    const tradeAmount=Array.isArray(t.dca_history)&&t.dca_history.length?t.dca_history.reduce((sum,x)=>sum+(Number(x.notional)||0),0):(Number(t.notional)||((Number(t.entry)||0)*(Number(t.qty)||0)));
    return `<div class="history-trade"><div class="head"><span><b class="side ${t.side==="LONG"?"L":"S"}">${t.side}</b> <b>${t.symbol}</b> <span class="tag">${t.leverage||'—'}x</span> <span class="tag">${t.margin_mode||'CROSS'}</span> <span class="tag ${t.daily_change_pct==null?'':(Number(t.daily_change_pct)>=0?'up':'dn')}">${t.daily_change_pct==null?'—':(Number(t.daily_change_pct)>=0?'+':'')+Number(t.daily_change_pct).toFixed(2)+'%'} 24H</span> <span class="tag">${({scalp:'Scalp',day:'Day',swing:'Swing'}[String(t.trade_style||'day').toLowerCase()]||'Day')}</span> ${t.auto_trade?'<span class="tag">BOT</span>':''} · ${t.hit||"CLOSED"}</span><b style="color:${pnl>=0?'var(--gn)':'var(--rd)'}">${pnl>=0?'+':''}${fmt(pnl)}</b></div><div class="vals"><div><em>Entry Price</em><strong>${fmtSymbolPrice(t.symbol,t.entry,t.price_precision)}</strong></div><div><em>Close Price</em><strong>${exit?fmtSymbolPrice(t.symbol,exit,t.price_precision):'—'}</strong></div><div><em>Quantity</em><strong>${fmt(t.qty)}</strong></div><div><em>Trade Amount</em><strong>${fmt(tradeAmount)} USDT</strong></div><div class="trade-time"><em>Opened</em><strong>${openedAt}</strong></div><div class="trade-time"><em>Closed</em><strong>${closedAt}</strong></div><div class="trade-time"><em>Time</em><strong>${durationShort(openedStamp,t.closed_at,t.duration_seconds)}</strong></div><div><em>SL %</em><strong>${fmtSymbolPrice(t.symbol,t.stop_loss,t.price_precision)} · ${Number(t.entry)>0?Math.abs((Number(t.stop_loss)-Number(t.entry))/Number(t.entry)*100).toFixed(2):'—'}%</strong></div><div><em>TP1 %</em><strong>${fmtSymbolPrice(t.symbol,t.tp1,t.price_precision)} · ${Number(t.entry)>0?((t.side==='LONG'?(Number(t.tp1)-Number(t.entry)):(Number(t.entry)-Number(t.tp1)))/Number(t.entry)*100).toFixed(2):'—'}%</strong></div></div>${marketSummaryHTML(t)}${marketStructureHTML(t.market_structure,t.price_precision)}<div class="actions"><button class="btn dim" onclick="shareTrade('${t.id}')">Share</button></div></div>`;
  }).join('');h+='</details>';}
  // Keep the page anchored while refreshing trade data; rebuilding cards must not jump mobile scroll.
  const tradeListHeight=tradeList.offsetHeight;
  tradeList.style.minHeight=tradeListHeight+'px';
  tradeList.innerHTML=h||'<div class="empty">No trades</div>';
  // Defensive cleanup: never show TP2 as a field on an open My Trades card.
  tradeList.querySelectorAll('.card').forEach(card=>{if(card.querySelector('.actions button[onclick^="closeTrade("]'))card.querySelectorAll('.vals > div').forEach(row=>{const label=row.querySelector('em');if(label&&label.textContent.trim().toUpperCase()==='TP2')row.remove();});});
  for(const oldNode of openCharts){const fresh=document.getElementById(oldNode.id);if(fresh)fresh.replaceWith(oldNode);}
  requestAnimationFrame(()=>{window.scrollTo({left:0,top:scrollY,behavior:'instant'});requestAnimationFrame(()=>{tradeList.style.minHeight='';window.scrollTo({left:0,top:scrollY,behavior:'instant'});});});
  }catch(e){console.error('loadTrades',e);}finally{clearTimeout(timeout);window._loadingTrades=false;}
}

async function addDcaTrade(id){
  const raw=prompt('Additional position size in USDT notional (market price will be used):');
  if(raw===null)return;
  const amount=Number(raw);
  if(!(Number.isFinite(amount)&&amount>0)){toast('Enter a valid USDT amount');return;}
  if(!confirm('Add '+fmt(amount)+' USDT to this running paper trade? Average entry and size will change. Existing SL is not automatically widened.'))return;
  try{
    const response=await fetch('/api/trades/'+encodeURIComponent(id)+'/dca',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({amount_usd:amount})});
    const data=await response.json();
    if(!response.ok||!data.ok){toast(data.error||'DCA failed');return;}
    toast('Additional paper entry added');loadTrades();
  }catch(e){toast('DCA failed: '+(e.message||'network error'));}
}
function shareTrade(id){const t=window._tradeCache&&window._tradeCache[String(id)];if(!t){toast('Trade details unavailable');return;}window._sharingTrade=t;shareBg.style.display='flex';drawShareCard(t);}
function closeShare(){shareBg.style.display='none';}
function drawShareCard(t){
 const c=shareCanvas,ctx=c.getContext('2d'),w=c.width,h=c.height;
 const closed=t.status==='CLOSED',pnl=closed?(Number(t.realized_pnl)||0):((Number(t.realized_pnl)||0)+(Number(t.unrealized_pnl)||0)),exit=closed?(Number(t.close_price)||Number(t.current_price)||0):(Number(t.current_price)||Number(t.entry)||0);
 const positive=pnl>=0,green='#16c784',red='#f6465d';
 const bg=ctx.createLinearGradient(0,0,w,h);bg.addColorStop(0,'#101b2d');bg.addColorStop(1,'#070b14');ctx.fillStyle=bg;ctx.fillRect(0,0,w,h);
 function rr(x,y,ww,hh,r,fill,stroke){ctx.beginPath();ctx.roundRect(x,y,ww,hh,r);ctx.fillStyle=fill;ctx.fill();if(stroke){ctx.strokeStyle=stroke;ctx.lineWidth=2;ctx.stroke();}}
 function txt(value,x,y,size,color,weight){ctx.fillStyle=color;ctx.font=(weight||'500')+' '+size+'px Inter,Arial,sans-serif';ctx.fillText(String(value),x,y);}
 rr(42,42,w-84,h-84,28,'#0b1322','#23334d');
 txt('DAILY SIGNAL',82,112,28,'#22d3ee','800');txt('TRADE RECEIPT',82,154,17,'#8798b5','700');
 rr(82,192,w-164,132,20,'#101d31');txt(t.symbol||'—',112,250,42,'#f2f6ff','800');
 const side=String(t.side||'').toUpperCase();rr(112,270,side==='LONG'?130:140,34,10,side==='LONG'?'#123b35':'#421c2b');txt(side,132,295,19,side==='LONG'?'#16c784':'#f6465d','800');
 const status=closed?'CLOSED':'RUNNING';txt(status,w-245,295,17,closed?'#a8b4c8':'#22d3ee','800');
  const qty=Number(t.qty)||0;const leverage=Number(t.leverage)||1;const notional=Number(t.notional)||((Number(t.entry)||0)*qty);const margin=Number(t.margin)||(notional/leverage);const pnlPct=margin>0?(pnl/margin)*100:0;const rows=[['ENTRY PRICE',fmtSymbolPrice(t.symbol,t.entry,t.price_precision)],[closed?'CLOSE PRICE':'MARK PRICE',fmtSymbolPrice(t.symbol,exit,t.price_precision)],['TRADE AMOUNT','$'+fmt(notional)],['QUANTITY',fmt(qty)],['LEVERAGE',leverage+'x'],['MARGIN','$'+fmt(margin)+' USDT']];
  txt(closed?'REALIZED PNL':'TOTAL PNL',112,390,18,'#8798b5','700');txt((positive?'+':'-')+'$'+fmt(Math.abs(pnl)),112,475,66,positive?green:red,'800');
  ctx.textAlign='right';txt('PNL %',788,390,18,'#8798b5','700');txt((pnl>=0?'+':'')+fmt(pnlPct)+'%',788,475,42,positive?green:red,'800');ctx.textAlign='left';
    for(let i=0;i<rows.length;i++){const col=i%2,row=Math.floor(i/2),x=112+col*340,yy=565+row*94;txt(rows[i][0],x,yy,15,'#8191ad','700');txt(rows[i][1],x,yy+42,24,'#edf3ff','700');}
 const openedStamp=t.created_at?new Date(t.created_at).toLocaleString():'—'; const closedStamp=t.closed_at?new Date(t.closed_at).toLocaleString():'—'; const stamp=closed?('Opened '+openedStamp+' · Closed '+closedStamp):('Opened '+openedStamp);
 ctx.strokeStyle='#25334a';ctx.beginPath();ctx.moveTo(112,925);ctx.lineTo(w-112,925);ctx.stroke();
 txt((t.hit||status)+'  ·  '+(t.margin_mode||'CROSS'),112,975,18,'#b6c4dc','600');txt(stamp||'Paper Trading',112,1018,15,'#8191ad');txt('SIMULATED PAPER TRADE · NOT AN EXCHANGE EXECUTION',112,1050,12,'#5d6e8c','600');
}
function shareFileName(){const t=window._sharingTrade||{};return (t.symbol||'trade')+'-'+(t.status||'trade').toLowerCase()+'.png';}
function shareSummary(){const t=window._sharingTrade||{},closed=t.status==='CLOSED',pnl=closed?(Number(t.realized_pnl)||0):((Number(t.realized_pnl)||0)+(Number(t.unrealized_pnl)||0)),exit=closed?(Number(t.close_price)||Number(t.current_price)||0):(Number(t.current_price)||Number(t.entry)||0);return (t.symbol||'Trade')+' '+(t.side||'')+' · '+(closed?'Closed':'Running')+' · Entry '+fmtPrice(t.entry)+' · '+(closed?'Close ':'Mark ')+fmtPrice(exit)+' · PnL '+(pnl>=0?'+':'-')+'$'+fmt(Math.abs(pnl))+' · Paper trade';}
function shareCardBlob(){return new Promise(resolve=>shareCanvas.toBlob(resolve,'image/png'));}
async function saveShareCard(){const blob=await shareCardBlob();if(!blob){toast('Could not create image');return;}const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=shareFileName();a.click();setTimeout(()=>URL.revokeObjectURL(url),1500);}


async function cancelPending(id){if(!confirm('Cancel pending limit order?'))return;const d=await(await fetch('/api/trades/'+id,{method:'DELETE'})).json();if(d.ok){toast('Limit order canceled');loadTrades();}else toast(d.error||'Cancel failed');}
async function cancelAllPending(event){if(event){event.preventDefault();event.stopPropagation();}if(!confirm('Cancel ALL pending orders? Running positions will not be affected.'))return;try{const r=await fetch('/api/trades/pending',{method:'DELETE'}),d=await r.json();if(r.ok&&d.ok){toast(d.canceled+' pending orders canceled');loadTrades();}else toast(d.error||'Bulk cancel failed');}catch(e){toast('Bulk cancel failed: '+(e.message||'network error'));}}
async function closeTrade(id){if(!confirm('Close trade?'))return;const d=await(await fetch('/api/trades/'+id+'/close',{method:'POST'})).json();if(d.ok){toast('Closed');speak('Trade closed');setAcc(d.account);alertBar.style.display='none';loadTrades();loadSignals();}}
async function applySuggestedTrail(id,activationPct,callbackPct){const t=(window._tradeCache||{})[String(id)]||{};const entry=Number(t.entry)||0;if(!(entry>0))return;const act=entry*(String(t.side).toUpperCase()==='LONG'?1+activationPct/100:1-activationPct/100);try{const r=await fetch('/api/trades/'+id,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({callback_rate:callbackPct,trailing_activation_price:act,profit_lock_enabled:true,profit_activation_pct:activationPct,profit_callback_pct:callbackPct})}),d=await r.json();if(r.ok&&d.ok){toast('Suggested trailing protection applied');loadTrades();}else toast(d.error||'Could not apply trailing');}catch(e){toast('Could not apply trailing');}}
function trailPrompt(id,cur,activation){manageTradeId.value=id;manageTitle.textContent='Trailing Stop';manageLevelsFields.style.display='none';manageTrailFields.style.display='block';manageTrailActivation.value=activation||'';manageTrailRate.value=cur||1;manageBg.style.display='flex';}
function closeManage(){manageBg.style.display='none';}
async function saveManage(){const id=manageTradeId.value;let body={};if(manageLevelsFields.style.display!=='none'){const sl=+manageSL.value,tp1=+manageTP1.value,tp2=manageTP2.value.trim()?+manageTP2.value:null,orig=window._manageOriginalLevels||{};if(!(sl>0&&tp1>0)){toast('Enter valid Stop Loss and Take Profit');return;}if(sl!==orig.sl)body.stop_loss=sl;if(tp1!==orig.tp1)body.tp1=tp1;if((tp2??0)!==(orig.tp2??0))body.tp2=tp2;if(!Object.keys(body).length){closeManage();toast('No protection changes');return;}}else{const rate=+manageTrailRate.value;if(!Number.isFinite(rate)||rate<0||rate>5||(rate>0&&rate<0.1)){toast('Callback must be 0 or 0.1–5%');return;}body={callback_rate:rate};if(manageTrailActivation.value.trim()){const p=+manageTrailActivation.value;if(!Number.isFinite(p)||p<=0){toast('Invalid activation price');return;}body.trailing_activation_price=p;}else{body.trailing_activation_price=0;}}try{const response=await fetch('/api/trades/'+id,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const d=await response.json();if(response.ok&&d.ok){closeManage();toast('Position protection updated');loadTrades();}else toast(d.error||'Update failed');}catch(e){toast('Could not save protection: '+(e.message||'network error'),5000);}}
async function editLevels(id,sl,tp1,tp2){const t=(window._tradeCache||{})[String(id)]||{};manageTradeId.value=id;manageTitle.textContent='Stop Loss / Take Profit';manageTrailFields.style.display='none';manageLevelsFields.style.display='block';window._manageEntry=Number(t.entry)||0;window._manageSide=String(t.side||'LONG').toUpperCase();manageSL.value=sl;manageTP1.value=tp1;manageTP2.value=tp2||'';syncLevelPct('sl','price');syncLevelPct('tp','price');syncLevelPct('tp2','price');window._manageOriginalLevels={sl:Number(sl),tp1:Number(tp1),tp2:Number(tp2)||0};manageBg.style.display='flex';}
function syncLevelPct(kind,source){const isSL=kind==='sl',isTP2=kind==='tp2',priceEl=isSL?manageSL:(isTP2?manageTP2:manageTP1),pctEl=isSL?manageSLPct:(isTP2?manageTP2Pct:manageTP1Pct),entry=Number(window._manageEntry)||0,side=window._manageSide||'LONG';if(!(entry>0))return;if(source==='price'){const p=Number(priceEl.value);if(!(p>0))return;const pct=side==='LONG'?(isSL?(entry-p)/entry*100:(p-entry)/entry*100):(isSL?(p-entry)/entry*100:(entry-p)/entry*100);pctEl.value=Number.isFinite(pct)?Math.max(0,pct).toFixed(3):'';}else{const pct=Number(pctEl.value);if(!(pct>=0))return;const f=pct/100,p=side==='LONG'?(isSL?entry*(1-f):entry*(1+f)):(isSL?entry*(1+f):entry*(1-f));priceEl.value=p>0?Number(p.toPrecision(10)):'';}}
async function clearTradeHistory(){if(!confirm('Delete closed/canceled trade history? Open positions will remain.'))return;const d=await(await fetch('/api/trades/history',{method:'DELETE'})).json();if(d.ok){toast(d.deleted+' history records cleared');loadTrades();}}
function loadMovers(){const m=window._movers||{gainers:[],losers:[]};const mk=(arr,up)=>arr.map(x=>`<div class="i" onclick="symIn.value='${x.symbol}';showTab('signals');analyzeManual()"><b>${x.symbol.replace(/(USDT|USDC)$/,'')}</b><span class="${up?'up':'dn'}">${x.change>=0?'+':''}${Number(x.change).toFixed(2)}%</span></div>`).join('')||'<div class="empty">Waiting for scan…</div>';gainers.innerHTML=mk(m.gainers||[],1);losers.innerHTML=mk(m.losers||[],0);}
async function loadAccount(){const a=await(await fetch('/api/account')).json();setAcc(a);renderStats(a.stats,accStats);sOpen.textContent=a.open_trades||0;}
async function adjustFunds(action){const input=document.getElementById('fundAmount'),amount=Number(input.value);if(!(Number.isFinite(amount)&&amount>0)){toast('Enter a valid USDT amount');return;}const label=action==='add'?'Add':'Remove';if(!confirm(label+' $'+fmt(amount)+' USDT '+(action==='add'?'to':'from')+' paper account?'))return;try{const r=await fetch('/api/account/funds',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,amount})}),d=await r.json();if(!r.ok||!d.ok){toast(d.error||'Balance adjustment failed');return;}setAcc(d.account);const account=await(await fetch('/api/account')).json();renderStats(account.stats,accStats);input.value='';toast(label+'ed $'+fmt(amount)+' USDT');loadTrades();}catch(e){toast('Balance adjustment failed: '+(e.message||'network error'));}}
async function checkAlerts(){try{const d=await(await fetch('/api/alerts')).json();if(d.count>0){const msg=d.alerts.map(t=>`${t.hit} ${t.symbol} ${t.pnl_pct||0}%`).join(' · ');alertBar.textContent='ALERT · '+msg;alertBar.style.display='block';d.alerts.forEach(t=>{const key=t.id+t.hit;if(!spokenAlerts.has(key)){spokenAlerts.add(key);speak(`${t.symbol.replace('USDT','')} ${t.hit} hit. PnL ${t.pnl_pct} percent`);beep(4);}});}else alertBar.style.display='none';}catch(e){}}
loadBotSettings();
loadSignals();
setInterval(()=>{if(!pinManual)loadSignals();},20000);
setInterval(checkAlerts,10000);
setInterval(refreshModalMarket,1500);
// Refresh trade data in-place every 3 seconds; never reload the document.
window.addEventListener('scroll',()=>{window._lastTradeScrollAt=Date.now();},{passive:true});
setInterval(()=>{if(!document.hidden&&document.getElementById('tab-trades').style.display!=='none')loadTrades();},2000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&document.getElementById('tab-trades').style.display!=='none')loadTrades();});
setInterval(async()=>{const a=await(await fetch('/api/account')).json();setAcc(a);sOpen.textContent=a.open_trades||0;},8000);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("api:app", host="0.0.0.0", port=port, reload=False)