# Money Mintter Architecture

## Non-negotiable boundaries

1. Decision and execution are separate.
2. The paper broker is replaceable by a live broker adapter.
3. Candidate discovery is coarse; deep analysis is internal.
4. At most five new candidates enter deep monitoring; existing positions are managed separately.
5. Fresh confirmation may re-enter immediately after a confirmed exit.
6. Risk is derived from stop distance and hard budgets.
7. Trading Wallet and Profit Reserve are internal accounting buckets.
8. Time is timezone-aware.
9. Restart recovery fails closed on unexplained broker/state mismatches.
10. Live execution is gated by health, data freshness, reconciliation and watchdog state.

## Production path

    External universe source
            |
            v
    Candidate verification
            |
            v
    Candidate ranker (max 5)
            |
            v
    Intraday strategy
            |
            v
    Risk engine
            |
            v
    Broker / trade manager

## Research path

    Historical 1-minute candles
            |
            v
    Previous-session context
            |
            v
    Strategy derives causal intraday liquidity
            |
            v
    Strategy (lsr.v4)
            |
            v
    Event-order-aware backtester
            |
            v
    Walk-forward / out-of-sample validation

## Phases

### Phase 1: local vertical slice
Domain, sessions, candidate cap, risk, paper execution, wallet accounting, tests and CI.

### Phase 2: strategy and research
Two-sided intraday strategy, historical adapters, deterministic replay, backtest and walk-forward validation.

### Phase 3: integration boundaries
Provider-neutral universe and market-data adapters, exchange calendars and broker-neutral live boundary.

### Phase 4: persistence and recovery
Snapshots, events, restart reconciliation, idempotency and durable order state.

### Phase 5: operations
Reconnects, alerts, metrics, deployment automation and watchdog hardening.

### Phase 6: broker integration
IBKR or another provider after historical validation and paper-trading evidence. Live execution remains disabled by default.
