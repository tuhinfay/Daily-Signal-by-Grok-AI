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
- REST OHLCV ingestion drops the final, potentially-forming candle and validates OHLCV values.
- V5 requires recent timestamped 1D/4H/1H/15M frames before producing a Hybrid signal.
- Added regime classification with UNKNOWN fail-safe.
- Wired six weighted evidence categories into Hybrid scoring: regime, HTF alignment, structure, momentum, volume/futures and entry/risk.
- Added bounded futures evidence blending for funding, open-interest change and order-book imbalance when those observations are returned by Binance.
- Final V5 qualification enforces score >=75, RR >=2, closed/valid data and a known spread <=0.10%. Missing spread fails closed.
- Existing scan pipeline requests derivatives/order-book context for at most four unique top-ranked symbols to bound API use. Other symbols without spread observations are intentionally not qualified for V5.
- Hybrid is available in manual analysis and paper-trading selection; both automatic and manual Hybrid analysis use the same final spread/RR/data qualification.
- Hybrid paper bot uses the configured 0.5% balance risk sizing, caps concurrent V5 paper positions at two, and skips DCA. Paper trades remain simulated; no live order placement was added.
- Signal cards expose the six-category V5 score breakdown.
- Added unit tests for core validation, weighted evidence and hard-filter behavior; GitHub Actions runs unittest discovery.

Still outstanding before V5 can be enabled by default:
- Add end-to-end integration tests for the API scan and paper-bot path with mocked Binance responses (the current 28-test unit suite passes in Python 3.12).
- Improve structure analysis beyond the current confirmed rolling swing/breakout references (e.g. swing sequence, liquidity sweeps and retests).
- Add mark-price-aware entry checks and verify risk sizing against exchange precision/minimum-notional constraints.
- Add historical backtesting with fees, slippage and funding; add walk-forward and 30-day paper-forward evaluation.
- Expand analysis output/dashboard to expose hard-filter rejection reasons and full regime/futures context more clearly.
- Calibrate score thresholds, RR and spread limits using out-of-sample results rather than treating initial values as proven.

Keep `V5_ENABLED=false` until integration tests and out-of-sample validation pass. Score is a heuristic, not a win-probability estimate. No live exchange order execution is part of this branch.
