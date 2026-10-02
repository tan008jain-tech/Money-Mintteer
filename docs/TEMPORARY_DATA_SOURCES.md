# Temporary Market-Data Stack

Money Mintter keeps temporary providers behind provider-neutral boundaries so they can be replaced by IBKR later.

## Temporary universe source

FMP is currently used only for coarse universe discovery:

- most-actives is the required discovery endpoint;
- biggest-gainers and biggest-losers are optional enrichments;
- FMP values are normalized into Candidate objects;
- the deep strategy does not treat provider movement scores as trade signals.

## Temporary historical research source

HF Data Library supplies the 1-minute OHLCV research dataset.

The current research path is:

HF 1-minute candles
-> PreviousSessionContext
-> lsr.v4
-> StrategyBacktester
-> WalkForwardValidator

Intraday liquidity is derived from candles inside the strategy and is never accepted as a provider-supplied future-aware signal.

## Credentials

API keys stay in the local environment or deployment secret store and are never committed.

## Replacement path

When IBKR is available, implement the provider-specific data interfaces behind the existing boundaries. Strategy, risk, paper execution, persistence and recovery should not require provider-specific changes.
