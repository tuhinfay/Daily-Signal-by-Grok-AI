# Daily Signal by Grok AI

Binance USDT-M futures multi-timeframe signal scanner + paper trading dashboard.

- Signals: Long / Short with Entry, SL, TP1, TP2, score, liquidity
- Paper trades: leverage, amount / margin / risk% / qty, Cross or Isolated
- Tools: DCA, trailing stop, edit SL/TP, movers, voice alerts
- Storage: **SQLite by default** · **PostgreSQL/Neon if `DATABASE_URL` is set**

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
| `stop.bat` | Stops only the recorded PID after verifying it is this project's Uvicorn process |

```bat
start.bat
REM open http://127.0.0.1:8083
REM health: http://127.0.0.1:8083/health

stop.bat
```

### Update VPS code

```bat
stop.bat
git pull
.venv\Scripts\pip install -r requirements.txt
start.bat
```

---

## Render + Neon (persistent trades)

| `DATABASE_URL` | Storage |
|----------------|----------|
| Not set | SQLite (may reset on free Render redeploy) |
| Valid Neon/Postgres URL | PostgreSQL — persists account + trades |

When a PostgreSQL URL is configured but the database cannot be reached, startup fails with a clear error instead of silently switching to an empty local SQLite database. This prevents data from appearing to disappear or being split across two databases.

### Steps

1. Neon project → copy **connection string**
2. Render Web Service → **Environment** → add:
   ```text
   DATABASE_URL=postgresql://USER:PASSWORD@HOST/DB?sslmode=require
   ```
3. Build: `pip install -r requirements.txt`
4. Start: `uvicorn api:app --host 0.0.0.0 --port $PORT`
5. Redeploy

Logs should show: `[db] Using PostgreSQL with warm connection pool (DATABASE_URL)`

If the URL is wrong or Neon is temporarily unavailable, check the Render logs and restore the database connection; the app intentionally does not fall back to SQLite.

---

## Local

```bash
pip install -r requirements.txt
uvicorn api:app --host 0.0.0.0 --port 8000
```

---

## Disclaimer

Educational / paper-trading tool only. **Not financial advice.**

**Daily Signal by Grok AI**
