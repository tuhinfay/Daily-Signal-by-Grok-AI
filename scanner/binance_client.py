"""Binance data fetcher"""

import ccxt
import os
import re
import time
import pandas as pd
from typing import List, Optional, Tuple, Dict, Any

from .data_validator import closed_candles


class BinanceClient:
    def __init__(self, futures: bool = True):
        api_key = os.environ.get("BINANCE_API_KEY", "")
        api_secret = os.environ.get("BINANCE_API_SECRET", "")
        exchange_config = {
            "enableRateLimit": True,
            "rateLimit": 250,
            "options": {"defaultType": "future" if futures else "spot"},
        }
        if api_key and api_secret:
            exchange_config.update({"apiKey": api_key, "secret": api_secret})
        self.exchange = ccxt.binance(exchange_config)
        self.futures = futures
        self._has_api_credentials = bool(api_key and api_secret)
        self._markets_loaded = False
        self._lev_cache: Dict[str, Optional[int]] = {}
        self._tickers_cache: Dict[str, dict] = {}
        self.rate_limited = False
        self.rate_limit_until = 0.0

    def _mark_rate_limit(self, error):
        message = str(error)
        if "418" in message or "-1003" in message or "Too many requests" in message:
            self.rate_limited = True
            match = re.search(r"banned until\s+(\d{10,})", message, re.IGNORECASE)
            if match:
                try:
                    self.rate_limit_until = max(self.rate_limit_until, int(match.group(1)) / 1000.0)
                except (TypeError, ValueError):
                    pass
            if self.rate_limit_until > time.time():
                until = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(self.rate_limit_until))
                print(f"[binance] IP ban active until {until}; pausing REST requests")
            else:
                print("[binance] rate limit detected; pausing further REST requests for this scan")

    def _cached_ticker(self, symbol: str):
        clean = self._clean_symbol(symbol)
        for key, ticker in self._tickers_cache.items():
            if self._clean_symbol(key) == clean:
                return ticker
        return None

    def _ensure_markets(self):
        if not self._markets_loaded:
            try:
                self.exchange.load_markets()
                self._markets_loaded = True
            except Exception as e:
                print("markets load", e)

    @staticmethod
    def _clean_symbol(symbol: str) -> str:
        clean = str(symbol).upper().replace("/", "")
        if ":USDT" in clean:
            clean = clean.split(":")[0]
        return clean

    def _ccxt_symbol(self, symbol: str) -> str:
        value = str(symbol).upper()
        if "/" in value:
            if self.futures and value.endswith("/USDT") and ":USDT" not in value:
                return value + ":USDT"
            return value
        clean = self._clean_symbol(value)
        if clean.endswith("USDT"):
            base = clean[:-4]
            return f"{base}/USDT:USDT" if self.futures else f"{base}/USDT"
        return value

    def get_max_leverage(self, symbol: str) -> Optional[int]:
        """Return Binance's verified maximum leverage when leverage tiers are available."""
        clean = self._clean_symbol(symbol)
        if clean in self._lev_cache:
            return self._lev_cache[clean]
        if self.rate_limited:
            return None
        self._ensure_markets()
        maximum = None
        ccxt_symbol = self._ccxt_symbol(clean)
        try:
            market = self.exchange.market(ccxt_symbol)
            if self._has_api_credentials and self.exchange.has.get("fetchMarketLeverageTiers"):
                tiers = self.exchange.fetch_market_leverage_tiers(ccxt_symbol)
                values = []
                for tier in tiers or []:
                    value = tier.get("maxLeverage")
                    if value is not None:
                        values.append(int(float(value)))
                if values:
                    maximum = max(values)
            if maximum is None:
                leverage_limits = ((market.get("limits") or {}).get("leverage") or {})
                value = leverage_limits.get("max")
                info = market.get("info") or {}
                candidates = [
                    value,
                    market.get("maxLeverage"),
                    info.get("maxLeverage"),
                    (info.get("leverageFilter") or {}).get("maxLeverage"),
                ]
                for candidate in candidates:
                    if candidate is not None:
                        try:
                            maximum = int(float(candidate))
                            break
                        except (TypeError, ValueError):
                            continue
        except Exception as e:
            print(f"leverage {clean}: {e}")
        if maximum is not None:
            maximum = max(1, min(maximum, 150))
        else:
            print(f"leverage {clean}: exact maximum unavailable; configure read-only Binance API credentials")
        self._lev_cache[clean] = maximum
        return maximum

    def get_top_usdt_pairs(self, top_n: int = 30, min_volume: float = 12_000_000) -> List[Tuple[str, float]]:
        try:
            tickers = self.exchange.fetch_tickers()
            self._tickers_cache = tickers or {}
            usdt_pairs = []
            for symbol, data in tickers.items():
                clean = self._clean_symbol(symbol)
                if not clean.endswith("USDT"):
                    continue
                base = clean[:-4]
                if any(x in base for x in ("UP", "DOWN", "BULL", "BEAR", "2L", "2S", "3L", "3S")):
                    continue
                qv = data.get("quoteVolume")
                if qv is None:
                    qv = (data.get("info") or {}).get("quoteVolume")
                try:
                    vol = float(qv or 0)
                except Exception:
                    continue
                if vol >= min_volume:
                    usdt_pairs.append((clean, vol))
            usdt_pairs.sort(key=lambda x: x[1], reverse=True)
            if usdt_pairs:
                return usdt_pairs[:top_n]
        except Exception as e:
            self._mark_rate_limit(e)
            print("top pairs", e)
        return [
            ("BTCUSDT", 2e9), ("ETHUSDT", 8e8), ("SOLUSDT", 3e8), ("BNBUSDT", 1e8),
            ("XRPUSDT", 1e8), ("DOGEUSDT", 8e7), ("ADAUSDT", 5e7), ("AVAXUSDT", 4e7),
            ("LINKUSDT", 4e7), ("SUIUSDT", 3e7), ("NEARUSDT", 2e7), ("AAVEUSDT", 2e7),
        ][:top_n]

    def get_24h_change_pct(self, symbol: str) -> Optional[float]:
        """Return Binance Futures rolling 24-hour ticker percentage, not a UTC daily candle."""
        ticker = self._cached_ticker(symbol)
        if ticker is None:
            if self.rate_limited:
                return None
            try:
                ticker = self.exchange.fetch_ticker(self._ccxt_symbol(symbol))
            except Exception as e:
                self._mark_rate_limit(e)
                print(f"24h change {symbol}: {e}")
                return None
        value = ticker.get("percentage")
        if value is None:
            value = (ticker.get("info") or {}).get("priceChangePercent")
        try:
            number = float(value)
            return round(number, 2) if number == number and abs(number) != float("inf") else None
        except (TypeError, ValueError, OverflowError):
            return None

    def get_movers(self, top_n: int = 12, min_volume: float = 0) -> Dict[str, List[dict]]:
        """Match Binance USDⓈ-M Futures 24h Top Gainer/Loser ticker ranking.

        Uses the official all-symbol /fapi/v1/ticker/24hr payload and ranks
        active USDT- and USDC-margined perpetual contracts by priceChangePercent.
        min_volume is retained for call compatibility but intentionally ignored:
        Binance's ranking is not restricted by our local liquidity threshold.
        """
        gainers, losers = [], []
        if self.rate_limited:
            return {"gainers": gainers, "losers": losers}
        try:
            # Use Binance's authoritative USDⓈ-M exchangeInfo universe rather
            # than CCXT's normalized markets_by_id, which can omit valid contracts.
            exchange_info = self.exchange.fapiPublicGetExchangeInfo() or {}
            contracts = {
                str(item.get("symbol") or "").upper(): item
                for item in (exchange_info.get("symbols") or [])
            }
            # The all-symbol ticker endpoint returns every USDⓈ-M contract.
            tickers = self.exchange.fapiPublicGetTicker24hr()
            rows = []
            for ticker in tickers or []:
                symbol = str(ticker.get("symbol") or "").upper()
                contract = contracts.get(symbol)
                if not contract:
                    continue
                quote_asset = str(contract.get("quoteAsset") or "").upper()
                margin_asset = str(contract.get("marginAsset") or "").upper()
                if (
                    quote_asset not in ("USDT", "USDC")
                    or margin_asset not in ("USDT", "USDC")
                    or contract.get("contractType") != "PERPETUAL"
                    or contract.get("status") != "TRADING"
                ):
                    continue
                try:
                    change = float(ticker.get("priceChangePercent"))
                    volume = float(ticker.get("quoteVolume") or 0)
                    last = float(ticker.get("lastPrice") or 0)
                    if not all(map(lambda value: value == value and abs(value) != float("inf"), (change, volume, last))):
                        continue
                    if last <= 0:
                        continue
                except (TypeError, ValueError, OverflowError):
                    continue
                rows.append({
                    "symbol": symbol,
                    "change": change,
                    "volume": volume,
                    "price": last,
                    "period": "rolling 24h",
                })

            # Gainers: highest percentage first. Losers: lowest percentage first.
            rows.sort(key=lambda row: row["change"])
            count = max(0, int(top_n))
            losers = rows[:count]
            gainers = list(reversed(rows[-count:])) if count else []
        except Exception as e:
            self._mark_rate_limit(e)
            print("movers", e)
        return {"gainers": gainers, "losers": losers}

    def fetch_ohlcv(self, symbol: str, timeframe: str = "15m", limit: int = 150) -> Optional[pd.DataFrame]:
        try:
            if self.rate_limited:
                return None
            ccxt_symbol = self._ccxt_symbol(symbol)
            # USD-M Futures 1d klines default to UTC 00:00
            # (= Bangladesh 06:00); other timeframes use their normal intervals.
            ohlcv = self.exchange.fetch_ohlcv(ccxt_symbol, timeframe=timeframe, limit=limit)
            if not ohlcv or len(ohlcv) < 30:
                return None
            df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
            df.set_index("timestamp", inplace=True)
            for c in ("open", "high", "low", "close", "volume"):
                df[c] = pd.to_numeric(df[c], errors="coerce")
            # REST OHLCV includes the currently forming final candle.
            return closed_candles(df, last_candle_closed=False, min_rows=30)
        except Exception as e:
            self._mark_rate_limit(e)
            print(f"ohlcv {symbol} {timeframe}: {e}")
            return None

    def get_current_price(self, symbol: str) -> Optional[float]:
        if self.rate_limited:
            return None
        try:
            ticker = self._cached_ticker(symbol)
            if ticker is None:
                ticker = self.exchange.fetch_ticker(self._ccxt_symbol(symbol))
            value = ticker.get("last")
            return float(value) if value is not None else None
        except Exception as e:
            self._mark_rate_limit(e)
            print(f"ticker {symbol}: {e}")
            return None

    def get_quote_volume_24h(self, symbol: str) -> Optional[float]:
        """Return 24h quote turnover in USDT, when present in the cached ticker."""
        ticker = self._cached_ticker(symbol)
        if ticker is None:
            if self.rate_limited:
                return None
            try:
                ticker = self.exchange.fetch_ticker(self._ccxt_symbol(symbol))
            except Exception as e:
                self._mark_rate_limit(e)
                print(f"quote volume {symbol}: {e}")
                return None
        value = ticker.get("quoteVolume")
        if value is None:
            value = (ticker.get("info") or {}).get("quoteVolume")
        try:
            volume = float(value or 0)
            return volume if volume > 0 else None
        except (TypeError, ValueError):
            return None

    def get_mark_price(self, symbol: str) -> Optional[float]:
        """Return Futures mark price; last traded price is only a fallback."""
        if self.rate_limited:
            return None
        try:
            ticker = self._cached_ticker(symbol)
            if ticker is None:
                ticker = self.exchange.fetch_ticker(self._ccxt_symbol(symbol))
            info = ticker.get("info") or {}
            # Do not silently substitute last-traded price: V5 callers use this
            # value as a mark-price risk check, so absence must remain explicit.
            value = info.get("markPrice") or ticker.get("mark")
            if value is None and self.exchange.has.get("fetchFundingRate"):
                funding = self.exchange.fetch_funding_rate(self._ccxt_symbol(symbol)) or {}
                value = funding.get("markPrice") or (funding.get("info") or {}).get("markPrice")
            price = float(value) if value is not None else None
            return price if price and price > 0 else None
        except Exception as e:
            self._mark_rate_limit(e)
            print(f"mark price {symbol}: {e}")
            return None

    def get_derivatives_context(self, symbol: str) -> Dict[str, Optional[float]]:
        """Fetch optional public Futures funding and open-interest context."""
        result: Dict[str, Optional[float]] = {
            "funding_rate": None, "open_interest": None, "open_interest_usd": None,
            "oi_change_pct": None, "oi_change_24h_pct": None, "mark_price": None,
            "oi_updated_at": None,
        }
        if self.rate_limited:
            return result
        market_symbol = self._ccxt_symbol(symbol)
        try:
            if self.exchange.has.get("fetchFundingRate"):
                funding = self.exchange.fetch_funding_rate(market_symbol) or {}
                value = funding.get("fundingRate")
                if value is not None:
                    result["funding_rate"] = float(value)
                mark_value = funding.get("markPrice") or (funding.get("info") or {}).get("markPrice")
                if mark_value is not None and float(mark_value) > 0:
                    result["mark_price"] = float(mark_value)
        except Exception as e:
            self._mark_rate_limit(e)
            print(f"funding {symbol}: {e}")
        try:
            if not self.rate_limited and self.exchange.has.get("fetchOpenInterest"):
                oi = self.exchange.fetch_open_interest(market_symbol) or {}
                info = oi.get("info") or {}
                value = oi.get("openInterestAmount") or oi.get("openInterest") or info.get("openInterest")
                if value is not None:
                    result["open_interest"] = float(value)
                value_usd = oi.get("openInterestValue") or info.get("openInterestValue") or info.get("sumOpenInterestValue")
                if value_usd is not None and float(value_usd) > 0:
                    result["open_interest_usd"] = float(value_usd)
                elif result["open_interest"] is not None:
                    mark = result.get("mark_price") or self.get_mark_price(symbol)
                    if mark is not None and mark > 0:
                        result["open_interest_usd"] = result["open_interest"] * mark
                result["oi_updated_at"] = int(time.time() * 1000)
        except Exception as e:
            self._mark_rate_limit(e)
            print(f"open interest {symbol}: {e}")
        try:
            if not self.rate_limited and self.exchange.has.get("fetchOpenInterestHistory"):
                history = self.exchange.fetch_open_interest_history(market_symbol, timeframe="1h", limit=25) or []
                values_1h, values_24h = [], []
                for row in history:
                    info = row.get("info") or {}
                    value = (row.get("openInterestValue") or row.get("openInterestAmount")
                             or row.get("openInterest") or info.get("sumOpenInterestValue")
                             or info.get("sumOpenInterest"))
                    if value is not None and float(value) > 0:
                        values_1h.append(float(value))
                if len(values_1h) >= 2 and values_1h[-2] > 0:
                    result["oi_change_pct"] = (values_1h[-1] / values_1h[-2] - 1.0) * 100.0
                if len(values_1h) >= 25 and values_1h[0] > 0:
                    result["oi_change_24h_pct"] = (values_1h[-1] / values_1h[0] - 1.0) * 100.0
        except Exception as e:
            self._mark_rate_limit(e)
            print(f"open interest history {symbol}: {e}")
        return result

    def get_global_derivatives_context(self, symbol: str) -> Dict[str, Optional[float]]:
        """Blend public Binance, Bybit and OKX derivatives signals when available.

        OI percentage changes are compared within each venue, then median-aggregated;
        raw contract quantities are deliberately not summed across exchanges.
        """
        base = self.get_derivatives_context(symbol)
        changes, funding, sources = [], [], []
        if base.get("oi_change_pct") is not None:
            changes.append(float(base["oi_change_pct"]))
            sources.append("Binance")
        if base.get("funding_rate") is not None:
            funding.append(float(base["funding_rate"]))
        clean = self._clean_symbol(symbol)
        if not clean.endswith("USDT"):
            return {**base, "oi_sources": sources, "oi_coverage": len(sources)}
        try:
            import statistics
            for exchange_id in ("bybit", "okx"):
                try:
                    exchange_class = getattr(ccxt, exchange_id, None)
                    if exchange_class is None:
                        continue
                    venue = exchange_class({"enableRateLimit": True, "timeout": 5000, "options": {"defaultType": "swap"}})
                    market_symbol = self._ccxt_symbol(clean)
                    if not venue.has.get("fetchOpenInterestHistory"):
                        continue
                    history = venue.fetch_open_interest_history(market_symbol, timeframe="1h", limit=2) or []
                    values = []
                    for row in history:
                        info = row.get("info") or {}
                        value = (row.get("openInterestValue") or row.get("openInterestAmount")
                                 or info.get("sumOpenInterestValue") or info.get("sumOpenInterest")
                                 or info.get("openInterest"))
                        if value is not None and float(value) > 0:
                            values.append(float(value))
                    if len(values) >= 2 and values[-2] > 0:
                        changes.append((values[-1] / values[-2] - 1.0) * 100.0)
                        sources.append(exchange_id.title())
                    if venue.has.get("fetchFundingRate"):
                        rate = venue.fetch_funding_rate(market_symbol) or {}
                        if rate.get("fundingRate") is not None:
                            funding.append(float(rate["fundingRate"]))
                except Exception as exc:
                    print(f"[global-derivatives] {exchange_id} {clean}: {exc}")
        except Exception as exc:
            print(f"[global-derivatives] setup {clean}: {exc}")
        result = dict(base)
        result["oi_change_pct"] = round(statistics.median(changes), 2) if changes else None
        result["funding_rate"] = statistics.median(funding) if funding else None
        result["oi_sources"] = sources
        result["oi_coverage"] = len(sources)
        return result


    def get_daily_market_context(self, symbol: str, current_price: Optional[float] = None) -> Dict[str, Optional[float]]:
        """Binance USD-M active UTC daily candle plus 7D/30D/1Y close-to-close returns."""
        result: Dict[str, Optional[float]] = {
            "daily_open": None, "daily_high": None, "daily_low": None,
            "change_7d_pct": None, "change_30d_pct": None, "change_1y_pct": None,
        }
        df = self.fetch_ohlcv(symbol, timeframe="1d", limit=370)
        if df is None or len(df) < 31:
            return result
        try:
            last = df.iloc[-1]
            price = float(current_price or last["close"])
            for key, col in (("daily_open", "open"), ("daily_high", "high"), ("daily_low", "low")):
                value = float(last[col])
                result[key] = value if value > 0 else None
            for days, key in ((7, "change_7d_pct"), (30, "change_30d_pct"), (365, "change_1y_pct")):
                if len(df) > days:
                    base = float(df["close"].iloc[-(days + 1)])
                    if base > 0 and price > 0:
                        result[key] = round((price / base - 1.0) * 100.0, 2)
        except (TypeError, ValueError, KeyError, IndexError, OverflowError):
            pass
        return result

    def get_price_precision(self, symbol: str) -> Optional[int]:
        """Return Binance's displayed price decimal places from PRICE_FILTER tickSize."""
        try:
            self._ensure_markets()
            market = self.exchange.market(self._ccxt_symbol(symbol))
            info = market.get("info") or {}
            for rule in info.get("filters") or []:
                if rule.get("filterType") == "PRICE_FILTER":
                    tick = str(rule.get("tickSize") or "").rstrip("0")
                    if "." in tick:
                        return max(0, len(tick.split(".", 1)[1]))
            precision = (market.get("precision") or {}).get("price")
            if precision is not None:
                if getattr(self.exchange, "precisionMode", None) == getattr(ccxt, "TICK_SIZE", object()):
                    tick = f"{float(precision):.16f}".rstrip("0")
                    return max(0, len(tick.split(".", 1)[1])) if "." in tick else 0
                return max(0, int(precision))
        except Exception as e:
            print(f"price precision {symbol}: {e}")
        return None

    def get_order_book_context(self, symbol: str, limit: int = 20) -> Dict[str, Optional[float]]:
        """Return visible order-book depth only; not hidden liquidity or liquidation data."""
        result: Dict[str, Optional[float]] = {
            "book_imbalance_pct": None, "spread_pct": None,
            "bid_depth_usdt": None, "ask_depth_usdt": None,
            "bid_wall_price": None, "ask_wall_price": None,
        }
        if self.rate_limited:
            return result
        try:
            book = self.exchange.fetch_order_book(self._ccxt_symbol(symbol), limit=limit) or {}
            bids, asks = book.get("bids") or [], book.get("asks") or []
            bid_rows = [(float(x[0]), float(x[1]), float(x[0])*float(x[1])) for x in bids if len(x)>=2 and float(x[0])>0 and float(x[1])>0]
            ask_rows = [(float(x[0]), float(x[1]), float(x[0])*float(x[1])) for x in asks if len(x)>=2 and float(x[0])>0 and float(x[1])>0]
            bid_qty = sum(x[2] for x in bid_rows); ask_qty = sum(x[2] for x in ask_rows)
            total = bid_qty + ask_qty
            result["bid_depth_usdt"] = bid_qty
            result["ask_depth_usdt"] = ask_qty
            if total > 0:
                result["book_imbalance_pct"] = (bid_qty - ask_qty) / total * 100.0
            if bid_rows:
                result["bid_wall_price"] = max(bid_rows, key=lambda x:x[2])[0]
            if ask_rows:
                result["ask_wall_price"] = max(ask_rows, key=lambda x:x[2])[0]
            if bids and asks:
                bid, ask = float(bids[0][0]), float(asks[0][0])
                mid = (bid + ask) / 2.0
                if bid > 0 and ask >= bid and mid > 0:
                    result["spread_pct"] = (ask - bid) / mid * 100.0
        except Exception as e:
            self._mark_rate_limit(e)
            print(f"order book {symbol}: {e}")
        return result

    def get_market_pressure_context(self, symbol: str) -> Dict[str, Any]:
        """Return Binance USD-M public crowd, taker-flow and quote-volume snapshots by timeframe."""
        result = {"timeframes": {}, "source": "Binance USD-M Futures"}
        if self.rate_limited:
            return result
        clean = self._clean_symbol(symbol)
        periods = ("5m", "15m", "1h", "4h", "1d")
        for period in periods:
            row = {"long_pct": None, "short_pct": None, "long_short_ratio": None,
                   "buy_usdt": None, "sell_usdt": None, "total_volume_usdt": None,
                   "buy_pct": None, "sell_pct": None, "timestamp": None}
            try:
                ratio_method = getattr(self.exchange, "fapiDataGetGlobalLongShortAccountRatio", None)
                rows = ratio_method({"symbol": clean, "period": period, "limit": 1}) if callable(ratio_method) else []
                if rows:
                    item = rows[-1]
                    if item.get("longAccount") is not None:
                        row["long_pct"] = round(float(item["longAccount"]) * 100, 2)
                    if item.get("shortAccount") is not None:
                        row["short_pct"] = round(float(item["shortAccount"]) * 100, 2)
                    if item.get("longShortRatio") is not None:
                        row["long_short_ratio"] = round(float(item["longShortRatio"]), 3)
                    row["timestamp"] = item.get("timestamp")
            except Exception as exc:
                self._mark_rate_limit(exc)
                print(f"[market-pressure] long/short {clean} {period}: {exc}")
            try:
                # Kline fields 7 and 10 are quote turnover and taker-buy quote turnover.
                kline_method = getattr(self.exchange, "fapiPublicGetKlines", None)
                candles = kline_method({"symbol": clean, "interval": period, "limit": 1}) if callable(kline_method) and not self.rate_limited else []
                if candles:
                    candle = candles[-1]
                    total = float(candle[7])
                    buy = float(candle[10])
                    sell = max(0.0, total - buy)
                    if total >= 0 and buy >= 0:
                        row["total_volume_usdt"] = round(total, 2)
                        row["buy_usdt"] = round(buy, 2)
                        row["sell_usdt"] = round(sell, 2)
                        if total > 0:
                            row["buy_pct"] = round(buy / total * 100, 2)
                            row["sell_pct"] = round(sell / total * 100, 2)
                        row["volume_timestamp"] = candle[0]
            except Exception as exc:
                self._mark_rate_limit(exc)
                print(f"[market-pressure] volume {clean} {period}: {exc}")
            result["timeframes"][period] = row
            if self.rate_limited:
                break
        return result

    def get_market_structure_context(self, symbol: str) -> Dict[str, Optional[float]]:
        """Fetch Binance public crowd positioning and taker flow where available."""
        result: Dict[str, Optional[float]] = {
            "long_short_ratio": None, "taker_buy_sell_ratio": None,
            "long_account_pct": None, "short_account_pct": None,
        }
        if self.rate_limited:
            return result
        clean = self._clean_symbol(symbol)
        for method_name, key in (
            ("fapiDataGetGlobalLongShortAccountRatio", "long_short_ratio"),
            ("fapiDataGetTakerlongshortRatio", "taker_buy_sell_ratio"),
        ):
            try:
                method = getattr(self.exchange, method_name, None)
                if not callable(method):
                    continue
                rows = method({"symbol": clean, "period": "5m", "limit": 1}) or []
                if rows:
                    row = rows[-1]
                    value = row.get("longShortRatio") if key == "long_short_ratio" else row.get("buySellRatio")
                    if value is not None:
                        result[key] = float(value)
                    if key == "long_short_ratio":
                        if row.get("longAccount") is not None:
                            result["long_account_pct"] = float(row["longAccount"]) * 100.0
                        if row.get("shortAccount") is not None:
                            result["short_account_pct"] = float(row["shortAccount"]) * 100.0
            except Exception as e:
                self._mark_rate_limit(e)
                print(f"market structure {key} {symbol}: {e}")
        return result

    def get_order_constraints(self, symbol: str) -> Dict[str, Optional[float]]:
        """Return public market lot/notional limits without placing or signing an order."""
        result = {"qty_step": None, "min_qty": None, "max_qty": None, "min_notional": None, "price_tick": None}
        try:
            self._ensure_markets()
            market = self.exchange.market(self._ccxt_symbol(symbol))
            precision = (market.get("precision") or {}).get("amount")
            if precision is not None:
                if getattr(self.exchange, "precisionMode", None) == getattr(ccxt, "TICK_SIZE", object()):
                    step = float(precision)
                else:
                    step = 10.0 ** (-int(precision))
                if step > 0:
                    result["qty_step"] = step
            limits = market.get("limits") or {}
            amount_limits = limits.get("amount") or {}
            cost_limits = limits.get("cost") or {}
            result["min_qty"] = float(amount_limits["min"]) if amount_limits.get("min") is not None else None
            result["max_qty"] = float(amount_limits["max"]) if amount_limits.get("max") is not None else None
            result["min_notional"] = float(cost_limits["min"]) if cost_limits.get("min") is not None else None
            # Binance filter values are the authoritative fallback when CCXT's
            # normalized limits omit exchange-specific filters.
            info = market.get("info") or {}
            for rule in info.get("filters") or []:
                kind = rule.get("filterType")
                if kind in ("LOT_SIZE", "MARKET_LOT_SIZE"):
                    step = float(rule.get("stepSize") or 0)
                    if step > 0:
                        result["qty_step"] = step
                    if rule.get("minQty") is not None:
                        result["min_qty"] = float(rule["minQty"])
                    if rule.get("maxQty") is not None:
                        result["max_qty"] = float(rule["maxQty"])
                if kind == "PRICE_FILTER":
                    tick = float(rule.get("tickSize") or 0)
                    if tick > 0:
                        result["price_tick"] = tick
                if kind in ("MIN_NOTIONAL", "NOTIONAL"):
                    value = rule.get("minNotional") or rule.get("notional")
                    if value is not None:
                        result["min_notional"] = float(value)
        except Exception as e:
            print(f"order constraints {symbol}: {e}")
        return result

    def get_market_liquidity(self, symbol: str) -> str:
        """Classify liquidity from the contract's current 24h USDT quote turnover."""
        try:
            ticker = self._cached_ticker(symbol)
            if ticker is None:
                if self.rate_limited:
                    return "UNKNOWN"
                ticker = self.exchange.fetch_ticker(self._ccxt_symbol(symbol))
            quote_volume = ticker.get("quoteVolume")
            if quote_volume is None:
                quote_volume = (ticker.get("info") or {}).get("quoteVolume")
            volume = float(quote_volume or 0)
            if volume <= 0:
                return "UNKNOWN"
            return ("VERY HIGH" if volume >= 1e8 else
                    "HIGH" if volume >= 4e7 else
                    "MED" if volume >= 1.2e7 else "LOW")
        except Exception as e:
            self._mark_rate_limit(e)
            print(f"liquidity {symbol}: {e}")
            return "UNKNOWN"
