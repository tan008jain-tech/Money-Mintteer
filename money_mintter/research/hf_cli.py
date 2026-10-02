from __future__ import annotations

import argparse
from datetime import date, timedelta

from money_mintter.data.hf import HfCandleLoader, HfDataError
from money_mintter.research.backtest import BacktestConfig, StrategyBacktester
from money_mintter.strategy import LiquiditySweepReversalStrategy, StrategyConfig, Timeframe


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Money Mintter multi-timeframe intraday backtest.")
    parser.add_argument("csv_path")
    parser.add_argument("symbol")
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD")
    parser.add_argument("--timezone", default="America/New_York")
    parser.add_argument("--entry-minutes", type=int, choices=(3, 5), default=5)
    parser.add_argument("--warmup-days", type=int, default=10)
    parser.add_argument("--spread-bps", type=float, default=5.0)
    parser.add_argument("--slippage-bps", type=float, default=5.0)
    parser.add_argument("--commission-bps", type=float, default=2.0)
    parser.add_argument("--max-holding-minutes", type=int, default=120)
    args = parser.parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    if start > end:
        parser.error("--start cannot be after --end")
    if args.warmup_days < 0:
        parser.error("--warmup-days cannot be negative")

    try:
        candles = HfCandleLoader().load(
            args.csv_path,
            args.symbol,
            start=start - timedelta(days=args.warmup_days),
            end=end,
            timezone_name=args.timezone,
        )
    except (OSError, HfDataError, ValueError) as exc:
        print(f"HF backtest data error: {exc}")
        return 1

    entry_tf = Timeframe.THREE_MIN if args.entry_minutes == 3 else Timeframe.FIVE_MIN
    strategy = LiquiditySweepReversalStrategy(StrategyConfig(entry_timeframe=entry_tf))
    report = StrategyBacktester(
        strategy,
        BacktestConfig(
            spread_bps=args.spread_bps,
            slippage_bps=args.slippage_bps,
            commission_bps=args.commission_bps,
            max_holding_minutes=args.max_holding_minutes,
            evaluation_start=start,
            evaluation_end=end,
            timezone_name=args.timezone,
        ),
    ).run(candles)

    print(f"Symbol: {args.symbol.upper()}")
    print(f"Evaluation period: {start} to {end}")
    print(
        "Architecture: 4H/daily filters -> 1H context -> 15M structure -> "
        f"{args.entry_minutes}M entry -> 1M execution/tracking"
    )
    print(f"Warmup: {args.warmup_days} days")
    print(f"Raw candles loaded: {len(candles)}")
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
