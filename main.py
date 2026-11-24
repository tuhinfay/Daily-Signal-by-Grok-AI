#!/usr/bin/env python3
"""
Daily Signal by Grok AI
Binance Multi-Timeframe Trading Signal Scanner
"""

import argparse
import json
import time
from datetime import datetime, timezone
from typing import List

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text

import config
from scanner.binance_client import BinanceClient
from scanner.signal_engine import SignalEngine

console = Console()


def print_banner():
    banner = Text()
    banner.append("DAILY SIGNAL BY GROK AI", style="bold cyan")
    banner.append("  |  ", style="dim")
    banner.append("Binance Futures Scanner", style="bold white")
    console.print(Panel(banner, border_style="cyan"))


def scan(symbols: List[str] = None, top_n: int = None, as_json: bool = False):
    client = BinanceClient(futures=True)
    engine = SignalEngine()

    if symbols is None:
        top_n = top_n or config.TOP_N_PAIRS
        console.print(f"[dim]Fetching top {top_n} USDT pairs by volume...[/dim]")
        symbols = client.get_top_usdt_pairs(top_n=top_n, min_volume=config.MIN_VOLUME_USDT)

    console.print(f"[bold]Scanning {len(symbols)} pairs...[/bold]\n")

    signals = []
    timeframes = ["15m", "1h", "4h", "1d"]

    for i, symbol in enumerate(symbols, 1):
        console.print(f"[{i}/{len(symbols)}] {symbol}", end="\r")

        data = {}
        for tf in timeframes:
            limit = config.CANDLE_LIMIT.get(tf, 150)
            df = client.fetch_ohlcv(symbol, timeframe=tf, limit=limit)
            if df is not None:
                data[tf] = df
            time.sleep(0.15)  # be kind to rate limits

        if len(data) < 3:
            continue

        result = engine.analyze_pair(symbol, data)
        if result:
            signals.append(result)

    console.print(" " * 40, end="\r")  # clear line

    # Sort by score
    signals.sort(key=lambda x: x["score"], reverse=True)

    if as_json or config.EXPORT_JSON:
        output = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "signals": signals,
        }
        with open(config.JSON_FILENAME, "w") as f:
            json.dump(output, f, indent=2)
        console.print(f"[green]Saved {len(signals)} signals to {config.JSON_FILENAME}[/green]")

    # Pretty print
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    console.print()
    console.print(Panel(
        f"[bold cyan]DAILY SIGNAL BY GROK AI[/bold cyan]  |  {now}\n"
        f"Found [bold green]{len(signals)}[/bold green] high-quality signals",
        border_style="cyan"
    ))

    if not signals:
        console.print("[yellow]No signals met the minimum score threshold.[/yellow]")
        return signals

    table = Table(show_header=True, header_style="bold magenta", border_style="dim")
    table.add_column("Side", style="bold", width=7)
    table.add_column("Symbol", style="cyan", width=12)
    table.add_column("Score", justify="center", width=7)
    table.add_column("Entry", justify="right")
    table.add_column("Stop Loss", justify="right")
    table.add_column("TP1", justify="right")
    table.add_column("RR", justify="center")
    table.add_column("Reasons", max_width=45)

    for s in signals:
        side_style = "bold green" if s["side"] == "LONG" else "bold red"
        side_text = f"[{side_style}]{s['side']}[/{side_style}]"

        reasons = " | ".join(s["reasons"][:3])

        table.add_row(
            side_text,
            s["symbol"],
            str(s["score"]),
            f"{s['entry']}",
            f"{s['stop_loss']} ({s['risk_pct']:+.2f}%)",
            f"{s['tp1']} ({s['tp1_pct']:+.2f}%)",
            f"1:{s['rr']}",
            reasons,
        )

    console.print(table)
    console.print()

    # Detailed view for top signals
    for s in signals[:5]:
        color = "green" if s["side"] == "LONG" else "red"
        detail = (
            f"[bold {color}]{s['side']}[/bold {color}]  {s['symbol']}   Score: {s['score']}/100\n"
            f"  Entry     : {s['entry']}\n"
            f"  Stop Loss : {s['stop_loss']}   ({s['risk_pct']:+.2f}%)\n"
            f"  TP1       : {s['tp1']}   ({s['tp1_pct']:+.2f}%)\n"
            f"  TP2       : {s['tp2']}   ({s['tp2_pct']:+.2f}%)\n"
            f"  Risk:Reward : 1 : {s['rr']}\n"
            f"  Reasons   : {' | '.join(s['reasons'])}"
        )
        console.print(Panel(detail, border_style=color, title=s["symbol"]))

    return signals


def main():
    parser = argparse.ArgumentParser(description="Daily Signal by Grok AI - Binance Signal Scanner")
    parser.add_argument("--symbols", nargs="+", help="Specific symbols to scan (e.g. BTCUSDT ETHUSDT)")
    parser.add_argument("--top", type=int, default=None, help="Number of top volume pairs to scan")
    parser.add_argument("--json", action="store_true", help="Export results to signals.json")
    parser.add_argument("--loop", action="store_true", help="Run continuously every 15 minutes")
    args = parser.parse_args()

    print_banner()

    if args.loop:
        console.print("[bold yellow]Continuous mode: scanning every 15 minutes. Ctrl+C to stop.[/bold yellow]\n")
        while True:
            try:
                scan(symbols=args.symbols, top_n=args.top, as_json=args.json)
                console.print("\n[dim]Next scan in 15 minutes...[/dim]\n")
                time.sleep(15 * 60)
            except KeyboardInterrupt:
                console.print("\n[yellow]Stopped by user.[/yellow]")
                break
    else:
        scan(symbols=args.symbols, top_n=args.top, as_json=args.json)


if __name__ == "__main__":
    main()
