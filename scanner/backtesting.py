"""Causal single-position OHLCV backtesting and chronological walk-forward splits.

Signals are evaluated at a bar close and entered on the next bar open. Stop is
checked before target when both are touched in one bar (conservative ordering).
Fees, slippage and optional funding are charged explicitly.
"""
from __future__ import annotations

from typing import Callable, Dict, Iterable, List, Optional
import math
import pandas as pd


def run_backtest(frame: pd.DataFrame, signal_fn: Callable, *,
                 fee_rate: float = 0.0005, slippage_rate: float = 0.0002,
                 funding_rate_per_bar=0.0,
                 risk_fraction: float = 0.005, min_rr: float = 0.0,
                 max_entry_deviation_atr: Optional[float] = None) -> Dict:
    required = {"open", "high", "low", "close"}
    if frame is None or not required.issubset(frame.columns) or len(frame) < 3:
        raise ValueError("At least three OHLCV bars with OHLC columns are required")
    if not (0 <= fee_rate < 0.1 and 0 <= slippage_rate < 0.1 and 0 <= risk_fraction <= 1):
        raise ValueError("Invalid fee, slippage or risk settings")
    data = frame.sort_index()
    trades, equity, peak, max_dd = [], 1.0, 1.0, 0.0
    i = 0
    while i < len(data) - 1:
        signal = signal_fn(data.iloc[:i+1].copy(), i)
        if not signal:
            i += 1
            continue
        side = str(signal.get("side", "")).upper()
        if side not in ("LONG", "SHORT"):
            i += 1
            continue
        entry_raw = float(data.iloc[i+1]["open"])
        stop, target = float(signal["stop_loss"]), float(signal["take_profit"])
        try:
            spread_rate = float(signal.get("spread_pct") or 0.0) / 100.0
        except (TypeError, ValueError):
            i += 1
            continue
        if not math.isfinite(spread_rate) or spread_rate < 0 or spread_rate >= 0.1:
            i += 1
            continue
        half_cost = slippage_rate + spread_rate/2
        if entry_raw <= 0 or (side == "LONG" and not stop < entry_raw < target) or (side == "SHORT" and not target < entry_raw < stop):
            i += 1
            continue
        entry = entry_raw * (1 + half_cost if side == "LONG" else 1 - half_cost)
        risk = abs(entry - stop)
        reward = (target-entry) if side == "LONG" else (entry-target)
        if risk <= 0 or reward <= 0 or (min_rr > 0 and reward/risk < min_rr):
            i += 1
            continue
        if max_entry_deviation_atr is not None:
            try:
                reference = float(signal.get("reference_entry"))
                atr_value = float(signal.get("atr"))
                if atr_value <= 0 or abs(entry_raw-reference)/atr_value > max_entry_deviation_atr:
                    i += 1
                    continue
            except (TypeError, ValueError):
                i += 1
                continue
        exit_price, reason, exit_i = None, "END_OF_DATA", len(data)-1
        for j in range(i+1, len(data)):
            bar = data.iloc[j]
            hi, lo = float(bar["high"]), float(bar["low"])
            hit_stop = lo <= stop if side == "LONG" else hi >= stop
            hit_target = hi >= target if side == "LONG" else lo <= target
            if hit_stop:  # conservative: stop first if both touched
                exit_price, reason, exit_i = stop, "STOP", j
                break
            if hit_target:
                exit_price, reason, exit_i = target, "TARGET", j
                break
        if exit_price is None:
            exit_price = float(data.iloc[-1]["close"])
        exit_price *= (1 - half_cost if side == "LONG" else 1 + half_cost)
        direction = 1 if side == "LONG" else -1
        gross_return = direction * (exit_price-entry) / entry
        bars_held = exit_i - (i+1)
        if callable(funding_rate_per_bar):
            funding_cost = float(funding_rate_per_bar(i+1, exit_i, side))
        elif isinstance(funding_rate_per_bar, pd.Series):
            rates = funding_rate_per_bar.reindex(data.index[i+1:exit_i+1]).fillna(0.0)
            funding_cost = float(rates.sum()) * direction
        else:
            funding_cost = float(funding_rate_per_bar) * bars_held * direction
        net_return = gross_return - 2*fee_rate - funding_cost
        # Fixed fractional risk budget scaled by realized R multiple.
        r_multiple = direction * (exit_price-entry) / risk
        equity *= max(0.0, 1.0 + risk_fraction * r_multiple - 2*fee_rate*risk_fraction - funding_cost*risk_fraction)
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak-equity)/peak if peak else 0.0)
        trades.append({"side": side, "entry_index": i+1, "exit_index": exit_i,
                       "entry": entry, "exit": exit_price, "reason": reason,
                       "gross_return": gross_return, "net_return": net_return,
                       "r_multiple": r_multiple, "equity": equity})
        i = max(exit_i+1, i+1)
    wins = [t for t in trades if t["net_return"] > 0]
    losses = [t for t in trades if t["net_return"] < 0]
    gross_profit = sum(t["net_return"] for t in wins)
    gross_loss = abs(sum(t["net_return"] for t in losses))
    return {"trades": trades, "trade_count": len(trades),
            "win_rate": len(wins)/len(trades) if trades else 0.0,
            "profit_factor": round(gross_profit/gross_loss, 4) if gross_loss else ("INF" if gross_profit else None),
            "expectancy": sum(t["net_return"] for t in trades)/len(trades) if trades else 0.0,
            "max_drawdown": max_dd, "ending_equity": equity}


