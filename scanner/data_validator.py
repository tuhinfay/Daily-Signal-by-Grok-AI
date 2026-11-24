"""V5 market-data validation helpers."""
from __future__ import annotations
from typing import Optional
import pandas as pd

def closed_candles(frame: Optional[pd.DataFrame], *, last_candle_closed: bool = False, min_rows: int = 30):
    """Return valid completed candles. REST callers conservatively drop the final row."""
    if frame is None or not isinstance(frame, pd.DataFrame) or frame.empty:
        return None
    required = {"open", "high", "low", "close", "volume"}
    if not required.issubset(frame.columns):
        return None
    data = frame.copy()
    if not last_candle_closed:
        data = data.iloc[:-1].copy()
    if data.empty:
        return None
    for col in required:
        data[col] = pd.to_numeric(data[col], errors="coerce")
    data = data.replace([float("inf"), float("-inf")], float("nan")).dropna(subset=list(required))
    valid = ((data["open"] > 0) & (data["high"] > 0) & (data["low"] > 0) &
             (data["close"] > 0) & (data["volume"] >= 0) &
             (data["high"] >= data[["open", "close", "low"]].max(axis=1)) &
             (data["low"] <= data[["open", "close", "high"]].min(axis=1)))
    data = data.loc[valid].sort_index()
    return data if len(data) >= min_rows else None

def is_fresh(frame: Optional[pd.DataFrame], max_age_seconds: int, now=None) -> bool:
    """Check timestamp-indexed market data against a freshness SLA."""
    if frame is None or frame.empty or not isinstance(frame.index, pd.DatetimeIndex):
        return False
    stamp = frame.index[-1]
    current = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
    current = current.tz_localize("UTC") if current.tzinfo is None else current.tz_convert("UTC")
    age = (current - stamp).total_seconds()
    return 0 <= age <= max(0, int(max_age_seconds))
