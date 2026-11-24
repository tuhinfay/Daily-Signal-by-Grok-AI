"""Dependency-light tests for V5 safety and scoring primitives."""
import unittest
import pandas as pd
from scanner.data_validator import closed_candles
from scanner.market_regime import classify_regime
from scanner.scoring import score_candidate, qualify_candidate

class V5CoreTests(unittest.TestCase):
    def make_frame(self, count=35):
        idx = pd.date_range("2026-01-01", periods=count, freq="15min")
        return pd.DataFrame({"open":[100.0]*count,"high":[102.0]*count,
          "low":[99.0]*count,"close":[101.0]*count,"volume":[10.0]*count}, index=idx)
    def test_rest_drops_forming_candle(self):
        frame=self.make_frame(); result=closed_candles(frame)
        self.assertEqual(len(result),len(frame)-1)
        self.assertEqual(result.index[-1],frame.index[-2])
    def test_verified_closed_stream_keeps_last(self):
        frame=self.make_frame()
        self.assertEqual(len(closed_candles(frame,last_candle_closed=True)),len(frame))
    def test_bad_ohlc_rejected(self):
        frame=self.make_frame(); frame.iloc[-3,frame.columns.get_loc("high")]=1
        self.assertIsNone(closed_candles(frame))
    def test_insufficient_history_rejected(self):
        self.assertIsNone(closed_candles(self.make_frame(20)))
    def test_unknown_regime_when_data_missing(self):
        self.assertEqual(classify_regime({})["regime"],"UNKNOWN")
    def test_strong_bull_regime(self):
        row={"close":120,"ema50":115,"ema200":100,"adx":30,
             "di_plus":28,"di_minus":10,"atr_pct":2}
        self.assertEqual(classify_regime(row)["regime"],"STRONG_BULL")
    def test_scoring_weights_sum_to_100(self):
        result=score_candidate({})
        self.assertEqual(sum(x["weight"] for x in result["breakdown"].values()),100)
    def test_candle_filter_overrides_high_score(self):
        scored=score_candidate({k:1 for k in ("market_regime","htf_alignment",
          "market_structure","momentum","volume_futures","entry_risk")})
        ok,reason=qualify_candidate(scored,rr=3,data_valid=True,candle_closed=False)
        self.assertFalse(ok); self.assertEqual(reason,"CANDLE_NOT_CLOSED")

if __name__ == "__main__":
    unittest.main()
