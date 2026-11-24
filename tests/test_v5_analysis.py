"""Tests for V5 evidence aggregation and hard execution filters."""
import unittest
import pandas as pd
from scanner.v5_analysis import apply_futures_evidence, finalize_v5, validate_v5_frames, reward_risk_at_price


class V5AnalysisTests(unittest.TestCase):
    def test_missing_futures_data_does_not_create_futures_credit(self):
        evidence = {"volume_futures": 0.2}
        result = apply_futures_evidence(evidence, "LONG", {})
        self.assertEqual(result["volume_futures"], 0.2)

    def test_observed_futures_context_is_used(self):
        evidence = {"volume_futures": 0.2}
        result = apply_futures_evidence(
            evidence, "LONG",
            {"funding_rate": -0.0001, "oi_change_pct": 2, "book_imbalance_pct": 20},
        )
        self.assertGreater(result["volume_futures"], evidence["volume_futures"])

    def test_missing_spread_fails_closed(self):
        evidence = {k: 1.0 for k in (
            "market_regime", "htf_alignment", "market_structure",
            "momentum", "volume_futures", "entry_risk",
        )}
        scored, ok, reason = finalize_v5(
            evidence, rr=3, spread_pct=None, data_valid=True, candle_closed=True
        )
        self.assertEqual(scored["score"], 100)
        self.assertFalse(ok)
        self.assertEqual(reason, "SPREAD_UNAVAILABLE")

    def test_wide_spread_cannot_be_overridden_by_score(self):
        evidence = {k: 1.0 for k in (
            "market_regime", "htf_alignment", "market_structure",
            "momentum", "volume_futures", "entry_risk",
        )}
        _, ok, reason = finalize_v5(
            evidence, rr=3, spread_pct=0.11, data_valid=True, candle_closed=True
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "SPREAD_TOO_WIDE")

    def test_v5_requires_recent_timestamped_frames(self):
        now = pd.Timestamp.now(tz="UTC")
        frames = {}
        for timeframe, seconds in (("1d", 86400), ("4h", 14400), ("1h", 3600), ("15m", 900)):
            index = pd.date_range(end=now - pd.Timedelta(minutes=1), periods=55, freq=pd.Timedelta(seconds=seconds))
            frames[timeframe] = pd.DataFrame({
                "open": [100.0] * 55, "high": [102.0] * 55,
                "low": [99.0] * 55, "close": [101.0] * 55, "volume": [10.0] * 55,
            }, index=index)
        self.assertTrue(validate_v5_frames(frames))
        frames["15m"].index = frames["15m"].index - pd.Timedelta(hours=2)
        self.assertFalse(validate_v5_frames(frames))

    def test_required_mark_price_and_current_rr_are_hard_filters(self):
        evidence = {k: 1.0 for k in (
            "market_regime", "htf_alignment", "market_structure",
            "momentum", "volume_futures", "entry_risk",
        )}
        rr = reward_risk_at_price("LONG", 101, 95, 113)
        self.assertEqual(rr, 2.0)
        _, ok, reason = finalize_v5(
            evidence, rr=rr, spread_pct=0.02, data_valid=True, candle_closed=True,
            mark_price=102, entry=100, atr=1, require_mark_price=True,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "MARK_PRICE_DEVIATION_TOO_LARGE")

    def test_rr_below_two_fails(self):
        evidence = {k: 1.0 for k in (
            "market_regime", "htf_alignment", "market_structure",
            "momentum", "volume_futures", "entry_risk",
        )}
        _, ok, reason = finalize_v5(
            evidence, rr=1.9, spread_pct=0.02, data_valid=True, candle_closed=True
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "RISK_REWARD_BELOW_MINIMUM")


if __name__ == "__main__":
    unittest.main()
