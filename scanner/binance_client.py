"""Binance data fetcher"""

import ccxt
import pandas as pd
from typing import List, Optional, Tuple, Dict


class BinanceClient:
    def __init__(self, futures: bool = True):
        self.exchange = ccxt.binance({
            "enableRateLimit": True,
            "options": {"defaultType": "future" if futures else "spot"},
        })
        self.futures = futures
        self._markets_loaded = False
        self._lev_cache: Dict[str, int] = {}

    def _ensure_markets(self):
        if not self._markets_loaded:
            try:
                self.exchange.load_markets()
                self._markets_loaded = True
            except Exception as e:
                print("markets load", e)

    def get_max_leverage(self, symbol: str) -> int:
        """Best-effort max leverage for USDT-M futures symbol."""
        clean = symbol.replace("/", "").upper()
        if clean in self._lev_cache:
            return self._lev_cache[clean]
        self._ensure_markets()
        lev = 20  # safe default
        try:
            ccxt_symbol = clean.replace("USDT", "/USDT") if clean.endswith("USDT") else clean
            m = self.exchange.markets.get(ccxt_symbol) or self.exchange.markets.get(clean)
            if m:
                # limits.leverage.max if present
                limits = (m.get("limits") or {}).get("leverage") or {}
                if limits.get("max"):
                    lev = int(limits["max"])
                info = m.get("info") or {}
                # binance sometimes exposes brackets
                if "brackets" in info and info["brackets"]:
                    try:
                        lev = max(int(b.get("initialLeverage", lev)) for b in info["brackets"])
                    except Exception:
                        pass
            # major coins often higher on binance futures
            majors = {"BTCUSDT": 125, "ETHUSDT": 100, "BNBUSDT": 75, "SOLUSDT": 75,
                      "XRPUSDT": 75, "DOGEUSDT": 75, "ADAUSDT": 75, "LINKUSDT": 75}
            if clean in majors and majors[clean] > lev:
                lev = majors[clean]
            # meme / high vol often 50-75
            if lev < 50 and any(x in clean for x in ("PEPE", "WIF", "BONK", "FLOKI", "SHIB")):
                lev = 50
        except Exception:
            pass
        lev = max(1, min(lev, 150))
        self._lev_cache[clean] = lev
        return lev

    def get_top_usdt_pairs(self, top_n: int = 30, min_volume: float = 12_000_000) -> List[Tuple[str, float]]:
        try:
            tickers = self.exchange.fetch_tickers()
            usdt_pairs = []
            for symbol, data in tickers.items():
                clean = symbol.replace("/", "")
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
            print("top pairs", e)
        return [
            ("BTCUSDT", 2e9), ("ETHUSDT", 8e8), ("SOLUSDT", 3e8), ("BNBUSDT", 1e8),
            ("XRPUSDT", 1e8), ("DOGEUSDT", 8e7), ("ADAUSDT", 5e7), ("AVAXUSDT", 4e7),
            ("LINKUSDT", 4e7), ("SUIUSDT", 3e7), ("NEARUSDT", 2e7), ("AAVEUSDT", 2e7),
        ][:top_n]

    def get_movers(self, top_n: int = 12, min_volume: float = 5_000_000) -> Dict[str, List[dict]]:
        """Top gainers / losers by 24h % change among liquid USDT pairs."""
        gainers, losers = [], []
        try:
            tickers = self.exchange.fetch_tickers()
            rows = []
            for symbol, data in tickers.items():
                clean = symbol.replace("/", "")
                if not clean.endswith("USDT"):
                    continue
                base = clean[:-4]
                if any(x in base for x in ("UP", "DOWN", "BULL", "BEAR", "2L", "2S", "3L", "3S")):
                    continue
                qv = data.get("quoteVolume") or (data.get("info") or {}).get("quoteVolume") or 0
                try:
                    vol = float(qv)
                    ch = data.get("percentage")
                    if ch is None:
                        ch = float((data.get("info") or {}).get("priceChangePercent") or 0)
                    else:
                        ch = float(ch)
                    last = float(data.get("last") or 0)
                except Exception:
                    continue
                if vol < min_volume or last <= 0:
                    continue
                rows.append({"symbol": clean, "change": ch, "volume": vol, "price": last})
            rows.sort(key=lambda x: x["change"], reverse=True)
            gainers = rows[:top_n]
            losers = list(reversed(rows[-top_n:])) if len(rows) >= top_n else sorted(rows, key=lambda x: x["change"])[:top_n]
        except Exception as e:
            print("movers", e)
        return {"gainers": gainers, "losers": losers}

    def fetch_ohlcv(self, symbol: str, timeframe: str = "15m", limit: int = 150) -> Optional[pd.DataFrame]:
        try:
            if "/" in symbol:
                ccxt_symbol = symbol
            elif symbol.endswith("USDT:USDT"):
                # CCXT Binance USD-M futures unified symbols use BASE/USDT:USDT.
                ccxt_symbol = f"{symbol[:-9]}/USDT:USDT"
            else:
                ccxt_symbol = symbol.replace("USDT", "/USDT") if symbol.endswith("USDT") else symbol
            ohlcv = self.exchange.fetch_ohlcv(ccxt_symbol, timeframe=timeframe, limit=limit)
            if not ohlcv or len(ohlcv) < 30:
                return None
            df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
            df.set_index("timestamp", inplace=True)
            for c in ("open", "high", "low", "close", "volume"):
                df[c] = pd.to_numeric(df[c], errors="coerce")
            return df.dropna()
        except Exception as e:
            print(f"ohlcv {symbol} {timeframe}: {e}")
            return None

    def get_current_price(self, symbol: str) -> Optional[float]:
        try:
            ccxt_symbol = symbol.replace("USDT", "/USDT") if "/" not in symbol else symbol
            return float(self.exchange.fetch_ticker(ccxt_symbol)["last"])
        except Exception:
            return None
