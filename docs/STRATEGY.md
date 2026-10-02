# Money Mintter Strategy Baseline

Money Mintter now uses a **two-sided intraday** liquidity-sweep reversal strategy, version `lsr.v2`.

## Long path

Prior-day low sweep -> bullish market-structure shift -> bullish displacement -> close-confirmed breakout -> long signal.

## Short path

Prior-day high sweep -> bearish market-structure shift -> bearish displacement -> close-confirmed breakdown -> short signal.

Both directions use mirrored ATR, volume, spread, chase, structural-stop, reward-room, and quality-score controls.

## Research boundary

This implementation proves only that the rules can be executed deterministically. It does **not** establish profitability. The strategy must be tested across many symbols, dates, and market regimes using realistic execution costs and chronological out-of-sample validation.

The previous long-only `lsr.v1` implementation remains in Git history for auditability.
