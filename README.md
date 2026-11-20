# Daily Signal by Grok AI

Binance USDT-M futures multi-timeframe signal scanner + paper trading dashboard.

- Signals: Long / Short with Entry, SL, TP1, TP2, score, liquidity
- Paper trades: leverage, amount / margin / risk% / qty, Cross or Isolated
- Tools: DCA, trailing stop, edit SL/TP, movers, voice alerts
- Storage: **SQLite by default** (`daily_signal.db`)

Repo: https://github.com/tuhinfay/Daily-Signal-by-Grok-AI

---

## VPS (Windows) — one-click

**Neon is NOT required on VPS.** SQLite file on disk is enough.

### First-time setup

```bat
git clone https://github.com/tuhinfay/Daily-Signal-by-Grok-AI.git
cd Daily-Signal-by-Grok-AI
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

### Start / Stop

| File | Action |
|------|--------|
| `start.bat` | Starts API on **port 8083** (background), writes `runtime.pid`, logs in `logs\` |
| `stop.bat` | Stops by PID **and** frees port 8083 if PID was stale |

```bat
start.bat
REM open http://127.0.0.1:8083
REM health: http://127.0.0.1:8083/health

stop.bat
```

### Keep VPS code up to date

```bat
stop.bat
git pull
.venv\Scripts\pip install -r requirements.txt
start.bat
```

> If the VPS clone is several commits behind GitHub, always `git pull` after `stop.bat`.

---

## Render

### Deploy

1. Render → **New** → **Web Service** (or Blueprint with `render.yaml`)
2. Connect this GitHub repo
3. Settings:
   - **Build:** `pip install -r requirements.txt`
   - **Start:** `uvicorn api:app --host 0.0.0.0 --port $PORT`
   - **Python:** 3.12
4. Deploy

### Data persistence on Render

| Setup | Result |
|-------|--------|
| Free web service + SQLite only | App works, but **DB can reset** on redeploy/restart (ephemeral disk) |
| Neon Postgres + code migration | Trade history can persist |

**Important:** A Neon project alone does **not** persist data until the app is switched from SQLite to Postgres (`DATABASE_URL`).

Current code path: `scanner/db.py` → SQLite file `daily_signal.db`.

If you already created Neon project **Daily-Signal-by-Grok-AI**:
1. Copy the Neon `DATABASE_URL`
2. Ask a developer to add optional Postgres support (use Postgres when `DATABASE_URL` is set, else SQLite)
3. On Render, add env var `DATABASE_URL`

Until that migration is done, Render = SQLite (may wipe on redeploy).

---

## Local (any OS)

```bash
pip install -r requirements.txt
uvicorn api:app --host 0.0.0.0 --port 8000
# http://localhost:8000
```

---

## Main API

| Endpoint | Description |
|----------|-------------|
| `GET /` | Dashboard |
| `GET /api/signals` | Signals + account + stats |
| `GET /api/trades` | Paper trades |
| `POST /api/trades` | Open paper trade |
| `POST /api/auto-scan?enabled=true` | Auto-scan on/off |
| `GET /scan?top=28` | Trigger scan |
| `GET /health` | Health check |

---

## Disclaimer

Educational / paper-trading tool only. **Not financial advice.** Crypto trading is high risk.

**Daily Signal by Grok AI**
