"""
Core Signal Engine - Multi-timeframe confluence scoring
"""

from typing import Dict, Optional
import pandas as pd
from .indicators import add_indicators, get_latest
from .risk import calculate_levels
import config


class SignalEngine:
    def __init__(self):
        self.min_score = config.MIN_SCORE

    def analyze_pair(self, symbol: str, data: Dict[str, pd.DataFrame], volume_24h: float = 0) -> Optional[Dict]:
        scores = {"long": 0, "short": 0}
        reasons_long = []
        reasons_short = []

        # ---- 1D Regime ----
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
                reasons_long.append("1D Price>EMA50")
            else:
                scores["short"] += 10
                reasons_short.append("1D Price<EMA50")

            if ema9_d > ema21_d:
                scores["long"] += 7
                reasons_long.append("1D EMA9>21")
            else:
                scores["short"] += 7
                reasons_short.append("1D EMA9<21")

        # ---- 4H Trend ----
        df_4h = data.get("4h")
        adx_val = 0
        if df_4h is not None and len(df_4h) > 40:
            df_4h = add_indicators(df_4h)
            latest_4h = get_latest(df_4h)
            adx_val = latest_4h.get("adx", 0) or 0
            di_plus = latest_4h.get("di_plus", 0) or 0
            di_minus = latest_4h.get("di_minus", 0) or 0

            if adx_val > config.ADX_TREND_THRESHOLD:
                if di_plus > di_minus:
                    scores["long"] += 12
                    reasons_long.append(f"4H ADX{adx_val:.0f} Bull")
                else:
                    scores["short"] += 12
                    reasons_short.append(f"4H ADX{adx_val:.0f} Bear")

            macd = latest_4h.get("macd", 0) or 0
            macd_sig = latest_4h.get("macd_signal", 0) or 0
            if macd > macd_sig:
                scores["long"] += 9
                reasons_long.append("4H MACD Bull")
            else:
                scores["short"] += 9
                reasons_short.append("4H MACD Bear")

            if (latest_4h.get("macd_hist", 0) or 0) > 0:
                scores["long"] += 4
            else:
                scores["short"] += 4

            if latest_4h.get("ema9", 0) > latest_4h.get("ema21", 0):
                scores["long"] += 6
            else:
                scores["short"] += 6

        # ---- 1H Momentum ----
        df_1h = data.get("1h")
        rsi_val = 50
        if df_1h is not None and len(df_1h) > 40:
            df_1h = add_indicators(df_1h)
            latest_1h = get_latest(df_1h)
            rsi_val = latest_1h.get("rsi", 50) or 50

            if rsi_val < config.RSI_OVERSOLD:
                scores["long"] += 11
                reasons_long.append(f"1H RSI {rsi_val:.0f} OS")
            elif rsi_val > config.RSI_OVERBOUGHT:
                scores["short"] += 11
                reasons_short.append(f"1H RSI {rsi_val:.0f} OB")
            elif rsi_val < 48:
                scores["long"] += 5
            elif rsi_val > 52:
                scores["short"] += 5

            if latest_1h.get("ema9", 0) > latest_1h.get("ema21", 0):
                scores["long"] += 7
                reasons_long.append("1H EMA9>21")
            else:
                scores["short"] += 7
                reasons_short.append("1H EMA9<21")

            if (latest_1h.get("vol_ratio", 1) or 1) > 1.25:
                if scores["long"] >= scores["short"]:
                    scores["long"] += 5
                    reasons_long.append("Vol Spike")
                else:
                    scores["short"] += 5
                    reasons_short.append("Vol Spike")

        # ---- 15m Entry ----
        df_15 = data.get("15m")
        atr = None
        entry_price = None

        if df_15 is not None and len(df_15) > 40:
            df_15 = add_indicators(df_15)
            latest_15 = get_latest(df_15)
            entry_price = latest_15.get("close")
            atr = latest_15.get("atr")

            macd_hist = latest_15.get("macd_hist", 0) or 0
            prev_hist = df_15["macd_hist"].iloc[-2] if len(df_15) > 1 else 0

            if macd_hist > 0 and prev_hist <= 0:
                scores["long"] += 10
                reasons_long.append("15m MACD Cross↑")
            elif macd_hist < 0 and prev_hist >= 0:
                scores["short"] += 10
                reasons_short.append("15m MACD Cross↓")
            elif macd_hist > 0:
                scores["long"] += 4
            else:
                scores["short"] += 4

            stoch_k = latest_15.get("stoch_k", 50) or 50
            if stoch_k < 25:
                scores["long"] += 7
                reasons_long.append("15m Stoch OS")
            elif stoch_k > 75:
                scores["short"] += 7
                reasons_short.append("15m Stoch OB")

            close = latest_15.get("close", 0) or 0
            bb_lower = latest_15.get("bb_lower", 0) or 0
            bb_upper = latest_15.get("bb_upper", 0) or 0
            if bb_lower > 0 and close <= bb_lower * 1.008:
                scores["long"] += 6
                reasons_long.append("Near BB Low")
            elif bb_upper > 0 and close >= bb_upper * 0.992:
                scores["short"] += 6
                reasons_short.append("Near BB High")

            if latest_15.get("ema9", 0) > latest_15.get("ema21", 0):
                scores["long"] += 4
            else:
                scores["short"] += 4

        long_score = min(int(scores["long"]), 100)
        short_score = min(int(scores["short"]), 100)

        side = None
        score = 0
        reasons = []

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

        # Liquidity label
        vol = volume_24h or 0
        if vol >= 100_000_000:
            liq = "VERY HIGH"
        elif vol >= 40_000_000:
            liq = "HIGH"
        elif vol >= 15_000_000:
            liq = "MED"
        else:
            liq = "LOW"

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
            "rsi": round(float(rsi_val), 1),
            "adx": round(float(adx_val), 1),
            "liquidity": liq,
            "volume_24h": round(vol),
            "timeframe": "15m entry | 1H+4H+1D confirm",
        }
