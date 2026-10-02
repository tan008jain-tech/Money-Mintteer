# Money Mintter

Money Mintter is an event-driven market-analysis and paper-trading system designed to evolve toward autonomous multi-market trading.

## Current decision stack

- external coarse universe boundary;
- hard market-quality verification;
- maximum of five deep candidates;
- two-sided intraday liquidity-sweep strategy, lsr.v4;
- causal prior-session and intraday liquidity analysis;
- local market-structure shift, displacement, volume and close confirmation;
- ATR-based structural risk and reward-room checks;
- explicit veto reasons and deterministic confidence scoring;
- immediate re-entry allowed after a fresh confirmation.

## Research stack

- event-order-aware long/short backtester;
- spread, slippage and commission modelling;
- next-bar execution;
- pessimistic same-bar stop/target collision handling;
- profit factor, expectancy-R, drawdown and consecutive-loss metrics;
- chronological walk-forward validation;
- HF Data Library 1-minute historical adapter;
- causal session context and intraday liquidity calculation.

## Strategy warning

There is no verified profitability yet. Synthetic tests verify implementation behavior, not market edge. Real historical testing, realistic costs, multi-symbol coverage and out-of-sample validation are required before paper or live deployment.

## Temporary data stack

FMP supplies the coarse universe during the temporary development period.

HF Data Library supplies historical 1-minute research data.

IBKR can later replace the temporary provider layer without changing the strategy, risk or paper-trading interfaces.

## Live boundary

Money Mintter is fail-closed by default. Live broker credentials are not accepted through chat and are not committed to the repository.
