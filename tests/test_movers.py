import unittest

from scanner.binance_client import BinanceClient


class FakeDailyExchange:
    def __init__(self):
        self.calls = []

    def fetch_ohlcv(self, symbol, timeframe, limit):
        self.calls.append((symbol, timeframe, limit))
        return [[0, 100.0, 112.0, 99.0, 110.0, 1000.0]]


class DailyMoverBoundaryTests(unittest.TestCase):
    def test_movers_use_binance_standard_daily_candle(self):
        client = BinanceClient.__new__(BinanceClient)
        client.rate_limited = False
        client._markets_loaded = True
        client._tickers_cache = {
            "BTC/USDT:USDT": {"quoteVolume": 20_000_000, "last": 110.0}
        }
        client.exchange = FakeDailyExchange()

        result = client.get_movers(top_n=1, min_volume=1)

        self.assertEqual(client.exchange.calls, [("BTC/USDT:USDT", "1d", 1)])
        self.assertEqual(len(result["gainers"]), 1)
        self.assertAlmostEqual(result["gainers"][0]["change"], 10.0)
        self.assertEqual(result["gainers"][0]["period"], "Binance UTC daily (BD 06:00)")


if __name__ == "__main__":
    unittest.main()
