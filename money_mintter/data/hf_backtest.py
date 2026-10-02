from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from money_mintter.data.hf import HfCandleLoader
from money_mintter.research.backtest import BacktestConfig, BacktestReport, StrategyBacktester
from money_mintter.research.context import PreviousSessionContext
from money_mintter.strategy import LiquiditySweepReversalStrategy


@dataclass(frozen=True, slots=True)
class HfBacktestResult:
    candle_count: int
    report: BacktestReport


def run_hf_backtest(
    csv_path: str | Path,
    symbol: str,
    *,
    start: date,
    end: date,
    timezone_name: str = "America/New_York",
) -> HfBacktestResult:
    candles = HfCandleLoader().load(
        csv_path,
        symbol,
        start=start,
        end=end,
        timezone_name=timezone_name,
    )
    context = PreviousSessionContext(
        candles,
        timezone_name=timezone_name,
        spread_bps=5.0,
    )
    report = StrategyBacktester(
        LiquiditySweepReversalStrategy(),
        BacktestConfig(),
    ).run(candles, lambda index: context(index, candles))
    return HfBacktestResult(len(candles), report)
