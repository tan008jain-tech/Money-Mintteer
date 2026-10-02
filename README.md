# Money Mintter

Money Mintter is an event-driven market-analysis and paper-trading system designed to evolve toward autonomous multi-market trading.

## Current decision stack

- external coarse universe boundary;
- hard market-quality verification;
- maximum of five deep candidates;
- two-sided intraday liquidity-sweep strategy, lsr.v3;
- prior-session plus causal intraday liquidity context;
- local market-structure shift, displacement, volume and close confirmation;
- ATR-based structural risk and reward-room checks;
- explicit veto reasons and deterministic confidence scoring;
- immediate re-entry allowed after a fresh confirmation.

## Research stack

- event-order-aware long/short backtester;
- spread, slippage and commission modelling;
- next-bar execution;
- pessimistic same-bar stop/target collision handling;
- profit factor, expectancy-R, drawdown and consecutive-loss metrics;
- chronological walk-forward validation;
- HF Data Library 1-minute historical adapter;
- causal session/intraday context.

## Safety and operations

- PaperBroker with non-ideal fills;
- PostgreSQL runtime snapshot/event boundary;
- restart recovery and broker/state reconciliation;
- heartbeat/watchdog with RUN/PAUSE/HALT;
- fail-closed live execution guard;
- provider-neutral live broker boundary;
- exchange-calendar adapter;
- read-only dashboard.

## Strategy warning

There is no verified profitability yet. Synthetic tests verify implementation behavior, not market edge. Real historical testing, realistic costs, multi-symbol coverage and out-of-sample validation are required before paper or live deployment.

## Architecture

    External universe source
            |
            v
    Opportunity Analyzer
            |
            v
    Candidate Ranker ---- max 5
            |
            v
    Intraday Strategy (lsr.v3)
            |
            +-- liquidity sweep/reclaim
            +-- local MSS
            +-- displacement
            +-- confirmation
            |
            v
    Risk Engine
            |
            v
    PaperBroker / LiveBrokerAdapter
            |
            v
    Trade Manager

Separate research path:

    Historical 1-minute data
            |
            v
    Causal context
            |
            v
    StrategyBacktester
            |
            v
    WalkForwardValidator

## Temporary data stack

- FMP: coarse universe discovery before IBKR.
- HF Data Library: temporary historical 1-minute research.
- IBKR later: market data, universe and execution behind provider-neutral boundaries.

## Hard live-trading boundary

Money Mintter is fail-closed by default. Live broker credentials are not accepted through chat and are not committed to the repository.
