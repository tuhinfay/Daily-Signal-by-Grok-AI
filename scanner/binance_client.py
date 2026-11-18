"""
Binance data fetcher using ccxt
"""

import ccxt
import pandas as pd
from typing import List, Optional
import time


class BinanceClient:
    def __init__(self, futures: bool = True):
        self.exchange = ccxt.binance({
            "enableRateLimit": True,
            "options": {
                "defaultType": "future" if futures else "spot",
            }
        })
        self.futures = futures

    def get_top_usdt_pairs(self, top_n: int = 50, min_volume: float = 5_000_000) -> List[str]:
        """Return top USDT pairs by 24h quote volume."""
        try:
            tickers = self.exchange.fetch_tickers()
            usdt_pairs = []

            for symbol, data in tickers.items():
                if not symbol.endswith("/USDT") and not symbol.endswith("USDT"):
                    continue
                # Normalize symbol
                clean = symbol.replace("/", "")
                if not clean.endswith("USDT"):
                    continue

                quote_volume = data.get("quoteVolume") or data.get("info", {}).get("quoteVolume")
                if quote_volume is None:
                    continue
                try:
                    vol = float(quote_volume)
                except Exception:
                    continue

                if vol >= min_volume:
                    usdt_pairs.append((clean, vol))

            # Sort by volume descending
            usdt_pairs.sort(key=lambda x: x[1], reverse=True)
            return [p[0] for p in usdt_pairs[:top_n]]

        except Exception as e:
            print(f"[ERROR] Failed to fetch top pairs: {e}")
            # Fallback popular pairs
            return [
                "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
                "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT",
                "MATICUSDT", "LTCUSDT", "ATOMUSDT", "UNIUSDT", "NEARUSDT",
            ][:top_n]

    def fetch_ohlcv(self, symbol: str, timeframe: str = "15m", limit: int = 200) -> Optional[pd.DataFrame]:
        """Fetch OHLCV and return as DataFrame."""
        try:
            # ccxt uses BTC/USDT format
            ccxt_symbol = symbol.replace("USDT", "/USDT") if "/" not in symbol else symbol

            ohlcv = self.exchange.fetch_ohlcv(ccxt_symbol, timeframe=timeframe, limit=limit)
            if not ohlcv:
                return None

            df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
            df.set_index("timestamp", inplace=True)
            return df

        except Exception as e:
            print(f"[WARN] Failed to fetch {symbol} {timeframe}: {e}")
            return None

    def get_current_price(self, symbol: str) -> Optional[float]:
        try:
            ccxt_symbol = symbol.replace("USDT", "/USDT") if "/" not in symbol else symbol
            ticker = self.exchange.fetch_ticker(ccxt_symbol)
            return float(ticker["last"])
        except Exception:
            return None
