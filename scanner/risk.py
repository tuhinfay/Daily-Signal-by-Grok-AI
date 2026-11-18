"""
Risk management - Stop Loss & Take Profit calculator
"""

from typing import Dict, Optional
import config


def calculate_levels(
    side: str,
    entry: float,
    atr: float,
    rr_min: float = None,
) -> Optional[Dict]:
    """
    Calculate Stop Loss and Take Profit levels based on ATR.

    side: "LONG" or "SHORT"
    entry: current / entry price
    atr: Average True Range value
    """
    if atr is None or atr <= 0 or entry <= 0:
        return None

    rr_min = rr_min or config.MIN_RR

    if side.upper() == "LONG":
        sl = entry - (atr * config.ATR_SL_MULTIPLIER)
        tp1 = entry + (atr * config.ATR_TP1_MULTIPLIER)
        tp2 = entry + (atr * config.ATR_TP2_MULTIPLIER)

        risk = entry - sl
        reward1 = tp1 - entry
        reward2 = tp2 - entry

    elif side.upper() == "SHORT":
        sl = entry + (atr * config.ATR_SL_MULTIPLIER)
        tp1 = entry - (atr * config.ATR_TP1_MULTIPLIER)
        tp2 = entry - (atr * config.ATR_TP2_MULTIPLIER)

        risk = sl - entry
        reward1 = entry - tp1
        reward2 = entry - tp2
    else:
        return None

    if risk <= 0:
        return None

    rr1 = reward1 / risk
    rr2 = reward2 / risk

    # Reject if RR is too low
    if rr1 < rr_min:
        return None

    return {
        "entry": round(entry, 6),
        "stop_loss": round(sl, 6),
        "tp1": round(tp1, 6),
        "tp2": round(tp2, 6),
        "risk_pct": round((risk / entry) * 100, 2),
        "tp1_pct": round((reward1 / entry) * 100, 2),
        "tp2_pct": round((reward2 / entry) * 100, 2),
        "rr1": round(rr1, 2),
        "rr2": round(rr2, 2),
    }
