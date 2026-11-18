"""
Daily Signal by Grok AI - Configuration
"""

# ============== SCAN SETTINGS ==============
TOP_N_PAIRS = 50              # How many top volume pairs to scan
MIN_VOLUME_USDT = 5_000_000   # Minimum 24h volume filter
MIN_SCORE = 65                # Minimum confluence score to display signal

# Timeframes used for multi-TF analysis
TIMEFRAMES = {
    "entry": "15m",           # Entry trigger
    "momentum": "1h",         # Momentum confirmation
    "trend": "4h",            # Trend direction
    "regime": "1d",           # Higher timeframe regime
}

# How many candles to fetch per timeframe
CANDLE_LIMIT = {
    "15m": 200,
    "1h": 200,
    "4h": 150,
    "1d": 100,
}

# ============== RISK MANAGEMENT ==============
# ATR multipliers
ATR_SL_MULTIPLIER = 1.5       # Stop Loss distance
ATR_TP1_MULTIPLIER = 2.0      # Take Profit 1
ATR_TP2_MULTIPLIER = 3.5      # Take Profit 2

# Minimum Risk:Reward to accept a signal
MIN_RR = 1.5

# ============== INDICATOR SETTINGS ==============
RSI_PERIOD = 14
RSI_OVERSOLD = 35
RSI_OVERBOUGHT = 65

EMA_FAST = 9
EMA_MID = 21
EMA_SLOW = 50
EMA_TREND = 200

MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

ADX_PERIOD = 14
ADX_TREND_THRESHOLD = 22

BB_PERIOD = 20
BB_STD = 2.0

# ============== OUTPUT ==============
SHOW_NEUTRAL = False          # Show HOLD signals or not
EXPORT_JSON = False           # Default JSON export
JSON_FILENAME = "signals.json"

# Optional: Telegram / Discord webhooks (leave empty to disable)
TELEGRAM_BOT_TOKEN = ""
TELEGRAM_CHAT_ID = ""
DISCORD_WEBHOOK_URL = ""
