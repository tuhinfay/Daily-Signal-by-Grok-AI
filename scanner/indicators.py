"""
Technical Indicators calculation
"""

import pandas as pd
import numpy as np
from ta.trend import EMAIndicator, MACD, ADXIndicator
from ta.momentum import RSIIndicator, StochRSIIndicator
from ta.volatility import BollingerBands, AverageTrueRange
import config


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add all required technical indicators to DataFrame."""
    if df is None or len(df) < 50:
        return df

    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"]

    # EMAs
    df["ema9"] = EMAIndicator(close, window=config.EMA_FAST).ema_indicator()
    df["ema21"] = EMAIndicator(close, window=config.EMA_MID).ema_indicator()
    df["ema50"] = EMAIndicator(close, window=config.EMA_SLOW).ema_indicator()
    df["ema200"] = EMAIndicator(close, window=config.EMA_TREND).ema_indicator()

    # RSI
    df["rsi"] = RSIIndicator(close, window=config.RSI_PERIOD).rsi()

    # Stochastic RSI
    stoch_rsi = StochRSIIndicator(close, window=14, smooth1=3, smooth2=3)
    df["stoch_k"] = stoch_rsi.stochrsi_k()
    df["stoch_d"] = stoch_rsi.stochrsi_d()

    # MACD
    macd = MACD(close, window_slow=config.MACD_SLOW, window_fast=config.MACD_FAST, window_sign=config.MACD_SIGNAL)
    df["macd"] = macd.macd()
    df["macd_signal"] = macd.macd_signal()
    df["macd_hist"] = macd.macd_diff()

    # ADX
    adx = ADXIndicator(high, low, close, window=config.ADX_PERIOD)
    df["adx"] = adx.adx()
    df["di_plus"] = adx.adx_pos()
    df["di_minus"] = adx.adx_neg()

    # Bollinger Bands
    bb = BollingerBands(close, window=config.BB_PERIOD, window_dev=config.BB_STD)
    df["bb_upper"] = bb.bollinger_hband()
    df["bb_mid"] = bb.bollinger_mavg()
    df["bb_lower"] = bb.bollinger_lband()
    df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / df["bb_mid"]

    # ATR
    df["atr"] = AverageTrueRange(high, low, close, window=14).average_true_range()

    # Volume SMA
    df["vol_sma"] = volume.rolling(window=20).mean()
    df["vol_ratio"] = volume / df["vol_sma"]

    return df


def get_latest(df: pd.DataFrame) -> dict:
    """Return the latest row as a clean dict."""
    if df is None or df.empty:
        return {}
    row = df.iloc[-1]
    return row.to_dict()
