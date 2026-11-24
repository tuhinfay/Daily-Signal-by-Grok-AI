"""V5 evidence extraction and final qualification for completed USDT-M candles."""
from __future__ import annotations

from typing import Dict, Optional
import math

from .indicators import add_indicators, get_latest
from .market_regime import classify_regime
from .scoring import score_candidate, qualify_candidate
from .data_validator import is_fresh
from .market_structure import analyze_structure


def _num(row, key, default=None):
    try:
        value = float(row.get(key))
        return value if math.isfinite(value) else default
    except (TypeError, ValueError, AttributeError):
        return default


def _frame_row(data, timeframe):
    frame = data.get(timeframe)
    if frame is None or len(frame) < 50:
        return {}
    try:
        return get_latest(add_indicators(frame))
    except (KeyError, ValueError, TypeError):
        return {}



def validate_v5_frames(data: Dict) -> bool:
    """Require all V5 timeframes to be recent timestamped frames from the closed-candle client."""
    intervals = {"1d": 86400, "4h": 14400, "1h": 3600, "15m": 900}
    for timeframe, seconds in intervals.items():
        frame = data.get(timeframe)
        if frame is None or len(frame) < 50 or not is_fresh(frame, seconds * 2):
            return False
    return True


def build_v5_evidence(data: Dict, side: str, bias_1d: str, bias_4h: str,
                      rr: float, volume_24h: float = 0) -> Dict[str, float]:
    """Create six independent, bounded evidence values (0..1); unavailable data gets no credit."""
    daily = _frame_row(data, "1d")
    four_h = _frame_row(data, "4h")
    one_h = _frame_row(data, "1h")
    fifteen = _frame_row(data, "15m")
    direction = str(side).upper()
    regime = classify_regime(daily)
    aligned_1d = (direction == "LONG" and bias_1d == "bull") or (direction == "SHORT" and bias_1d == "bear")
    aligned_4h = (direction == "LONG" and bias_4h == "bull") or (direction == "SHORT" and bias_4h == "bear")
    htf = (int(aligned_1d) + int(aligned_4h)) / 2.0

    regime_match = (regime["bias"] == "bull" and direction == "LONG") or (regime["bias"] == "bear" and direction == "SHORT")
    regime_score = min(1.0, float(regime.get("confidence", 0)) / 70.0) if regime_match else 0.0

    structure_data = analyze_structure(data.get("4h"))
    ema_bull = (_num(four_h, "ema9", 0) or 0) > (_num(four_h, "ema21", 0) or 0)
    ema_aligned = ema_bull if direction == "LONG" else not ema_bull
    structure_score = (0.30 if ema_aligned else 0.0) + (
        structure_data["long_score"] if direction == "LONG" else structure_data["short_score"]
    ) * 0.70

    rsi = _num(one_h, "rsi")
    macd = _num(one_h, "macd")
    macd_signal = _num(one_h, "macd_signal")
    mom1 = (rsi is not None and ((direction == "LONG" and rsi >= 50) or (direction == "SHORT" and rsi <= 50)))
    mom2 = (macd is not None and macd_signal is not None and
            ((direction == "LONG" and macd > macd_signal) or (direction == "SHORT" and macd < macd_signal)))
    momentum_score = (int(bool(mom1)) + int(bool(mom2))) / 2.0

    vol_ratio = _num(fifteen, "vol_ratio")
    volume_score = min(1.0, max(0.0, (vol_ratio or 0.0) / 1.5)) if vol_ratio is not None else 0.0
    # 24h turnover is liquidity context, not directional futures-flow evidence.
    if volume_24h <= 0:
        volume_score = 0.0

    rr_value = _num({"rr": rr}, "rr", 0) or 0
    atr_pct = _num(fifteen, "atr_pct")
    rr_quality = min(1.0, max(0.0, rr_value / 2.5))
    volatility_quality = 1.0 if atr_pct is not None and 0 < atr_pct <= 3.0 else 0.5 if atr_pct is not None and atr_pct <= 5.0 else 0.0
    entry_risk = 0.7 * rr_quality + 0.3 * volatility_quality

    return {
        "market_regime": regime_score,
        "htf_alignment": htf,
        "market_structure": structure_score,
        "momentum": momentum_score,
        "volume_futures": volume_score,
        "entry_risk": entry_risk,
        "structure_detail": structure_data,
    }


