import unittest

import pandas as pd

from scanner.backtest import simulate_trade


class BacktestSimulationTests(unittest.TestCase):
    def test_stop_loss_wins_if_stop_and_tp1_touch_same_candle(self):
        future = pd.DataFrame([{"high": 103.0, "low": 98.0, "close": 101.0}])
        result = simulate_trade("LONG", 100.0, 99.0, 102.0, 104.0, future, fee_rate=0, slippage_rate=0)
        self.assertEqual(result["exit"], "SL")
        self.assertEqual(result["r"], -1.0)

    def test_tp1_then_tp2_realizes_half_at_each_target(self):
        future = pd.DataFrame([
            {"high": 102.5, "low": 100.5, "close": 102.0},
            {"high": 104.5, "low": 101.5, "close": 104.0},
        ])
        result = simulate_trade("LONG", 100.0, 99.0, 102.0, 104.0, future, fee_rate=0, slippage_rate=0)
        self.assertEqual(result["exit"], "TP2")
        self.assertEqual(result["r"], 3.0)

    def test_short_trade_tp1_then_stop_keeps_partial_profit(self):
        future = pd.DataFrame([
            {"high": 99.5, "low": 97.5, "close": 98.0},
            {"high": 101.0, "low": 97.0, "close": 100.0},
        ])
        result = simulate_trade("SHORT", 100.0, 101.0, 98.0, 96.0, future, fee_rate=0, slippage_rate=0)
        self.assertEqual(result["exit"], "SL_AFTER_TP1")
        self.assertEqual(result["r"], 0.5)

    def test_invalid_risk_geometry_is_rejected(self):
        future = pd.DataFrame([{"high": 102.0, "low": 99.0, "close": 100.0}])
        self.assertIsNone(simulate_trade("LONG", 100.0, 101.0, 102.0, 104.0, future, fee_rate=0, slippage_rate=0))


if __name__ == "__main__":
    unittest.main()
