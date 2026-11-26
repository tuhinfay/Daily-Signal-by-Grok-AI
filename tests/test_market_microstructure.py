import unittest

import pandas as pd

from scanner.binance_client import BinanceClient
from scanner.signal_engine import SignalEngine
from api import _trade_duration_seconds, _trade_opened_at


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
            def fapiPublicGetExchangeInfo(self):
                return {"symbols": [
                    {"symbol": "BTCUSDT", "quoteAsset": "USDT", "marginAsset": "USDT", "contractType": "PERPETUAL", "status": "TRADING"},
                    {"symbol": "ETHUSDT", "quoteAsset": "USDT", "marginAsset": "USDT", "contractType": "PERPETUAL", "status": "TRADING"},
                    {"symbol": "SOLUSDC", "quoteAsset": "USDC", "marginAsset": "USDC", "contractType": "PERPETUAL", "status": "TRADING"},
                    {"symbol": "OLDUSDT", "quoteAsset": "USDT", "marginAsset": "USDT", "contractType": "CURRENT_QUARTER", "status": "TRADING"},
                    {"symbol": "OFFUSDT", "quoteAsset": "USDT", "marginAsset": "USDT", "contractType": "PERPETUAL", "status": "SETTLING"},
                ]}

            def fapiPublicGetTicker24hr(self):
                return [
                    {"symbol": "BTCUSDT", "priceChangePercent": "8.0", "quoteVolume": "1", "lastPrice": "100"},
                    {"symbol": "ETHUSDT", "priceChangePercent": "-5.0", "quoteVolume": "1", "lastPrice": "50"},
                    {"symbol": "SOLUSDC", "priceChangePercent": "12.0", "quoteVolume": "1", "lastPrice": "25"},
                    {"symbol": "OLDUSDT", "priceChangePercent": "99.0", "quoteVolume": "1", "lastPrice": "10"},
                    {"symbol": "OFFUSDT", "priceChangePercent": "-99.0", "quoteVolume": "1", "lastPrice": "10"},
                ]

        client = BinanceClient.__new__(BinanceClient)
        client.rate_limited = False
        client.rate_limit_until = 0
        client._markets_loaded = True
        client.exchange = MoverExchange()
        result = client.get_movers(top_n=5, min_volume=1000000)
        self.assertEqual([row["symbol"] for row in result["gainers"]], ["SOLUSDC", "BTCUSDT", "ETHUSDT"])
        self.assertEqual([row["symbol"] for row in result["losers"]], ["ETHUSDT", "BTCUSDT", "SOLUSDC"])
        self.assertEqual(result["gainers"][0]["change"], 12.0)

    def test_market_structure_fetches_long_short_and_taker_ratios(self):
        class StructureExchange:
            def fapiDataGetGlobalLongShortAccountRatio(self, params):
                self.global_params = params
                return [{"longShortRatio": "1.5", "longAccount": "0.6", "shortAccount": "0.4"}]

            def fapiDataGetTakerlongshortRatio(self, params):
                self.taker_params = params
                return [{"buySellRatio": "1.2"}]

        exchange = StructureExchange()
        client = BinanceClient.__new__(BinanceClient)
        client.rate_limited = False
        client.futures = True
        client.exchange = exchange
        result = client.get_market_structure_context("BTCUSDT")
        self.assertEqual(result["long_short_ratio"], 1.5)
        self.assertEqual(result["long_account_pct"], 60.0)
        self.assertEqual(result["short_account_pct"], 40.0)
        self.assertEqual(result["taker_buy_sell_ratio"], 1.2)
        self.assertEqual(exchange.global_params, {"symbol": "BTCUSDT", "period": "5m", "limit": 1})

    def test_trade_duration_uses_fill_time_and_returns_seconds(self):
        trade = {
            "created_at": "2026-11-25T09:00:00+00:00",
            "filled_at": "2026-11-25T10:00:00+00:00",
            "closed_at": "2026-11-25T10:05:58+00:00",
        }
        self.assertEqual(_trade_opened_at(trade), "2026-11-25T10:00:00+00:00")
        self.assertEqual(_trade_duration_seconds(trade), 358)

    def test_trade_duration_falls_back_to_first_dca_timestamp(self):
        trade = {
            "dca_history": [{"created_at": "2026-11-25T10:00:00Z"}],
            "closed_at": "2026-11-25T10:01:05Z",
        }
        self.assertEqual(_trade_opened_at(trade), "2026-11-25T10:00:00Z")
        self.assertEqual(_trade_duration_seconds(trade), 65)

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
