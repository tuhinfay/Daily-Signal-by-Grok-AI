"""Best-effort public Binance USD-M liquidation stream collector.

The all-market force-order stream is forward-only. Data starts when this process
connects; it is not historical exchange-wide liquidation history.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict

import websocket


_lock = threading.Lock()
_orders: Dict[str, Dict[str, Any]] = {}
_started_at = None
_connected = False
_thread_started = False


def _on_open(_ws):
    global _connected, _started_at
    with _lock:
        _connected = True
        if _started_at is None:
            _started_at = datetime.now(timezone.utc).isoformat()


def _on_close(_ws, _status, _message):
    global _connected
    with _lock:
        _connected = False


def _on_error(_ws, error):
    print(f"[liquidation-feed] websocket error: {error}")


def _on_message(_ws, message):
    try:
        payload = json.loads(message)
        event = payload.get("o") or {}
        symbol = str(event.get("s") or "").upper()
        side = str(event.get("S") or "").upper()
        order_id = str(event.get("i") or "")
        event_ms = int(event.get("T") or payload.get("E") or 0)
        price = float(event.get("ap") or event.get("p") or 0)
        executed_qty = float(event.get("z") or event.get("q") or 0)
        if not symbol or side not in ("BUY", "SELL") or not order_id or event_ms <= 0 or price <= 0 or executed_qty <= 0:
            return
        # SELL liquidation orders close LONGs; BUY liquidation orders close SHORTs.
        row = {
            "symbol": symbol,
            "liquidated_side": "LONG" if side == "SELL" else "SHORT",
            "timestamp_ms": event_ms,
            "notional_usdt": price * executed_qty,
        }
        key = symbol + ":" + order_id
        with _lock:
            old = _orders.get(key)
            # Force-order stream can publish updates for the same order; keep its
            # largest cumulative executed notional rather than double-counting it.
            if old is None or row["notional_usdt"] > old["notional_usdt"]:
                _orders[key] = row
            cutoff = int(time.time() * 1000) - 86_400_000
            for old_key, old_row in list(_orders.items()):
                if old_row["timestamp_ms"] < cutoff:
                    _orders.pop(old_key, None)
    except Exception as exc:
        print(f"[liquidation-feed] message parse failed: {exc}")


def start_liquidation_feed():
    global _thread_started
    with _lock:
        if _thread_started:
            return
        _thread_started = True

    def run():
        while True:
            try:
                ws = websocket.WebSocketApp(
                    "wss://fstream.binance.com/ws/!forceOrder@arr",
                    on_open=_on_open,
                    on_message=_on_message,
                    on_error=_on_error,
                    on_close=_on_close,
                )
                ws.run_forever(ping_interval=120, ping_timeout=20)
            except Exception as exc:
                print(f"[liquidation-feed] reconnecting after error: {exc}")
            with _lock:
                global _connected
                _connected = False
            time.sleep(5)

    threading.Thread(target=run, name="binance-liquidation-feed", daemon=True).start()


def get_liquidation_summary(symbol: str) -> Dict[str, Any]:
    clean = str(symbol or "").upper().replace("/", "").split(":")[0]
    now_ms = int(time.time() * 1000)
    windows = {"5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
    with _lock:
        rows = [dict(row) for row in _orders.values() if row["symbol"] == clean]
        started = _started_at
        connected = _connected
    result = {}
    for label, duration in windows.items():
        selected = [row for row in rows if now_ms - duration <= row["timestamp_ms"] <= now_ms]
        long_usdt = sum(row["notional_usdt"] for row in selected if row["liquidated_side"] == "LONG")
        short_usdt = sum(row["notional_usdt"] for row in selected if row["liquidated_side"] == "SHORT")
        result[label] = {
            "long_usdt": round(long_usdt, 2),
            "short_usdt": round(short_usdt, 2),
            "total_usdt": round(long_usdt + short_usdt, 2),
            "orders": len(selected),
        }
    return {
        "timeframes": result,
        "source": "Binance all-market force-order stream",
        "connected": connected,
        "collected_since": started,
        "historical": False,
    }
