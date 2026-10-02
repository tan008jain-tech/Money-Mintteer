from __future__ import annotations

import argparse
from datetime import date

from money_mintter.data.hf import HfCandleLoader, HfDataError
from money_mintter.research.backtest import BacktestConfig, StrategyBacktester
from money_mintter.research.context import PreviousSessionContext
from money_mintter.strategy import LiquiditySweepReversalStrategy


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the Money Mintter strategy on an HF 1-minute CSV."
    )
    parser.add_argument("csv_path")
    parser.add_argument("symbol")
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD")
    parser.add_argument("--timezone", default="America/New_York")
    parser.add_argument("--spread-bps", type=float, default=5.0)
    parser.add_argument("--slippage-bps", type=float, default=5.0)
    parser.add_argument("--commission-bps", type=float, default=2.0)
    parser.add_argument("--max-holding-bars", type=int, default=60)
    args = parser.parse_args()

    try:
        start = date.fromisoformat(args.start)
        end = date.fromisoformat(args.end)
    except ValueError as exc:
        parser.error(f"dates must use YYYY-MM-DD: {exc}")

    if start > end:
        parser.error("--start cannot be after --end")

    try:
        candles = HfCandleLoader().load(
            args.csv_path,
            args.symbol,
            start=start,
            end=end,
            timezone_name=args.timezone,
        )
    except (OSError, HfDataError, ValueError) as exc:
        print(f"HF backtest data error: {exc}")
        return 1

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
            max_holding_bars=args.max_holding_bars,
        ),
    ).run(candles, lambda index: context(index, candles))

    print(f"Symbol: {args.symbol.upper()}")
    print(f"Period: {start} to {end}")
    print(f"Candles: {len(candles)}")
    print(f"Trades: {report.trade_count}")
    print(f"Wins / losses: {report.wins} / {report.losses}")
    print(f"Win rate: {report.win_rate:.2%}")
    print(f"Profit factor: {report.profit_factor:.3f}")
    print(f"Expectancy (R): {report.expectancy_r:.3f}")
    print(f"Average win (R): {report.average_win_r:.3f}")
    print(f"Average loss (R): {report.average_loss_r:.3f}")
    print(f"Max drawdown: {report.max_drawdown_pct:.2f}%")
    print(f"Max consecutive losses: {report.max_consecutive_losses}")
    print(f"Net P&L: {report.net_pnl:.2f}")
    print(f"Return: {report.return_pct:.2f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
