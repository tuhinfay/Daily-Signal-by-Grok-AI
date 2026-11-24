"""Risk-based paper position sizing with exchange lot/notional constraints."""
from __future__ import annotations
import math


def calculate_position_size(*, balance: float, risk_pct: float, entry: float,
                             stop_loss: float, qty_step: float,
                             min_qty: float = 0.0, min_notional: float = 0.0,
                             max_qty: float = None) -> dict:
    values = (balance, risk_pct, entry, stop_loss, qty_step, min_qty, min_notional)
    if any(not math.isfinite(float(v)) for v in values):
        raise ValueError("Sizing inputs must be finite")
    if balance <= 0 or not 0 < risk_pct <= 100 or entry <= 0 or qty_step <= 0:
        raise ValueError("Invalid balance, risk, entry or quantity step")
    distance = abs(entry-stop_loss)
    if distance <= 0:
        raise ValueError("Stop loss must differ from entry")
    risk_budget = balance*risk_pct/100
    raw_qty = risk_budget/distance
    qty = math.floor((raw_qty+1e-12)/qty_step)*qty_step
    if max_qty is not None:
        qty = min(qty, float(max_qty))
        qty = math.floor((qty+1e-12)/qty_step)*qty_step
    notional = qty*entry
    if qty < min_qty or notional < min_notional or qty <= 0:
        return {"ok":False,"reason":"BELOW_EXCHANGE_MINIMUM","quantity":qty,
                "notional":notional,"risk_budget":risk_budget}
    return {"ok":True,"reason":"SIZED","quantity":qty,"notional":notional,
            "risk_budget":risk_budget,"estimated_risk":qty*distance,
            "risk_pct_actual":qty*distance/balance*100}
