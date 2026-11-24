"""Adapter that runs the live Hybrid V5 signal engine on historical multi-timeframe CSV data."""
from __future__ import annotations

from typing import Dict
import pandas as pd
import config
from .signal_engine import SignalEngine

_INTERVALS = {"15m":900, "1h":3600, "4h":14400, "1d":86400}


def make_v5_signal_fn(symbol: str, frames: Dict[str, pd.DataFrame], *,
                      assumed_spread_pct: float = 0.05):
    """Build a causal signal callback for scanner.backtesting.run_backtest.

    Historical mark is approximated by the next 15m bar open in the backtester;
    historical order-book spread is unavailable, so a caller-supplied assumption
    is mandatory and must remain within the configured hard limit.
    """
    if set(_INTERVALS) - set(frames):
        raise ValueError("Historical V5 backtest needs 15m, 1h, 4h and 1d CSV frames")
    if not 0 <= float(assumed_spread_pct) <= config.V5_MAX_SPREAD_PCT:
        raise ValueError("Assumed spread exceeds V5 hard spread limit")
    normalized = {}
    for tf, frame in frames.items():
        if tf not in _INTERVALS:
            continue
        if not isinstance(frame.index, pd.DatetimeIndex):
            raise ValueError(f"{tf} frame must have a DatetimeIndex")
        normalized[tf] = frame.sort_index()
    engine = SignalEngine()

    def signal_fn(prefix_15m: pd.DataFrame, index: int):
        if len(prefix_15m) < 100:
            return None
        bar_close_time = prefix_15m.index[-1] + pd.Timedelta(seconds=_INTERVALS["15m"])
        aligned = {"15m": prefix_15m}
        for tf in ("1h", "4h", "1d"):
            frame = normalized[tf]
            completed = frame.loc[
                (frame.index + pd.Timedelta(seconds=_INTERVALS[tf])) <= bar_close_time
            ]
            minimum = 205 if tf == "1d" else 50
            if len(completed) < minimum:
                return None
            aligned[tf] = completed
        if len(aligned["15m"]) < 50:
            return None
        turnover = float((prefix_15m["close"] * prefix_15m["volume"]).tail(96).sum())
        if turnover < config.MIN_VOLUME_USDT:
            return None
        signal = engine.analyze_pair(
            symbol, aligned, volume_24h=turnover, style="hybrid", historical=True
        )
        if not signal or int(signal.get("score") or 0) < config.V5_MIN_SCORE:
            return None
        return {
            "side": signal["side"],
            "stop_loss": float(signal["stop_loss"]),
            "take_profit": float(signal["tp1"]),
            "reference_entry": float(signal["entry"]),
            "atr": float(signal["atr"]),
            "score": int(signal["score"]),
            "spread_pct": float(assumed_spread_pct),
        }

    return signal_fn
