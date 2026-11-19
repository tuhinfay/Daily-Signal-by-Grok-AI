"""
Core Signal Engine v3 — Balanced accuracy for Binance Futures

Goals:
- Produce usable signals in real markets (not zero)
- Prefer HTF alignment for higher quality
- Soft penalties instead of hard blocks
- Clear quality grades A+ / A / B
"""

from typing import Dict, Optional, List, Tuple
import pandas as pd
from .indicators import add_indicators, get_latest, get_prev
from .risk import calculate_levels
import config


class SignalEngine:
    def __init__(self):
        self.min_score = config.MIN_SCORE

    @staticmethod
    def _safe(d: dict, key: str, default=0.0):
        try:
            v = d.get(key, default)
            if v is None:
                return default
            if isinstance(v, float) and v != v:  # NaN
                return default
            return float(v)
        except Exception:
            return default

    def _ema_stack_bull(self, row: dict) -> bool:
        return (
            self._safe(row, "ema9") > self._safe(row, "ema21")
            and self._safe(row, "close") > self._safe(row, "ema50")
        )

    def _ema_stack_bear(self, row: dict) -> bool:
        return (
            self._safe(row, "ema9") < self._safe(row, "ema21")
            and self._safe(row, "close") < self._safe(row, "ema50")
        )

    # ------------------------------------------------------------------
    def _score_1d(self, df) -> Tuple[int, int, List[str], List[str], str]:
        long_p = short_p = 0
        lr, sr = [], []
        bias = "neutral"
        if df is None or len(df) < 40:
            return 0, 0, lr, sr, bias

        df = add_indicators(df)
        r = get_latest(df)
        close = self._safe(r, "close")
        ema50 = self._safe(r, "ema50")
        ema200 = self._safe(r, "ema200")

        if ema50 > 0 and close > ema50:
            long_p += 12
            lr.append("1D > EMA50")
            bias = "bull"
        elif ema50 > 0 and close < ema50:
            short_p += 12
            sr.append("1D < EMA50")
            bias = "bear"

        if ema200 > 0:
            if close > ema200:
                long_p += 6
                if bias == "neutral":
                    bias = "bull"
            else:
                short_p += 6
                if bias == "neutral":
                    bias = "bear"

        if self._safe(r, "ema9") > self._safe(r, "ema21"):
            long_p += 5
            lr.append("1D EMA9>21")
        else:
            short_p += 5
            sr.append("1D EMA9<21")

        if self._safe(r, "macd") > self._safe(r, "macd_signal"):
            long_p += 4
        else:
            short_p += 4

        return long_p, short_p, lr, sr, bias

    def _score_4h(self, df) -> Tuple[int, int, List[str], List[str], float, str]:
        long_p = short_p = 0
        lr, sr = [], []
        adx_val = 0.0
        bias = "neutral"
        if df is None or len(df) < 40:
            return 0, 0, lr, sr, adx_val, bias

        df = add_indicators(df)
        r = get_latest(df)
        adx_val = self._safe(r, "adx")
        di_p = self._safe(r, "di_plus")
        di_m = self._safe(r, "di_minus")

        if adx_val >= config.ADX_STRONG:
            if di_p > di_m:
                long_p += 14
                lr.append(f"4H strong ADX{adx_val:.0f}")
                bias = "bull"
            else:
                short_p += 14
                sr.append(f"4H strong ADX{adx_val:.0f}")
                bias = "bear"
        elif adx_val >= config.ADX_TREND_THRESHOLD:
            if di_p > di_m:
                long_p += 10
                lr.append(f"4H ADX{adx_val:.0f}")
                bias = "bull"
            else:
                short_p += 10
                sr.append(f"4H ADX{adx_val:.0f}")
                bias = "bear"
        else:
            if di_p > di_m:
                long_p += 4
            else:
                short_p += 4

        if self._ema_stack_bull(r):
            long_p += 8
            lr.append("4H EMA bull")
            if bias != "bear":
                bias = "bull"
        elif self._ema_stack_bear(r):
            short_p += 8
            sr.append("4H EMA bear")
            if bias != "bull":
                bias = "bear"

        hist = self._safe(r, "macd_hist")
        if self._safe(r, "macd") > self._safe(r, "macd_signal") and hist >= 0:
            long_p += 7
            lr.append("4H MACD+")
        elif self._safe(r, "macd") < self._safe(r, "macd_signal") and hist <= 0:
            short_p += 7
            sr.append("4H MACD-")

        return long_p, short_p, lr, sr, adx_val, bias

    def _score_1h(self, df) -> Tuple[int, int, List[str], List[str], float]:
        long_p = short_p = 0
        lr, sr = [], []
        rsi_val = 50.0
        if df is None or len(df) < 40:
            return 0, 0, lr, sr, rsi_val

        df = add_indicators(df)
        r = get_latest(df)
        prev = get_prev(df, 1)
        rsi_val = self._safe(r, "rsi", 50)
        rsi_prev = self._safe(prev, "rsi", 50)

        if rsi_val <= config.RSI_OVERSOLD:
            long_p += 10
            lr.append(f"1H RSI {rsi_val:.0f} OS")
        elif rsi_val >= config.RSI_OVERBOUGHT:
            short_p += 10
            sr.append(f"1H RSI {rsi_val:.0f} OB")
        elif rsi_val < 48 and rsi_val >= rsi_prev:
            long_p += 6
            lr.append("1H RSI rising")
        elif rsi_val > 52 and rsi_val <= rsi_prev:
            short_p += 6
            sr.append("1H RSI falling")
        elif rsi_val < 50:
            long_p += 3
        else:
            short_p += 3

        if self._safe(r, "ema9") > self._safe(r, "ema21"):
            long_p += 7
            lr.append("1H EMA9>21")
        else:
            short_p += 7
            sr.append("1H EMA9<21")

        hist = self._safe(r, "macd_hist")
        hist_p = self._safe(prev, "macd_hist")
        if hist > 0 and hist >= hist_p:
            long_p += 5
        elif hist < 0 and hist <= hist_p:
            short_p += 5

        vol_r = self._safe(r, "vol_ratio", 1)
        if vol_r >= 1.25:
            if long_p >= short_p:
                long_p += 5
                lr.append("1H vol+")
            else:
                short_p += 5
                sr.append("1H vol+")

        return long_p, short_p, lr, sr, rsi_val

    def _score_15m(self, df) -> Tuple[int, int, List[str], List[str], Optional[float], Optional[float]]:
        long_p = short_p = 0
        lr, sr = [], []
        atr = entry = None
        if df is None or len(df) < 40:
            return 0, 0, lr, sr, atr, entry

        df = add_indicators(df)
        r = get_latest(df)
        prev = get_prev(df, 1)

        entry = self._safe(r, "close")
        atr = self._safe(r, "atr")
        if atr <= 0:
            atr = None

        hist = self._safe(r, "macd_hist")
        hist_p = self._safe(prev, "macd_hist")
        if hist > 0 and hist_p <= 0:
            long_p += 11
            lr.append("15m MACD cross↑")
        elif hist < 0 and hist_p >= 0:
            short_p += 11
            sr.append("15m MACD cross↓")
        elif hist > 0:
            long_p += 5
        else:
            short_p += 5

        sk = self._safe(r, "stoch_k", 50)
        sk_p = self._safe(prev, "stoch_k", 50)
        sd = self._safe(r, "stoch_d", 50)
        if sk < 30 and sk > sk_p:
            long_p += 7
            lr.append("15m Stoch up")
        elif sk > 70 and sk < sk_p:
            short_p += 7
            sr.append("15m Stoch down")
        elif sk < 25:
            long_p += 4
        elif sk > 75:
            short_p += 4

        close = self._safe(r, "close")
        bb_l = self._safe(r, "bb_lower")
        bb_u = self._safe(r, "bb_upper")
        if bb_l > 0 and close <= bb_l * 1.01:
            long_p += 6
            lr.append("15m BB low")
        elif bb_u > 0 and close >= bb_u * 0.99:
            short_p += 6
            sr.append("15m BB high")

        if self._safe(r, "ema9") > self._safe(r, "ema21"):
            long_p += 4
        else:
            short_p += 4

        return long_p, short_p, lr, sr, atr, entry

    # ------------------------------------------------------------------
    def analyze_pair(
        self,
        symbol: str,
        data: Dict[str, pd.DataFrame],
        volume_24h: float = 0,
    ) -> Optional[Dict]:

        l1, s1, lr1, sr1, bias_1d = self._score_1d(data.get("1d"))
        l4, s4, lr4, sr4, adx_val, bias_4h = self._score_4h(data.get("4h"))
        lh, sh, lrh, srh, rsi_val = self._score_1h(data.get("1h"))
        l15, s15, lr15, sr15, atr, entry = self._score_15m(data.get("15m"))

        # Only skip truly dead markets
        if adx_val > 0 and adx_val < config.MIN_ADX_FOR_SIGNAL:
            return None

        long_score = l1 + l4 + lh + l15
        short_score = s1 + s4 + sh + s15

        # Soft HTF preference (bonus / mild penalty — NOT hard block)
        if getattr(config, "PREFER_HTF_ALIGN", True):
            if bias_1d == "bull" and bias_4h == "bull":
                long_score += 12
                lr1.append("HTF aligned BULL")
            elif bias_1d == "bear" and bias_4h == "bear":
                short_score += 12
                sr1.append("HTF aligned BEAR")

            # mild penalty if fighting 1D
            if bias_1d == "bear":
                long_score = int(long_score * 0.80)
            if bias_1d == "bull":
                short_score = int(short_score * 0.80)

            # mild 4H penalty
            if bias_4h == "bear":
                long_score = int(long_score * 0.90)
            if bias_4h == "bull":
                short_score = int(short_score * 0.90)

        long_score = min(int(long_score), 100)
        short_score = min(int(short_score), 100)

        side = None
        score = 0
        reasons: List[str] = []
        margin = 3  # small edge is enough

        if long_score >= self.min_score and long_score >= short_score + margin:
            side = "LONG"
            score = long_score
            reasons = lr1 + lr4 + lrh + lr15
        elif short_score >= self.min_score and short_score >= long_score + margin:
            side = "SHORT"
            score = short_score
            reasons = sr1 + sr4 + srh + sr15
        else:
            return None

        if entry is None or atr is None or atr <= 0:
            return None

        levels = calculate_levels(side, float(entry), float(atr))
        if levels is None:
            return None

        vol = volume_24h or 0
        if vol >= 100_000_000:
            liq = "VERY HIGH"
        elif vol >= 40_000_000:
            liq = "HIGH"
        elif vol >= 12_000_000:
            liq = "MED"
        else:
            liq = "LOW"

        # Quality
        aligned = (
            (side == "LONG" and bias_1d == "bull" and bias_4h == "bull")
            or (side == "SHORT" and bias_1d == "bear" and bias_4h == "bear")
        )
        if score >= 72 and aligned:
            quality = "A+"
        elif score >= 62 or aligned:
            quality = "A"
        else:
            quality = "B"

        return {
            "symbol": symbol,
            "side": side,
            "score": score,
            "quality": quality,
            "entry": levels["entry"],
            "stop_loss": levels["stop_loss"],
            "tp1": levels["tp1"],
            "tp2": levels["tp2"],
            "risk_pct": levels["risk_pct"],
            "tp1_pct": levels["tp1_pct"],
            "tp2_pct": levels["tp2_pct"],
            "rr": levels["rr1"],
            "reasons": reasons[:6],
            "atr": round(float(atr), 6),
            "rsi": round(float(rsi_val), 1),
            "adx": round(float(adx_val), 1),
            "bias_1d": bias_1d,
            "bias_4h": bias_4h,
            "liquidity": liq,
            "volume_24h": round(vol),
            "timeframe": "15m entry | 1H | 4H | 1D",
        }
