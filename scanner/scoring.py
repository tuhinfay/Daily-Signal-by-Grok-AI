"""V5 quality-first scoring and hard trade qualification."""
import math
WEIGHTS = {
    "market_regime": 15, "htf_alignment": 20, "market_structure": 20,
    "momentum": 15, "volume_futures": 15, "entry_risk": 15,
}

def score_candidate(evidence):
    """Score one normalized evidence value per category (0..1)."""
    breakdown, total = {}, 0
    for category, weight in WEIGHTS.items():
        try:
            value = float(evidence.get(category, 0))
            if value != value:
                value = 0
        except (TypeError, ValueError):
            value = 0
        value = min(1.0, max(0.0, value))
        points = round(weight * value)
        breakdown[category] = {"weight": weight, "evidence": value, "points": points}
        total += points
    grade = "A+" if total >= 85 else "A" if total >= 75 else "B" if total >= 65 else "C"
    return {"score": total, "grade": grade, "breakdown": breakdown}

def qualify_candidate(scored, *, rr, data_valid, candle_closed, spread_pct=0.0,
                      min_score=75, min_rr=2.0, max_spread_pct=0.10):
    """Hard filters cannot be overridden by a high score."""
    if not data_valid:
        return False, "INVALID_OR_STALE_DATA"
    if not candle_closed:
        return False, "CANDLE_NOT_CLOSED"
    try:
        spread_value, rr_value = float(spread_pct), float(rr)
        if not math.isfinite(spread_value) or not math.isfinite(rr_value) or spread_value < 0 or rr_value < 0:
            return False, "INVALID_RISK_INPUT"
        if spread_value > max_spread_pct:
            return False, "SPREAD_TOO_WIDE"
        if rr_value < min_rr:
            return False, "RISK_REWARD_BELOW_MINIMUM"
        if int(scored.get("score", 0)) < min_score:
            return False, "QUALITY_SCORE_BELOW_MINIMUM"
    except (TypeError, ValueError):
        return False, "INVALID_RISK_INPUT"
    return True, "QUALIFIED"
