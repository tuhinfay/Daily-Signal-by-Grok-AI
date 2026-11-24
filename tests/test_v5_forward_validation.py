"""Tests for elapsed-time and sample-size gates in forward paper validation."""
import unittest
from datetime import datetime, timedelta, timezone
from scanner.forward_validation import evaluate_forward_trades


class ForwardValidationTests(unittest.TestCase):
    def test_short_period_never_claims_validated(self):
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=29)
        trades = [{"status":"CLOSED","closed_at":end.isoformat(),"realized_pnl":2.0} for _ in range(40)]
        result = evaluate_forward_trades(trades, start=start, end=end)
        self.assertFalse(result["ready"])
        self.assertEqual(result["status"], "COLLECTING_FORWARD_DATA")

    def test_requires_minimum_closed_trade_sample(self):
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=35)
        trades = [{"status":"CLOSED","closed_at":end.isoformat(),"realized_pnl":2.0} for _ in range(29)]
        result = evaluate_forward_trades(trades, start=start, end=end)
        self.assertFalse(result["ready"])
        self.assertEqual(result["closed_trades"], 29)

    def test_mature_sample_reports_metrics(self):
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=35)
        trades = []
        for i in range(30):
            trades.append({"status":"CLOSED","closed_at":(start+timedelta(days=i+1)).isoformat(),
                           "realized_pnl": 2.0 if i%2 == 0 else -1.0})
        result = evaluate_forward_trades(trades, start=start, end=end)
        self.assertTrue(result["ready"])
        self.assertEqual(result["closed_trades"], 30)
        self.assertEqual(result["win_rate_pct"], 50.0)
        self.assertAlmostEqual(result["profit_factor"], 2.0)


if __name__ == "__main__":
    unittest.main()
