"""
Binance data fetcher using ccxt
"""

import ccxt
import pandas as pd
from typing import List, Optional, Tuple


class BinanceClient:
    def __init__(self, futures: bool = True):
        self.exchange = ccxt.binance({
            "enableRateLimit": True,
            "options": {"defaultType": "future" if futures else "spot"},
        })
        self.futures = futures

    def get_top_usdt_pairs(self, top_n: int = 30, min_volume: float = 12_000_000) -> List[Tuple[str, float]]:
        try:
            tickers = self.exchange.fetch_tickers()
            usdt_pairs = []

            for symbol, data in tickers.items():
                if "/" not in symbol and not symbol.endswith("USDT"):
                    continue
                clean = symbol.replace("/", "")
                if not clean.endswith("USDT"):
                    continue
                # skip leveraged tokens
                base = clean[:-4]
                if any(x in base for x in ("UP", "DOWN", "BULL", "BEAR", "2L", "2S", "3L", "3S")):
                    continue

                quote_volume = data.get("quoteVolume")
                if quote_volume is None:
                    info = data.get("info") or {}
                    quote_volume = info.get("quoteVolume") or info.get("qv")
                if quote_volume is None:
                    continue
                try:
                    vol = float(quote_volume)
                except Exception:
                    continue

                if vol >= min_volume:
                    usdt_pairs.append((clean, vol))

            usdt_pairs.sort(key=lambda x: x[1], reverse=True)
            out = usdt_pairs[:top_n]
            if out:
                return out
        except Exception as e:
            print(f"[ERROR] top pairs: {e}")

        # solid fallback list
        return [
            ("BTCUSDT", 2e9), ("ETHUSDT", 8e8), ("SOLUSDT", 3e8),
            ("BNBUSDT", 1e8), ("XRPUSDT", 1e8), ("DOGEUSDT", 8e7),
            ("ADAUSDT", 5e7), ("AVAXUSDT", 4e7), ("LINKUSDT", 4e7),
            ("DOTUSDT", 3e7), ("LTCUSDT", 3e7), ("ATOMUSDT", 2e7),
            ("NEARUSDT", 2e7), ("UNIUSDT", 2e7), ("AAVEUSDT", 2e7),
            ("APTUSDT", 2e7), ("ARBUSDT", 2e7), ("OPUSDT", 2e7),
            ("SUIUSDT", 3e7), ("PEPEUSDT", 5e7),
        ][:top_n]

    def fetch_ohlcv(self, symbol: str, timeframe: str = "15m", limit: int = 150) -> Optional[pd.DataFrame]:
        try:
            if "/" in symbol:
                ccxt_symbol = symbol
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
            print(f"[WARN] {symbol} {timeframe}: {e}")
            return None

    def get_current_price(self, symbol: str) -> Optional[float]:
        try:
            ccxt_symbol = symbol.replace("USDT", "/USDT") if "/" not in symbol else symbol
            ticker = self.exchange.fetch_ticker(ccxt_symbol)
            return float(ticker["last"])
        except Exception:
            return None
