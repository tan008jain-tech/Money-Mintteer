from __future__ import annotations

import argparse

from money_mintter.data.csv import CandleCsvLoader
from money_mintter.research.backtest import BacktestConfig, StrategyBacktester
from money_mintter.research.context import PreviousSessionContext
from money_mintter.strategy import LiquiditySweepReversalStrategy


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a Money Mintter strategy backtest from OHLCV CSV.")
    parser.add_argument("csv_path")
    parser.add_argument("symbol")
    parser.add_argument("--timezone", default="UTC")
    parser.add_argument("--spread-bps", type=float, default=5.0)
    parser.add_argument("--slippage-bps", type=float, default=5.0)
    parser.add_argument("--commission-bps", type=float, default=2.0)
    args = parser.parse_args()

    candles = CandleCsvLoader().load(args.csv_path, args.symbol)
    context = PreviousSessionContext(
        candles,
        timezone_name=args.timezone,
        spread_bps=args.spread_bps,
    )
    report = StrategyBacktester(
        LiquiditySweepReversalStrategy(),
        BacktestConfig(
            spread_bps=args.spread_bps,
            slippage_bps=args.slippage_bps,
            commission_bps=args.commission_bps,
        ),
    ).run(candles, lambda index: context(index, candles))

    print(f"Trades: {report.trade_count}")
    print(f"Win rate: {report.win_rate:.2%}")
    print(f"Profit factor: {report.profit_factor:.3f}")
    print(f"Expectancy (R): {report.expectancy_r:.3f}")
    print(f"Max drawdown: {report.max_drawdown_pct:.2f}%")
    print(f"Net P&L: {report.net_pnl:.2f}")


if __name__ == "__main__":
    main()
