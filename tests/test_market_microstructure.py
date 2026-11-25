import unittest

import pandas as pd

from scanner.binance_client import BinanceClient
from scanner.signal_engine import SignalEngine


class FakeExchange:
    def __init__(self):
        self.has = {"fetchOrderBook": True}

    def fetch_order_book(self, symbol, limit=20):
        return {
            "bids": [[100.0, 2.0], [99.0, 1.0]],
            "asks": [[101.0, 1.0], [102.0, 1.0]],
        }


class MarketMicrostructureTests(unittest.TestCase):
    def test_24h_change_uses_ticker_percentage(self):
        class TickerExchange:
            def fetch_ticker(self, symbol):
                return {"percentage": 4.567}
        client = BinanceClient.__new__(BinanceClient)
        client.rate_limited = False
        client.futures = True
        client.exchange = TickerExchange()
        client._tickers_cache = {}
        self.assertEqual(client.get_24h_change_pct("BTCUSDT"), 4.57)

    def test_movers_match_usdm_perpetual_ticker_ranking(self):
        class MoverExchange:
            def __init__(self):
                self.markets_by_id = {
                    "BTCUSDT": {"quote": "USDT", "swap": True, "info": {"contractType": "PERPETUAL", "status": "TRADING"}},
                    "ETHUSDT": {"quote": "USDT", "swap": True, "info": {"contractType": "PERPETUAL", "status": "TRADING"}},
                    "OLDUSDT": {"quote": "USDT", "swap": True, "info": {"contractType": "CURRENT_QUARTER", "status": "TRADING"}},
                    "OFFUSDT": {"quote": "USDT", "swap": True, "info": {"contractType": "PERPETUAL", "status": "SETTLING"}},
                }

            def fapiPublicGetTicker24hr(self):
                return [
                    {"symbol": "BTCUSDT", "priceChangePercent": "8.0", "quoteVolume": "1", "lastPrice": "100"},
                    {"symbol": "ETHUSDT", "priceChangePercent": "-5.0", "quoteVolume": "1", "lastPrice": "50"},
                    {"symbol": "OLDUSDT", "priceChangePercent": "99.0", "quoteVolume": "1", "lastPrice": "10"},
                    {"symbol": "OFFUSDT", "priceChangePercent": "-99.0", "quoteVolume": "1", "lastPrice": "10"},
                ]

        client = BinanceClient.__new__(BinanceClient)
        client.rate_limited = False
        client.rate_limit_until = 0
        client._markets_loaded = True
        client.exchange = MoverExchange()
        result = client.get_movers(top_n=5, min_volume=1000000)
        self.assertEqual([row["symbol"] for row in result["gainers"]], ["BTCUSDT"])
        self.assertEqual([row["symbol"] for row in result["losers"]], ["ETHUSDT"])
        self.assertEqual(result["gainers"][0]["change"], 8.0)

    def test_order_book_uses_quote_notional_imbalance_and_spread(self):
        client = BinanceClient.__new__(BinanceClient)
        client.rate_limited = False
        client.futures = True
        client.exchange = FakeExchange()
        result = client.get_order_book_context("BTCUSDT")
        expected = ((299.0 - 203.0) / (299.0 + 203.0)) * 100.0
        self.assertAlmostEqual(result["book_imbalance_pct"], expected, places=5)
        self.assertAlmostEqual(result["spread_pct"], (1.0 / 100.5) * 100.0, places=5)

    def test_breakout_retest_requires_close_back_above_prior_level(self):
        rows = []
        for i in range(80):
            rows.append({"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1000.0})
        rows[-2] = {"open": 101.0, "high": 106.0, "low": 100.0, "close": 105.0, "volume": 2500.0}
        rows[-1] = {"open": 101.2, "high": 103.0, "low": 101.1, "close": 102.0, "volume": 1800.0}
        frame = pd.DataFrame(rows)
        _, _, long_reasons, _, _, _ = SignalEngine()._score_15m(frame)
        self.assertIn("Breakout retest held", long_reasons)


if __name__ == "__main__":
    unittest.main()
