# Money Mintter

Money Mintter is an event-driven market-analysis and paper-trading system designed to evolve toward autonomous multi-market trading.

## Current system

- FMP coarse universe during temporary development.
- Maximum five deep candidates; existing positions managed separately.
- Daily / 4H broad filters.
- 1H primary intraday trend and liquidity.
- 15M secondary structure and liquidity.
- 5M or 3M entry analysis.
- 1M execution and tracking only.
- Long and short signals.
- Causal multi-timeframe aggregation.
- Structure-aware stops, reward-room checks and risk sizing.
- Event-order-aware backtesting with session and time exits.
- HF Data Library 1-minute historical adapter.
- Chronological walk-forward validation boundary.

## Strategy status

lsr.mtf.v1 is the current research baseline. It is an implementation baseline, not a profitability claim.

Do not judge it from one symbol or one month. Run diagnostics first, then multi-symbol and multi-regime backtests, followed by out-of-sample validation.

## Temporary data path

FMP -> universe candidates

HF Data Library 1M -> 4H / 1H / 15M / 5M or 3M -> strategy -> 1M execution-aware backtester

IBKR will later replace temporary market-data and execution providers behind normalized interfaces.

## Security

API keys stay in local environment files or deployment secret stores and are never committed.
