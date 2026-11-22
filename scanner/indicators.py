"""Technical indicators with guarded warm-up periods and market structure."""

import numpy as np
import pandas as pd
from ta.trend import EMAIndicator, MACD, ADXIndicator
from ta.momentum import RSIIndicator, StochRSIIndicator
from ta.volatility import BollingerBands, AverageTrueRange
import config


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or len(df) < 50:
        return df
    df = df.copy()
    close, high, low, volume = df["close"], df["high"], df["low"], df["volume"]
    typical = (high + low + close) / 3.0

    for name, window in (("ema9", config.EMA_FAST), ("ema21", config.EMA_MID),
                         ("ema50", config.EMA_SLOW), ("ema200", config.EMA_TREND)):
        df[name] = EMAIndicator(close, window=window).ema_indicator()

    df["rsi"] = RSIIndicator(close, window=config.RSI_PERIOD).rsi()
    stoch = StochRSIIndicator(close, window=14, smooth1=3, smooth2=3)
    df["stoch_k"], df["stoch_d"] = stoch.stochrsi_k(), stoch.stochrsi_d()

    macd = MACD(close, window_slow=config.MACD_SLOW, window_fast=config.MACD_FAST,
                window_sign=config.MACD_SIGNAL)
    df["macd"], df["macd_signal"], df["macd_hist"] = macd.macd(), macd.macd_signal(), macd.macd_diff()

    adx = ADXIndicator(high, low, close, window=config.ADX_PERIOD)
    df["adx"], df["di_plus"], df["di_minus"] = adx.adx(), adx.adx_pos(), adx.adx_neg()

    bb = BollingerBands(close, window=config.BB_PERIOD, window_dev=config.BB_STD)
    df["bb_upper"], df["bb_mid"], df["bb_lower"] = bb.bollinger_hband(), bb.bollinger_mavg(), bb.bollinger_lband()
    df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / df["bb_mid"].replace(0, np.nan)

    df["atr"] = AverageTrueRange(high, low, close, window=14).average_true_range()
    df["atr_pct"] = (df["atr"] / close.replace(0, np.nan)) * 100
    df["vol_sma"] = volume.rolling(20, min_periods=20).mean()
    df["vol_ratio"] = volume / df["vol_sma"].replace(0, np.nan)

    # Rolling VWAP and a lightweight 48-bar volume-profile POC proxy.
    pv = typical * volume
    df["vwap"] = pv.rolling(config.VWAP_WINDOW, min_periods=max(10, config.VWAP_WINDOW // 2)).sum() / volume.rolling(
        config.VWAP_WINDOW, min_periods=max(10, config.VWAP_WINDOW // 2)).sum().replace(0, np.nan)
    def _poc(window):
        p = typical.iloc[max(0, window.name - config.VOLUME_PROFILE_WINDOW + 1):window.name + 1]
        v = volume.iloc[max(0, window.name - config.VOLUME_PROFILE_WINDOW + 1):window.name + 1]
        if len(p) < 10 or float(v.sum()) <= 0:
            return np.nan
        bins = np.linspace(float(p.min()), float(p.max()) + 1e-12, config.VOLUME_PROFILE_BINS + 1)
        ids = np.clip(np.digitize(p.to_numpy(), bins) - 1, 0, config.VOLUME_PROFILE_BINS - 1)
        totals = np.bincount(ids, weights=v.to_numpy(), minlength=config.VOLUME_PROFILE_BINS)
        idx = int(np.argmax(totals))
        return float((bins[idx] + bins[idx + 1]) / 2)
    df["volume_poc"] = pd.Series(
        [_poc(pd.Series([i], name=i)) for i in range(len(df))], index=df.index, dtype=float
    )

    df["body"] = (close - df["open"]).abs()
    df["range"] = (high - low).replace(0, np.nan)
    df["body_ratio"] = df["body"] / df["range"]
    # Confirmed rolling structure references; current candle compared with prior bars only.
    df["swing_high_ref"] = high.shift(1).rolling(config.STRUCTURE_WINDOW).max()
    df["swing_low_ref"] = low.shift(1).rolling(config.STRUCTURE_WINDOW).min()
    df["breakout_up"] = close > df["swing_high_ref"]
    df["breakout_down"] = close < df["swing_low_ref"]
    df["hh"] = high > high.shift(1)
    df["ll"] = low < low.shift(1)
    return df


def get_latest(df: pd.DataFrame) -> dict:
    return {} if df is None or df.empty else df.iloc[-1].to_dict()


def get_prev(df: pd.DataFrame, n: int = 1) -> dict:
    return {} if df is None or len(df) <= n else df.iloc[-(n + 1)].to_dict()
