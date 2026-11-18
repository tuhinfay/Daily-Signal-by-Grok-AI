#!/usr/bin/env python3
"""
Daily Signal by Grok AI - FastAPI Web Server
Exposes trading signals via REST API
"""

from fastapi import FastAPI, Query, BackgroundTasks
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Optional
from datetime import datetime, timezone
import uvicorn
import os

from scanner.binance_client import BinanceClient
from scanner.signal_engine import SignalEngine
import config

app = FastAPI(
    title="Daily Signal by Grok AI",
    description="Binance Multi-Timeframe Trading Signal Scanner",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Simple in-memory cache
_cache = {
    "signals": [],
    "last_updated": None,
    "scanning": False,
}


def run_scan(symbols: Optional[List[str]] = None, top_n: int = 40):
    """Run the scanner and update cache."""
    global _cache
    _cache["scanning"] = True

    try:
        client = BinanceClient(futures=True)
        engine = SignalEngine()

        if symbols is None:
            symbols = client.get_top_usdt_pairs(top_n=top_n, min_volume=config.MIN_VOLUME_USDT)

        signals = []
        timeframes = ["15m", "1h", "4h", "1d"]

        for symbol in symbols:
            data = {}
            for tf in timeframes:
                limit = config.CANDLE_LIMIT.get(tf, 150)
                df = client.fetch_ohlcv(symbol, timeframe=tf, limit=limit)
                if df is not None:
                    data[tf] = df

            if len(data) < 3:
                continue

            result = engine.analyze_pair(symbol, data)
            if result:
                signals.append(result)

        signals.sort(key=lambda x: x["score"], reverse=True)

        _cache["signals"] = signals
        _cache["last_updated"] = datetime.now(timezone.utc).isoformat()

    except Exception as e:
        print(f"Scan error: {e}")
    finally:
        _cache["scanning"] = False


@app.get("/", response_class=HTMLResponse)
async def home():
    """Simple HTML dashboard."""
    signals = _cache["signals"]
    last = _cache["last_updated"] or "Never"
    scanning = _cache["scanning"]

    rows = ""
    for s in signals[:20]:
        color = "#22c55e" if s["side"] == "LONG" else "#ef4444"
        rows += f"""
        <tr>
            <td style="color:{color};font-weight:bold">{s['side']}</td>
            <td><b>{s['symbol']}</b></td>
            <td>{s['score']}</td>
            <td>{s['entry']}</td>
            <td>{s['stop_loss']} <small>({s['risk_pct']:+.1f}%)</small></td>
            <td>{s['tp1']} <small>({s['tp1_pct']:+.1f}%)</small></td>
            <td>1:{s['rr']}</td>
        </tr>
        """

    status = "🔄 Scanning..." if scanning else f"✅ Last updated: {last}"

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Daily Signal by Grok AI</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body {{ font-family: system-ui, sans-serif; background: #0f172a; color: #e2e8f0; margin: 0; padding: 20px; }}
            h1 {{ color: #38bdf8; margin-bottom: 4px; }}
            .sub {{ color: #94a3b8; margin-bottom: 24px; }}
            table {{ width: 100%; border-collapse: collapse; background: #1e293b; border-radius: 12px; overflow: hidden; }}
            th, td {{ padding: 12px 16px; text-align: left; border-bottom: 1px solid #334155; }}
            th {{ background: #1e293b; color: #94a3b8; font-size: 13px; text-transform: uppercase; }}
            tr:hover {{ background: #334155; }}
            .btn {{ display: inline-block; background: #0ea5e9; color: white; padding: 10px 20px; border-radius: 8px; text-decoration: none; margin: 8px 8px 8px 0; font-weight: 600; }}
            .btn:hover {{ background: #0284c7; }}
            .status {{ margin: 16px 0; color: #94a3b8; }}
        </style>
    </head>
    <body>
        <h1>Daily Signal by Grok AI</h1>
        <div class="sub">Binance Multi-Timeframe Trading Signals</div>

        <a class="btn" href="/scan">🔄 Run New Scan</a>
        <a class="btn" href="/api/signals" style="background:#64748b">📡 JSON API</a>

        <div class="status">{status} | Showing {len(signals)} signals</div>

        <table>
            <thead>
                <tr>
                    <th>Side</th>
                    <th>Symbol</th>
                    <th>Score</th>
                    <th>Entry</th>
                    <th>Stop Loss</th>
                    <th>TP1</th>
                    <th>RR</th>
                </tr>
            </thead>
            <tbody>
                {rows if rows else '<tr><td colspan="7">No signals yet. Click "Run New Scan".</td></tr>'}
            </tbody>
        </table>

        <p style="margin-top:32px;color:#64748b;font-size:13px">
            Not financial advice. Always DYOR. Powered by Grok AI.
        </p>
    </body>
    </html>
    """
    return html


@app.get("/api/signals")
async def get_signals():
    """Return cached signals as JSON."""
    return {
        "last_updated": _cache["last_updated"],
        "scanning": _cache["scanning"],
        "count": len(_cache["signals"]),
        "signals": _cache["signals"],
    }


@app.get("/scan")
async def trigger_scan(background_tasks: BackgroundTasks, top: int = Query(40, ge=10, le=80)):
    """Trigger a new scan in the background."""
    if _cache["scanning"]:
        return {"status": "already_scanning", "message": "A scan is already running."}

    background_tasks.add_task(run_scan, top_n=top)
    return {
        "status": "started",
        "message": f"Scanning top {top} pairs in background. Refresh / in 30-60 seconds.",
    }


@app.get("/health")
async def health():
    return {"status": "ok", "service": "Daily Signal by Grok AI"}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("api:app", host="0.0.0.0", port=port, reload=False)
