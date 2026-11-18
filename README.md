# Daily Signal by Grok AI

**Advanced Binance Crypto Trading Signal Scanner**

Scans USDT perpetual futures pairs on Binance using multi-timeframe technical analysis and generates high-confidence **Long / Short** signals with Entry, Stop Loss, Take Profit levels.

---

## Features

- Multi-timeframe analysis (15m + 1h + 4h + 1d)
- Indicators: EMA, RSI, StochRSI, MACD, ADX, Bollinger Bands, ATR, Volume
- Automatic Stop Loss & Take Profit (ATR-based)
- Web Dashboard + JSON API
- Ready for Render / any cloud deployment

---

## Quick Start (Local)

```bash
git clone https://github.com/tuhinfay/Daily-Signal-by-Grok-AI.git
cd Daily-Signal-by-Grok-AI
pip install -r requirements.txt

# CLI mode
python main.py

# Web mode
python api.py
# then open http://localhost:8000
```

---

## Deploy on Render (Recommended)

### Method 1: Blueprint (Easiest)

1. Go to [https://dashboard.render.com](https://dashboard.render.com)
2. Click **New +** → **Blueprint**
3. Connect your GitHub account and select this repository
4. Render will detect `render.yaml` and create the service automatically
5. Click **Apply**

### Method 2: Manual Web Service

1. Go to [https://dashboard.render.com](https://dashboard.render.com)
2. Click **New +** → **Web Service**
3. Connect the repo: `tuhinfay/Daily-Signal-by-Grok-AI`
4. Settings:
   - **Name**: `daily-signal-by-grok-ai`
   - **Runtime**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `uvicorn api:app --host 0.0.0.0 --port $PORT`
   - **Instance Type**: Free
5. Click **Create Web Service**

After deploy finishes you will get a URL like:
`https://daily-signal-by-grok-ai.onrender.com`

### How to use after deploy

- Open the URL → see the dashboard
- Click **Run New Scan** → wait 30-60 seconds → refresh
- JSON API: `https://your-url.onrender.com/api/signals`

> **Note**: Free tier sleeps after 15 minutes of inactivity. First request after sleep takes ~30-50 seconds.

---

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /` | Web Dashboard |
| `GET /api/signals` | Latest signals (JSON) |
| `GET /scan?top=40` | Trigger a new scan |
| `GET /health` | Health check |

---

## CLI Options

```bash
python main.py                  # Scan top 50 pairs
python main.py --top 100        # Scan more pairs
python main.py --symbols BTCUSDT ETHUSDT
python main.py --json           # Export to signals.json
python main.py --loop           # Continuous every 15 min
```

---

## Disclaimer

This is an educational tool. **Not financial advice.**  
Crypto trading involves substantial risk of loss. Always do your own research.

---

**Daily Signal by Grok AI**  
Powered by Grok • Built for serious traders