def walk_forward_splits(frame: pd.DataFrame, *, train_fraction: float = 0.5,
                        validation_fraction: float = 0.2, test_fraction: float = 0.1,
                        step_fraction: float = 0.1) -> List[Dict]:
    """Generate expanding-train, rolling validation/test windows in chronological order."""
    if frame is None or len(frame) < 20:
        raise ValueError("At least 20 bars are required for walk-forward evaluation")
    fractions = (train_fraction, validation_fraction, test_fraction, step_fraction)
    if any(not 0 < x < 1 for x in fractions) or train_fraction + validation_fraction + test_fraction > 1:
        raise ValueError("Invalid walk-forward fractions")
    n = len(frame)
    train_n = max(10, int(n * train_fraction))
    val_n = max(2, int(n * validation_fraction))
    test_n = max(2, int(n * test_fraction))
    step_n = max(1, int(n * step_fraction))
    folds, origin = [], train_n
    while origin + val_n + test_n <= n:
        folds.append({
            "fold": len(folds) + 1,
            "train": frame.iloc[:origin].copy(),
            "validation": frame.iloc[origin:origin+val_n].copy(),
            "test": frame.iloc[origin+val_n:origin+val_n+test_n].copy(),
        })
        origin += step_n
    if not folds:
        raise ValueError("Not enough rows to create a complete walk-forward fold")
    return folds


def run_walk_forward(frame: pd.DataFrame, fit_fn: Callable, *,
                     train_fraction: float = 0.5, validation_fraction: float = 0.2,
                     test_fraction: float = 0.1, step_fraction: float = 0.1,
                     backtest_kwargs: Optional[Dict] = None) -> Dict:
    """Fit/tune only on each fold's past data, then evaluate the untouched test window."""
    reports = []
    for fold in walk_forward_splits(
        frame, train_fraction=train_fraction,
        validation_fraction=validation_fraction, test_fraction=test_fraction,
        step_fraction=step_fraction,
    ):
        signal_fn = fit_fn(fold["train"], fold["validation"])
        if not callable(signal_fn):
            raise ValueError("fit_fn must return a callable signal function")
        report = run_backtest(fold["test"], signal_fn, **(backtest_kwargs or {}))
        reports.append({"fold": fold["fold"], "train_rows": len(fold["train"]),
                        "validation_rows": len(fold["validation"]),
                        "test_rows": len(fold["test"]), "metrics": report})
    counts = [x["metrics"]["trade_count"] for x in reports]
    total = sum(counts)
    wins = sum(x["metrics"]["win_rate"] * x["metrics"]["trade_count"] for x in reports)
    return {"folds": reports, "fold_count": len(reports), "trade_count": total,
            "weighted_win_rate": wins/total if total else 0.0}
