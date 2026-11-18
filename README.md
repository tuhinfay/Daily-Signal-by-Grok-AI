# Daily Signal by Grok AI

**Advanced Binance Crypto Trading Signal Scanner**

Scans all USDT perpetual futures pairs on Binance, applies multi-timeframe technical analysis, and generates high-confidence **Long / Short** signals with:

- Entry Price
- Stop Loss
- Take Profit (multiple levels)
- Risk : Reward ratio
- Confidence Score
- Detailed reasoning

Built with love using Grok AI.

---

## Features

- Scans top liquid USDT pairs (configurable)
- Multi-timeframe analysis: 15m + 1h + 4h + 1d
- Indicators used:
  - EMA (9, 21, 50, 200)
  - RSI + Stochastic RSI
  - MACD + Histogram
  - ADX + DI+/DI-
  - Bollinger Bands
  - ATR (for dynamic SL/TP)
  - Volume analysis
- Confluence-based scoring system
- Automatic Stop Loss & Take Profit calculation (ATR-based)
- Clean terminal output + JSON export
- Ready for Telegram / Discord webhook integration

---

## Quick Start

### 1. Clone & Install

```bash
git clone https://github.com/tuhinfay/Daily-Signal-by-Grok-AI.git
cd Daily-Signal-by-Grok-AI
pip install -r requirements.txt
```

### 2. Run Scanner

```bash
# Scan top 50 pairs (default)
python main.py

# Scan specific pairs
python main.py --symbols BTCUSDT ETHUSDT SOLUSDT

# Scan more pairs
python main.py --top 100

# Output as JSON
python main.py --json

# Continuous mode (every 15 minutes)
python main.py --loop
```

---

## Example Output

```
============================================================
  DAILY SIGNAL BY GROK AI  |  2026-11-18 13:45 UTC
============================================================

[STRONG LONG]  SOLUSDT   Score: 82/100
  Entry     : 178.45
  Stop Loss : 172.10   (-3.56%)
  TP1       : 185.20   (+3.78%)
  TP2       : 192.80   (+8.04%)
  RR        : 1 : 2.25
  Reasons   : EMA9 > EMA21 (1h) | MACD bullish cross (15m) | RSI reclaim 45 | ADX rising

[STRONG SHORT] DOGEUSDT  Score: 78/100
  Entry     : 0.3842
  Stop Loss : 0.3975   (+3.46%)
  TP1       : 0.3680   (-4.22%)
  ...
```

---

## Project Structure

```
Daily-Signal-by-Grok-AI/
├── main.py                 # Entry point
├── scanner/
│   ├── __init__.py
│   ├── binance_client.py   # Binance data fetcher
│   ├── indicators.py       # Technical indicators
│   ├── signal_engine.py    # Core scoring & signal logic
│   └── risk.py             # SL / TP calculator
├── config.py               # Settings
├── requirements.txt
└── README.md
```

---

## Configuration

Edit `config.py`:

- `TOP_N_PAIRS` → how many pairs to scan
- `MIN_SCORE` → minimum score to show signal (default 65)
- `TIMEFRAMES` → which candles to use
- Risk parameters (ATR multipliers for SL/TP)

---

## Disclaimer

This is an educational / research tool.  
**Not financial advice.**  
Crypto trading involves substantial risk of loss.  
Always do your own research and manage risk properly.

---

**Daily Signal by Grok AI**  
Powered by Grok • Built for serious traders
