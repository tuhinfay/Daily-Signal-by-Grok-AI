"""
Daily Signal by Grok AI - Configuration
Balanced accuracy for Binance Futures (signals + quality)
"""

# ============== SCAN SETTINGS ==============
TOP_N_PAIRS = 30
MIN_VOLUME_USDT = 12_000_000   # liquid but not too strict
MIN_SCORE = 50                 # practical bar so signals appear

TIMEFRAMES = {
    "entry": "15m",
    "momentum": "1h",
    "trend": "4h",
    "regime": "1d",
}

CANDLE_LIMIT = {
    "15m": 120,
    "1h": 120,
    "4h": 100,
    "1d": 90,
}

# ============== RISK MANAGEMENT ==============
ATR_SL_MULTIPLIER = 1.5
ATR_TP1_MULTIPLIER = 2.0
ATR_TP2_MULTIPLIER = 3.5
MIN_RR = 1.25

# ============== INDICATOR SETTINGS ==============
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

# Soft HTF preference (not hard block)
REQUIRE_HTF_ALIGN = False
PREFER_HTF_ALIGN = True
MIN_ADX_FOR_SIGNAL = 12        # only skip ultra-dead markets

SHOW_NEUTRAL = False
EXPORT_JSON = False
JSON_FILENAME = "signals.json"

TELEGRAM_BOT_TOKEN = ""
TELEGRAM_CHAT_ID = ""
DISCORD_WEBHOOK_URL = ""
