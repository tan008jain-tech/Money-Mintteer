from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, timedelta

from money_mintter.data.hf import HfCandleLoader, HfDataError
from money_mintter.domain import Side
from money_mintter.strategy import LiquiditySweepReversalStrategy, StrategyConfig, Timeframe
from money_mintter.strategy.timeframes import MultiTimeframeSeries


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Diagnose the multi-timeframe intraday strategy on HF 1-minute data."
    )
    parser.add_argument("csv_path")
    parser.add_argument("symbol")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--timezone", default="America/New_York")
    parser.add_argument("--entry-minutes", type=int, choices=(3, 5), default=5)
    parser.add_argument("--warmup-days", type=int, default=10)
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
        print(f"HF diagnostics data error: {exc}")
        return 1

    entry_tf = Timeframe.THREE_MIN if args.entry_minutes == 3 else Timeframe.FIVE_MIN
    series = MultiTimeframeSeries.from_1m(
        candles,
        entry_timeframe=entry_tf,
        timezone_name=args.timezone,
    )
    strategy = LiquiditySweepReversalStrategy(
        StrategyConfig(entry_timeframe=entry_tf)
    )

    print(
        "Architecture: 4H/daily filters -> 1H context -> 15M structure -> "
        f"{entry_tf.value} entry -> 1M execution"
    )
    print(f"Evaluation period: {start} to {end}")
    print(f"Warmup: {args.warmup_days} days")

    for side, label in ((Side.BUY, "LONG"), (Side.SELL, "SHORT")):
        reasons: Counter[str] = Counter()
        vetoes: Counter[str] = Counter()
        sweeps = mss = confirmations = passed = evaluations = 0

        for snapshot in series.snapshots(lookback_bars=96):
            local_date = snapshot.as_of.astimezone(
                snapshot.entry[-1].timestamp.tzinfo
            ).date()
            if not start <= local_date <= end:
                continue

            result = strategy.analyze_side(snapshot, side)
            evaluations += 1
            reasons[result.reason] += 1
            vetoes.update(result.vetoes)
            sweeps += int(result.sweep_index is not None)
            mss += int(result.mss_index is not None)
            confirmations += int(result.trigger_index is not None)
            passed += int(result.passed)

        print(f"{label}:")
        print(f"  Evaluations: {evaluations}")
        print(f"  Sweep observations: {sweeps}")
        print(f"  MSS observations: {mss}")
        print(f"  Confirmation observations: {confirmations}")
        print(f"  Passed setups: {passed}")
        print("  Top reasons:")
        for reason, count in reasons.most_common(12):
            print(f"    {count:6d}  {reason}")
        print("  Veto counts:")
        for veto, count in vetoes.most_common(12):
            print(f"    {count:6d}  {veto}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
