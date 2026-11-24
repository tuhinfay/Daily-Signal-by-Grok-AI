"""Causal market-structure features computed only from completed OHLCV bars."""
from __future__ import annotations

from typing import Dict
import pandas as pd


def analyze_structure(frame: pd.DataFrame, lookback: int = 20) -> Dict:
    """Return directional BOS/sweep/reclaim evidence using prior bars only."""
    empty = {"bias": "neutral", "long_score": 0.0, "short_score": 0.0,
             "bullish_bos": False, "bearish_bos": False,
             "bullish_sweep": False, "bearish_sweep": False,
             "swing_high": None, "swing_low": None}
    if frame is None or len(frame) < max(lookback + 2, 25):
        return empty
    required = {"open", "high", "low", "close"}
    if not required.issubset(frame.columns):
        return empty
    try:
        prior = frame.iloc[-lookback-1:-1]
        row = frame.iloc[-1]
        high_ref, low_ref = float(prior["high"].max()), float(prior["low"].min())
        op, hi, lo, close = (float(row[k]) for k in ("open", "high", "low", "close"))
        if min(op, hi, lo, close, high_ref, low_ref) <= 0 or hi < max(op, close, lo) or lo > min(op, close, hi):
            return empty
        rng = max(hi - lo, 1e-12)
        lower_wick = min(op, close) - lo
        upper_wick = hi - max(op, close)
        bullish_bos = close > high_ref
        bearish_bos = close < low_ref
        bullish_sweep = lo < low_ref and close > low_ref and lower_wick / rng >= 0.30
        bearish_sweep = hi > high_ref and close < high_ref and upper_wick / rng >= 0.30
        long_score = (0.65 if bullish_bos else 0.0) + (0.75 if bullish_sweep else 0.0)
        short_score = (0.65 if bearish_bos else 0.0) + (0.75 if bearish_sweep else 0.0)
        bias = "bull" if long_score > short_score else "bear" if short_score > long_score else "neutral"
        return {"bias": bias, "long_score": min(1.0, long_score),
                "short_score": min(1.0, short_score),
                "bullish_bos": bullish_bos, "bearish_bos": bearish_bos,
                "bullish_sweep": bullish_sweep, "bearish_sweep": bearish_sweep,
                "swing_high": high_ref, "swing_low": low_ref}
    except (TypeError, ValueError, KeyError, OverflowError):
        return empty
