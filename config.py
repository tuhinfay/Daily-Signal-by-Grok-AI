"""
Daily Signal by Grok AI - Configuration
Tuned for higher accuracy on Binance Futures
"""

# ============== SCAN SETTINGS ==============
TOP_N_PAIRS = 25
MIN_VOLUME_USDT = 20_000_000   # High liquidity only
MIN_SCORE = 58                 # Slightly higher bar for quality

TIMEFRAMES = {
    "entry": "15m",
    "momentum": "1h",
    "trend": "4h",
    "regime": "1d",
}

CANDLE_LIMIT = {
    "15m": 150,
    "1h": 150,
    "4h": 120,
    "1d": 100,
}

# ============== RISK MANAGEMENT ==============
ATR_SL_MULTIPLIER = 1.6
ATR_TP1_MULTIPLIER = 2.2
ATR_TP2_MULTIPLIER = 3.8
MIN_RR = 1.4

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
ADX_TREND_THRESHOLD = 22       # Need real trend, not chop
ADX_STRONG = 28

BB_PERIOD = 20
BB_STD = 2.0

# Quality gates
REQUIRE_HTF_ALIGN = True       # 1D + 4H must agree with side
REQUIRE_VOLUME = True          # Prefer volume confirmation
MIN_ADX_FOR_SIGNAL = 18        # Skip dead markets

SHOW_NEUTRAL = False
EXPORT_JSON = False
JSON_FILENAME = "signals.json"

TELEGRAM_BOT_TOKEN = ""
TELEGRAM_CHAT_ID = ""
DISCORD_WEBHOOK_URL = ""
