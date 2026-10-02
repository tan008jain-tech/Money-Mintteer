# Money Mintter Strategy

Current implementation: lsr.mtf.v3

The authoritative strategy definition is \`docs/STRATEGY_LOCK.md\`. This file summarizes the implementation shape only.

## Runtime hierarchy

Daily / 4H broad context
-> 1H major intraday liquidity
-> 15M secondary intraday liquidity
-> 5M or 3M entry
-> 1M execution/tracking

## Liquidity zones

The implementation clusters nearby liquidity levels across timeframes into zones.

A zone records its contributing sources and a relative strength score. Higher-timeframe sources contribute more weight. A zone containing both 15M and 1H liquidity is explicitly recognized as confluence.

Daily/4H liquidity may reinforce a zone but do not independently create an entry.

## Entry flow

1. Identify a mapped intraday liquidity zone.
2. Wait for the 5M/3M price to sweep the zone.
3. Require reclaim/rejection.
4. Require a local market-structure shift.
5. Require displacement.
6. Require confirmation through the MSS level.
7. Use the sweep/MSS structure for the stop.
8. Use the next meaningful opposing liquidity zone as the destination.
9. Record percentage target distance and risk distance.
10. Allow a new setup later when a new liquidity event forms.

R:R remains a measurement for research reporting only. It is not the primary setup gate.

The higher-timeframe directional tendency contributes to quality scoring but does not act as a blunt countertrend veto.

Volume confirmation is optional and is not a hard requirement by default.

Exact numerical thresholds remain implementation parameters and must be validated rather than tuned to manufacture trade counts.
