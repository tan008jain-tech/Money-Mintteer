# Money Mintter Strategy Baseline

## Purpose

This is the first machine-testable strategy for Money Mintter. It is deliberately conservative because the system is intended to prioritize trade quality and risk control over signal count.

The design translates a liquidity-trap / liquidity-sweep reversal concept into deterministic rules that can later be measured against real historical data. It is not treated as proven profitable until it survives out-of-sample testing with realistic spread, slippage, fees, market sessions, and execution delays.

## Research translation

The source material reviewed for this build consistently emphasizes liquidity traps/sweeps, price action, market structure, confirmation, risk management, and avoiding weak conditions. A June 2026 public strategy description specifically frames the setup around liquidity sweeps, smart-money concepts, price action, and NIFTY/SENSEX examples.

The exact source methodology is not published as a formal machine specification, so this repository uses an explicit engineering translation rather than pretending an informal chart-reading method is already an algorithm.

## Long setup

The first production target is long-only.

1. **Reference level**
   The prior trading day's low is the primary sell-side liquidity level.

2. **Liquidity sweep**
   Price must trade below that low by a meaningful but not excessive amount, then close back above it. The wick must be substantial relative to the candle range.

3. **Market structure shift**
   After the sweep, price must close above the strongest recent pre-sweep structural high.

4. **Displacement**
   The structure-shift candle must have a strong bullish body. Volume confirmation is required when volume data is available.

5. **Confirmation break**
   A later candle must break the displacement candle high and close above it. A wick-only break is rejected.

6. **No-chase filter**
   The confirmation close cannot be too far above the displacement high relative to ATR.

7. **Structural stop**
   Stop goes below the sweep/MSS structure with a small ATR buffer. If the stop is too wide relative to ATR, reject the trade.

8. **Reward-room filter**
   Known resistance above the entry must leave at least the configured minimum R multiple. If no nearer resistance is known, the strategy uses the preferred R multiple as a provisional target.

9. **Quality score**
   Sweep rejection, displacement quality, relative volume, reward/risk, and imbalance confluence are combined into a single score. The setup must clear the quality threshold.

10. **Execution**
    The strategy emits a signal only after the confirmation candle closes. Historical tests must model the actual next executable price instead of assuming a perfect fill at the reference entry.

## Deliberate rejection rules

Money Mintter should reject:

- weak or non-reclaimed sweeps
- breakdown-like sweeps that penetrate too deeply
- stale reversals that fail to shift structure quickly
- weak displacement
- weak volume confirmation
- wick-only confirmation breaks
- overextended confirmation closes
- wide structural stops
- poor reward room
- wide spreads
- setups below the confidence threshold

A rejected setup is a normal outcome, not a failure of the system. Precision comes from refusing mediocre conditions.

## Candidate analyser

Before deep strategy analysis, the opportunity analyzer applies hard market-quality gates to externally supplied normalized candidate scores:

- liquidity >= 0.72
- activity >= 0.60
- usable volatility between 0.40 and 0.95
- signal-quality >= 0.65
- spread <= 20 bps when spread data is available
- combined score >= 0.74

The five-candidate deep-monitoring cap remains an orchestration policy outside this analyzer.

## What is intentionally *not* claimed yet

This baseline has no verified real-market win rate, expectancy, or drawdown estimate.

Those numbers require the research/backtesting layer to run the exact rules across timestamp-aligned historical data with:

- survivorship-aware universes
- corporate-action-adjusted prices
- exchange calendars
- realistic bid/ask spreads
- slippage
- fees
- execution latency
- next-bar/intrabar fill logic
- parameter walk-forward testing
- out-of-sample validation

Only after that process should parameters be changed from baseline defaults.
