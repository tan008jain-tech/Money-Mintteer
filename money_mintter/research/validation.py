from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable, Iterable
from zoneinfo import ZoneInfo

from money_mintter.research.backtest import BacktestConfig, BacktestReport, StrategyBacktester
from money_mintter.strategy import Candle, LiquiditySweepReversalStrategy


@dataclass(frozen=True, slots=True)
class WalkForwardWindow:
    train_start: int
    train_end: int
    test_start: int
    test_end: int


@dataclass(frozen=True, slots=True)
class ValidationResult:
    window: WalkForwardWindow
    train_report: BacktestReport
    test_report: BacktestReport


class WalkForwardValidator:
    """Run chronological train/test measurements without shuffling.

    The current MTF backtester derives all higher-timeframe context directly
    from 1-minute history. Each evaluation segment therefore receives the full
    history available up to that segment's end, while evaluation_start/end
    controls which dates contribute to the measured report.
    """

    def __init__(self, backtest_config: BacktestConfig | None = None) -> None:
        self.backtest_config = backtest_config or BacktestConfig()

    def windows(
        self,
        total_bars: int,
        *,
        train_size: int,
        test_size: int,
        step: int | None = None,
    ) -> tuple[WalkForwardWindow, ...]:
        if min(total_bars, train_size, test_size) <= 0:
            raise ValueError("window sizes must be positive")
        step = step or test_size
        if step <= 0:
            raise ValueError("step must be positive")

        result: list[WalkForwardWindow] = []
        train_start = 0
        while train_start + train_size + test_size <= total_bars:
            result.append(
                WalkForwardWindow(
                    train_start,
                    train_start + train_size,
                    train_start + train_size,
                    train_start + train_size + test_size,
                )
            )
            train_start += step
        return tuple(result)

    def evaluate(
        self,
        candles: list[Candle],
        *,
        windows: Iterable[WalkForwardWindow],
        strategy_factory: Callable[[], LiquiditySweepReversalStrategy],
    ) -> tuple[ValidationResult, ...]:
        if not candles:
            raise ValueError("walk-forward evaluation requires candles")

        ordered = sorted(candles, key=lambda candle: candle.timestamp)
        timezone_name = self.backtest_config.timezone_name
        results: list[ValidationResult] = []

        for window in windows:
            if not 0 <= window.train_start < window.train_end <= window.test_start < window.test_end <= len(ordered):
                raise ValueError("walk-forward window is outside candle bounds")

            train_slice = ordered[:window.train_end]
            test_slice = ordered[:window.test_end]

            market_tz = ZoneInfo(timezone_name)
            train_start_date = train_slice[window.train_start].timestamp.astimezone(market_tz).date()
            train_end_date = train_slice[window.train_end - 1].timestamp.astimezone(market_tz).date()
            test_start_date = ordered[window.test_start].timestamp.astimezone(market_tz).date()
            test_end_date = ordered[window.test_end - 1].timestamp.astimezone(market_tz).date()

            train_config = replace(
                self.backtest_config,
                evaluation_start=train_start_date,
                evaluation_end=train_end_date,
            )
            test_config = replace(
                self.backtest_config,
                evaluation_start=test_start_date,
                evaluation_end=test_end_date,
            )

            train_report = StrategyBacktester(
                strategy_factory(),
                train_config,
            ).run(train_slice)

            test_report = StrategyBacktester(
                strategy_factory(),
                test_config,
            ).run(test_slice)

            results.append(
                ValidationResult(
                    window=window,
                    train_report=train_report,
                    test_report=test_report,
                )
            )

        return tuple(results)
