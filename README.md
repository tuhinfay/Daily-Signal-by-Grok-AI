# Daily Signal by Grok AI

Binance USDT-M futures multi-timeframe signal scanner + paper-trading dashboard.

- Signals: Long / Short with Entry, SL, TP1, TP2, score and market context
- Paper trading: simulated positions only; no live order execution
- Storage: SQLite by default; PostgreSQL/Neon when `DATABASE_URL` is configured
- V5: opt-in development mode; V4 remains the default until V5 validation is complete

Repository: https://github.com/tuhinfay/Daily-Signal-by-Grok-AI

---

## VPS deployment (Ubuntu / Debian Linux)

These instructions are for a fresh Ubuntu/Debian VPS. Commands are run in the VPS SSH terminal.

### 1. Install system packages

```bash
sudo apt update
sudo apt upgrade -y
sudo apt install -y git curl ca-certificates build-essential python3 python3-venv python3-pip python3-dev
```

Check Python (Python 3.10+ recommended):

```bash
python3 --version
git --version
```

### 2. Download the V5 development branch

```bash
sudo mkdir -p /opt/daily-signal
sudo chown -R "$USER":"$USER" /opt/daily-signal
git clone --branch v5-development --single-branch https://github.com/tuhinfay/Daily-Signal-by-Grok-AI.git /opt/daily-signal
cd /opt/daily-signal
```

If you already cloned the repository, switch to the V5 branch instead:

```bash
cd /opt/daily-signal
git fetch origin
git switch v5-development
git pull origin v5-development
```

### 3. Create Python virtual environment and install dependencies

```bash
cd /opt/daily-signal
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements.txt
```

The packages in `requirements.txt` include FastAPI, Uvicorn, CCXT, Pandas, NumPy, TA, HTTPX, WebSocket client and PostgreSQL support. You do not need to install them one by one.

### 4. Run the application

For a quick foreground test:

```bash
cd /opt/daily-signal
source .venv/bin/activate
uvicorn api:app --host 0.0.0.0 --port 8083
```

Keep that SSH session open while testing. Open:

- Dashboard: `http://YOUR_VPS_IP:8083`
- Health check: `http://YOUR_VPS_IP:8083/health`

Press `Ctrl+C` to stop the foreground process.

### 5. Run in the background

For a simple background run without keeping SSH open:

```bash
cd /opt/daily-signal
mkdir -p logs
nohup .venv/bin/uvicorn api:app --host 0.0.0.0 --port 8083 > logs/app.log 2>&1 &
echo $! > runtime.pid
```

View logs:

```bash
tail -f /opt/daily-signal/logs/app.log
```

Stop the background process:

```bash
cd /opt/daily-signal
if [ -f runtime.pid ]; then
  kill "$(cat runtime.pid)" 2>/dev/null || true
  rm -f runtime.pid
fi
```

Start it again by running the background command in this section.

### 6. Optional: allow the dashboard port through UFW

Only do this if you intend to access port 8083 directly. For a public production deployment, prefer a reverse proxy with HTTPS and suitable access controls.

```bash
sudo ufw allow OpenSSH
sudo ufw allow 8083/tcp
sudo ufw status
```

If UFW is not enabled and you want to enable it, first make sure the OpenSSH rule is present, then run `sudo ufw enable`.

### 7. Update to the latest V5 branch code

```bash
cd /opt/daily-signal
if [ -f runtime.pid ]; then
  kill "$(cat runtime.pid)" 2>/dev/null || true
  rm -f runtime.pid
fi
git fetch origin
git pull --ff-only origin v5-development
.venv/bin/python -m pip install -r requirements.txt
mkdir -p logs
nohup .venv/bin/uvicorn api:app --host 0.0.0.0 --port 8083 > logs/app.log 2>&1 &
echo $! > runtime.pid
```

### VPS notes

- Keep the project directory on persistent disk. SQLite data is stored locally; back up the database file before moving or rebuilding the VPS.
- No Binance API key is needed for public market-data endpoints. Never add exchange trading permissions for this paper-trading setup.
- V5 is deliberately disabled for automatic scanning by default (`V5_ENABLED=false`). To test it only in an isolated staging deployment, explicitly set the environment variable `V5_ENABLED=true`; leave it unset/false in production until the forward-validation gates pass. The branch now includes backtesting, walk-forward and forward-paper evaluation tools, but a real 30-day paper-forward sample and staging end-to-end validation still require elapsed time. Do not enable V5 in production before those gates pass.
- This project is designed for signal analysis and simulated paper trading. It does not place live orders.
- For a production setup, configure HTTPS/reverse proxy, firewall rules, process supervision (for example systemd), log rotation and database backups.

---

## VPS (Windows) — one-click

Neon is not required on VPS. SQLite on persistent disk is sufficient.

### First-time setup

```bat
git clone --branch v5-development https://github.com/tuhinfay/Daily-Signal-by-Grok-AI.git
cd Daily-Signal-by-Grok-AI
python -m venv .venv
.venv\\Scripts\\python -m pip install --upgrade pip
.venv\\Scripts\\pip install -r requirements.txt
```

### Start / Stop

| File | Action |
|------|--------|
| `start.bat` | Starts API on port 8083 in the background; writes `runtime.pid` and logs in `logs\\` |
| `stop.bat` | Stops the app process |

```bat
start.bat
REM Dashboard: http://127.0.0.1:8083
REM Health:    http://127.0.0.1:8083/health

stop.bat
```

### Update Windows VPS

```bat
stop.bat
git pull origin v5-development
.venv\\Scripts\\pip install -r requirements.txt
start.bat
```

---

## Render deployment

The repository's `render.yaml` defines a Python web service.

- Build command: `pip install -r requirements.txt`
- Start command: `uvicorn api:app --host 0.0.0.0 --port $PORT`
- Configure `DATABASE_URL` in the Render dashboard if you want PostgreSQL persistence (for example Neon).
- Without a persistent database/disk, local SQLite data may not survive service replacement or redeploy.

---

## Local development

Linux / macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
uvicorn api:app --host 127.0.0.1 --port 8000 --reload
```

Windows PowerShell:

```powershell
py -m venv .venv
.venv\\Scripts\\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
uvicorn api:app --host 127.0.0.1 --port 8000 --reload
```

Run the unit tests:

```bash
python -m unittest discover -s tests -v
```

Check the V5 paper-forward validation window (requires 30 elapsed days and at least 30 closed paper trades):

```bash
curl http://127.0.0.1:8083/api/v5/forward-validation
```

The endpoint reports `COLLECTING_FORWARD_DATA` until both maturity conditions are met. It does not claim a backtest or paper sample has passed before the data exists. See [V5 implementation status](V5_IMPLEMENTATION.md) for backtest and walk-forward module details.

---

## Disclaimer

Educational market-analysis and paper-trading software only. Not financial advice. Trading futures involves substantial risk.
