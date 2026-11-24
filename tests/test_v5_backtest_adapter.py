"""Tests for the historical V5 signal adapter's timeframe close-time alignment."""
import unittest
from unittest.mock import patch
import pandas as pd

from scanner.v5_backtest import make_v5_signal_fn


class StubEngine:
    seen = None
    def analyze_pair(self, symbol, data, **kwargs):
        StubEngine.seen = data
        return {"score":80,"side":"LONG","stop_loss":95,"tp1":110,"entry":100,"atr":2,
                "v5_evidence":{"regime_detail":{"regime":"STRONG_BULL"}}}


class V5BacktestAdapterTests(unittest.TestCase):
    def make_frame(self, index):
        n = len(index)
        return pd.DataFrame({
            "open":[100.0]*n,"high":[101.0]*n,"low":[99.0]*n,
            "close":[100.0]*n,"volume":[2000.0]*n,
        }, index=index)

    def test_only_completed_higher_timeframe_bars_are_used(self):
        current = pd.Timestamp("2026-01-10 10:00:00", tz="UTC")
        prefix_index = pd.date_range(end=current, periods=100, freq="15min")
        frames = {
            "15m":self.make_frame(prefix_index),
            "1h":self.make_frame(pd.date_range(end=current, periods=60, freq="1h")),
            "4h":self.make_frame(pd.date_range(end=current, periods=60, freq="4h")),
            "1d":self.make_frame(pd.date_range(end=current.normalize(), periods=220, freq="1D")),
        }
        with patch("scanner.v5_backtest.SignalEngine", StubEngine):
            signal_fn = make_v5_signal_fn("BTCUSDT", frames, assumed_spread_pct=0.05)
            result = signal_fn(frames["15m"], 99)
        self.assertIsNotNone(result)
        # 10:00 1h candle has not closed by 10:15, so only 09:00 is eligible.
        self.assertEqual(StubEngine.seen["1h"].index[-1], current - pd.Timedelta(hours=1))
        # 08:00 4h candle closes at noon and is not eligible at 10:15.
        self.assertLess(StubEngine.seen["4h"].index[-1] + pd.Timedelta(hours=4),
                        current + pd.Timedelta(minutes=15))
        self.assertTrue(result["score"] >= 75)
        self.assertEqual(result["spread_pct"], 0.05)

    def test_rejects_assumed_spread_above_hard_limit(self):
        frames = {tf:self.make_frame(pd.date_range("2025-01-01", periods=220, freq="D"))
                  for tf in ("15m","1h","4h","1d")}
        with self.assertRaises(ValueError):
            make_v5_signal_fn("BTCUSDT", frames, assumed_spread_pct=0.2)


if __name__ == "__main__":
    unittest.main()
