"""Daily Signal by Grok AI"""

TOP_N_PAIRS = 30
MIN_VOLUME_USDT = 12_000_000
MIN_SCORE = 50

TIMEFRAMES = {"entry": "15m", "momentum": "1h", "trend": "4h", "regime": "1d"}
CANDLE_LIMIT = {"5m": 120, "15m": 120, "1h": 120, "4h": 100, "1d": 90}

# Style-specific entry TF preference (used by API filter)
STYLE_ENTRY_TF = {
    "scalp": "5m",
    "day": "15m",
    "swing": "1h",
}

ATR_SL_MULTIPLIER = 1.5
ATR_TP1_MULTIPLIER = 2.0
ATR_TP2_MULTIPLIER = 3.5
MIN_RR = 1.25

RSI_PERIOD = 14
RSI_OVERSOLD = 38
RSI_OVERBOUGHT = 62
EMA_FAST = 9
EMA_MID = 21
EMA_SLOW = 50
EMA_TREND = 200
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
ADX_PERIOD = 14
ADX_TREND_THRESHOLD = 18
ADX_STRONG = 25
BB_PERIOD = 20
BB_STD = 2.0

PREFER_HTF_ALIGN = True
MIN_ADX_FOR_SIGNAL = 12

DEMO_BALANCE = 10000.0
DEFAULT_LEVERAGE = 10
MAX_LEVERAGE = 150
RISK_PER_TRADE_PCT = 1.0

# CLI export settings (used by main.py)
EXPORT_JSON = False
JSON_FILENAME = "signals.json"