def apply_futures_evidence(evidence: Dict[str, float], side: str,
                           context: Optional[Dict] = None) -> Dict[str, float]:
    """Blend only actually supplied derivatives/order-book observations; missing values earn no credit."""
    result = dict(evidence)
    context = context or {}
    observations = []
    funding = _num(context, "funding_rate")
    oi_change = _num(context, "oi_change_pct")
    imbalance = _num(context, "book_imbalance_pct")
    direction = str(side).upper()
    if funding is not None:
        observations.append(1.0 if ((direction == "LONG" and funding <= 0.0002) or
                                    (direction == "SHORT" and funding >= -0.0002)) else 0.0)
    if oi_change is not None:
        # OI expansion signals participation, not direction by itself.
        observations.append(0.5 if oi_change > 0 else 0.25)
    if imbalance is not None:
        observations.append(1.0 if ((direction == "LONG" and imbalance > 0) or
                                    (direction == "SHORT" and imbalance < 0)) else 0.0)
    if observations:
        result["volume_futures"] = 0.5 * result.get("volume_futures", 0.0) + 0.5 * (sum(observations) / len(observations))
    return result




def reward_risk_at_price(side: str, entry: float, stop_loss: float, take_profit: float) -> float:
    """Recalculate TP1 reward/risk at the current mark reference, not the stale candle close."""
    try:
        entry, stop_loss, take_profit = float(entry), float(stop_loss), float(take_profit)
        risk = abs(entry-stop_loss)
        reward = (take_profit-entry) if str(side).upper() == "LONG" else (entry-take_profit)
        if risk <= 0 or reward <= 0:
            return 0.0
        return reward/risk
    except (TypeError, ValueError):
        return 0.0


def validate_mark_price(entry: float, mark_price: Optional[float], atr: float,
                        max_deviation_atr: float = 0.5) -> tuple[bool, str, Optional[float]]:
    """Reject stale entry plans when a genuine futures mark price is unavailable or too far away."""
    try:
        entry, atr = float(entry), float(atr)
        mark = float(mark_price)
        if not all(math.isfinite(x) and x > 0 for x in (entry, atr, mark)):
            return False, "MARK_PRICE_UNAVAILABLE", None
        deviation = abs(mark-entry)/atr
        if deviation > max_deviation_atr:
            return False, "MARK_PRICE_DEVIATION_TOO_LARGE", deviation
        return True, "MARK_PRICE_CONFIRMED", deviation
    except (TypeError, ValueError):
        return False, "MARK_PRICE_UNAVAILABLE", None


def finalize_v5(evidence: Dict[str, float], *, rr: float, spread_pct: Optional[float],
                data_valid: bool, candle_closed: bool, mark_price: Optional[float] = None,
                entry: Optional[float] = None, atr: Optional[float] = None,
                require_mark_price: bool = False):
    """Fail closed if spread is unavailable; never let score override execution-quality filters."""
    scored = score_candidate(evidence)
    if spread_pct is None:
        return scored, False, "SPREAD_UNAVAILABLE"
    if require_mark_price:
        valid_mark, mark_reason, _ = validate_mark_price(entry, mark_price, atr)
        if not valid_mark:
            return scored, False, mark_reason
    ok, reason = qualify_candidate(
        scored, rr=rr, data_valid=data_valid, candle_closed=candle_closed,
        spread_pct=spread_pct, min_score=75, min_rr=2.0, max_spread_pct=0.10,
    )
    return scored, ok, reason
