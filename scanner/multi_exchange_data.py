"""Multi-exchange public market data (no exchange API keys required).

CCXT supplies public perpetual trade and order-book snapshots across venues.
Marketlight supplies free cross-exchange OI/funding and hourly liquidation data
under CC BY 4.0. Missing data is kept null and sample coverage is exposed.
"""
from __future__ import annotations

import concurrent.futures
import threading
import time
from typing import Any, Dict

import ccxt
import httpx

_EXCHANGE_IDS = ("binanceusdm", "bybit", "okx", "bitget", "gateio", "mexc", "kucoinfutures", "hyperliquid")
_TIMEOUT_MS = 4000
_CACHE_TTL = 30.0
_lock = threading.RLock()
_clients: Dict[str, Any] = {}
_cache: Dict[str, Dict[str, Any]] = {}
_marketlight_cache: Dict[str, Dict[str, Any]] = {}
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="multi-market")
# Keep multi-venue collection off the scanner/request path. One symbol refresh at
# a time avoids concurrent use of shared CCXT clients; venue requests still fan out.
_refresh_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="multi-refresh")
_refreshing = set()
_MAX_QUEUED_REFRESHES = 8


def _clean(symbol: str) -> str:
    return str(symbol or "").upper().replace("/", "").split(":")[0]


def _base(symbol: str) -> str:
    clean = _clean(symbol)
    for quote in ("USDT", "USDC"):
        if clean.endswith(quote):
            return clean[:-len(quote)]
    return clean


def _client(exchange_id: str):
    with _lock:
        if exchange_id in _clients:
            return _clients[exchange_id]
        cls = getattr(ccxt, exchange_id, None)
        if cls is None:
            return None
        options = {"defaultType": "swap"}
        if exchange_id == "binanceusdm":
            options["defaultType"] = "future"
        instance = cls({"enableRateLimit": True, "timeout": _TIMEOUT_MS, "options": options})
        _clients[exchange_id] = instance
        return instance


def _find_perpetual(exchange, base: str):
    markets = exchange.load_markets()
    candidates = []
    for market in markets.values():
        if not market.get("active", True) or not market.get("swap") or not market.get("linear"):
            continue
        if str(market.get("base") or "").upper() != base:
            continue
        if str(market.get("quote") or "").upper() not in ("USDT", "USDC"):
            continue
        if str(market.get("settle") or market.get("quote") or "").upper() not in ("USDT", "USDC"):
            continue
        candidates.append(market)
    if not candidates:
        return None, None
    candidates.sort(key=lambda m: (m.get("quote") != "USDT", m.get("settle") != "USDT"))
    return candidates[0].get("symbol"), candidates[0]


