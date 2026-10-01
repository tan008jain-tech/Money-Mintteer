# Money Mintter

Money Mintter is an event-driven market-analysis and paper-trading system designed to evolve toward autonomous multi-market trading.

## First vertical slice

This repository currently contains the foundation for:

- global market-session awareness using timezone-aware schedules
- coarse candidate ranking with a hard maximum of 5 deep candidates
- a broker abstraction with a realistic paper broker
- conservative risk-based position sizing
- Trading Wallet / Profit Reserve accounting with a 20% reserve of realized profit
- event-driven trade management for stop and target handling
- an end-to-end executable simulation
- unit and integration tests
- Docker packaging and GitHub CI

The system deliberately does **not** connect to a real broker or place live orders in this slice.

## Architecture boundary

```text
market/session discovery
        |
        v
external screener --> candidate ranking --> max 5 deep candidates
                                      |
                                      v
                              strategy signals
                                      |
                                      v
                                risk engine
                                      |
                                      v
                            broker interface
                           /              \\
                  PaperBroker        future live adapters
                           |
                           v
                     trade manager
                           |
                           v
                  wallet / reserve ledger
```

The strategy layer is intentionally separated from execution. This lets the trading logic evolve without rewriting broker integration.

## Run locally

```bash
python -m pip install -e .
pytest
python -m money_mintter.main
```

## Important prototype boundary

The paper broker is a simulator. It is not evidence that a strategy is profitable. Live trading requires validated market-data adapters, broker-specific order semantics, exchange calendars, persistent state, monitoring, kill switches, reconciliation, and extensive testing under realistic fills and failure conditions.
