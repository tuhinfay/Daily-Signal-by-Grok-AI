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
