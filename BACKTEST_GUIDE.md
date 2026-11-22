# Historical backtest guide

The backtester runs the same `SignalEngine` on historical, timestamp-aligned OHLCV data and evaluates each signal after the entry candle closes. It fills at the next entry-timeframe candle open.

## Required CSV columns

Each CSV must contain these columns:

`timestamp,open,high,low,close,volume`

- Use UTC timestamps.
- Timestamps should be candle **open** times.
- Keep candles sorted oldest to newest; duplicate timestamps are discarded.
- Use the same symbol for all timeframe files.

## Timeframes by strategy

| Strategy | Required files |
|---|---|
| scalp | 5m, 15m, 1h, 4h |
| day | 15m, 1h, 4h, 1d |
| swing | 1h, 4h, 1d |

The strategy needs enough warm-up history. In particular, the daily/4h EMA200 regime logic needs at least about 205 completed candles on its regime timeframe. A short sample may produce zero trades; that is not a profitability result.

## Run on Windows / VPS

From the project folder, with dependencies installed:

```bash
python -m scanner.backtest --15m data/BTCUSDT_15m.csv --1h data/BTCUSDT_1h.csv --4h data/BTCUSDT_4h.csv --1d data/BTCUSDT_1d.csv --style day --step 4 --max-bars 96 --out backtest_trades.csv
```

For scalp, pass `--5m`, `--15m`, `--1h`, and `--4h`, with `--style scalp`. For swing, pass `--1h`, `--4h`, and `--1d`, with `--style swing`.

Optional execution-cost controls:

- `--fee-rate 0.0005`: taker fee per side, default 0.05%.
- `--slippage-rate 0.0002`: adverse slippage per side, default 0.02%.
- `--step 4`: evaluate every fourth entry candle; set 1 for every candle (slower).
- `--max-bars 96`: maximum forward entry candles to hold a simulated trade.

The output summary includes trade count, win rate, profit factor, expectancy in R, net R, and maximum drawdown in R. Individual trades are written to the CSV specified by `--out`.

## Conservative assumptions and limitations

- If SL and a target are both touched in the same candle, SL is counted first.
- TP1 closes 50% and TP2 closes the remaining 50%.
- Fees and slippage are approximated; funding payments, exchange-specific fee tiers, spread impact, partial fills, and latency are not fully simulated.
- This is an offline validation tool, not proof of future profitability. Test several symbols and market regimes, reserve out-of-sample data, and compare paper-trading results before risking capital.
- The backtester reads historical CSVs only; it does not call Binance or place orders.
