"""Minimum-duration forward-paper validation for persisted simulated trades."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, Iterable
import math


def evaluate_forward_trades(trades: Iterable[Dict], *, start: datetime,
                            end: datetime, minimum_days: int = 30) -> Dict:
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    days = max(0.0, (end-start).total_seconds()/86400)
    closed = []
    for trade in trades or []:
        if str(trade.get("status", "")).upper() != "CLOSED":
            continue
        stamp = trade.get("closed_at") or trade.get("created_at")
        try:
            dt = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if start <= dt <= end:
                pnl = float(trade.get("realized_pnl") or 0)
                if math.isfinite(pnl):
                    closed.append((dt, pnl))
        except (TypeError, ValueError):
            continue
    closed.sort(key=lambda item: item[0])
    pnls = [pnl for _, pnl in closed]
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x < 0]
    gross_profit, gross_loss = sum(wins), abs(sum(losses))
    equity = peak = 0.0
    max_dd = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        max_dd = max(max_dd, peak-equity)
    count = len(pnls)
    return {
        "ready": days >= minimum_days and count >= 30,
        "minimum_days": minimum_days,
        "elapsed_days": round(days, 2),
        "closed_trades": count,
        "win_rate_pct": round(len(wins)/count*100, 2) if count else 0.0,
        "profit_factor": round(gross_profit/gross_loss, 4) if gross_loss else (None if not gross_profit else "INF"),
        "expectancy_usd": round(sum(pnls)/count, 4) if count else 0.0,
        "net_pnl_usd": round(sum(pnls), 4),
        "max_drawdown_usd": round(max_dd, 4),
        "status": "READY_FOR_REVIEW" if days >= minimum_days and count >= 30 else "COLLECTING_FORWARD_DATA",
    }
