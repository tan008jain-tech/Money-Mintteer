# Money Mintter Architecture

## Non-negotiable boundaries

1. **Decision and execution are separate.** Strategy code produces a signal; the broker layer owns order semantics.
2. **The paper broker must be replaceable.** A future live adapter implements the same broker interface without changing the strategy or risk engine.
3. **Candidate discovery is coarse.** An external screener can shrink the universe; Money Mintter re-ranks and verifies candidates before deep monitoring.
4. **Deep monitoring is capped.** At most five new candidates are selected by the candidate-ranker policy. Active positions are managed separately and are not evicted by a better new candidate.
5. **No arbitrary re-entry cooldown.** A fresh confirmation may re-enter immediately after an exit.
6. **Risk is explicit.** Position size derives from stop distance and a hard risk budget, rather than from a target profit amount.
7. **Profit reserve is accounting, not a second broker.** The broker owns actual cash; the internal ledger tracks Trading Wallet and Profit Reserve buckets and must reconcile with broker/account state later.
8. **Time is timezone-aware.** Market sessions use named IANA time zones and can be backed by exchange calendars for holidays and special sessions.
9. **Restart recovery fails closed.** Persisted state is expected state; the broker is the source of truth for actual positions/cash. Any unexplained mismatch pauses or halts execution.
10. **Live execution is gated.** Health, market-data freshness, reconciliation and watchdog state must all be green before a live gateway can submit an order.

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

### Phase 2: strategy and research

The first strict long-only liquidity-sweep reversal strategy, opportunity analyzer, deterministic replay, backtester and walk-forward validation framework are implemented. They remain unproven until real historical data is loaded.

### Phase 3: integration boundaries

Provider-neutral market-data/screener adapters, exchange calendars and a broker-neutral live adapter are implemented. Real provider credentials are still intentionally absent.

### Phase 4: persistence and recovery

PostgreSQL snapshot/event persistence, restart reconciliation and fail-closed runtime supervision are implemented. Durable order-level audit data and idempotency still need to be expanded before live execution.

### Phase 5: operations

Watchdog/health state and a read-only dashboard are implemented. Data-feed reconnects, alert delivery, metrics and deployment automation remain for the production hardening pass.

### Phase 6: broker integration

The live execution boundary exists, but actual broker credentials/account setup are still required. Live trading remains disabled by default.

### Next hardening work

Expand durable order/fee/event records, idempotency, reconnect handling, alert delivery, metrics, deployment automation, and provider-specific adapters. Only then should historical real-market testing feed into paper trading, and only after that should live execution be considered.