def _venue_snapshot(exchange_id: str, base: str):
    result = {"exchange": exchange_id, "status": "unavailable", "timeframes": {},
              "bid_usdt": None, "ask_usdt": None, "book_imbalance_pct": None,
              "trades_count": 0, "error": None}
    started = time.time()
    try:
        exchange = _client(exchange_id)
        if exchange is None:
            result["error"] = "CCXT adapter unavailable"
            return result
        symbol, market = _find_perpetual(exchange, base)
        if not symbol:
            result["error"] = "No active linear USDT/USDC perpetual for this asset"
            return result
        trades_error = None
        try:
            trades = exchange.fetch_trades(symbol, limit=500) or []
        except Exception as exc:
            trades = []
            trades_error = str(exc)[:140]
        now_ms = int(time.time() * 1000)
        windows = {"5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
        periods = {tf: {"buy_usdt": 0.0, "sell_usdt": 0.0, "total_volume_usdt": 0.0,
                        "trades_count": 0, "sampled": True} for tf in windows}
        contract_size = float(market.get("contractSize") or 1.0)
        for trade in trades:
            stamp = trade.get("timestamp")
            if stamp is None:
                continue
            age = now_ms - int(stamp)
            if age < 0 or age > windows["1d"]:
                continue
            price = float(trade.get("price") or 0)
            amount = float(trade.get("amount") or 0) * contract_size
            notional = price * amount
            side = str(trade.get("side") or "").lower()
            if notional <= 0 or side not in ("buy", "sell"):
                continue
            for tf, duration in windows.items():
                if age <= duration:
                    row = periods[tf]
                    row["total_volume_usdt"] += notional
                    row["buy_usdt" if side == "buy" else "sell_usdt"] += notional
                    row["trades_count"] += 1
        book_error = None
        try:
            book = exchange.fetch_order_book(symbol, limit=20) or {}
            bids, asks = book.get("bids") or [], book.get("asks") or []
            bid_value = sum(float(p) * float(q) * contract_size for p, q, *_ in bids if float(p) > 0 and float(q) > 0)
            ask_value = sum(float(p) * float(q) * contract_size for p, q, *_ in asks if float(p) > 0 and float(q) > 0)
        except Exception as exc:
            bid_value = ask_value = 0.0
            book_error = str(exc)[:140]
        depth_total = bid_value + ask_value
        if trades_error and book_error:
            result["error"] = "Trades: " + trades_error + " | Book: " + book_error
            return result
        result.update({
            "status": "ok", "symbol": symbol, "timeframes": periods,
            "bid_usdt": round(bid_value, 2) if not book_error else None,
            "ask_usdt": round(ask_value, 2) if not book_error else None,
            "book_imbalance_pct": round((bid_value - ask_value) / depth_total * 100, 2) if depth_total and not book_error else None,
            "trades_count": len(trades), "updated_at": int(time.time() * 1000),
            "trades_error": trades_error, "book_error": book_error,
            "latency_ms": round((time.time() - started) * 1000, 1),
        })
    except Exception as exc:
        result["error"] = str(exc)[:180]
    return result


def _marketlight(base: str):
    now = time.time()
    with _lock:
        cached = _marketlight_cache.get(base)
        if cached and now - cached.get("_cached_at", 0) < 60:
            return cached
    result = {"source": "Marketlight (CC BY 4.0)", "updated_ms": None,
              "open_interest_usd": None, "oi_change_24h_pct": None,
              "funding_by_exchange": [], "liquidations": {}, "status": "unavailable"}
    try:
        with httpx.Client(timeout=5.0, headers={"User-Agent": "DailySignal/1.0"}) as client:
            response = client.get("https://marketlight.ai/api/public/derivatives", params={"base": base})
            if response.status_code == 200:
                data = response.json()
                oi = data.get("oi") or {}
                result["updated_ms"] = data.get("updatedMs")
                result["open_interest_usd"] = oi.get("total")
                change = oi.get("change24h")
                result["oi_change_24h_pct"] = float(change) * 100 if change is not None else None
                result["funding_by_exchange"] = data.get("funding", {}).get("byExchange") or []
                result["status"] = "ok"
            hourly = client.get("https://marketlight.ai/bt/api/public/liquidations/hourly",
                                params={"symbol": base + "USDT"})
            if hourly.status_code == 200:
                bars = (hourly.json() or {}).get("hours") or []
                if bars:
                    long_1h = float((bars[-1] or {}).get("long") or 0)
                    short_1h = float((bars[-1] or {}).get("short") or 0)
                    long_4h = sum(float((bar or {}).get("long") or 0) for bar in bars[-4:])
                    short_4h = sum(float((bar or {}).get("short") or 0) for bar in bars[-4:])
                    long_1d = sum(float((bar or {}).get("long") or 0) for bar in bars[-24:])
                    short_1d = sum(float((bar or {}).get("short") or 0) for bar in bars[-24:])
                    result["liquidations"] = {
                        "1h": {"long_usdt": round(long_1h, 2), "short_usdt": round(short_1h, 2)},
                        "4h": {"long_usdt": round(long_4h, 2), "short_usdt": round(short_4h, 2)},
                        "1d": {"long_usdt": round(long_1d, 2), "short_usdt": round(short_1d, 2)},
                    }
    except Exception as exc:
        result["error"] = str(exc)[:180]
    result["_cached_at"] = now
    with _lock:
        _marketlight_cache[base] = result
    return result


def _refresh_multi_exchange_context(base: str) -> None:
    try:
        futures = {exchange_id: _executor.submit(_venue_snapshot, exchange_id, base)
                   for exchange_id in _EXCHANGE_IDS}
        venues_by_id = {}
        future_to_id = {future: exchange_id for exchange_id, future in futures.items()}
        try:
            for future in concurrent.futures.as_completed(future_to_id, timeout=5.0):
                exchange_id = future_to_id[future]
                try:
                    venues_by_id[exchange_id] = future.result()
                except Exception as exc:
                    venues_by_id[exchange_id] = {"exchange": exchange_id, "status": "unavailable", "error": str(exc)[:180]}
        except concurrent.futures.TimeoutError:
            pass
        for exchange_id, future in futures.items():
            if exchange_id not in venues_by_id:
                future.cancel()
                venues_by_id[exchange_id] = {"exchange": exchange_id, "status": "unavailable", "error": "Snapshot timed out"}
        venues = [venues_by_id[exchange_id] for exchange_id in _EXCHANGE_IDS]
        valid = [venue for venue in venues if venue.get("status") == "ok"]
        periods = ("5m", "15m", "1h", "4h", "1d")
        aggregated = {}
        for tf in periods:
            buy = sum(float((v.get("timeframes") or {}).get(tf, {}).get("buy_usdt") or 0) for v in valid)
            sell = sum(float((v.get("timeframes") or {}).get(tf, {}).get("sell_usdt") or 0) for v in valid)
            total = buy + sell
            count = sum(1 for v in valid if (v.get("timeframes") or {}).get(tf, {}).get("trades_count", 0) > 0)
            aggregated[tf] = {
                "buy_usdt": round(buy, 2) if total else None,
                "sell_usdt": round(sell, 2) if total else None,
                "total_volume_usdt": round(total, 2) if total else None,
                "buy_pct": round(buy / total * 100, 2) if total else None,
                "sell_pct": round(sell / total * 100, 2) if total else None,
                "exchange_count": count, "sampled_trades": True,
            }
        bid = sum(float(v.get("bid_usdt") or 0) for v in valid)
        ask = sum(float(v.get("ask_usdt") or 0) for v in valid)
        depth_total = bid + ask
        ml = _marketlight(base)
        output = {
            "source": "CCXT public exchange APIs + Marketlight (CC BY 4.0)",
            "base": base, "updated_at": int(time.time() * 1000),
            "exchange_count": len(valid), "exchange_total": len(_EXCHANGE_IDS),
            "exchanges": venues, "timeframes": aggregated,
            "bid_usdt": round(bid, 2) if depth_total else None,
            "ask_usdt": round(ask, 2) if depth_total else None,
            "book_imbalance_pct": round((bid - ask) / depth_total * 100, 2) if depth_total else None,
            "marketlight": ml, "_cached_at": time.time(),
        }
        with _lock:
            _cache[base] = output
    except Exception as exc:
        print(f"[multi-market] refresh {base} failed: {exc}")
    finally:
        with _lock:
            _refreshing.discard(base)


def get_multi_exchange_context(symbol: str) -> Dict[str, Any]:
    """Return cached multi-venue data immediately and refresh stale symbols in background."""
    base = _base(symbol)
    now = time.time()
    with _lock:
        cached = _cache.get(base)
        if cached and now - cached.get("_cached_at", 0) < _CACHE_TTL:
            return cached
        if base not in _refreshing and len(_refreshing) < _MAX_QUEUED_REFRESHES:
            _refreshing.add(base)
            try:
                _refresh_executor.submit(_refresh_multi_exchange_context, base)
            except Exception:
                _refreshing.discard(base)
        if cached:
            stale = dict(cached)
            stale["stale"] = True
            stale["refreshing"] = base in _refreshing
            return stale
    return {
        "source": "CCXT public exchange APIs + Marketlight (CC BY 4.0)",
        "base": base, "updated_at": None, "exchange_count": 0,
        "exchange_total": len(_EXCHANGE_IDS), "exchanges": [],
        "timeframes": {}, "bid_usdt": None, "ask_usdt": None,
        "book_imbalance_pct": None, "marketlight": {"status": "refreshing", "liquidations": {}},
        "refreshing": base in _refreshing,
    }
