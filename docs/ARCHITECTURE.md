# Money Mintter Architecture

## Non-negotiable boundaries

1. **Decision and execution are separate.** Strategy code produces a signal; the broker layer owns order semantics.
2. **The paper broker must be replaceable.** A future live adapter implements the same broker interface without changing the strategy or risk engine.
3. **Candidate discovery is coarse.** An external screener can shrink the universe; Money Mintter re-ranks and verifies candidates before deep monitoring.
4. **Deep monitoring is capped.** At most five new candidates are selected by the candidate-ranker policy. Active positions are managed separately and are not evicted by a better new candidate.
5. **No arbitrary re-entry cooldown.** A fresh confirmation may re-enter immediately after an exit.
6. **Risk is explicit.** Position size derives from stop distance and a hard risk budget, rather than from a target profit amount.
7. **Profit reserve is accounting, not a second broker.** The broker owns actual cash; the internal ledger tracks Trading Wallet and Profit Reserve buckets and must reconcile with broker/account state later.
8. **Time is timezone-aware.** Market sessions use named IANA time zones. Exchange holidays and special sessions are a future calendar adapter, not hardcoded weekend logic.

## Production evolution

```text
External screener
      |
      v
Session engine -> Candidate verification/ranking -> Strategy engine
                                               |
                                               v
                                            Risk engine
                                               |
                                               v
                                      Execution / Broker API
                                               |
                                               v
                                         Trade manager
                                               |
                                               v
                              PostgreSQL + audit/event history
                                               |
                    +--------------------------+------------------+
                    |                                             |
                    v                                             v
             Dashboard / API                              Monitoring / watchdog
```

Separate research infrastructure will later handle backtests, parameter experiments, regime analysis, and model training. Production should only load validated strategy versions.

## Phased build

### Phase 1: verified local vertical slice

Current repository state: domain objects, sessions, candidate cap, risk sizing, paper broker, wallet/profit-reserve accounting, trade lifecycle, tests, CI, Docker packaging.

### Phase 2: live data boundaries

Implement streaming/polling market-data adapters and an external screener adapter. Add exchange calendars and holiday handling. Persist market/session state.

### Phase 3: strategy research

Implement the actual price-action/structure/liquidity strategy as a versioned research module. Backtest with realistic costs, slippage, latency assumptions, and out-of-sample validation.

### Phase 4: persistence and recovery

Add PostgreSQL, durable order/trade/event records, idempotent order submission, restart recovery, broker reconciliation, and audit logs.

### Phase 5: operations

Add Redis/event transport where justified, watchdogs, data-feed reconnects, process health, alerting, metrics, dashboards, and controlled deployment.

### Phase 6: broker integration

Implement a broker-specific adapter only after the paper workflow is stable. Live trading remains disabled by default and requires explicit configuration plus independent risk/kill-switch checks.
