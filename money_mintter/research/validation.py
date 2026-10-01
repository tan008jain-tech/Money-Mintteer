from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

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
    """Run fixed strategy variants over chronological train/test windows.

    The validator never shuffles time series and does not select parameters from
    the test segment. A caller supplies the already-selected strategy factory for
    each run, keeping model selection outside the test measurement.
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
        context_provider: Callable[[int], object],
    ) -> tuple[ValidationResult, ...]:
        results: list[ValidationResult] = []
        for window in windows:
            train = candles[window.train_start:window.train_end]
            test = candles[window.test_start:window.test_end]

            def train_context(index: int):
                return context_provider(window.train_start + index)

            def test_context(index: int):
                return context_provider(window.test_start + index)

            train_report = StrategyBacktester(
                strategy_factory(),
                self.backtest_config,
            ).run(train, train_context)
            test_report = StrategyBacktester(
                strategy_factory(),
                self.backtest_config,
            ).run(test, test_context)

            results.append(ValidationResult(window, train_report, test_report))
        return tuple(results)
