"""
Daily Signal by Grok AI - Configuration
"""

# ============== SCAN SETTINGS ==============
TOP_N_PAIRS = 30              # How many top volume pairs to scan (lower = faster)
MIN_VOLUME_USDT = 3_000_000   # Minimum 24h volume filter
MIN_SCORE = 52                # Minimum confluence score (lowered for more signals)

# Timeframes used for multi-TF analysis
TIMEFRAMES = {
    "entry": "15m",
    "momentum": "1h",
    "trend": "4h",
    "regime": "1d",
}

# How many candles to fetch per timeframe
CANDLE_LIMIT = {
    "15m": 120,
    "1h": 120,
    "4h": 100,
    "1d": 80,
}

# ============== RISK MANAGEMENT ==============
ATR_SL_MULTIPLIER = 1.5
ATR_TP1_MULTIPLIER = 2.0
ATR_TP2_MULTIPLIER = 3.5
MIN_RR = 1.3                  # Slightly relaxed

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
ADX_TREND_THRESHOLD = 20

BB_PERIOD = 20
BB_STD = 2.0

# ============== OUTPUT ==============
SHOW_NEUTRAL = False
EXPORT_JSON = False
JSON_FILENAME = "signals.json"

TELEGRAM_BOT_TOKEN = ""
TELEGRAM_CHAT_ID = ""
DISCORD_WEBHOOK_URL = ""
