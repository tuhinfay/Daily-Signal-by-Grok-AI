# Daily Signal V5 — Development Plan

Target configuration: **Hybrid / Quality First / Signal + Paper Trading**.

## Safety-first rollout
1. Validate and use completed candles only for signal calculations.
2. Introduce explicit market-regime classification; UNKNOWN is the fail-safe.
3. Introduce category-level scoring with fixed weights and separate hard filters.
4. Integrate Hybrid timeframe policy: 1D/4H bias, 1H setup, 15M entry; 5M is optional refinement.
5. Add structure/liquidity and futures context only when source data is present and fresh.
6. Keep paper execution simulated; do not add live order placement.
7. Add chronological backtests, costs, lookahead checks and forward-paper validation.

## Initial quality policy
- Minimum score: 75/100.
- A+: 85–100; A: 75–84; B: 65–74; C: below 65.
- Initial minimum RR: 1:2.
- Data validity, closed candle, spread and RR are hard filters.
- Score is a ranking heuristic, not a probability of profit.

## Resource policy
- Keep derivatives context optional and request-bounded.
- Cache market metadata/tickers and avoid redundant per-symbol requests.
- Prefer bounded batches and sequential scan scheduling over concurrent bursts.
- Treat unavailable/stale inputs as missing evidence, not directional evidence.

## Validation
Run: python -m unittest discover -s tests -v

Do not promote V5 to the default scanner until integration tests and out-of-sample backtesting pass.


## Current branch status

Implemented on `v5-development`:
- REST OHLCV ingestion drops the final potentially-forming candle, validates OHLCV values, and requires fresh timestamped 1D/4H/1H/15M history.
- UNKNOWN market regime fails closed. Hybrid uses 1D bias, 4H structure, 1H setup and 15M confirmed entry.
- Six weighted evidence categories total 100 points; score bands and the minimum 75 score are enforced independently from hard filters.
- Causal market-structure module detects prior-range break of structure, liquidity sweep/reclaim and confirmed HH/HL or LH/LL swing sequences; pivot labels require right-side confirmation bars to avoid lookahead.
- Funding, OI and order-book evidence is used only when returned. OI expansion is treated as participation, not directional proof.
- Final V5 qualification requires fresh data, completed candles, minimum 24h quote turnover, known non-extreme regime, score >=75, mark price within 0.5 ATR of planned entry, mark-referenced RR >=2 and known spread <=0.10%. Missing mark/spread fails closed.
- Auto scan requests V5-specific mark and exchange lot metadata only for a bounded set of top candidates; V4 scan does not pay those extra requests.
- Hybrid paper bot uses mark-referenced entry, 0.5% risk sizing rounded down to exchange quantity step, minimum quantity/notional checks, maximum two open V5 paper positions and no DCA.
- No live order execution has been introduced. Signal cards expose the six-category score breakdown.
- Added fee/slippage/funding-aware causal backtest engine. It enters at the next bar open, charges assumed bid/ask spread on both sides, uses conservative stop-first handling if SL and TP hit in one bar, and reports win rate, profit factor, expectancy and max drawdown.
- Added a historical V5 signal adapter that aligns 1D/4H/1H/15M bars by actual candle close time, requires minimum quote turnover and applies next-open RR and ATR-gap guards.
- Added `scripts/run_v5_backtest.py` to run the actual Hybrid V5 engine against user-supplied 15m/1h/4h/1d CSV files and optional historical funding CSV.
- Added expanding-window walk-forward folds and a runner that fits on train/validation windows and reports untouched test windows.
- Added a 30-day forward-paper validation endpoint: `GET /api/v5/forward-validation`. It requires at least 30 elapsed days and 30 closed paper trades before reporting READY_FOR_REVIEW; it subtracts estimated round-trip fees and any recorded funding cost. It cannot be pre-completed by code.
- Added unit tests for data validation, regime/scoring, structure, backtest costs/lookahead, walk-forward, mark-price behavior, lot constraints, risk sizing and forward validation.

## Remaining operational validation
- Deploy this branch to a staging Render/VPS environment and run the paper bot with V5 enabled only in that isolated staging environment.
- Collect at least 30 real elapsed days and 30 closed V5 paper trades using `/api/v5/forward-validation`; inspect results by side, grade and market regime before promotion.
- Run end-to-end tests against a mocked full Binance scan + paper-bot cycle. The current automated suite covers individual components but does not yet replace staging validation.
- Threshold calibration still requires a separate out-of-sample dataset; initial score, RR, spread and mark-deviation thresholds are not proven profitable.

`V5_ENABLED` reads an environment variable and defaults to false. Set `V5_ENABLED=true` only in an isolated staging deployment for paper-forward collection. Keep it unset/false in production until the operational and out-of-sample gates above are satisfied. Score is a heuristic, not a win-probability estimate. No live exchange order execution is part of this branch.
