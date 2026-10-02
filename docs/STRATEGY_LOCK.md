# Money Mintter - Locked Strategy Definition

Status: LOCKED RESEARCH CONTRACT
Strategy family: Liquidity-driven short-duration intraday trading
Current implementation must conform to this document.

## 1. Purpose

Money Mintter is an intraday strategy engine. It is designed to capture shorter-duration moves created by liquidity sweeps/rejections and movement toward other meaningful liquidity.

It is NOT a swing-trading strategy.

The objective is not to predict the daily close. The objective is to identify an actionable intraday liquidity event, enter after the reaction is confirmed, manage risk tightly, take shorter-duration profit, and permit a fresh re-entry when a genuinely new setup appears.

Long and short trades are both part of the strategy.

## 2. Core market model

Liquidity is treated as a price area where orders are likely concentrated, not merely as a single exact support/resistance line.

A liquidity area becomes more important when:
- it is visible on a higher timeframe;
- multiple timeframes identify liquidity at or near the same area;
- the level has obvious structural significance;
- price has repeatedly interacted with the area;
- it represents a meaningful high/low, session extreme, previous-day extreme, or clustered highs/lows.

Higher-timeframe liquidity is stronger context, not an automatic trade signal.

A 15M liquidity level can be traded on its own when price produces a valid reaction. If a meaningful 1H liquidity level overlaps or sits very near that 15M level, the area receives stronger confluence.

The system must therefore reason about liquidity zones and their relative importance, not simply collect every swing high/low into one undifferentiated list.

## 3. Timeframe hierarchy

### Daily
Broad external context only.

Use it to identify large liquidity and understand whether price may be interacting with a larger-scale destination or obstacle.

Daily data must not directly generate an entry.

### 4H
Broad external context only.

Use it to identify larger liquidity that may influence an intraday move and to avoid treating an intraday setup as isolated from obvious higher-timeframe structure.

4H data must not directly generate an entry.

### 1H
Primary intraday map.

Use it to identify:
- important intraday liquidity;
- major swing highs/lows;
- significant range/session structure;
- the broader intraday tendency;
- larger liquidity near a 15M area.

The 1H chart provides context and confluence, not a standalone directional command.

### 15M
Secondary intraday map.

Use it to identify:
- developing/nearer liquidity;
- secondary swing highs/lows;
- local ranges and structural areas;
- liquidity that price is approaching during the active intraday move.

A 15M level becomes more important when it aligns with meaningful 1H liquidity.

### 5M / 3M
Entry timeframe.

This is where the actual setup must be confirmed.

The default entry timeframe is 5M. 3M is an allowed alternative for later testing.

The entry timeframe is responsible for:
- observing the sweep;
- confirming the reaction/reclaim;
- detecting the local structure shift/displacement;
- confirming the actual entry.

### 1M
Execution and tracking only.

1M may be used for:
- next-bar execution modelling;
- monitoring stops/targets;
- tracking an open position;
- optional later execution refinement.

1M must NOT be the primary timeframe used to discover the setup or decide that a trade exists.

## 4. Directional logic

The strategy does not say:

"1H is bullish, therefore buy."

Instead it asks:

"Where is liquidity, where is price approaching, what higher-timeframe liquidity is reinforcing this area, and what does price actually do when it reaches the area?"

Daily/4H information should influence context and confidence. It should not become a blunt rule that automatically blocks every countertrend intraday reaction merely because the larger chart points the other way.

A countertrend intraday trade may exist when the actual liquidity event and entry-timeframe reaction are valid.

## 5. Liquidity mapping process

Before looking for an entry, Money Mintter should build a point-in-time liquidity map.

The map should distinguish at least:
- Daily/4H major liquidity;
- 1H major intraday liquidity;
- 15M secondary intraday liquidity;
- previous-day high/low where relevant;
- meaningful session/range extremes;
- obvious clustered/equal highs and lows when detectable.

Levels must be weighted by timeframe and structural significance.

The system should be able to say conceptually:

"15M sell-side liquidity exists here, and important 1H sell-side liquidity overlaps or sits close to it."

That is a stronger area than an isolated minor 15M swing.

## 6. Liquidity sweep model

A sweep is NOT automatically a trade.

### Long scenario

The system identifies sell-side liquidity below price.

Price approaches the area and trades through the liquidity.

Then price rejects/reclaims the area rather than simply continuing lower.

The important event is:

sell-side liquidity taken -> reaction/reclaim -> potential move upward.

### Short scenario

The mirror image:

buy-side liquidity is identified above price.

Price trades through the liquidity.

