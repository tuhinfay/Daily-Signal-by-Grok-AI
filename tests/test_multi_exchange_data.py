import unittest
from unittest.mock import patch

from scanner import multi_exchange_data as data


class MultiExchangeAggregationTests(unittest.TestCase):
    def setUp(self):
        data._cache.clear()
        data._marketlight_cache.clear()

    def test_aggregates_only_available_venue_trade_samples(self):
        def venue(exchange_id, base):
            return {
                "exchange": exchange_id, "status": "ok" if exchange_id in ("binanceusdm", "bybit") else "unavailable",
                "timeframes": {
                    "5m": {"buy_usdt": 120.0, "sell_usdt": 80.0, "total_volume_usdt": 200.0, "trades_count": 5}
                } if exchange_id in ("binanceusdm", "bybit") else {},
                "bid_usdt": 1000.0 if exchange_id in ("binanceusdm", "bybit") else None,
                "ask_usdt": 500.0 if exchange_id in ("binanceusdm", "bybit") else None,
            }

        with patch.object(data, "_venue_snapshot", side_effect=venue), patch.object(
            data, "_marketlight", return_value={"status": "unavailable", "liquidations": {}}
        ):
            result = data.get_multi_exchange_context("BTCUSDT")

        self.assertEqual(result["exchange_count"], 2)
        self.assertEqual(result["timeframes"]["5m"]["buy_usdt"], 240.0)
        self.assertEqual(result["timeframes"]["5m"]["sell_usdt"], 160.0)
        self.assertEqual(result["timeframes"]["5m"]["total_volume_usdt"], 400.0)
        self.assertEqual(result["timeframes"]["5m"]["exchange_count"], 2)
        self.assertAlmostEqual(result["book_imbalance_pct"], 33.333, places=2)

    def test_no_available_venue_keeps_missing_metrics_null(self):
        with patch.object(data, "_venue_snapshot", return_value={
            "exchange": "offline", "status": "unavailable", "timeframes": {},
            "bid_usdt": None, "ask_usdt": None,
        }), patch.object(data, "_marketlight", return_value={"status": "unavailable", "liquidations": {}}):
            result = data.get_multi_exchange_context("UNKNOWNUSDT")

        self.assertEqual(result["exchange_count"], 0)
        self.assertIsNone(result["timeframes"]["5m"]["buy_usdt"])
        self.assertIsNone(result["timeframes"]["5m"]["total_volume_usdt"])
        self.assertIsNone(result["book_imbalance_pct"])


if __name__ == "__main__":
    unittest.main()
