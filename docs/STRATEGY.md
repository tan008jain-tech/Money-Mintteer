# Money Mintter Strategy Baseline

Money Mintter's current research strategy is lsr.v3.

## Intent

The system is intended to find shorter-duration intraday opportunities while remaining selective about execution quality. The strategy therefore uses local intraday liquidity as well as prior-session liquidity.

## Flow

prior-session / confirmed intraday liquidity
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

## Liquidity model

The point-in-time context can contain:
- previous-session high/low;
- recently confirmed intraday swing highs/lows;
- the recent rolling extreme as a historical fallback.

No current or future candle is included when constructing those levels.

## Why lsr.v3 exists

The previous baseline treated the previous session's extreme as the dominant sweep level and compared the sweep against a broad 24-bar pre-sweep range. That was much narrower than the intended short-duration intraday use case.

lsr.v3 measures a broader opportunity set by using local intraday liquidity and shorter causal structure windows. This is a research hypothesis, not a profitability claim.

## Validation boundary

A signal count is not proof of edge. Validation must use multiple symbols, multiple market regimes, realistic spread/slippage/fees, event-order-aware execution, chronological walk-forward tests and out-of-sample evaluation.

The strategy version is embedded in every TradeSignal so results can be audited later.
