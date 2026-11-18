#!/usr/bin/env python3
"""
Daily Signal by Grok AI - FastAPI Web Server
"""

from fastapi import FastAPI, Query, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Optional
from datetime import datetime, timezone
import uvicorn
import os
import traceback

from scanner.binance_client import BinanceClient
from scanner.signal_engine import SignalEngine
import config

app = FastAPI(
    title="Daily Signal by Grok AI",
    description="Binance Multi-Timeframe Trading Signal Scanner",
    version="1.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_cache = {
    "signals": [],
    "last_updated": None,
    "scanning": False,
    "error": None,
    "scanned_count": 0,
}


def run_scan(symbols: Optional[List[str]] = None, top_n: int = 25):
    global _cache
    _cache["scanning"] = True
    _cache["error"] = None

    try:
        client = BinanceClient(futures=True)
        engine = SignalEngine()

        if symbols is None:
            symbols = client.get_top_usdt_pairs(
                top_n=top_n,
                min_volume=config.MIN_VOLUME_USDT
            )

        signals = []
        timeframes = ["15m", "1h", "4h", "1d"]

        for i, symbol in enumerate(symbols):
            data = {}
            for tf in timeframes:
                limit = config.CANDLE_LIMIT.get(tf, 100)
                df = client.fetch_ohlcv(symbol, timeframe=tf, limit=limit)
                if df is not None and len(df) > 30:
                    data[tf] = df

            if len(data) < 3:
                continue

            try:
                result = engine.analyze_pair(symbol, data)
                if result:
                    signals.append(result)
            except Exception:
                continue

        signals.sort(key=lambda x: x["score"], reverse=True)

        _cache["signals"] = signals
        _cache["last_updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        _cache["scanned_count"] = len(symbols)

    except Exception as e:
        _cache["error"] = str(e)
        print(f"Scan error: {e}")
        traceback.print_exc()
    finally:
        _cache["scanning"] = False


@app.on_event("startup")
async def startup_event():
    """Auto scan a few pairs on startup so page is not empty."""
    import threading
    def initial_scan():
        run_scan(top_n=15)
    threading.Thread(target=initial_scan, daemon=True).start()


@app.get("/", response_class=HTMLResponse)
async def home():
    signals = _cache["signals"]
    last = _cache["last_updated"] or "Never"
    scanning = _cache["scanning"]
    error = _cache["error"]
    scanned = _cache["scanned_count"]

    rows = ""
    for s in signals[:25]:
        color = "#22c55e" if s["side"] == "LONG" else "#ef4444"
        reasons = " • ".join(s.get("reasons", [])[:3])
        rows += f"""
        <tr>
            <td style="color:{color};font-weight:700">{s['side']}</td>
            <td><b>{s['symbol']}</b></td>
            <td><b>{s['score']}</b></td>
            <td>{s['entry']}</td>
            <td>{s['stop_loss']}<br><small style="color:#94a3b8">{s['risk_pct']:+.1f}%</small></td>
            <td>{s['tp1']}<br><small style="color:#94a3b8">{s['tp1_pct']:+.1f}%</small></td>
            <td>1:{s['rr']}</td>
            <td style="font-size:12px;color:#94a3b8;max-width:220px">{reasons}</td>
        </tr>
        """

    if scanning:
        status = "🔄 Scanning in progress... Please wait 20-40 seconds then refresh."
    elif error:
        status = f"⚠️ Error: {error}"
    else:
        status = f"✅ Last updated: {last} | Scanned {scanned} pairs | {len(signals)} signals found"

    empty_msg = ""
    if not signals and not scanning:
        empty_msg = """
        <tr><td colspan="8" style="text-align:center;padding:30px;color:#94a3b8">
            No signals yet. Click <b>Run New Scan</b> below.
        </td></tr>
        """

    html = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <title>Daily Signal by Grok AI</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <meta http-equiv="refresh" content="{15 if scanning else 120}">
        <style>
            * {{ box-sizing: border-box; }}
            body {{ font-family: system-ui, -apple-system, sans-serif; background: #0f172a; color: #e2e8f0; margin: 0; padding: 16px; }}
            h1 {{ color: #38bdf8; margin: 0 0 4px 0; font-size: 1.6rem; }}
            .sub {{ color: #94a3b8; margin-bottom: 20px; font-size: 0.95rem; }}
            .btn {{ display: inline-block; background: #0ea5e9; color: white; padding: 11px 20px; border-radius: 8px;
                   text-decoration: none; margin: 6px 8px 6px 0; font-weight: 600; font-size: 14px; border: none; cursor: pointer; }}
            .btn:hover {{ background: #0284c7; }}
            .btn.secondary {{ background: #475569; }}
            .btn.secondary:hover {{ background: #64748b; }}
            .status {{ margin: 14px 0 18px; color: #94a3b8; font-size: 14px; padding: 10px 14px;
                      background: #1e293b; border-radius: 8px; }}
            .table-wrap {{ overflow-x: auto; border-radius: 12px; }}
            table {{ width: 100%; border-collapse: collapse; background: #1e293b; min-width: 700px; }}
            th, td {{ padding: 11px 14px; text-align: left; border-bottom: 1px solid #334155; vertical-align: top; }}
            th {{ background: #1e293b; color: #94a3b8; font-size: 12px; text-transform: uppercase; letter-spacing: 0.03em; }}
            tr:hover {{ background: #33415544; }}
            footer {{ margin-top: 28px; color: #64748b; font-size: 12px; }}
        </style>
    </head>
    <body>
        <h1>Daily Signal by Grok AI</h1>
        <div class="sub">Binance Futures • Multi-Timeframe Signals</div>

        <a class="btn" href="/scan">🔄 Run New Scan</a>
        <a class="btn secondary" href="/api/signals">📡 JSON API</a>
        <a class="btn secondary" href="/">↻ Refresh</a>

        <div class="status">{status}</div>

        <div class="table-wrap">
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
                    <th>Reasons</th>
                </tr>
            </thead>
            <tbody>
                {rows if rows else empty_msg}
            </tbody>
        </table>
        </div>

        <footer>
            Not financial advice. Always DYOR. Powered by Grok AI.<br>
            Free tier may sleep after inactivity — first load can take 30-50s.
        </footer>
    </body>
    </html>
    """
    return html


@app.get("/api/signals")
async def get_signals():
    return {
        "last_updated": _cache["last_updated"],
        "scanning": _cache["scanning"],
        "error": _cache["error"],
        "scanned_count": _cache["scanned_count"],
        "count": len(_cache["signals"]),
        "signals": _cache["signals"],
    }


@app.get("/scan")
async def trigger_scan(background_tasks: BackgroundTasks, top: int = Query(25, ge=10, le=50)):
    """Start scan then redirect back to dashboard."""
    if not _cache["scanning"]:
        background_tasks.add_task(run_scan, top_n=top)
    return RedirectResponse(url="/", status_code=303)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "Daily Signal by Grok AI",
        "scanning": _cache["scanning"],
        "signals": len(_cache["signals"]),
    }


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("api:app", host="0.0.0.0", port=port, reload=False)
