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
