"""Tests for V5 evidence aggregation and hard execution filters."""
import unittest
from scanner.v5_analysis import apply_futures_evidence, finalize_v5


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
