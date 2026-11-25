import unittest

import api
from scanner import liquidation_feed


class LiquidationEstimateTests(unittest.TestCase):
    def setUp(self):
        api._account = {"balance": 50.0}
        api._trades = {}

    def test_isolated_long_formula(self):
        trade = {
            "id": "L1", "side": "LONG", "entry": 100.0, "qty": 1.0,
            "margin": 10.0, "margin_mode": "ISOLATED",
            "maintenance_margin_rate": 0.005, "maintenance_amount": 0.0,
            "current_price": 95.0,
        }
        expected = (100.0 - 10.0) / (1.0 - 0.005)
        self.assertAlmostEqual(api._estimate_liquidation_price(trade), expected, places=8)
        api._update_trade_liquidation_fields(trade)
        self.assertEqual(trade["liquidation_price_type"], "ISOLATED_ESTIMATE")
        self.assertEqual(trade["liquidation_risk"], "HIGH")

    def test_isolated_short_formula(self):
        trade = {
            "id": "S1", "side": "SHORT", "entry": 100.0, "qty": 1.0,
            "margin": 10.0, "margin_mode": "ISOLATED",
            "maintenance_margin_rate": 0.005, "maintenance_amount": 0.0,
            "current_price": 105.0,
        }
        expected = (100.0 + 10.0) / (1.0 + 0.005)
        self.assertAlmostEqual(api._estimate_liquidation_price(trade), expected, places=8)
        api._update_trade_liquidation_fields(trade)
        self.assertEqual(trade["liquidation_risk"], "HIGH")

    def test_cross_long_uses_wallet_balance(self):
        trade = {
            "id": "C1", "side": "LONG", "entry": 100.0, "qty": 1.0,
            "margin": 10.0, "margin_mode": "CROSS",
            "maintenance_margin_rate": 0.005, "maintenance_amount": 0.0,
            "current_price": 60.0,
        }
        expected = (100.0 - 50.0) / (1.0 - 0.005)
        self.assertAlmostEqual(api._estimate_liquidation_price(trade), expected, places=8)

    def test_invalid_trade_has_no_liquidation_price(self):
        self.assertIsNone(api._estimate_liquidation_price({"side": "LONG", "qty": 0}))


    def test_liquidation_summary_has_distinct_1h_4h_and_1d_windows(self):
        now_ms = int(__import__("time").time() * 1000)
        with liquidation_feed._lock:
            original = dict(liquidation_feed._orders)
            liquidation_feed._orders.clear()
            liquidation_feed._orders["BTCUSDT:1"] = {
                "symbol": "BTCUSDT", "liquidated_side": "LONG",
                "timestamp_ms": now_ms - 10_000, "notional_usdt": 125.0,
            }
            liquidation_feed._orders["BTCUSDT:2"] = {
                "symbol": "BTCUSDT", "liquidated_side": "SHORT",
                "timestamp_ms": now_ms - 120_000, "notional_usdt": 75.0,
            }
            liquidation_feed._orders["BTCUSDT:3"] = {
                "symbol": "BTCUSDT", "liquidated_side": "LONG",
                "timestamp_ms": now_ms - 7_200_000, "notional_usdt": 300.0,
            }
            liquidation_feed._orders["BTCUSDT:4"] = {
                "symbol": "BTCUSDT", "liquidated_side": "SHORT",
                "timestamp_ms": now_ms - 43_200_000, "notional_usdt": 500.0,
            }
        try:
            result = liquidation_feed.get_liquidation_summary("BTCUSDT")
            self.assertEqual(list(result["timeframes"]), ["1h", "4h", "1d"])
            self.assertEqual(result["timeframes"]["1h"]["long_usdt"], 125.0)
            self.assertEqual(result["timeframes"]["1h"]["short_usdt"], 75.0)
            self.assertEqual(result["timeframes"]["4h"]["long_usdt"], 425.0)
            self.assertEqual(result["timeframes"]["4h"]["short_usdt"], 75.0)
            self.assertEqual(result["timeframes"]["1d"]["long_usdt"], 425.0)
            self.assertEqual(result["timeframes"]["1d"]["short_usdt"], 575.0)
        finally:
            with liquidation_feed._lock:
                liquidation_feed._orders.clear()
                liquidation_feed._orders.update(original)


if __name__ == "__main__":
    unittest.main()
