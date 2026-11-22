import unittest

import numpy as np
import pandas as pd

from scanner.indicators import add_indicators
from scanner.signal_engine import SignalEngine


class IndicatorWarmupTests(unittest.TestCase):
    def make_ohlcv(self, count=260):
        x = np.arange(count, dtype=float)
        close = 100.0 + (x * 0.08) + np.sin(x / 5.0) * 1.8
        return pd.DataFrame(
            {
                "open": close - 0.2,
                "high": close + 0.8,
                "low": close - 0.8,
                "close": close,
                "volume": 1000.0 + (x % 17) * 25.0,
            },
            index=pd.date_range("2025-01-01", periods=count, freq="h"),
        )

    def test_long_history_populates_ema200_vwap_and_volume_profile(self):
        result = add_indicators(self.make_ohlcv())
        latest = result.iloc[-1]
        self.assertTrue(np.isfinite(latest["ema200"]))
        self.assertTrue(np.isfinite(latest["vwap"]))
        self.assertTrue(np.isfinite(latest["volume_poc"]))
        self.assertTrue(np.isfinite(latest["atr"]))

    def test_short_daily_history_fails_closed_for_ema200_bias(self):
        short = self.make_ohlcv(120)
        long_score, short_score, _, _, bias = SignalEngine()._score_1d(short)
        self.assertEqual((long_score, short_score), (0, 0))
        self.assertEqual(bias, "neutral")


if __name__ == "__main__":
    unittest.main()
