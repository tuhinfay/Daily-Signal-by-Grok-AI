"""Structure-aware risk management with ATR fallback."""

from typing import Dict, Optional
import math
import pandas as pd
import config


def calculate_levels(side: str, entry: float, atr: float, rr_min: float = None,
                     market_data: Optional[pd.DataFrame] = None) -> Optional[Dict]:
    if atr is None or atr <= 0 or entry <= 0:
        return None
    side = str(side).upper()
    if side not in ("LONG", "SHORT"):
        return None

    rr_min = float(rr_min or config.MIN_RR)
    risk_floor = max(atr * 0.8, entry * 0.0005)
    risk_cap = atr * 3.0
    sl_atr = atr * 1.5
    tp1_atr = atr * 2.0
    tp2_atr = atr * 3.5

    # Use completed candles only; exclude the currently forming candle.
    highs, lows = [], []
    if market_data is not None and len(market_data) >= 22:
        try:
            sample = market_data.iloc[-49:-1] if len(market_data) >= 49 else market_data.iloc[:-1]
            highs = [float(x) for x in sample["high"].dropna().tolist() if math.isfinite(float(x))]
            lows = [float(x) for x in sample["low"].dropna().tolist() if math.isfinite(float(x))]
        except (KeyError, TypeError, ValueError):
            highs, lows = [], []

    if side == "LONG":
        structure_sl = min(lows[-20:]) - atr * 0.2 if lows else None
        sl = entry - sl_atr
        if structure_sl is not None and structure_sl < entry:
            distance = entry - structure_sl
            if risk_floor <= distance <= risk_cap:
                sl = min(sl, structure_sl)
        risk = entry - sl
        if risk <= 0 or risk > risk_cap:
            sl = entry - sl_atr
            risk = sl_atr

        resistances = [x for x in highs if x > entry]
        valid_resistances = [x for x in resistances if (x-entry) >= risk*rr_min]
        near_resistance = min(valid_resistances) if valid_resistances else None
        far_resistance = max(resistances) if resistances else None
        tp1 = near_resistance if near_resistance is not None else entry + tp1_atr
        tp2 = far_resistance if far_resistance and far_resistance > tp1 + atr*0.25 else entry + max(tp2_atr, risk*2.5)
        reward1, reward2 = tp1-entry, tp2-entry
    else:
        structure_sl = max(highs[-20:]) + atr * 0.2 if highs else None
        sl = entry + sl_atr
        if structure_sl is not None and structure_sl > entry:
            distance = structure_sl - entry
            if risk_floor <= distance <= risk_cap:
                sl = max(sl, structure_sl)
        risk = sl-entry
        if risk <= 0 or risk > risk_cap:
            sl = entry + sl_atr
            risk = sl_atr

        supports = [x for x in lows if x < entry]
        valid_supports = [x for x in supports if (entry-x) >= risk*rr_min]
        near_support = max(valid_supports) if valid_supports else None
        far_support = min(supports) if supports else None
        tp1 = near_support if near_support is not None else entry-tp1_atr
        tp2 = far_support if far_support and far_support < tp1-atr*0.25 else entry-max(tp2_atr, risk*2.5)
        reward1, reward2 = entry-tp1, entry-tp2

    if reward1 <= 0 or reward2 <= reward1:
        return None
    rr1, rr2 = reward1/risk, reward2/risk
    if rr1 < rr_min:
        return None

    def rnd(x):
        if x >= 100:
            return round(x, 2)
        if x >= 1:
            return round(x, 4)
        return round(x, 6)

    return {
        "entry": rnd(entry), "stop_loss": rnd(sl), "tp1": rnd(tp1), "tp2": rnd(tp2),
        "risk_pct": round((risk/entry)*100, 2),
        "tp1_pct": round((reward1/entry)*100, 2),
        "tp2_pct": round((reward2/entry)*100, 2),
        "rr1": round(rr1, 2), "rr2": round(rr2, 2),
        "sl_basis": "SWING+ATR" if structure_sl is not None and sl != (entry-sl_atr if side=="LONG" else entry+sl_atr) else "ATR",
        "tp_basis": "STRUCTURE" if (near_resistance if side=="LONG" else near_support) == tp1 else "ATR",
    }
