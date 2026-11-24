import unittest

import pandas as pd

from scanner.risk import calculate_levels


class StructureAwareRiskTests(unittest.TestCase):
    def test_long_levels_respect_swing_support_and_resistance(self):
        rows = []
        for i in range(60):
            rows.append({
                "high": 104.0 if i < 40 else (108.0 if i < 55 else 112.0),
                "low": 96.0 if i < 40 else 97.0,
                "close": 100.0,
            })
        frame = pd.DataFrame(rows)
        levels = calculate_levels("LONG", 100.0, 2.0, market_data=frame)
        self.assertIsNotNone(levels)
        self.assertLess(levels["stop_loss"], 96.0)
        self.assertGreater(levels["tp1"], 100.0)
        self.assertGreater(levels["tp2"], levels["tp1"])
        self.assertGreaterEqual(levels["rr1"], 1.25)
        self.assertNotEqual(levels["rr1"], 1.33)

    def test_short_levels_respect_swing_resistance_and_support(self):
        rows = []
        for i in range(60):
            rows.append({
                "high": 105.0 if i >= 40 else 104.0,
                "low": 96.0 if i < 40 else (92.0 if i < 55 else 88.0),
                "close": 100.0,
            })
        frame = pd.DataFrame(rows)
        levels = calculate_levels("SHORT", 100.0, 2.0, market_data=frame)
        self.assertIsNotNone(levels)
        self.assertGreater(levels["stop_loss"], 104.0)
        self.assertLess(levels["tp1"], 100.0)
        self.assertLess(levels["tp2"], levels["tp1"])
        self.assertGreaterEqual(levels["rr1"], 1.25)


if __name__ == "__main__":
    unittest.main()
