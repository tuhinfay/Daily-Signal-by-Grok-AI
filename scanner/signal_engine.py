"""Signal Engine v4 — Professional trade plans"""

from typing import Dict, Optional, List, Tuple
import math
import pandas as pd
from .indicators import add_indicators, get_latest, get_prev
from .risk import calculate_levels
from .v5_analysis import build_v5_evidence, score_candidate, validate_v5_frames
import config


class SignalEngine:
    def __init__(self):
        self.min_score = config.MIN_SCORE

    @staticmethod
    def _safe(d, key, default=0.0):
        try:
            v = d.get(key, default)
            if v is None:
                return default
            value = float(v)
            return value if math.isfinite(value) else default
        except Exception:
            return default

    def _ema_bull(self, r):
        return self._safe(r, "ema9") > self._safe(r, "ema21") and self._safe(r, "close") > self._safe(r, "ema50")

    def _ema_bear(self, r):
        return self._safe(r, "ema9") < self._safe(r, "ema21") and self._safe(r, "close") < self._safe(r, "ema50")

    def _score_1d(self, df):
        long_p = short_p = 0
        lr, sr = [], []
        bias = "neutral"
        if df is None or len(df) < 205:
            return 0, 0, lr, sr, bias
        df = add_indicators(df)
        r = get_latest(df)
        c, e50, e200 = self._safe(r, "close"), self._safe(r, "ema50"), self._safe(r, "ema200")
        if e50 > 0 and c > e50:
            long_p += 12; lr.append("1D>EMA50"); bias = "bull"
        elif e50 > 0:
            short_p += 12; sr.append("1D<EMA50"); bias = "bear"
        if e200 > 0:
            if c > e200:
                long_p += 6
                bias = bias if bias != "neutral" else "bull"
            else:
                short_p += 6
                bias = bias if bias != "neutral" else "bear"
        if self._safe(r, "ema9") > self._safe(r, "ema21"):
            long_p += 5; lr.append("1D EMA↑")
        else:
            short_p += 5; sr.append("1D EMA↓")
        if self._safe(r, "macd") > self._safe(r, "macd_signal"):
            long_p += 4
        else:
            short_p += 4
        return long_p, short_p, lr, sr, bias

    def _score_4h(self, df):
        long_p = short_p = 0
        lr, sr = [], []
        adx_val = 0.0
        bias = "neutral"
        if df is None or len(df) < 50:
            return 0, 0, lr, sr, adx_val, bias
        df = add_indicators(df)
        r = get_latest(df)
        adx_val = self._safe(r, "adx")
        di_p, di_m = self._safe(r, "di_plus"), self._safe(r, "di_minus")
        if adx_val >= config.ADX_STRONG:
            if di_p > di_m:
                long_p += 14; lr.append(f"4H ADX{adx_val:.0f}↑"); bias = "bull"
            else:
                short_p += 14; sr.append(f"4H ADX{adx_val:.0f}↓"); bias = "bear"
        elif adx_val >= config.ADX_TREND_THRESHOLD:
            if di_p > di_m:
                long_p += 10; lr.append(f"4H ADX{adx_val:.0f}"); bias = "bull"
            else:
                short_p += 10; sr.append(f"4H ADX{adx_val:.0f}"); bias = "bear"
        else:
            long_p += 3 if di_p > di_m else 0
            short_p += 3 if di_m >= di_p else 0
        if self._ema_bull(r):
            long_p += 8; lr.append("4H structure↑"); bias = "bull" if bias != "bear" else bias
        elif self._ema_bear(r):
            short_p += 8; sr.append("4H structure↓"); bias = "bear" if bias != "bull" else bias
        hist = self._safe(r, "macd_hist")
        if self._safe(r, "macd") > self._safe(r, "macd_signal") and hist >= 0:
            long_p += 7; lr.append("4H MACD+")
        elif self._safe(r, "macd") < self._safe(r, "macd_signal") and hist <= 0:
            short_p += 7; sr.append("4H MACD-")
        if self._safe(r, "close") > self._safe(r, "swing_high_ref"):
            long_p += 6; lr.append("4H structure breakout↑")
        elif self._safe(r, "close") < self._safe(r, "swing_low_ref"):
            short_p += 6; sr.append("4H structure breakout↓")
        return long_p, short_p, lr, sr, adx_val, bias

    def _score_1h(self, df):
        long_p = short_p = 0
        lr, sr = [], []
        rsi_val = 50.0
        if df is None or len(df) < 50:
            return 0, 0, lr, sr, rsi_val
        df = add_indicators(df)
        r, prev = get_latest(df), get_prev(df, 1)
        rsi_val = self._safe(r, "rsi", 50)
        rsi_prev = self._safe(prev, "rsi", 50)
        # Do not fade a strong move just because RSI is stretched: require a confirmed turn.
        if rsi_prev <= config.RSI_OVERSOLD and rsi_val > rsi_prev and rsi_val > config.RSI_OVERSOLD:
            long_p += 8; lr.append(f"RSI rebound {rsi_val:.0f}")
        elif rsi_prev >= config.RSI_OVERBOUGHT and rsi_val < rsi_prev and rsi_val < config.RSI_OVERBOUGHT:
            short_p += 8; sr.append(f"RSI rollover {rsi_val:.0f}")
        elif rsi_val >= 52 and rsi_val > rsi_prev:
            long_p += 5; lr.append("RSI momentum↑")
        elif rsi_val <= 48 and rsi_val < rsi_prev:
            short_p += 5; sr.append("RSI momentum↓")
        if self._safe(r, "ema9") > self._safe(r, "ema21"):
            long_p += 7; lr.append("1H EMA↑")
        else:
            short_p += 7; sr.append("1H EMA↓")
        hist, hist_p = self._safe(r, "macd_hist"), self._safe(prev, "macd_hist")
        if hist > 0 and hist >= hist_p:
            long_p += 5
        elif hist < 0 and hist <= hist_p:
            short_p += 5
        vol_r = self._safe(r, "vol_ratio", 1)
        if vol_r >= 1.25:
            if long_p >= short_p:
                long_p += 5; lr.append("Vol+")
            else:
                short_p += 5; sr.append("Vol+")
        vwap = self._safe(r, "vwap")
        close = self._safe(r, "close")
        if vwap > 0 and close > vwap:
            long_p += 3; lr.append("1H above VWAP")
        elif vwap > 0 and close < vwap:
            short_p += 3; sr.append("1H below VWAP")
        return long_p, short_p, lr, sr, rsi_val

    def _score_15m(self, df):
        long_p = short_p = 0
        lr, sr = [], []
        atr = entry = None
        if df is None or len(df) < 50:
            return 0, 0, lr, sr, atr, entry
        df = add_indicators(df)
        r, prev = get_latest(df), get_prev(df, 1)
        entry = self._safe(r, "close")
        atr = self._safe(r, "atr")
        if atr <= 0:
            atr = None
        hist, hist_p = self._safe(r, "macd_hist"), self._safe(prev, "macd_hist")
        if hist > 0 and hist_p <= 0:
            long_p += 11; lr.append("15m MACD↑")
        elif hist < 0 and hist_p >= 0:
            short_p += 11; sr.append("15m MACD↓")
        elif hist > 0:
            long_p += 5
        else:
            short_p += 5
        sk, sk_p = self._safe(r, "stoch_k", 50), self._safe(prev, "stoch_k", 50)
        if sk < 30 and sk > sk_p:
            long_p += 7; lr.append("Stoch↑")
        elif sk > 70 and sk < sk_p:
            short_p += 7; sr.append("Stoch↓")
        elif sk < 25:
            long_p += 4
        elif sk > 75:
            short_p += 4
        c, bl, bu = self._safe(r, "close"), self._safe(r, "bb_lower"), self._safe(r, "bb_upper")
        prev_close = self._safe(prev, "close")
        prev_lower, prev_upper = self._safe(prev, "bb_lower"), self._safe(prev, "bb_upper")
        # Band touches alone are not reversals; require re-entry into the band.
        if prev_lower > 0 and prev_close < prev_lower and c > bl:
            long_p += 5; lr.append("BB reclaim")
        elif prev_upper > 0 and prev_close > prev_upper and c < bu:
            short_p += 5; sr.append("BB rejection")
        vwap = self._safe(r, "vwap")
        if vwap > 0:
            if c > vwap:
                long_p += 3; lr.append("Above VWAP")
            elif c < vwap:
                short_p += 3; sr.append("Below VWAP")
        poc = self._safe(r, "volume_poc")
        if poc > 0:
            if c > poc:
                long_p += 2; lr.append("Above Volume POC")
            elif c < poc:
                short_p += 2; sr.append("Below Volume POC")
        if self._safe(r, "ema9") > self._safe(r, "ema21"):
            long_p += 4
        else:
            short_p += 4

        # Confirm a breakout retest on the completed entry candle, not a wick-only breakout.
        retest_atr = atr or 0.0
        prior_level_up = self._safe(prev, "swing_high_ref")
        prior_level_down = self._safe(prev, "swing_low_ref")
        bullish_retest = (
            bool(prev.get("breakout_up", False)) and prior_level_up > 0 and retest_atr > 0
            and self._safe(r, "low") <= prior_level_up + retest_atr * 0.25
            and c > prior_level_up and c > self._safe(r, "open")
        )
        bearish_retest = (
            bool(prev.get("breakout_down", False)) and prior_level_down > 0 and retest_atr > 0
            and self._safe(r, "high") >= prior_level_down - retest_atr * 0.25
            and c < prior_level_down and c < self._safe(r, "open")
        )
        if bullish_retest:
            long_p += 8; lr.append("Breakout retest held")
        elif bearish_retest:
            short_p += 8; sr.append("Breakdown retest held")
        return long_p, short_p, lr, sr, atr, entry

    def analyze_pair(self, symbol: str, data: Dict[str, pd.DataFrame], volume_24h: float = 0,
                     style: str = "day") -> Optional[Dict]:
        style = style if style in ("scalp", "day", "swing", "hybrid") else "day"
        if style == "scalp":
            # 5m entry, 15m momentum, 1h structure, 4h regime.
            l1, s1, lr1, sr1, bias_1d = self._score_1d(data.get("4h"))
            l4, s4, lr4, sr4, adx_val, bias_4h = self._score_4h(data.get("1h"))
            lh, sh, lrh, srh, rsi_val = self._score_1h(data.get("15m"))
            l15, s15, lr15, sr15, atr, entry = self._score_15m(data.get("5m"))
            timeframe = "5m entry · 15m · 1h · 4h"
        elif style == "swing":
            # 1h entry, 4h momentum/structure, 1d regime.
            l1, s1, lr1, sr1, bias_1d = self._score_1d(data.get("1d"))
            l4, s4, lr4, sr4, adx_val, bias_4h = self._score_4h(data.get("4h"))
            lh, sh, lrh, srh, rsi_val = self._score_1h(data.get("4h"))
            l15, s15, lr15, sr15, atr, entry = self._score_15m(data.get("1h"))
            timeframe = "1h entry · 4h · 1d"
        elif style == "hybrid":
            # V5: daily bias, 4h structure, 1h setup, 15m confirmed entry.
            l1, s1, lr1, sr1, bias_1d = self._score_1d(data.get("1d"))
            l4, s4, lr4, sr4, adx_val, bias_4h = self._score_4h(data.get("4h"))
            lh, sh, lrh, srh, rsi_val = self._score_1h(data.get("1h"))
            l15, s15, lr15, sr15, atr, entry = self._score_15m(data.get("15m"))
            timeframe = "15m entry · 1h setup · 4h structure · 1d bias"
        else:
            l1, s1, lr1, sr1, bias_1d = self._score_1d(data.get("1d"))
            l4, s4, lr4, sr4, adx_val, bias_4h = self._score_4h(data.get("4h"))
            lh, sh, lrh, srh, rsi_val = self._score_1h(data.get("1h"))
            l15, s15, lr15, sr15, atr, entry = self._score_15m(data.get("15m"))
            timeframe = "15m entry · 1h · 4h · 1d"

        if adx_val > 0 and adx_val < config.MIN_ADX_FOR_SIGNAL:
            return None

        long_score = l1 + l4 + lh + l15
        short_score = s1 + s4 + sh + s15

        if getattr(config, "PREFER_HTF_ALIGN", True):
            if bias_1d == "bull" and bias_4h == "bull":
                long_score += 12; lr1.append("HTF BULL")
            if bias_1d == "bear" and bias_4h == "bear":
                short_score += 12; sr1.append("HTF BEAR")
            if bias_1d == "bear":
                long_score = int(long_score * 0.80)
            if bias_1d == "bull":
                short_score = int(short_score * 0.80)
            if bias_4h == "bear":
                long_score = int(long_score * 0.90)
            if bias_4h == "bull":
                short_score = int(short_score * 0.90)

        long_score = min(int(long_score), 100)
        short_score = min(int(short_score), 100)

        side = score = None
        reasons = []
        margin = 3
        required_score = config.V5_MIN_SCORE if style == "hybrid" else self.min_score
        if long_score >= required_score and long_score >= short_score + margin:
            side, score, reasons = "LONG", long_score, lr1 + lr4 + lrh + lr15
        elif short_score >= required_score and short_score >= long_score + margin:
            side, score, reasons = "SHORT", short_score, sr1 + sr4 + srh + sr15
        else:
            return None

        if entry is None or atr is None or atr <= 0:
            return None
        entry_df = data.get("5m" if style == "scalp" else "1h" if style == "swing" else "15m")
        levels = calculate_levels(side, float(entry), float(atr), market_data=entry_df)
        if not levels:
            return None
        v5_assessment = None
        evidence = None
        v5_data_valid = False
        if style == "hybrid":
            v5_data_valid = validate_v5_frames(data)
            if not v5_data_valid:
                return None
            evidence = build_v5_evidence(
                data, side, bias_1d, bias_4h,
                float(levels.get("rr1") or 0), volume_24h or 0,
            )
            v5_assessment = score_candidate(evidence)
            if int(v5_assessment["score"]) < config.V5_MIN_SCORE:
                return None
            score = int(v5_assessment["score"])

        vol = volume_24h or 0
        liq = "VERY HIGH" if vol >= 1e8 else "HIGH" if vol >= 4e7 else "MED" if vol >= 1.2e7 else "LOW"

        aligned = (side == "LONG" and bias_1d == "bull" and bias_4h == "bull") or (
            side == "SHORT" and bias_1d == "bear" and bias_4h == "bear"
        )
        if style == "hybrid":
            quality = "A+" if score >= 85 else "A" if score >= 75 else "B"
        elif score >= 72 and aligned:
            quality = "A+"
        elif score >= 62 or aligned:
            quality = "A"
        else:
            quality = "B"

        # Setup classification
        if aligned and adx_val >= config.ADX_TREND_THRESHOLD:
            setup = "TREND FOLLOW"
        elif "OS" in " ".join(reasons) or "OB" in " ".join(reasons) or "BB" in " ".join(reasons):
            setup = "REVERSAL / MEAN REVERT"
        elif "MACD↑" in " ".join(reasons) or "MACD↓" in " ".join(reasons):
            setup = "MOMENTUM BREAK"
        else:
            setup = "CONFLUENCE"

        # Professional plan text
        plan = (
            f"{side} {symbol} | Setup: {setup} | Quality {quality}\n"
            f"Entry ≈ {levels['entry']} | SL {levels['stop_loss']} ({levels['risk_pct']}%) | "
            f"TP1 {levels['tp1']} ({levels['tp1_pct']}%) | TP2 {levels['tp2']} ({levels['tp2_pct']}%)\n"
            f"R:R TP1 1:{levels['rr1']} · TP2 1:{levels['rr2']} | Risk only 0.5–1% account | Partial TP1, trail rest"
        )

        return {
            "symbol": symbol,
            "side": side,
            "score": score,
            "quality": quality,
            "setup": setup,
            "plan": plan,
            "entry": levels["entry"],
            "stop_loss": levels["stop_loss"],
            "tp1": levels["tp1"],
            "tp2": levels["tp2"],
            "risk_pct": levels["risk_pct"],
            "tp1_pct": levels["tp1_pct"],
            "tp2_pct": levels["tp2_pct"],
            "rr": levels["rr1"],
            "rr_tp2": levels["rr2"],
            "sl_basis": levels.get("sl_basis", "ATR"),
            "tp_basis": levels.get("tp_basis", "ATR"),
            "reasons": reasons[:6],
            "atr": round(float(atr), 6),
            "rsi": round(float(rsi_val), 1),
            "adx": round(float(adx_val), 1),
            "bias_1d": bias_1d,
            "bias_4h": bias_4h,
            "liquidity": liq,
            "volume_24h": round(vol),
            "style": style,
            "timeframe": timeframe,
            "v5_score_breakdown": v5_assessment["breakdown"] if v5_assessment else None,
            "v5_evidence": evidence if style == "hybrid" else None,
            "v5_data_valid": v5_data_valid,
        }
