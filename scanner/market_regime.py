"""Lightweight V5 market-regime classification."""
from __future__ import annotations

def classify_regime(row, *, high_atr_pct: float = 4.0):
    """Classify a completed-candle indicator row; missing inputs fail closed."""
    def number(key):
        try:
            value = float(row.get(key))
            return value if value == value and value not in (float("inf"), float("-inf")) else None
        except (TypeError, ValueError):
            return None
    close, ema50, ema200 = number("close"), number("ema50"), number("ema200")
    adx, plus, minus, atr_pct = number("adx"), number("di_plus"), number("di_minus"), number("atr_pct")
    if None in (close, ema50, ema200, adx, plus, minus):
        return {"regime": "UNKNOWN", "bias": "neutral", "confidence": 0}
    if atr_pct is not None and atr_pct >= high_atr_pct:
        return {"regime": "HIGH_VOLATILITY", "bias": "neutral", "confidence": 0}
    bias = "bull" if close > ema50 and ema50 > ema200 and plus > minus else (
        "bear" if close < ema50 and ema50 < ema200 and minus > plus else "neutral")
    if bias == "neutral" or adx < 18:
        return {"regime": "RANGE", "bias": "neutral", "confidence": 0}
    strength = "STRONG_" if adx >= 25 else "WEAK_"
    return {"regime": strength + bias.upper(), "bias": bias,
            "confidence": min(100, max(0, int(adx * 2)))}
