from __future__ import annotations

import argparse
from collections import Counter
from datetime import date

from money_mintter.data.hf import HfCandleLoader
from money_mintter.domain import Side
from money_mintter.research.context import PreviousSessionContext
from money_mintter.strategy import LiquiditySweepReversalStrategy


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnose both sides of LSR on HF candles.")
    parser.add_argument("csv_path")
    parser.add_argument("symbol")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--timezone", default="America/New_York")
    args = parser.parse_args()

    candles = HfCandleLoader().load(
        args.csv_path,
        args.symbol,
        start=date.fromisoformat(args.start),
        end=date.fromisoformat(args.end),
        timezone_name=args.timezone,
    )
    context = PreviousSessionContext(candles, timezone_name=args.timezone)
    strategy = LiquiditySweepReversalStrategy()

    for side, label in ((Side.BUY, "LONG"), (Side.SELL, "SHORT")):
        reasons: Counter[str] = Counter()
        vetoes: Counter[str] = Counter()
        passed = 0
        sweep_observations = 0
        mss_observations = 0
        trigger_observations = 0
        evaluations = 0

        for index in range(1, len(candles)):
            strategy_input = context(index, candles)
            if strategy_input is None:
                continue

            evaluations += 1
            result = strategy.analyze_side(candles[: index + 1], strategy_input, side)
            reasons[result.reason] += 1
            vetoes.update(result.vetoes)

            if result.sweep_index is not None:
                sweep_observations += 1
            if result.mss_index is not None:
                mss_observations += 1
            if result.trigger_index is not None:
                trigger_observations += 1
            if result.passed:
                passed += 1

        print(f"{label}:")
        print(f"  Strategy evaluations: {evaluations}")
        print(f"  Sweep observations: {sweep_observations}")
        print(f"  MSS observations: {mss_observations}")
        print(f"  Trigger observations: {trigger_observations}")
        print(f"  Passed setups: {passed}")
        print("  Top reasons:")
        for reason, count in reasons.most_common(10):
            print(f"    {count:6d}  {reason}")
        print("  Veto counts:")
        for veto, count in vetoes.most_common(10):
            print(f"    {count:6d}  {veto}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
