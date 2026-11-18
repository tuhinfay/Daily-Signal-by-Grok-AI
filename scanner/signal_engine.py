"""
Core Signal Engine - Multi-timeframe confluence scoring
Improved for more realistic signal generation
"""

from typing import Dict, Optional
import pandas as pd
from .indicators import add_indicators, get_latest
from .risk import calculate_levels
import config


class SignalEngine:
    def __init__(self):
        self.min_score = config.MIN_SCORE

    def analyze_pair(self, symbol: str, data: Dict[str, pd.DataFrame]) -> Optional[Dict]:
        scores = {"long": 0, "short": 0}
        reasons_long = []
        reasons_short = []

        # ---- Higher Timeframe Regime (1D) ----
        df_d = data.get("1d")
        if df_d is not None and len(df_d) > 40:
            df_d = add_indicators(df_d)
            latest_d = get_latest(df_d)

            close_d = latest_d.get("close", 0)
            ema50_d = latest_d.get("ema50", 0)
            ema9_d = latest_d.get("ema9", 0)
            ema21_d = latest_d.get("ema21", 0)

            if close_d > ema50_d:
                scores["long"] += 10
                reasons_long.append("Price > EMA50 (1D)")
            else:
                scores["short"] += 10
                reasons_short.append("Price < EMA50 (1D)")

            if ema9_d > ema21_d:
                scores["long"] += 7
                reasons_long.append("EMA9 > EMA21 (1D)")
            else:
                scores["short"] += 7
                reasons_short.append("EMA9 < EMA21 (1D)")

        # ---- Trend Timeframe (4H) ----
        df_4h = data.get("4h")
        if df_4h is not None and len(df_4h) > 40:
            df_4h = add_indicators(df_4h)
            latest_4h = get_latest(df_4h)

            adx = latest_4h.get("adx", 0) or 0
            di_plus = latest_4h.get("di_plus", 0) or 0
            di_minus = latest_4h.get("di_minus", 0) or 0

            if adx > config.ADX_TREND_THRESHOLD:
                if di_plus > di_minus:
                    scores["long"] += 12
                    reasons_long.append(f"ADX {adx:.0f} bullish (4H)")
                else:
                    scores["short"] += 12
                    reasons_short.append(f"ADX {adx:.0f} bearish (4H)")

            macd = latest_4h.get("macd", 0) or 0
            macd_sig = latest_4h.get("macd_signal", 0) or 0
            macd_hist = latest_4h.get("macd_hist", 0) or 0

            if macd > macd_sig:
                scores["long"] += 9
                reasons_long.append("MACD bullish (4H)")
            else:
                scores["short"] += 9
                reasons_short.append("MACD bearish (4H)")

            if macd_hist > 0:
                scores["long"] += 4
            else:
                scores["short"] += 4

            # EMA structure 4H
            if latest_4h.get("ema9", 0) > latest_4h.get("ema21", 0):
                scores["long"] += 6
            else:
                scores["short"] += 6

        # ---- Momentum (1H) ----
        df_1h = data.get("1h")
        if df_1h is not None and len(df_1h) > 40:
            df_1h = add_indicators(df_1h)
            latest_1h = get_latest(df_1h)

            rsi = latest_1h.get("rsi", 50) or 50

            if rsi < config.RSI_OVERSOLD:
                scores["long"] += 11
                reasons_long.append(f"RSI oversold ({rsi:.0f})")
            elif rsi > config.RSI_OVERBOUGHT:
                scores["short"] += 11
                reasons_short.append(f"RSI overbought ({rsi:.0f})")
            elif rsi < 48:
                scores["long"] += 5
                reasons_long.append(f"RSI low ({rsi:.0f})")
            elif rsi > 52:
                scores["short"] += 5
                reasons_short.append(f"RSI high ({rsi:.0f})")

            if latest_1h.get("ema9", 0) > latest_1h.get("ema21", 0):
                scores["long"] += 7
                reasons_long.append("EMA9 > EMA21 (1H)")
            else:
                scores["short"] += 7
                reasons_short.append("EMA9 < EMA21 (1H)")

            vol_ratio = latest_1h.get("vol_ratio", 1) or 1
            if vol_ratio > 1.25:
                if scores["long"] >= scores["short"]:
                    scores["long"] += 5
                    reasons_long.append("Volume spike")
                else:
                    scores["short"] += 5
                    reasons_short.append("Volume spike")

        # ---- Entry Trigger (15m) ----
        df_15 = data.get("15m")
        atr = None
        entry_price = None

        if df_15 is not None and len(df_15) > 40:
            df_15 = add_indicators(df_15)
            latest_15 = get_latest(df_15)
            entry_price = latest_15.get("close")
            atr = latest_15.get("atr")

            # MACD histogram direction
            macd_hist = latest_15.get("macd_hist", 0) or 0
            prev_hist = df_15["macd_hist"].iloc[-2] if len(df_15) > 1 else 0

            if macd_hist > 0 and prev_hist <= 0:
                scores["long"] += 10
                reasons_long.append("MACD cross up (15m)")
            elif macd_hist < 0 and prev_hist >= 0:
                scores["short"] += 10
                reasons_short.append("MACD cross down (15m)")
            elif macd_hist > 0:
                scores["long"] += 4
            else:
                scores["short"] += 4

            # Stoch RSI
            stoch_k = latest_15.get("stoch_k", 50) or 50
            if stoch_k < 25:
                scores["long"] += 7
                reasons_long.append("StochRSI oversold")
            elif stoch_k > 75:
                scores["short"] += 7
                reasons_short.append("StochRSI overbought")

            # BB position
            close = latest_15.get("close", 0) or 0
            bb_lower = latest_15.get("bb_lower", 0) or 0
            bb_upper = latest_15.get("bb_upper", 0) or 0

            if bb_lower > 0 and close <= bb_lower * 1.008:
                scores["long"] += 6
                reasons_long.append("Near BB lower")
            elif bb_upper > 0 and close >= bb_upper * 0.992:
                scores["short"] += 6
                reasons_short.append("Near BB upper")

            # EMA micro structure
            if latest_15.get("ema9", 0) > latest_15.get("ema21", 0):
                scores["long"] += 4
            else:
                scores["short"] += 4

        # ---- Final Decision ----
        long_score = min(int(scores["long"]), 100)
        short_score = min(int(scores["short"]), 100)

        side = None
        score = 0
        reasons = []

        # More relaxed: only need 4 points difference instead of 8
        if long_score >= self.min_score and long_score > short_score + 4:
            side = "LONG"
            score = long_score
            reasons = reasons_long
        elif short_score >= self.min_score and short_score > long_score + 4:
            side = "SHORT"
            score = short_score
            reasons = reasons_short

        if side is None or entry_price is None or atr is None or atr <= 0:
            return None

        levels = calculate_levels(side, entry_price, atr)
        if levels is None:
            return None

        return {
            "symbol": symbol,
            "side": side,
            "score": score,
            "entry": levels["entry"],
            "stop_loss": levels["stop_loss"],
            "tp1": levels["tp1"],
            "tp2": levels["tp2"],
            "risk_pct": levels["risk_pct"],
            "tp1_pct": levels["tp1_pct"],
            "tp2_pct": levels["tp2_pct"],
            "rr": levels["rr1"],
            "reasons": reasons[:5],
            "atr": round(float(atr), 6),
        }
