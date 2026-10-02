# Money Mintter Strategy Baseline

Money Mintter's current research strategy is lsr.v4.

## Intent

The strategy is intended to find shorter-duration intraday opportunities while remaining selective about execution quality.

## Flow

prior-session or causal intraday liquidity
-> sweep + reclaim
-> local MSS
-> displacement
-> close-confirmed trigger
-> risk / room / quality
-> long or short

## Long path

Sell-side liquidity is swept and reclaimed. Price then breaks recent local structure to the upside with displacement and closes through the confirmation level.

## Short path

Buy-side liquidity is swept and reclaimed. Price then breaks recent local structure to the downside with displacement and closes through the confirmation level.

## Causal liquidity model

Prior-session high/low comes from PreviousSessionContext.

Intraday liquidity is derived inside the strategy, separately for each candidate sweep, using only candles strictly before that sweep. Confirmed local swing highs/lows and a historical rolling extreme are used. This avoids using a level that did not exist yet at the time of the sweep.

## Why this version exists

The earlier baseline was too dependent on prior-session extremes and a broad pre-sweep structure window. That produced a sparse opportunity set for intraday use.

lsr.v4 broadens the measurable intraday opportunity set while explicitly removing the look-ahead path found during the code audit.

This is a research hypothesis, not a profitability claim.

## Validation boundary

Signal frequency is not proof of edge. Validation must use multiple symbols, multiple regimes and months, realistic costs, chronological walk-forward tests and out-of-sample evaluation.

Every TradeSignal carries the strategy version for auditability.
