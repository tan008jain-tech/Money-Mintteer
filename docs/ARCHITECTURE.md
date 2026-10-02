# Money Mintter Architecture

## Decision hierarchy

External universe
  -> candidate verification
  -> candidate ranker (max 5)
  -> Daily / 4H filters
  -> 1H primary context
  -> 15M secondary structure
  -> 5M / 3M entry
  -> risk engine
  -> paper/live broker boundary

## Research hierarchy

HF 1-minute history
  -> causal 4H/daily filters
  -> causal 1H context
  -> causal 15M structure
  -> 5M or 3M entry candles
  -> strategy
  -> 1-minute execution-aware backtester
  -> walk-forward / out-of-sample validation

## Timeframe contract

1. Daily and 4H filter only.
2. 1H supplies primary intraday context and important liquidity.
3. 15M supplies secondary structure and liquidity.
4. 5M/3M is the only entry-analysis timeframe.
5. 1M is execution/tracking data only.
6. A higher-timeframe bar is used only after it is fully closed.
7. Liquidity levels must have existed before the setup being tested.

## Operational boundaries

- Candidate discovery is coarse; deep analysis is internal.
- At most five new candidates enter deep monitoring.
- Existing positions are managed independently.
- Risk is hard-gated.
- Recovery fails closed on broker/state mismatches.
- Live execution remains disabled until validated.
