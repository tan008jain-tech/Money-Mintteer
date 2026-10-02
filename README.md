# Money Mintter

Money Mintter is an event-driven market-analysis and paper-trading system designed to evolve toward autonomous multi-market trading.

## What is built now

### Decision stack
- external-screener boundary with a provider-neutral HTTP adapter
- hard market-quality verification before deep analysis
- maximum of five deep candidates
- long-only liquidity-sweep reversal strategy with structure, displacement, volume, confirmation, ATR, reward-room and anti-chase filters
- explicit rejection/veto reasons and deterministic confidence scoring
- no artificial re-entry cooldown

### Research stack
- event-order-aware backtester
- spread, slippage, commission and next-bar execution modelling
- pessimistic same-bar stop/target collision handling
- profit factor, expectancy-R, drawdown and consecutive-loss metrics
- chronological walk-forward validation framework
- CSV historical-data loader and deterministic replay feed

### Safety and operations
- PaperBroker with non-ideal fills
- PostgreSQL runtime snapshots and event log boundary
- restart recovery and broker/state reconciliation that fails closed
- heartbeat registry and watchdog with RUN/PAUSE/HALT states
- execution guard that blocks live orders unless health, data and reconciliation gates are green
- broker-neutral live adapter, with live trading disabled unless explicitly armed
- exchange-calendar adapter for holidays and special sessions
- read-only local dashboard

The real broker, real market-data provider and external screener can be connected later through these boundaries without rewriting the strategy.

## Strategy warning

The strategy is deliberately strict. It is an engineering translation of publicly described liquidity-trap/sweep, market-structure and execution concepts, not a claim that any particular public educator's exact discretionary method has been captured perfectly.

There is **no verified live win rate yet**. Synthetic unit fixtures prove that the implementation behaves as designed, not that the market will reward it. Real historical data, realistic execution assumptions, walk-forward testing and out-of-sample validation are required before changing strategy thresholds or considering live deployment.

## Architecture

```text
Global open markets
       |
       v
External screener
       |
       v
Opportunity Analyzer  ---- hard vetoes / quality gates
       |
       v
Candidate Ranker ---- max 5 deep candidates
       |
       v
Deep Strategy ---- liquidity sweep -> MSS -> displacement -> confirmation
       |
       v
Risk Engine
       |
       v
PaperBroker / LiveBrokerAdapter
       |
       v
Trade Manager
       |
       +---- PostgreSQL snapshots/events
       +---- Watchdog / recovery / reconciliation
       +---- Read-only dashboard

Separate research path:
historical CSV/replay -> prior-session context -> StrategyBacktester -> WalkForwardValidator -> validated strategy version
```

## Optional production dependencies

The core package has no mandatory runtime network dependencies.

```bash
python -m pip install -e ".[dev]"
# PostgreSQL runtime:
python -m pip install -e ".[postgres]"
# Exchange holidays / special sessions:
python -m pip install -e ".[calendars]"
```

## Run locally

```bash
pytest
python -m money_mintter.main
mm-backtest path/to/ohlcv.csv ABC --timezone America/New_York
```

## Hard live-trading boundary

Money Mintter does not receive broker credentials from chat and does not assume a live account exists. The live adapter is provider-neutral and is fail-closed by default. A real broker gateway, account permissions, market-data provider, reconciliation process and explicit safety configuration are required before any live order could be sent.

## Temporary FMP market-data integration

For development before IBKR is available, Money Mintter includes a disposable FMP adapter:

- `/most-actives` provides the coarse active universe.
- `/biggest-gainers` and `/biggest-losers` can enrich discovery when the account exposes them.
- `/historical-chart/1min` provides 1-minute OHLCV candles for intraday strategy/research validation.
- All provider access is behind internal interfaces, so replacing FMP with IBKR later does not change strategy, risk, paper execution, persistence, or recovery.

See `docs/TEMPORARY_DATA_SOURCES.md` for the integration boundary and credential handling.
