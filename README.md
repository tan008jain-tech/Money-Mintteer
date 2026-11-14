# Money Mintter

Money Mintter is an event-driven market-analysis and paper-trading system designed to evolve toward autonomous multi-market trading.

## Current system

- Alpaca asset discovery with Layer 1 structural eligibility and Layer 2 daily suitability screening (Layer 3 market-cap filtering deferred).
- Maximum five deep candidates; existing positions managed separately.
- Daily / 4H broad context and larger-liquidity awareness.
- 1H primary intraday liquidity map.
- 15M secondary liquidity map.
- 5M or 3M entry confirmation.
- 1M execution and tracking only.
- Long and short signals.
- Liquidity-zone confluence across timeframes.
- Structure-aware stops and opposing-liquidity destinations.
- Percentage target/risk measurements with R:R retained only as a research metric.
- Event-order-aware backtesting with session and time exits.
- HF Data Library 1-minute historical adapter.
- Chronological walk-forward validation boundary.

## Locked strategy

The authoritative strategy definition is in `docs/STRATEGY_LOCK.md`.

The current implementation baseline is `lsr.mtf.v3`.

The lock file is a research contract. Ordinary bug fixes must not silently change the market model described there.

## Research path

HF Data Library 1M
-> 4H / Daily broad context
-> 1H liquidity
-> 15M liquidity
-> 5M / 3M entry
-> 1M execution
-> backtest
-> walk-forward / out-of-sample validation

The presence of a trade signal is not evidence of profitability. Validate across symbols, months, regimes and realistic execution costs.

## Data path

Alpaca asset discovery & Layer 1/2 suitability -> universe candidates

HF Data Library 1M -> multi-timeframe strategy -> 1M execution-aware backtester

IBKR will later replace temporary market-data and execution providers behind normalized interfaces.

## Security

API keys stay in local environment files or deployment secret stores and are never committed.


## Market data and universe policy

The production pipeline separates consolidated historical suitability data from intraday scanning:

- `ALPACA_HISTORICAL_FEED=sip`: Layer 2 daily-bar suitability uses SIP history, with requests ending at least 15 minutes before the current time.
- `ALPACA_REALTIME_FEED=iex`: Layer 4 intraday polling and ICT acquisition use the configured realtime feed (IEX by default).
- `ALPACA_ALLOW_HISTORICAL_FEED_FALLBACK=false`: Cross-feed historical fallback is disabled by default. Actual feed provenance is persisted and incompatible Layer 2 caches are rejected.

Layer 1 discovery must meet `MIN_UNIVERSE_HEALTH` (default 50) and a persisted cache must be no older than `MAX_UNIVERSE_CACHE_AGE_HOURS` (default 24). Layer 2 returns up to `MAX_DAILY_POOL_SIZE` qualified symbols (default 50) and never pads the pool. Layer 4 selects up to 8 candidates. Execution capacity is controlled separately by `MAX_OPEN_POSITIONS` (default 5); open positions, broker-open orders, and uncertain pending submissions each consume a symbol slot.


## Execution and portfolio safety

- `MAX_OPEN_POSITIONS=5` is the portfolio limit, separate from Layer 4's eight-candidate selection cap. Existing positions, broker-open orders, and ambiguous pending submissions consume a symbol slot.
- `QUOTE_MAX_AGE_SECONDS=10` and `QUOTE_MAX_FUTURE_SKEW_SECONDS=2` define acceptable latest-quote timestamp freshness.
- `MAX_EXECUTION_PRICE_DEVIATION_PCT=1` limits the side-aware executable quote (ask for buys, bid for sells) to 1% away from the ICT signal entry. Missing, stale, invalid, crossed, or out-of-tolerance quotes fail closed.
- `MAX_QUOTE_SPREAD_PCT=1` rejects quotes whose bid/ask spread exceeds 1% of the quote midpoint. This is a separate liquidity/quote-quality guard; passing the signal-deviation check alone is not sufficient.
- The executable entry, rounded stop and target must retain directional bracket geometry and at least 1.10 reward/risk before sizing. The ICT signal-generation rules are unchanged.
