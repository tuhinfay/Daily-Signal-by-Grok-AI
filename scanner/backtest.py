"""Walk-forward backtest for Daily Signal using historical OHLCV CSV files.

CSV inputs need timestamp, open, high, low, close, volume columns.
Signals are evaluated only after candles close and filled at the next entry candle open.
If SL and a target are both touched in one candle, SL is assumed first (conservative).
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Dict, Optional

import pandas as pd

import config
from scanner.signal_engine import SignalEngine


TF_MINUTES = {"5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}
STYLE_TFS = {
    "scalp": {"entry": "5m", "required": ("5m", "15m", "1h", "4h")},
    "day": {"entry": "15m", "required": ("15m", "1h", "4h", "1d")},
    "swing": {"entry": "1h", "required": ("1h", "4h", "1d")},
}


def load_ohlcv_csv(path: str) -> pd.DataFrame:
    frame = pd.read_csv(path)
    required = {"timestamp", "open", "high", "low", "close", "volume"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"{path}: missing columns: {', '.join(sorted(missing))}")
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
    frame = frame.dropna(subset=["timestamp"]).set_index("timestamp").sort_index()
    for col in ("open", "high", "low", "close", "volume"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame = frame.dropna(subset=["open", "high", "low", "close", "volume"])
    return frame[~frame.index.duplicated(keep="last")]


def simulate_trade(side: str, fill: float, stop: float, tp1: float, tp2: float,
                   future: pd.DataFrame, max_bars: int = 96) -> Optional[Dict]:
    """Simulate 50% at TP1 and remainder at TP2/SL; return realized R multiple."""
    side = str(side).upper()
    if side not in ("LONG", "SHORT") or fill <= 0:
        return None
    risk = fill - stop if side == "LONG" else stop - fill
    if risk <= 0:
        return None
    if side == "LONG" and not (tp1 > fill and tp2 > tp1):
        return None
    if side == "SHORT" and not (tp1 < fill and tp2 < tp1):
        return None

    partial = False
    realized_r = 0.0
    rows = future.iloc[:max_bars]
    for offset, (_, candle) in enumerate(rows.iterrows(), start=1):
        high, low = float(candle["high"]), float(candle["low"])
        if side == "LONG":
            if low <= stop:
                realized_r += -0.5 if partial else -1.0
                return {"r": realized_r, "exit": "SL_AFTER_TP1" if partial else "SL", "bars": offset}
            if not partial and high >= tp1:
                partial = True
                realized_r += 0.5 * ((tp1 - fill) / risk)
                continue
            if partial and high >= tp2:
                realized_r += 0.5 * ((tp2 - fill) / risk)
                return {"r": realized_r, "exit": "TP2", "bars": offset}
        else:
            if high >= stop:
                realized_r += -0.5 if partial else -1.0
                return {"r": realized_r, "exit": "SL_AFTER_TP1" if partial else "SL", "bars": offset}
            if not partial and low <= tp1:
                partial = True
                realized_r += 0.5 * ((fill - tp1) / risk)
                continue
            if partial and low <= tp2:
                realized_r += 0.5 * ((fill - tp2) / risk)
                return {"r": realized_r, "exit": "TP2", "bars": offset}

    if rows.empty:
        return None
    last_close = float(rows.iloc[-1]["close"])
    remaining_r = ((last_close - fill) / risk) if side == "LONG" else ((fill - last_close) / risk)
    realized_r += 0.5 * remaining_r if partial else remaining_r
    return {"r": realized_r, "exit": "TIMEOUT_AFTER_TP1" if partial else "TIMEOUT", "bars": len(rows)}


def run_backtest(files: Dict[str, str], style: str = "day", step: int = 4,
                 max_bars: int = 96, min_score: Optional[int] = None) -> Dict:
    if style not in STYLE_TFS:
        raise ValueError("style must be scalp, day, or swing")
    required = STYLE_TFS[style]["required"]
    missing = [tf for tf in required if not files.get(tf)]
    if missing:
        raise ValueError(f"Missing CSV inputs for: {', '.join(missing)}")
    frames = {tf: load_ohlcv_csv(files[tf]) for tf in required}
    entry_tf = STYLE_TFS[style]["entry"]
    entry_frame = frames[entry_tf]
    engine = SignalEngine()
    if min_score is not None:
        engine.min_score = int(min_score)

    trades = []
    next_allowed_time = None
    duration = pd.Timedelta(minutes=TF_MINUTES[entry_tf])
    # Evaluate only closed candles and fill on the next candle's open.
    for i in range(50, len(entry_frame) - 1, max(1, int(step))):
        signal_bar = entry_frame.index[i]
        evaluation_time = signal_bar + duration
        if next_allowed_time is not None and signal_bar < next_allowed_time:
            continue
        data = {}
        for tf, frame in frames.items():
            closed = frame.loc[frame.index + pd.Timedelta(minutes=TF_MINUTES[tf]) <= evaluation_time]
            if len(closed) >= 30:
                data[tf] = closed
        if len(data) != len(required):
            continue
        signal = engine.analyze_pair("BACKTESTUSDT", data, volume_24h=100_000_000, style=style)
        if not signal:
            continue
        fill_bar = entry_frame.iloc[i + 1]
        fill = float(fill_bar["open"])
        future = entry_frame.iloc[i + 1:i + 1 + max_bars]
        if future.empty:
            continue
        result = simulate_trade(
            signal["side"], fill, float(signal["stop_loss"]),
            float(signal["tp1"]), float(signal["tp2"]), future, max_bars,
        )
        if result is None:
            continue
        trades.append({
            "signal_time": evaluation_time.isoformat(),
            "side": signal["side"], "score": signal["score"],
            "quality": signal["quality"], "fill": fill,
            "stop_loss": signal["stop_loss"], "tp1": signal["tp1"], "tp2": signal["tp2"],
            "rr_tp1": signal.get("rr"), "rr_tp2": signal.get("rr_tp2"),
            **result,
        })
        next_allowed_time = future.index[min(result["bars"] - 1, len(future) - 1)] + duration

    if not trades:
        return {"trades": [], "count": 0, "win_rate_pct": 0.0, "profit_factor": 0.0,
                "expectancy_r": 0.0, "max_drawdown_r": 0.0}

    results = pd.DataFrame(trades)
    r_values = results["r"].astype(float)
    gross_profit = float(r_values[r_values > 0].sum())
    gross_loss = abs(float(r_values[r_values < 0].sum()))
    equity = r_values.cumsum()
    drawdown = equity - equity.cummax().clip(lower=0)
    return {
        "trades": trades,
        "count": len(trades),
        "win_rate_pct": round(float((r_values > 0).mean() * 100), 2),
        "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss else (999.0 if gross_profit else 0.0),
        "expectancy_r": round(float(r_values.mean()), 3),
        "net_r": round(float(r_values.sum()), 3),
        "max_drawdown_r": round(abs(float(drawdown.min())), 3),
    }


def main():
    parser = argparse.ArgumentParser(description="Walk-forward Daily Signal historical backtest")
    parser.add_argument("--5m", dest="tf5m")
    parser.add_argument("--15m", dest="tf15m")
    parser.add_argument("--1h", dest="tf1h")
    parser.add_argument("--4h", dest="tf4h")
    parser.add_argument("--1d", dest="tf1d")
    parser.add_argument("--style", choices=tuple(STYLE_TFS), default="day")
    parser.add_argument("--step", type=int, default=4, help="Evaluate every N entry candles")
    parser.add_argument("--max-bars", type=int, default=96, help="Maximum forward entry candles per trade")
    parser.add_argument("--min-score", type=int, default=config.MIN_SCORE)
    parser.add_argument("--out", default="backtest_trades.csv")
    args = parser.parse_args()
    files = {"5m": args.tf5m, "15m": args.tf15m, "1h": args.tf1h, "4h": args.tf4h, "1d": args.tf1d}
    result = run_backtest(files, args.style, args.step, args.max_bars, args.min_score)
    trades = result.pop("trades")
    if trades:
        pd.DataFrame(trades).to_csv(args.out, index=False)
        result["trades_csv"] = str(Path(args.out))
    print(pd.Series(result).to_string())


if __name__ == "__main__":
    main()
