# Money Mintter Strategy

Current strategy version: lsr.mtf.v1

## Timeframe hierarchy

Daily / 4H
  -> broad filter only
1H
  -> primary intraday trend and important liquidity
15M
  -> secondary structure and liquidity
5M or 3M
  -> actual entry analysis
1M
  -> execution and tracking only

## Rules

### Daily and 4H
These do not create entries. They provide a broad directional filter and can block a trade when both higher-timeframe structures oppose it.

### 1H
Primary intraday context. Important swing highs/lows and the main intraday trend live here.

### 15M
Secondary context. It supplies nearer liquidity and refines the 1H structure.

### 5M / 3M
The entry engine looks for:

1. sweep of 1H or 15M liquidity;
2. reclaim;
3. local market-structure shift;
4. displacement and volume confirmation;
5. break-and-close confirmation;
6. structure-aware stop and opposing-liquidity reward room.

The latest entry candle must be the confirmation candle before a signal can be emitted.

### 1M
Raw 1-minute candles are not used to create the setup. They are used for next-bar execution, stop/target monitoring, session exits and position tracking.

## Research boundary

Signal frequency is not proof of edge. Validate across multiple symbols, multiple regimes, realistic costs, chronological walk-forward windows and out-of-sample periods.

Every signal carries the strategy version.
