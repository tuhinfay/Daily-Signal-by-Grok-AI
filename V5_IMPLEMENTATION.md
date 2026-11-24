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

Implemented in this first development increment:
- REST OHLCV ingestion drops the final, potentially-forming candle.
- Added market-data validation and freshness helpers.
- Added explicit regime classification with UNKNOWN fail-safe.
- Added fixed-weight quality scoring and non-overridable qualification filters.
- Added a Hybrid timeframe route to the signal engine (1D/4H/1H/15M).
- Added Hybrid to manual analysis and paper-trading strategy selection.
- Added unit tests and a GitHub Actions test workflow.

Not yet production-complete:
- The new category scorer is not yet wired into the full signal-engine decision path; Hybrid currently uses the existing component scoring with the new minimum score/RR gate.
- Market regime, structure and derivatives evidence still need full end-to-end integration.
- The automated scan remains on the existing V4 styles while V5_ENABLED is false.
- Historical backtest, walk-forward evaluation and paper-forward validation are still outstanding.

Keep V5_ENABLED=false until the remaining integration and tests are completed. No live exchange order execution is part of this branch.
