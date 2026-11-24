"""Causal market-structure features computed only from completed OHLCV bars."""
from __future__ import annotations

from typing import Dict
import pandas as pd


def analyze_structure(frame: pd.DataFrame, lookback: int = 20) -> Dict:
    """Return directional BOS/sweep/reclaim evidence using prior bars only."""
    empty = {"bias": "neutral", "long_score": 0.0, "short_score": 0.0,
             "bullish_bos": False, "bearish_bos": False,
             "bullish_sweep": False, "bearish_sweep": False,
             "higher_highs": False, "higher_lows": False,
             "lower_highs": False, "lower_lows": False,
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
        # Confirmed 2-left/2-right pivots; only pivots with both right bars
        # already closed are included, avoiding a look-ahead pivot label.
        highs, lows = frame["high"].astype(float).to_numpy(), frame["low"].astype(float).to_numpy()
        pivot_highs, pivot_lows = [], []
        for idx in range(2, len(frame)-2):
            if highs[idx] >= max(highs[idx-2:idx+3]):
                pivot_highs.append(highs[idx])
            if lows[idx] <= min(lows[idx-2:idx+3]):
                pivot_lows.append(lows[idx])
        higher_highs = len(pivot_highs) >= 2 and pivot_highs[-1] > pivot_highs[-2]
        lower_highs = len(pivot_highs) >= 2 and pivot_highs[-1] < pivot_highs[-2]
        higher_lows = len(pivot_lows) >= 2 and pivot_lows[-1] > pivot_lows[-2]
        lower_lows = len(pivot_lows) >= 2 and pivot_lows[-1] < pivot_lows[-2]
        long_score = (0.45 if bullish_bos else 0.0) + (0.65 if bullish_sweep else 0.0) + (0.25 if higher_highs and higher_lows else 0.0)
        short_score = (0.45 if bearish_bos else 0.0) + (0.65 if bearish_sweep else 0.0) + (0.25 if lower_highs and lower_lows else 0.0)
        bias = "bull" if long_score > short_score else "bear" if short_score > long_score else "neutral"
        return {"bias": bias, "long_score": min(1.0, long_score),
                "short_score": min(1.0, short_score),
                "bullish_bos": bullish_bos, "bearish_bos": bearish_bos,
                "bullish_sweep": bullish_sweep, "bearish_sweep": bearish_sweep,
                "higher_highs": higher_highs, "higher_lows": higher_lows,
                "lower_highs": lower_highs, "lower_lows": lower_lows,
                "swing_high": high_ref, "swing_low": low_ref}
    except (TypeError, ValueError, KeyError, OverflowError):
        return empty