Price then rejects/reclaims downward.

The important event is:

buy-side liquidity taken -> reaction/reclaim -> potential move downward.

A genuine continuation through the level must not be mislabeled as a successful sweep simply because the level was crossed.

## 7. Entry confirmation

The entry timeframe must confirm that the sweep produced a real reaction.

The intended conceptual sequence is:

liquidity area
-> approach
-> sweep
-> reclaim/rejection
-> local market-structure change / displacement
-> confirmation
-> entry

The exact numerical definitions for:
- swing lookback;
- wick size;
- ATR penetration;
- displacement size;
- volume threshold;
- confirmation window

are implementation parameters and are NOT permanently locked by this document.

They must be derived from this strategy concept and validated against historical data rather than chosen merely to manufacture a desired trade count.

## 8. Trade direction

Both directions are supported.

Long:
sell-side liquidity sweep followed by bullish reaction/confirmation.

Short:
buy-side liquidity sweep followed by bearish reaction/confirmation.

There is no permanent long-only restriction.

## 9. Trade destination / target model

Money Mintter should reason about where price may move next, not only about stop distance.

After a valid sweep and confirmation, identify the next meaningful opposing liquidity that may act as the destination/draw for the move.

Examples:
- nearby 15M liquidity;
- 1H liquidity;
- session extremes;
- previous-day extremes;
- other major mapped liquidity.

Percentage movement is the primary strategy-management concept requested for this system.

Risk/reward may be calculated and recorded as a research metric, but R:R must NOT replace the liquidity-based target concept.

## 10. Stop model

The stop must be structure-aware and relatively tight.

For a long, invalidation belongs beyond the relevant sweep/reaction structure.

For a short, invalidation belongs beyond the relevant sweep/reaction structure.

The system must not use an arbitrary fixed percentage stop simply because it is convenient.

The final numerical stop-buffer rules remain implementation parameters subject to validation.

## 11. Profit and position management

Money Mintter is intended for shorter-duration intraday trades.

The system should not force a trade to remain open merely to hit an arbitrary large R multiple.

Profit management should be based primarily on:
- percentage movement;
- progress toward mapped opposing liquidity;
- reaction quality;
- changing structure;
- continued validity of the original thesis.

A trade may be reduced/closed when the intended move has substantially played out even if a larger theoretical move remains possible.

Exact percentage thresholds are implementation parameters to be validated.

## 12. Re-entry

There is NO arbitrary cooldown after a trade exits.

A fresh trade is allowed when:
- the previous setup has ended;
- a new liquidity event occurs;
- the new setup has fresh confirmation;
- risk controls allow it.

A previous trade must not be reused as permission for an immediate duplicate entry without a new setup.

## 13. Multiple opportunities

The system should favour quality over forced trade quantity.

It may produce:
- many smaller intraday opportunities;
- several opportunities in the same symbol on the same day;
- no trade when no valid setup exists.

The system must never manufacture a trade merely to keep activity high.

## 14. What this strategy is NOT

The implementation must not silently drift into any of these:

- a 1-minute-only strategy;
- a swing-trading system;
- a generic moving-average trend strategy;
- "higher timeframe bullish = buy" logic;
- treating every swing high/low as equally important liquidity;
- using R:R as the primary definition of the setup;
- fixed-percentage stops unrelated to market structure;
- arbitrary cooldowns after exits;
- future-aware/look-ahead liquidity levels;
- numerical thresholds introduced only to produce a preferred number of trades.

## 15. Research and implementation rule

This document defines the strategy intent.

When implementation details are ambiguous, code must be brought back to this document rather than the strategy being silently changed to fit the code.

If a new implementation rule materially changes the market model, the rule must be explicitly reviewed and the locked definition updated before treating that rule as part of the official strategy.

Do not change this file as part of ordinary bug fixing.

## 16. Validation philosophy

A high number of signals is not success.

A low number of signals is not automatically failure.

The correct question is whether the implementation:
1. detects the intended liquidity situations;
2. distinguishes sweeps from continuation;
3. recognizes higher-timeframe confluence correctly;
4. confirms the reaction on 5M/3M;
5. manages risk as intended;
6. identifies plausible liquidity destinations;
7. produces positive out-of-sample expectancy after realistic execution costs.

Historical testing must remain causal and event-order-aware.

## 17. Permanent architecture contract

The intended production hierarchy is:

Daily / 4H broad context
-> 1H major intraday liquidity
-> 15M secondary liquidity
-> 5M/3M entry confirmation
-> 1M execution and tracking

This hierarchy is the baseline against which future strategy code must be reviewed.
