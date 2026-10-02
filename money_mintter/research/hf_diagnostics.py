from __future__ import annotations

import argparse
from collections import Counter
from datetime import date

from money_mintter.data.hf import HfCandleLoader
from money_mintter.research.context import PreviousSessionContext
from money_mintter.strategy import LiquiditySweepReversalStrategy


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnose LSR strategy vetoes on HF candles.")
    parser.add_argument("csv_path")
    parser.add_argument("symbol")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--timezone", default="America/New_York")
    args = parser.parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)

    candles = HfCandleLoader().load(
        args.csv_path,
        args.symbol,
        start=start,
        end=end,
        timezone_name=args.timezone,
    )
    context = PreviousSessionContext(candles, timezone_name=args.timezone)
    strategy = LiquiditySweepReversalStrategy()

    reasons: Counter[str] = Counter()
    vetoes: Counter[str] = Counter()
    passed = 0
    evaluations = 0

    for index in range(1, len(candles)):
        context_input = context(index, candles)
        if context_input is None:
            continue

        evaluations += 1
        result = strategy.analyze(candles[: index + 1], context_input)
        reasons[result.reason] += 1
        vetoes.update(result.vetoes)
        if result.passed:
            passed += 1

    print(f"Symbol: {args.symbol.upper()}")
    print(f"Period: {start} to {end}")
    print(f"Candles: {len(candles)}")
    print(f"Strategy evaluations: {evaluations}")
    print(f"Passed setups: {passed}")
    print("Top reasons:")
    for reason, count in reasons.most_common(15):
        print(f"  {count:6d}  {reason}")
    print("Veto counts:")
    for veto, count in vetoes.most_common(15):
        print(f"  {count:6d}  {veto}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
