"""Daily Signal by Grok AI"""

TOP_N_PAIRS = 30
MIN_VOLUME_USDT = 12_000_000
MIN_SCORE = 50

TIMEFRAMES = {"entry": "15m", "momentum": "1h", "trend": "4h", "regime": "1d"}
CANDLE_LIMIT = {"5m": 120, "15m": 120, "1h": 120, "4h": 220, "1d": 220}

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

# Market context indicators (rolling windows are measured in candles).
VWAP_WINDOW = 48
VOLUME_PROFILE_WINDOW = 48
VOLUME_PROFILE_BINS = 24
STRUCTURE_WINDOW = 20


# Daily Signal V5 opt-in defaults; V4 remains the production path until validated.
V5_ENABLED = False
V5_TRADING_STYLE = "hybrid"
V5_SIGNAL_FREQUENCY = "quality"
V5_MODE = "both"  # signal + simulated paper trading; no live orders
V5_MIN_SCORE = 75
V5_MIN_RR = 2.0
V5_MAX_SPREAD_PCT = 0.10
V5_SCORE_WEIGHTS = {
    "market_regime": 15,
    "htf_alignment": 20,
    "market_structure": 20,
    "momentum": 15,
    "volume_futures": 15,
    "entry_risk": 15,
}
V5_TIMEFRAMES = {
    "regime": "1d",
    "structure": "4h",
    "setup": "1h",
    "entry": "15m",
    "refinement": "5m",
}
V5_MAX_OPEN_PAPER_TRADES = 2
V5_RISK_PER_TRADE_PCT = 0.5
