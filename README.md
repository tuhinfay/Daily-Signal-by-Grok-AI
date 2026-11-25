# Daily Signal by Grok AI

**Binance USDⓈ-M Futures market scanner + technical signal dashboard + simulated paper-trading journal.**

> **Important:** This project analyses public market data and simulates trades inside its own database. It does **not** place real Binance orders, manage a real exchange account, or guarantee profitable signals. Futures trading is high risk.

- Repository: https://github.com/tuhinfay/Daily-Signal-by-Grok-AI
- Working branch for these instructions: **v5-enhancement**
- Default dashboard port: **8083**
- Default database: **SQLite** (file: daily_signal.db)
- Python: **3.10+ recommended; Python 3.12 is used in CI**

---

## Contents

1. [What the application does](#1-what-the-application-does)
2. [How the system works](#2-how-the-system-works)
3. [Dashboard guide](#3-dashboard-guide)
4. [Signal plans and abbreviations](#4-signal-plans-and-abbreviations)
5. [Market Structure explained](#5-market-structure-explained)
6. [Data sources, refresh and limitations](#6-data-sources-refresh-and-limitations)
7. [Fresh Windows laptop / Windows VPS setup](#7-fresh-windows-laptop--windows-vps-setup)
8. [Fresh Ubuntu / Debian VPS setup](#8-fresh-ubuntu--debian-vps-setup)
9. [macOS local setup](#9-macos-local-setup)
10. [Start, stop, update and logs](#10-start-stop-update-and-logs)
11. [Database, backups and migration](#11-database-backups-and-migration)
12. [Configuration and V5 mode](#12-configuration-and-v5-mode)
13. [Tests and developer commands](#13-tests-and-developer-commands)
14. [Troubleshooting](#14-troubleshooting)
15. [API endpoints](#15-api-endpoints)
16. [Disclaimer](#16-disclaimer)

---

## 1. What the application does

Daily Signal scans Binance USDT-margined perpetual futures markets and displays possible LONG and SHORT setups. It combines price candles, technical indicators, trend alignment, volume, futures context and risk levels.

### Main features

| Feature | What it does |
|---|---|
| Signals | Shows possible LONG/SHORT plans with Entry, SL, TP1, TP2, score and reasons |
| Scalp / Day / Swing | Runs separate timeframe combinations for short, intraday and longer trades |
| Movers | Shows Binance Futures gainers and losers |
| Market Structure | Shows account positioning, taker buy/sell volume, order-book context, OI, funding and liquidation context when available |
| Daily price context | Shows current UTC daily candle Open/High/Low and 7D/30D/1Y price changes |
| Support / Resistance | Finds nearby confirmed local pivot levels on the signal's strategy timeframe |
| My Trades | Simulates positions, tracks PnL, SL/TP, trade times and closed-trade history |
| Auto Trade | Optional bot that creates simulated paper positions according to its settings |
| Backtesting | Runs historical strategy research on supplied OHLCV CSV files |
| V5 research tools | Includes quality scoring, hard filters, backtest, walk-forward and forward-paper validation components |

### What it does NOT do

- It does not send BUY, SELL, leverage or close orders to your Binance account.
- It does not need a Binance API key for the public market-data functions.
- A displayed signal is not a promise or probability of profit.
- A missing value is not zero. Some public endpoints may be delayed, rate-limited or unavailable.
- Paper-trading PnL is not the same as an exchange fill. Real fees, funding, slippage, spread, latency and liquidation can differ.

---

## 2. How the system works

### High-level flow

~~~mermaid
flowchart TD
    A[Binance public Futures data] --> B[Python scanner]
    C[Public exchange context / Marketlight] --> B
    B --> D[Technical indicators and timeframe analysis]
    D --> E[LONG / SHORT candidates]
    E --> F[Score, filters and risk levels]
    F --> G[Signals dashboard]
    G --> H[User adds a paper trade]
    H --> I[SQLite or PostgreSQL database]
    I --> J[My Trades and history]
    K[Binance public price WebSocket / REST] --> J
    L[Binance force-order WebSocket] --> M[Liquidation rolling buffer]
    M --> G
~~~

### Main folders and files

| Path | Purpose |
|---|---|
| api.py | FastAPI web server, API routes, dashboard page, scan loop, paper-trade logic |
| main.py | Optional command-line scanner |
| config.py | Scanner thresholds, indicator settings, defaults and V5 settings |
| requirements.txt | Python package list |
| start.bat | Windows background startup with health check |
| stop.bat | Windows service stop helper |
| scanner/binance_client.py | Binance Futures public market-data access |
| scanner/signal_engine.py | Scalp/Day/Swing signal calculations |
| scanner/indicators.py | EMA, RSI, MACD, ADX, ATR, Bollinger Bands, VWAP and volume profile |
| scanner/risk.py | Entry, Stop Loss and Take Profit calculations |
| scanner/risk_sizing.py | Quantity sizing against risk and exchange quantity constraints |
| scanner/market_structure.py | Causal market-structure evidence such as BOS, sweeps and swing sequences |
| scanner/liquidation_feed.py | Binance all-market liquidation force-order WebSocket collector |
| scanner/multi_exchange_data.py | Public context from several exchanges and Marketlight |
| scanner/db.py | SQLite / PostgreSQL persistence |
| scanner/backtest.py, backtesting.py, v5_backtest.py | Historical simulation and evaluation modules |
| scanner/forward_validation.py | V5 forward-paper validation rules |
| scripts/run_v5_backtest.py | V5 Hybrid backtest command-line runner |
| tests/ | Unit tests |

---

## 3. Dashboard guide

### Signals tab

Each signal card is a **trade plan idea**, not an instruction to place a live order.

Example layout (illustrative values only):

~~~text
SHORT  BTCUSDT   A   10x max   -2.4% 24H

TREND FOLLOW · swing · 1h entry · 4h · 1d
HTF bear/bear · ADX 29.8 · RSI 24.8

24H O: 67,000 · H: 68,200 · L: 65,900
7D -3.2% · 30D +4.8% · 1Y +22.0% · S: 65,500 · R: 68,500

ENTRY  67,100       MARKET 66,950
SL     68,000       TP1    65,300
TP2    63,900       TP1 RR 1:2.0
TP2 RR 1:3.5        SL %   1.34%
TP1 %  2.68%        TP2 %  4.77%
~~~

- **LONG** means the setup expects price to rise.
- **SHORT** means the setup expects price to fall.
- **Score / grade** ranks the available evidence. It is not a win probability.
- **Entry** is the planned entry price. **Market** is the latest observed price.
- **SL** is the planned Stop Loss. **TP1 / TP2** are planned profit-taking levels.
- **RR** is Risk:Reward. The exact meaning is explained in the glossary below.
- **24H O/H/L** are the active Binance UTC daily candle's Open, High and Low.
- **7D / 30D / 1Y** are close-to-close percentage changes calculated against daily candle history and current price.
- **S / R** are the nearest detected support and resistance levels. They may be unavailable if candle history is insufficient.

### My Trades tab

My Trades is an internal **paper-trading ledger**. It records simulated positions and does not place an exchange order.

A trade can be:
- **OPEN** — a simulated position is running.
- **PENDING** — a simulated limit entry is waiting for its condition.
- **CLOSED** — the simulated position has been closed.
- **CANCELED** — a pending simulated entry was canceled.

The dashboard can show Entry Price, Current/Close Price, Quantity, Trade Amount, leverage setting, margin mode, unrealized/realized PnL, SL/TP, Opened, Closed and Time.

**Time** on a closed trade means the elapsed duration between opening and closing. It is not the clock time of day.

### Auto Trade

Auto Trade is a paper-trading automation feature. It may create simulated trades from eligible signals using the configured strategy and risk settings. Turning it on does not connect to a Binance trading account and does not execute live orders.

Review the bot's settings carefully. A simulated bot result does not prove that the same strategy will work with real fills.

### Movers

Movers ranks the available Binance USDⓈ-M contracts by their 24-hour price change. A large percentage move alone is not a trade signal; check trend, liquidity, volatility and risk levels too.

---

## 4. Signal plans and abbreviations

### Trading styles

| Short name | Full name | Meaning | Timeframes used by the current V4 scanner |
|---|---|---|---|
| Scalp | Scalping | Short-duration setup; seeks smaller moves and needs close monitoring | 5m entry, 15m momentum, 1h structure, 4h regime |
| Day | Day trading | Intraday setup; normally planned to be managed within the day | 15m entry, 1h momentum, 4h trend, 1d regime |
| Swing | Swing trading | Holds a move for a longer period than a scalp/day setup | 1h entry, 4h structure/momentum, 1d regime |
| Hybrid | V5 Hybrid | Quality-first multi-timeframe model | 15m entry, 1h setup, 4h structure, 1d regime; 5m optional refinement |

The timeframe labels are candle intervals:
- **5m** = each candle represents 5 minutes.
- **15m** = 15 minutes.
- **1h** = 1 hour.
- **4h** = 4 hours.
- **1d / 1D** = 1 day.

A timeframe is not a guaranteed holding time. A 1h entry means the setup uses the 1-hour candle for entry analysis; it does not mean the trade must close after one hour.

### Direction and plan terms

| Abbreviation | Full term | Meaning |
|---|---|---|
| LONG | Long position | Directional idea expects price to rise |
| SHORT | Short position | Directional idea expects price to fall |
| ENTRY | Entry price | Planned price to open the simulated position |
| MARKET | Market price | Latest observed market price |
| SL | Stop Loss | Price level intended to limit the loss |
| TP | Take Profit | Price level intended to take profit |
| TP1 | Take Profit 1 | First target; often used for partial profit-taking |
| TP2 | Take Profit 2 | Further target for the remaining position |
| SL % | Stop distance percent | Absolute percentage distance between Entry and SL |
| TP1 % | TP1 return percent | Direction-adjusted percentage distance from Entry to TP1 |
| TP2 % | TP2 return percent | Direction-adjusted percentage distance from Entry to TP2 |
| RR | Risk:Reward | Expected reward divided by planned risk |
| TP1 RR | TP1 Risk:Reward | Reward-to-risk ratio calculated to TP1 |
| TP2 RR | TP2 Risk:Reward | Reward-to-risk ratio calculated to TP2 |
| S | Support | Nearby price area where buyers may previously have defended price |
| R | Resistance | Nearby price area where sellers may previously have defended price |
| HTF | Higher Timeframe | A larger candle interval used to check the broader trend |
| TF | Timeframe | Candle interval, such as 15m or 4h |
| 24H | 24 hours | Rolling 24-hour ticker change, unless explicitly labelled as daily candle |
| O | Open | Opening price of the active daily candle |
| H | High | Highest price reached in the active daily candle so far |
| L | Low | Lowest price reached in the active daily candle so far |
| 7D / 30D / 1Y | 7 days / 30 days / 1 year | Daily-candle close-to-close change compared with current price |

### Example: how to read Entry, SL and targets

Suppose a LONG plan has:

~~~text
Entry = 100
SL    = 98
TP1   = 104
TP2   = 107
~~~

- Planned risk distance = 100 - 98 = 2, or **2%** of Entry.
- TP1 reward distance = 104 - 100 = 4, or **4%**.
- TP1 RR = 4 / 2 = **1:2**.
- TP2 reward distance = 107 - 100 = 7, or **7%**.
- TP2 RR = 7 / 2 = **1:3.5**.

For a SHORT, the direction is reversed: SL is above Entry and profit targets are below Entry. Percentages in the UI are direction-adjusted for targets; SL % is displayed as an absolute distance.

### Signal setup names

| Setup | Meaning |
|---|---|
| TREND FOLLOW | Higher-timeframe direction and trend strength support the same side |
| REVERSAL / MEAN REVERT | Evidence suggests a possible turn or move back toward an average/value area |
| MOMENTUM BREAK | Momentum indicators show a strengthening directional move |
| CONFLUENCE | Several signals agree, but the setup does not fit one of the labels above |

These are descriptive labels for the setup logic, not guarantees that a reversal or breakout will happen.

### Score and grade

**V4 / current default scanner:** the signal score is a heuristic assembled from timeframe-specific evidence. The current default minimum score is 50. A+ / A / B labels are quality categories assigned by the scanner; they are not percentages or win probabilities.

**V5 Hybrid:** uses a separate 100-point evidence model:
| Category | Weight | What it evaluates |
|---|---:|---|
| Market Regime | 15 | Whether the broader market environment is suitable/known |
| HTF Alignment | 20 | Agreement between higher timeframes |
| Market Structure | 20 | Break of structure, sweep/reclaim and swing evidence |
| Momentum | 15 | Direction and strength of momentum |
| Volume / Futures | 15 | Volume and available futures evidence |
| Entry / Risk | 15 | Entry quality and risk conditions |

V5 grade bands in the scoring module: **A+ 85–100, A 75–84, B 65–74, C below 65**. V5 also applies hard filters; a high score cannot override invalid/stale data, an unclosed candle, excessive spread, insufficient RR or a score below its threshold.

---

## 5. Market Structure explained

The Market Structure panel combines several different kinds of market evidence. Do not interpret all rows as the same type of measurement.

### Long / Short %

This is Binance **Global Long/Short Account Ratio**:
- **Long %** = share of accounts in the sampled Binance Futures account-ratio dataset that are net long.
- **Short %** = share that are net short.
- Example: Long 62% / Short 38% means more accounts are long in that dataset.

**It is not the amount of USDT held by long and short positions.** It does not mean 62% of all position value is long. The dashboard does not convert these account percentages into fictional dollar amounts.

### Buy / Sell and Vol

These values come from Binance USDⓈ-M Futures kline quote-volume fields for the selected timeframe:
- **Buy** = taker-buy quote turnover, in USDT.
- **Sell** = total quote turnover minus taker-buy quote turnover.
- **Vol** = total quote turnover for that candle, in USDT.
- Example: Buy 30M / Sell 29.5M / Vol 59.5M means the candle has about 59.5 million USDT quote turnover, of which about 30 million is taker-buy turnover.

The current candle can still be forming, so its volume may change before the candle closes. These are not guaranteed full completed-period totals while the candle is live.

### OI

**OI = Open Interest.** It estimates the outstanding quantity/value of open futures contracts that have not been closed or settled.

- **OI USDT** = available open-interest notional value in USDT, when the exchange/source provides it or it can be estimated from contract amount and mark price.
- **OI 24H** = percentage change over the available hourly history window, where supported.
- **OI 1H** = short-term percentage change when the feed provides it.
- Rising OI means more contracts are open; it does **not** by itself mean price will rise.
- Falling OI means positions/contracts are being reduced; it does **not** by itself prove a reversal.
- OI can be missing if the exchange endpoint does not return enough history or is rate-limited.

The code can blend public Binance, Bybit and OKX derivatives context when available. Cross-exchange coverage is not guaranteed to be complete at every refresh.

### Funding

**Funding Rate** is the periodic payment rate used to keep perpetual futures prices aligned with the underlying market. A positive rate commonly means longs pay shorts; a negative rate commonly means shorts pay longs. The exact next payment and amount depend on the contract and exchange rules.

High positive funding can indicate crowded longs; high negative funding can indicate crowded shorts. Funding alone is not a directional signal.

### Order Book / Book Imbalance

The order book lists resting bids and asks. The current context samples up to 20 visible levels:
- More bid notional than ask notional gives positive imbalance.
- More ask notional than bid notional gives negative imbalance.
- **Book +10%** is a snapshot imbalance, not proof that price will rise.
- Orders can be canceled or changed quickly; order-book depth is not guaranteed liquidity.

### LIQ — Liquidations

Liquidation data represents forced position closures:
- **L** = estimated notional of liquidated LONG positions.
- **S** = estimated notional of liquidated SHORT positions.
- **1H / 4H / 1D** = rolling one-hour, four-hour and one-day windows.
- Example: 1H L 500K / S 120K means the feed observed about 500,000 USDT of long liquidations and 120,000 USDT of short liquidations in that window.

The local Binance force-order WebSocket is **forward-only**: collection begins when the app connects. It is not historical exchange-wide liquidation data. If the app has just started, older portions of the 4H/1D windows are not available from this local buffer. Marketlight hourly liquidation data may be used as a historical fallback when available. If neither source provides a value, the UI should show unavailable rather than treating missing data as a real zero.

### Support and Resistance

- **S (Support)**: nearest confirmed local pivot low below the current/analysis price.
- **R (Resistance)**: nearest confirmed local pivot high above the current/analysis price.
- The pivot detector checks neighbouring candles on both sides, so the pivot is only confirmed after later candles exist.
- Levels are taken from the relevant strategy timeframe: Scalp 15m, Day 1h, Swing 4h (Hybrid uses 1h for the displayed setup context).
- These are technical reference levels, not guaranteed bounce or rejection prices.

---

## 6. Data sources, refresh and limitations

### Data sources

| Data | Source / method | Important limitation |
|---|---|---|
| Candles, prices, 24H ticker and movers | Binance USDⓈ-M public Futures REST via CCXT / Binance endpoints | Public API rate limits apply |
| Long/Short account ratio | Binance Futures data endpoint | Account percentage, not USDT position size |
| Buy/Sell/Vol | Binance Futures kline quote turnover and taker-buy quote turnover | Current candle can be incomplete |
| OI and funding | Public Binance derivatives endpoints; selected context may include Bybit/OKX | Some fields may be missing or delayed |
| Order book | Public order-book snapshot | Short-lived snapshot; orders can change |
| Multi-exchange context | CCXT public APIs for Binance USD-M, Bybit, OKX, Bitget, Gate.io, MEXC, KuCoin Futures and Hyperliquid | Some venues can timeout or lack the matching contract |
| Marketlight derivatives/liquidation | Marketlight public API; attribution is CC BY 4.0 | Depends on provider availability and coverage |
| Local liquidations | Binance all-market force-order WebSocket | Only collected while this app is connected; not historical |
| Paper-trade storage | SQLite by default; PostgreSQL if configured | Back up the database you actually use |

### Signal scan frequency

- Automatic scanning is enabled by default and the saved dashboard preference is restored at startup.
- The scanner waits about **90 seconds after a completed scan** before starting the next automatic scan.
- A scan can take longer depending on Binance response times, number of pairs, rate limits and optional context requests. Therefore, it is not a strict “every 90 seconds from the clock” schedule.
- The scanner analyses up to 26 top-volume USDT pairs per scan cycle.
- If Binance rate-limits the IP, the scanner pauses and retries after its cooldown. During that time the dashboard may show older signals or a scan warning.
- Market Structure refresh for a symbol is bounded by a 30-second cache. Some values are only refreshed when the symbol is requested or included in the scan.
- Price monitoring uses public WebSocket/REST paths separately from the signal scan.

### Why a card can say Loading

The dashboard fetches signals and market context separately. “Loading” can remain while a request is pending, the scan is still running, the server is unreachable, or the browser has stale state. Check the health endpoint and server logs before assuming that no signal exists.

### Rate limits and missing data

Do not repeatedly refresh or start several copies of the app to force faster updates. That can increase API requests and cause Binance IP cooldowns. Missing fields should be read as **not available**, not as zero or neutral evidence.

---

## 7. Fresh Windows laptop / Windows VPS setup

These steps are for Windows 10/11 or Windows Server with a desktop session.

### Step 1 — Install Git

1. Download Git for Windows: https://git-scm.com/download/win
2. Run the installer using the default options.
3. Open **Command Prompt** and check:

~~~bat
git --version
~~~

### Step 2 — Install Python

1. Download Python 3.12 (64-bit): https://www.python.org/downloads/windows/
2. During installation, tick **Add python.exe to PATH**.
3. Finish installation and open a new Command Prompt.
4. Check:

~~~bat
py --version
python --version
~~~

If the command is not found, reopen Command Prompt or repair the Python PATH installation.

### Step 3 — Clone this branch

Choose a folder where the app and its database can remain permanently, for example D:\Apps.

~~~bat
cd /d D:\Apps
git clone --branch v5-enhancement --single-branch https://github.com/tuhinfay/Daily-Signal-by-Grok-AI.git
cd Daily-Signal-by-Grok-AI
~~~

If the folder already exists, update it instead of cloning again:

~~~bat
cd /d D:\Apps\Daily-Signal-by-Grok-AI
git fetch origin
git switch v5-enhancement
git pull --ff-only origin v5-enhancement
~~~

### Step 4 — Create the virtual environment and install packages

~~~bat
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip setuptools wheel
.venv\Scripts\python.exe -m pip install -r requirements.txt
~~~

The virtual environment keeps this project's Python packages separate from other applications on the laptop/VPS.

### Step 5 — Start the application

~~~bat
start.bat
~~~

The script checks dependencies, Python syntax and port 8083. It starts the server hidden in the background and checks /health. It does **not** open a browser automatically.

Open the dashboard yourself:
- Same laptop: http://127.0.0.1:8083
- From another device on the same private network: http://WINDOWS-PC-LAN-IP:8083

Health check: http://127.0.0.1:8083/health

### Step 6 — Stop the application

Double-click stop.bat, or run:

~~~bat
stop.bat
~~~

Wait for the message that the service stopped and port 8083 is free before starting it again.

### Windows quick checklist

- [ ] Git installed
- [ ] Python 3.12 installed and on PATH
- [ ] Correct folder and v5-enhancement branch
- [ ] .venv created
- [ ] requirements.txt installed successfully
- [ ] start.bat reports healthy
- [ ] Dashboard opens at 127.0.0.1:8083

---

## 8. Fresh Ubuntu / Debian VPS setup

These instructions use an SSH terminal on a fresh Ubuntu/Debian server.

### Step 1 — Install system packages

~~~bash
sudo apt update
sudo apt upgrade -y
sudo apt install -y git curl ca-certificates build-essential python3 python3-venv python3-pip python3-dev
~~~

Check versions:

~~~bash
python3 --version
git --version
~~~

Python 3.10+ is recommended; Python 3.12 is used in CI.

### Step 2 — Create the project directory and clone

~~~bash
sudo mkdir -p /opt/daily-signal
sudo chown -R "$USER":"$USER" /opt/daily-signal
git clone --branch v5-enhancement --single-branch https://github.com/tuhinfay/Daily-Signal-by-Grok-AI.git /opt/daily-signal
cd /opt/daily-signal
~~~

For an existing installation:

~~~bash
cd /opt/daily-signal
git fetch origin
git switch v5-enhancement
git pull --ff-only origin v5-enhancement
~~~

### Step 3 — Install Python dependencies

~~~bash
cd /opt/daily-signal
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip setuptools wheel
.venv/bin/python -m pip install -r requirements.txt
~~~

### Step 4 — First run in the foreground

Run this first so you can see startup errors:

~~~bash
cd /opt/daily-signal
.venv/bin/python -m uvicorn api:app --host 0.0.0.0 --port 8083
~~~

Open:
- Dashboard: http://YOUR_VPS_IP:8083
- Health: http://YOUR_VPS_IP:8083/health

Stop foreground mode with **Ctrl+C**.

### Step 5 — Run persistently with systemd (recommended)

Create a service file:

~~~bash
sudo nano /etc/systemd/system/daily-signal.service
~~~

Paste this configuration. Replace YOUR_LINUX_USERNAME with the Linux account that owns /opt/daily-signal:

~~~ini
[Unit]
Description=Daily Signal by Grok AI
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=YOUR_LINUX_USERNAME
WorkingDirectory=/opt/daily-signal
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/daily-signal/.venv/bin/python -m uvicorn api:app --host 0.0.0.0 --port 8083
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
~~~

Save and enable:

~~~bash
sudo systemctl daemon-reload
sudo systemctl enable --now daily-signal
sudo systemctl status daily-signal
~~~

View logs:

~~~bash
sudo journalctl -u daily-signal -f
~~~

Stop / start / restart:

~~~bash
sudo systemctl stop daily-signal
sudo systemctl start daily-signal
sudo systemctl restart daily-signal
~~~

### Step 6 — Firewall and safe access

If using UFW and you intentionally need direct access to port 8083:

~~~bash
sudo ufw allow OpenSSH
sudo ufw allow 8083/tcp
sudo ufw status
~~~

Do not enable UFW until you have allowed SSH, or you may lock yourself out.

**Security warning:** the dashboard/API is not documented as having user login protection. Do not expose port 8083 openly to the public internet without adding suitable authentication and HTTPS through a properly configured reverse proxy/firewall. Prefer VPN or private network access.

---

## 9. macOS local setup

Install Git and Python 3.12 (for example, from the official Python installer or Homebrew), then open Terminal:

~~~bash
git clone --branch v5-enhancement --single-branch https://github.com/tuhinfay/Daily-Signal-by-Grok-AI.git
cd Daily-Signal-by-Grok-AI
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m uvicorn api:app --host 127.0.0.1 --port 8083
~~~

Open http://127.0.0.1:8083. Press Ctrl+C in Terminal to stop.

---

## 10. Start, stop, update and logs

### Windows

| Task | Command |
|---|---|
| Start | start.bat |
| Stop | stop.bat |
| Update code | git pull --ff-only origin v5-enhancement |
| Update dependencies | .venv\Scripts\python.exe -m pip install -r requirements.txt |
| Check health | Open http://127.0.0.1:8083/health |

Windows log files:
- logs\server.log
- logs\server-error.log
- PID file: logs\server.pid

### Ubuntu / Debian with systemd

| Task | Command |
|---|---|
| Start | sudo systemctl start daily-signal |
| Stop | sudo systemctl stop daily-signal |
| Restart | sudo systemctl restart daily-signal |
| Status | sudo systemctl status daily-signal |
| Live logs | sudo journalctl -u daily-signal -f |

Update:

~~~bash
cd /opt/daily-signal
sudo systemctl stop daily-signal
git fetch origin
git pull --ff-only origin v5-enhancement
.venv/bin/python -m pip install -r requirements.txt
sudo systemctl start daily-signal
sudo systemctl status daily-signal
~~~

### Health check

The health endpoint returns basic server status, whether a scan is running, whether auto scan is enabled, and the selected database backend:

~~~bash
curl http://127.0.0.1:8083/health
~~~

Expected response is JSON with status set to ok. A healthy HTTP server does not mean every market-data provider is currently returning data.

---

## 11. Database, backups and migration

### SQLite (default)

- No separate database server is required.
- Default database file: **daily_signal.db** in the application's working directory.
- Set DATA_DIR to use another persistent folder.
- Keep the project/database directory on persistent disk. Do not put the database inside a temporary directory.

Example backup while the application is stopped:

~~~bash
cp daily_signal.db daily_signal-backup-$(date +%F).db
~~~

On Windows, copy daily_signal.db to a safe backup folder after stopping the app.

### PostgreSQL / Neon (optional)

Set either DATABASE_URL or NEON_DATABASE_URL to a PostgreSQL connection string. Example format (replace all placeholders with your own credentials):

~~~text
postgresql://USER:PASSWORD@HOST:5432/DATABASE?sslmode=require
~~~

The application attempts PostgreSQL when a valid URL is configured. If PostgreSQL cannot be initialized, the current database layer logs the failure and falls back to SQLite. Check startup logs to confirm which backend is active.

Never commit a real database URL, password or API secret to Git. Use environment variables or your hosting provider's secret settings.

### Important backup rule

Before replacing a VPS, deleting a folder, switching database backend or reinstalling:
1. Stop the app.
2. Identify the active backend from /health or startup logs.
3. Back up the SQLite database file or take a PostgreSQL backup.
4. Keep a copy outside the project directory.
5. Restore and verify the data before deleting the old server.

Changing from SQLite to PostgreSQL does not automatically copy existing trades. A separate migration/export-import process is required.

---

## 12. Configuration and V5 mode

### Current default V4 configuration

The following values are defined in config.py:

| Setting | Default | Meaning |
|---|---:|---|
| TOP_N_PAIRS | 30 | CLI scanner's default number of pairs |
| MIN_VOLUME_USDT | 12,000,000 | Minimum 24h quote-volume filter |
| MIN_SCORE | 50 | Minimum V4 signal score |
| DEFAULT_LEVERAGE | 10 | Default paper-trade leverage setting |
| MAX_LEVERAGE | 150 | Application upper cap; actual symbol limit can be lower |
| RISK_PER_TRADE_PCT | 1.0 | Default risk percentage constant |
| ATR_SL_MULTIPLIER | 1.5 | Stop distance fallback multiplier |
| ATR_TP1_MULTIPLIER | 2.0 | TP1 fallback multiplier |
| ATR_TP2_MULTIPLIER | 3.5 | TP2 fallback multiplier |
| MIN_RR | 1.25 | Minimum TP1 reward-to-risk in the V4 risk calculator |

The web scan loop currently requests up to 26 pairs per scan. Config constants can have different purposes in the CLI scanner and web app.

### Technical indicator settings

| Name | Default | Meaning |
|---|---:|---|
| RSI_PERIOD | 14 | RSI lookback length |
| RSI_OVERSOLD | 38 | Oversold threshold used by signal logic |
| RSI_OVERBOUGHT | 62 | Overbought threshold used by signal logic |
| EMA_FAST / MID / SLOW / TREND | 9 / 21 / 50 / 200 | Exponential moving-average periods |
| MACD_FAST / SLOW / SIGNAL | 12 / 26 / 9 | MACD settings |
| ADX_PERIOD | 14 | ADX lookback |
| ADX_TREND_THRESHOLD | 18 | Trend-strength threshold |
| ADX_STRONG | 25 | Strong-trend threshold |
| BB_PERIOD / BB_STD | 20 / 2.0 | Bollinger Bands period and standard deviation |
| VWAP_WINDOW | 48 | Rolling VWAP candle window |
| VOLUME_PROFILE_WINDOW | 48 | Rolling volume-profile candle window |
| VOLUME_PROFILE_BINS | 24 | Price bins used by the POC proxy |
| STRUCTURE_WINDOW | 20 | Prior-candle structure reference window |

These are algorithm settings, not universal trading rules. Changing them changes signal behaviour and should be followed by tests and historical/forward evaluation.

### V5 is opt-in

V5 is disabled by default. The current default application path remains the V4 Scalp/Day/Swing scanner unless V5 is explicitly enabled.

V5 settings currently include:
- Hybrid timeframe plan: 1D regime, 4H structure, 1H setup, 15M entry and optional 5M refinement.
- Minimum score 75/100.
- Minimum RR 1:2.
- Maximum spread 0.10%.
- Maximum two open V5 paper positions.
- V5 risk sizing target 0.5% per trade, rounded down to exchange quantity step and checked against exchange minimum quantity/notional.
- Simulated paper execution only.

To enable V5 for an isolated test session in Linux:

~~~bash
cd /opt/daily-signal
sudo systemctl stop daily-signal
V5_ENABLED=true .venv/bin/python -m uvicorn api:app --host 0.0.0.0 --port 8083
~~~

This foreground command is for a temporary test; it does not change the systemd service configuration. To persist V5 mode, add the environment variable to the service's [Service] section, then run daemon-reload and restart.

For Windows, set the variable in the same Command Prompt before running start.bat:

~~~bat
set V5_ENABLED=true
start.bat
~~~

To return to default V4 mode in that Command Prompt:

~~~bat
set V5_ENABLED=false
start.bat
~~~

V5 should be evaluated in a separate staging/paper environment. Do not treat a successful unit test or historical backtest as proof of profitability. The V5 forward-validation endpoint requires at least 30 elapsed days and 30 closed paper trades before it can report READY_FOR_REVIEW.

---

## 13. Tests and developer commands

Activate the virtual environment, then run from the repository root.

### Compile Python files

~~~bash
python -m compileall -q api.py scanner tests scripts
~~~

### Run all unit tests

~~~bash
python -m unittest discover -s tests -v
~~~

### Embedded dashboard JavaScript syntax check (optional)

This check needs Node.js installed. It is also included in the GitHub Actions workflow.

~~~bash
python -c "import re; from api import PAGE; open('/tmp/daily-signal-dashboard.js', 'w').write(re.findall(r'<script>(.*?)</script>', PAGE, re.S)[-1])"
node --check /tmp/daily-signal-dashboard.js
~~~

On Windows, use a suitable temporary file path such as %TEMP%\daily-signal-dashboard.js in the Python command.

### Historical V5 Hybrid backtest

Prepare four CSV files with these columns:

~~~text
timestamp,open,high,low,close,volume
~~~

Use UTC timestamps, candle open times, oldest-to-newest ordering and the same symbol across files. The 1D file needs at least 205 completed daily candles for the daily EMA200 regime logic. Optional funding CSV columns: timestamp,funding_rate (decimal funding rate at settlement timestamps).

Example:

~~~bash
python scripts/run_v5_backtest.py --symbol BTCUSDT --15m btc_15m.csv --1h btc_1h.csv --4h btc_4h.csv --1d btc_1d.csv --funding btc_funding.csv --spread-pct 0.05
~~~

The backtest uses next-15m-open fills, estimated mark-price gap checks, explicit fee/slippage and supplied/assumed spread. It does not reconstruct historical order-book spread and mark price unless equivalent source data is supplied. Results are research estimates, not proof of future performance.

See [BACKTEST_GUIDE.md](BACKTEST_GUIDE.md) for the general backtest command and assumptions.

### V5 forward validation

After running V5 paper trading, query:

~~~bash
curl http://127.0.0.1:8083/api/v5/forward-validation
~~~

The endpoint reports COLLECTING_FORWARD_DATA until both maturity conditions are met: at least 30 elapsed days and at least 30 closed paper trades. See [V5_IMPLEMENTATION.md](V5_IMPLEMENTATION.md) for implementation and remaining validation work.

### CI workflow

GitHub Actions workflow: .github/workflows/v5-tests.yml. It installs requirements, compiles Python modules, checks embedded dashboard JavaScript with Node.js and runs the unit test suite for configured branch/PR events.

---

## 14. Troubleshooting

### Port 8083 is already in use

- Windows: run stop.bat from the same project folder. If it still reports another process, inspect the PID shown by PowerShell / Task Manager. Do not kill an unrelated application.
- Linux: check with sudo ss -ltnp | grep :8083 and stop the correct service/process.

### Windows start.bat closes but dashboard does not load

1. Open http://127.0.0.1:8083/health.
2. Check logs\server-error.log and logs\server.log.
3. Confirm that .venv\Scripts\python.exe exists.
4. Reinstall dependencies with .venv\Scripts\python.exe -m pip install -r requirements.txt.
5. Confirm that port 8083 is not occupied by another program.

### Python package installation fails

- Check internet connectivity.
- Update pip using the virtual environment's Python.
- Use a supported 64-bit Python version.
- On Linux, install python3-dev, build-essential and python3-venv.
- Read the first package error; do not repeatedly reinstall without addressing it.

### Dashboard shows old signals / scan warning

- Check /health and the dashboard's last-updated time.
- Check server logs for Binance rate-limit messages.
- Wait for the retry cooldown; do not launch multiple app copies.
- Confirm auto scan is enabled in the dashboard.

### Market Structure values show —

Possible causes:
- Binance endpoint did not return that metric.
- The symbol is not supported by the relevant public endpoint.
- Binance or another provider rate-limited/timed out.
- OI history does not contain enough points to calculate a change.
- Marketlight or a cross-exchange venue is temporarily unavailable.

### Liquidation shows — or zero

The local stream begins collecting after the app successfully connects. It cannot reconstruct liquidation events from before startup. A zero means no matching event was counted in the available window; an unavailable marker means the source did not provide usable data. The Marketlight fallback also depends on its public API coverage.

### Database unexpectedly appears empty

- Check /health and startup logs for SQLite vs PostgreSQL.
- Confirm the process is using the expected working directory and DATA_DIR.
- If DATABASE_URL is invalid/unreachable, the application may fall back to a new local SQLite file.
- Restore from a backup only after stopping the app.

### Browser does not show latest UI

Hard-refresh the browser (Ctrl+F5 on Windows) and check that the VPS pulled the intended branch and commit.

---

## 15. API endpoints

These endpoints are served by the FastAPI application on port 8083.

| Endpoint | Method | Purpose |
|---|---|---|
| / | GET | Dashboard HTML |
| /health | GET | Server, scan and database status |
| /api/signals | GET | Current signals; optional style filter |
| /api/market-structure/{symbol} | GET | Refresh/read market context for one symbol |
| /api/price-monitor | GET | Price monitor status |
| /api/trades | GET | Paper-trade list and account/equity context |
| /api/trades | POST | Create a simulated paper trade |
| /api/trades/{id}/close | POST | Close a simulated trade |
| /api/trades/{id} | PATCH | Update simulated trade protection/settings |
| /api/auto-trade | GET | Read auto-trade configuration |
| /api/v5/forward-validation | GET | V5 forward-paper validation status |
| /scan | GET | Request a scan and redirect to dashboard |

Example health request:

~~~bash
curl http://127.0.0.1:8083/health
~~~

Example signals request:

~~~bash
curl "http://127.0.0.1:8083/api/signals?style=all"
~~~

Supported style filters depend on the selected scanner mode; common V4 filters are scalp, day and swing.

---

## 16. Disclaimer

Daily Signal by Grok AI is an educational market-analysis and simulated paper-trading project. It is not financial advice, investment advice or a recommendation to buy or sell any asset. Cryptocurrency futures are highly leveraged and can result in rapid loss of capital. Verify data, understand liquidation risk and independently evaluate every trade. Never risk money you cannot afford to lose.
