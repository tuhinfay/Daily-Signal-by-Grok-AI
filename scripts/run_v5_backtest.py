"""Run a V5 Hybrid backtest from aligned historical OHLCV CSV files.

CSV format: timestamp,open,high,low,close,volume. Timestamps are candle-open
UTC timestamps. Optional funding CSV format: timestamp,funding_rate, with rates
as decimal fractions at funding settlement timestamps.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import pandas as pd

import config
from scanner.backtesting import run_backtest
from scanner.v5_backtest import make_v5_signal_fn


def load_ohlcv(path: str) -> pd.DataFrame:
    frame = pd.read_csv(path)
    if "timestamp" not in frame.columns:
        raise ValueError(f"{path}: expected a timestamp column")
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame = frame.set_index("timestamp").sort_index()
    required = ["open", "high", "low", "close", "volume"]
    if any(name not in frame.columns for name in required):
        raise ValueError(f"{path}: expected OHLCV columns {required}")
    for name in required:
        frame[name] = pd.to_numeric(frame[name], errors="coerce")
    return frame.dropna(subset=required)


def main():
    parser = argparse.ArgumentParser(description="Historical V5 Hybrid backtest")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--15m", dest="tf15", required=True, help="15m OHLCV CSV")
    parser.add_argument("--1h", dest="tf1h", required=True, help="1h OHLCV CSV")
    parser.add_argument("--4h", dest="tf4h", required=True, help="4h OHLCV CSV")
    parser.add_argument("--1d", dest="tf1d", required=True, help="1d OHLCV CSV; provide >=205 completed candles")
    parser.add_argument("--funding", help="Optional funding CSV with timestamp,funding_rate")
    parser.add_argument("--spread-pct", type=float, default=0.05,
                        help="Assumed historical spread in percent; must be <= V5 hard limit")
    parser.add_argument("--fee-rate", type=float, default=0.0005, help="Fee fraction per side")
    parser.add_argument("--slippage-rate", type=float, default=0.0002, help="Additional slippage fraction per side")
    parser.add_argument("--risk-fraction", type=float, default=0.005, help="Equity risk fraction per trade")
    parser.add_argument("--output", default="v5-backtest-result.json")
    args = parser.parse_args()

    frames = {
        "15m": load_ohlcv(args.tf15), "1h": load_ohlcv(args.tf1h),
        "4h": load_ohlcv(args.tf4h), "1d": load_ohlcv(args.tf1d),
    }
    funding = 0.0
    if args.funding:
        f = pd.read_csv(args.funding)
        if not {"timestamp", "funding_rate"}.issubset(f.columns):
            raise ValueError("Funding CSV must contain timestamp,funding_rate")
        f["timestamp"] = pd.to_datetime(f["timestamp"], utc=True)
        funding = pd.Series(pd.to_numeric(f["funding_rate"], errors="coerce").fillna(0).to_numpy(),
                            index=f["timestamp"]).sort_index()

    signal_fn = make_v5_signal_fn(args.symbol, frames, assumed_spread_pct=args.spread_pct)
    result = run_backtest(
        frames["15m"], signal_fn, fee_rate=args.fee_rate,
        slippage_rate=args.slippage_rate, funding_rate_per_bar=funding,
        risk_fraction=args.risk_fraction, min_rr=config.V5_MIN_RR,
        max_entry_deviation_atr=0.5,
    )
    summary = {key: value for key, value in result.items() if key != "trades"}
    summary["symbol"] = args.symbol.upper()
    summary["assumed_spread_pct"] = args.spread_pct
    summary["fee_rate_per_side"] = args.fee_rate
    summary["slippage_rate_per_side"] = args.slippage_rate
    summary["funding_data_supplied"] = bool(args.funding)
    summary["warning"] = "Research only. Historical mark price is approximated by next 15m open; spread is an assumption unless supplied from historical order-book data."
    Path(args.output).write_text(json.dumps(summary, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps(summary, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
