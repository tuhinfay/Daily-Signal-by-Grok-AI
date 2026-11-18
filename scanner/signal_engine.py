"""
Core Signal Engine v2 — Higher accuracy for Binance Futures

Design principles:
1. Higher timeframe (1D + 4H) defines BIAS — never fight it hard
2. 1H confirms momentum in same direction
3. 15m is only for ENTRY trigger (pullback / reclaim / cross)
4. Dead / choppy markets (low ADX) are skipped
5. Volume should support the move for high-quality signals
6. Conflicting TF scores are rejected, not forced
"""

from typing import Dict, Optional, List, Tuple
import pandas as pd
from .indicators import add_indicators, get_latest, get_prev
from .risk import calculate_levels
import config


class SignalEngine:
    def __init__(self):
        self.min_score = config.MIN_SCORE

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _safe(d: dict, key: str, default=0.0):
        v = d.get(key, default)
        if v is None or (isinstance(v, float) and (v != v)):  # NaN
            return default
        return v

    def _ema_stack_bull(self, row: dict) -> bool:
        return (
            self._safe(row, "ema9") > self._safe(row, "ema21")
            and self._safe(row, "ema21") > self._safe(row, "ema50")
        )

    def _ema_stack_bear(self, row: dict) -> bool:
        return (
            self._safe(row, "ema9") < self._safe(row, "ema21")
            and self._safe(row, "ema21") < self._safe(row, "ema50")
        )

    def _price_vs_ema50(self, row: dict) -> str:
        c = self._safe(row, "close")
        e = self._safe(row, "ema50")
        if e == 0:
            return "neutral"
        if c > e * 1.002:
            return "bull"
        if c < e * 0.998:
            return "bear"
        return "neutral"

    # ------------------------------------------------------------------
    # Per-timeframe scoring
    # ------------------------------------------------------------------
    def _score_regime_1d(self, df: pd.DataFrame) -> Tuple[int, int, List[str], List[str], str]:
        """Returns long_pts, short_pts, long_reasons, short_reasons, bias"""
        long_p = short_p = 0
        lr, sr = [], []
        bias = "neutral"

        if df is None or len(df) < 50:
            return 0, 0, lr, sr, bias

        df = add_indicators(df)
        r = get_latest(df)

        # Price vs EMA50 / EMA200 — strongest filter
        close = self._safe(r, "close")
        ema50 = self._safe(r, "ema50")
        ema200 = self._safe(r, "ema200")

        if close > ema50 and (ema200 == 0 or close > ema200):
            long_p += 14
            lr.append("1D above EMA50/200")
            bias = "bull"
        elif close < ema50 and (ema200 == 0 or close < ema200):
            short_p += 14
            sr.append("1D below EMA50/200")
            bias = "bear"
        elif close > ema50:
            long_p += 8
            lr.append("1D > EMA50")
            bias = "bull"
        elif close < ema50:
            short_p += 8
            sr.append("1D < EMA50")
            bias = "bear"

        if self._ema_stack_bull(r):
            long_p += 8
            lr.append("1D EMA stack bull")
            if bias == "neutral":
                bias = "bull"
        elif self._ema_stack_bear(r):
            short_p += 8
            sr.append("1D EMA stack bear")
            if bias == "neutral":
                bias = "bear"

        # Daily MACD
        if self._safe(r, "macd") > self._safe(r, "macd_signal"):
            long_p += 5
        else:
            short_p += 5

        return long_p, short_p, lr, sr, bias

    def _score_trend_4h(self, df: pd.DataFrame) -> Tuple[int, int, List[str], List[str], float, str]:
        long_p = short_p = 0
        lr, sr = [], []
        adx_val = 0.0
        bias = "neutral"

        if df is None or len(df) < 50:
            return 0, 0, lr, sr, adx_val, bias

        df = add_indicators(df)
        r = get_latest(df)

        adx_val = float(self._safe(r, "adx"))
        di_p = self._safe(r, "di_plus")
        di_m = self._safe(r, "di_minus")

        # ADX regime
        if adx_val >= config.ADX_STRONG:
            if di_p > di_m:
                long_p += 16
                lr.append(f"4H strong trend ADX{adx_val:.0f}")
                bias = "bull"
            else:
                short_p += 16
                sr.append(f"4H strong trend ADX{adx_val:.0f}")
                bias = "bear"
        elif adx_val >= config.ADX_TREND_THRESHOLD:
            if di_p > di_m:
                long_p += 11
                lr.append(f"4H trend ADX{adx_val:.0f}")
                bias = "bull"
            else:
                short_p += 11
                sr.append(f"4H trend ADX{adx_val:.0f}")
                bias = "bear"
        else:
            # Choppy — small points only, will be filtered later
            if di_p > di_m:
                long_p += 3
            else:
                short_p += 3

        # EMA structure
        if self._ema_stack_bull(r):
            long_p += 9
            lr.append("4H EMA stack bull")
            if bias != "bear":
                bias = "bull"
        elif self._ema_stack_bear(r):
            short_p += 9
            sr.append("4H EMA stack bear")
            if bias != "bull":
                bias = "bear"

        # MACD 4H
        macd = self._safe(r, "macd")
        sig = self._safe(r, "macd_signal")
        hist = self._safe(r, "macd_hist")
        if macd > sig and hist > 0:
            long_p += 8
            lr.append("4H MACD bull")
        elif macd < sig and hist < 0:
            short_p += 8
            sr.append("4H MACD bear")

        # Price vs EMA50
        pbias = self._price_vs_ema50(r)
        if pbias == "bull":
            long_p += 5
        elif pbias == "bear":
            short_p += 5

        return long_p, short_p, lr, sr, adx_val, bias

    def _score_momentum_1h(self, df: pd.DataFrame) -> Tuple[int, int, List[str], List[str], float]:
        long_p = short_p = 0
        lr, sr = [], []
        rsi_val = 50.0

        if df is None or len(df) < 50:
            return 0, 0, lr, sr, rsi_val

        df = add_indicators(df)
        r = get_latest(df)
        prev = get_prev(df, 1)

        rsi_val = float(self._safe(r, "rsi", 50))
        rsi_prev = float(self._safe(prev, "rsi", 50))

        # RSI zones + direction (reclaim is better than extreme)
        if rsi_val < config.RSI_OVERSOLD:
            long_p += 10
            lr.append(f"1H RSI OS {rsi_val:.0f}")
        elif rsi_val > config.RSI_OVERBOUGHT:
            short_p += 10
            sr.append(f"1H RSI OB {rsi_val:.0f}")
        elif 40 <= rsi_val <= 55 and rsi_val > rsi_prev:
            long_p += 7
            lr.append("1H RSI rising mid")
        elif 45 <= rsi_val <= 60 and rsi_val < rsi_prev:
            short_p += 7
            sr.append("1H RSI falling mid")
        elif rsi_val < 45:
            long_p += 4
        elif rsi_val > 55:
            short_p += 4

        # EMA
        if self._safe(r, "ema9") > self._safe(r, "ema21"):
            long_p += 8
            lr.append("1H EMA9>21")
        else:
            short_p += 8
            sr.append("1H EMA9<21")

        # MACD hist rising/falling
        hist = self._safe(r, "macd_hist")
        hist_prev = self._safe(prev, "macd_hist")
        if hist > 0 and hist >= hist_prev:
            long_p += 6
            lr.append("1H MACD hist+")
        elif hist < 0 and hist <= hist_prev:
            short_p += 6
            sr.append("1H MACD hist-")

        # Volume
        vol_r = float(self._safe(r, "vol_ratio", 1))
        if vol_r >= 1.35:
            if long_p >= short_p:
                long_p += 6
                lr.append("1H vol surge")
            else:
                short_p += 6
                sr.append("1H vol surge")
        elif vol_r >= 1.15:
            if long_p >= short_p:
                long_p += 3
            else:
                short_p += 3

        return long_p, short_p, lr, sr, rsi_val

    def _score_entry_15m(self, df: pd.DataFrame) -> Tuple[int, int, List[str], List[str], Optional[float], Optional[float]]:
        """Entry triggers only — pullback reclaim, cross, BB touch with structure."""
        long_p = short_p = 0
        lr, sr = [], []
        atr = None
        entry = None

        if df is None or len(df) < 50:
            return 0, 0, lr, sr, atr, entry

        df = add_indicators(df)
        r = get_latest(df)
        prev = get_prev(df, 1)

        entry = self._safe(r, "close")
        atr = self._safe(r, "atr")
        if atr <= 0:
            atr = None

        # MACD cross (fresh)
        hist = self._safe(r, "macd_hist")
        hist_prev = self._safe(prev, "macd_hist")
        if hist > 0 and hist_prev <= 0:
            long_p += 12
            lr.append("15m MACD cross↑")
        elif hist < 0 and hist_prev >= 0:
            short_p += 12
            sr.append("15m MACD cross↓")
        elif hist > 0 and hist > hist_prev:
            long_p += 5
        elif hist < 0 and hist < hist_prev:
            short_p += 5

        # Stoch RSI cross from extreme
        sk = self._safe(r, "stoch_k", 50)
        sd = self._safe(r, "stoch_d", 50)
        sk_p = self._safe(prev, "stoch_k", 50)
        if sk < 25 and sk > sk_p and sk > sd:
            long_p += 9
            lr.append("15m Stoch reclaim")
        elif sk > 75 and sk < sk_p and sk < sd:
            short_p += 9
            sr.append("15m Stoch reject")
        elif sk < 20:
            long_p += 4
        elif sk > 80:
            short_p += 4

        # BB mean-reversion only if not against strong trend later
        close = self._safe(r, "close")
        bb_l = self._safe(r, "bb_lower")
        bb_u = self._safe(r, "bb_upper")
        bb_m = self._safe(r, "bb_mid")
        if bb_l > 0 and close <= bb_l * 1.006:
            long_p += 7
            lr.append("15m BB lower")
        elif bb_u > 0 and close >= bb_u * 0.994:
            short_p += 7
            sr.append("15m BB upper")
        elif bb_m > 0 and close > bb_m and self._safe(r, "ema9") > self._safe(r, "ema21"):
            long_p += 3
        elif bb_m > 0 and close < bb_m and self._safe(r, "ema9") < self._safe(r, "ema21"):
            short_p += 3

        # Micro EMA
        if self._safe(r, "ema9") > self._safe(r, "ema21"):
            long_p += 4
        else:
            short_p += 4

        # Prefer non-doji entry candles
        body_r = self._safe(r, "body_ratio", 0)
        if body_r >= 0.45:
            if long_p > short_p:
                long_p += 3
            elif short_p > long_p:
                short_p += 3

        return long_p, short_p, lr, sr, atr, entry

    # ------------------------------------------------------------------
    # Main
    # ------------------------------------------------------------------
    def analyze_pair(
        self,
        symbol: str,
        data: Dict[str, pd.DataFrame],
        volume_24h: float = 0,
    ) -> Optional[Dict]:

        l1, s1, lr1, sr1, bias_1d = self._score_regime_1d(data.get("1d"))
        l4, s4, lr4, sr4, adx_val, bias_4h = self._score_trend_4h(data.get("4h"))
        l_h, s_h, lr_h, sr_h, rsi_val = self._score_momentum_1h(data.get("1h"))
        l15, s15, lr15, sr15, atr, entry = self._score_entry_15m(data.get("15m"))

        # Skip dead market
        if adx_val > 0 and adx_val < config.MIN_ADX_FOR_SIGNAL:
            return None

        long_score = l1 + l4 + l_h + l15
        short_score = s1 + s4 + s_h + s15

        # ---- HTF alignment gate (critical for accuracy) ----
        if config.REQUIRE_HTF_ALIGN:
            # Hard block: 1D strongly against trade
            if bias_1d == "bear":
                long_score = int(long_score * 0.45)  # heavy penalty
            if bias_1d == "bull":
                short_score = int(short_score * 0.45)

            # 4H should not fully oppose
            if bias_4h == "bear":
                long_score = int(long_score * 0.70)
            if bias_4h == "bull":
                short_score = int(short_score * 0.70)

            # Bonus for full alignment
            if bias_1d == "bull" and bias_4h == "bull":
                long_score += 10
                lr1.append("HTF aligned BULL")
            if bias_1d == "bear" and bias_4h == "bear":
                short_score += 10
                sr1.append("HTF aligned BEAR")

        long_score = min(int(long_score), 100)
        short_score = min(int(short_score), 100)

        # Need clear winner
        side = None
        score = 0
        reasons: List[str] = []

        margin = 6  # must beat opposite by this much
        if long_score >= self.min_score and long_score >= short_score + margin:
            # Extra quality: prefer HTF not opposing
            if config.REQUIRE_HTF_ALIGN and bias_1d == "bear":
                return None
            side = "LONG"
            score = long_score
            reasons = lr1 + lr4 + lr_h + lr15
        elif short_score >= self.min_score and short_score >= long_score + margin:
            if config.REQUIRE_HTF_ALIGN and bias_1d == "bull":
                return None
            side = "SHORT"
            score = short_score
            reasons = sr1 + sr4 + sr_h + sr15
        else:
            return None

        if entry is None or atr is None or atr <= 0:
            return None

        levels = calculate_levels(side, float(entry), float(atr))
        if levels is None:
            return None

        # Liquidity label
        vol = volume_24h or 0
        if vol >= 150_000_000:
            liq = "VERY HIGH"
        elif vol >= 50_000_000:
            liq = "HIGH"
        elif vol >= 20_000_000:
            liq = "MED"
        else:
            liq = "LOW"

        # Quality tag
        quality = "A"
        if score >= 75 and (
            (side == "LONG" and bias_1d == "bull" and bias_4h == "bull")
            or (side == "SHORT" and bias_1d == "bear" and bias_4h == "bear")
        ):
            quality = "A+"
        elif score >= 65:
            quality = "A"
        elif score >= 58:
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
            "timeframe": "15m entry | 1H mom | 4H trend | 1D regime",
        }
