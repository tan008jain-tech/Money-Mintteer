from datetime import datetime, timedelta, timezone

from money_mintter.research.backtest import BacktestConfig, StrategyBacktester
from money_mintter.strategy import Candle, LiquiditySweepReversalStrategy, StrategyConfig, StrategyInput


def bar(
    index: int,
    open_price: float,
    high: float,
    low: float,
    close: float,
    volume: float = 1000,
) -> Candle:
    return Candle(
        "ABC",
        datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc) + timedelta(minutes=index),
        open_price,
        high,
        low,
        close,
        volume,
    )


def dataset() -> list[Candle]:
    return [
        *(bar(i, 99.2, 99.35, 99.0, 99.2) for i in range(30)),
        bar(30, 99.05, 99.25, 98.92, 99.12, 2200),
        bar(31, 99.12, 99.45, 99.05, 99.40, 2500),
        bar(32, 99.40, 99.60, 99.30, 99.50, 1800),
        bar(33, 99.50, 103.0, 99.30, 102.60, 2400),
        bar(34, 102.60, 103.0, 102.0, 102.50, 1200),
    ]


def context_provider(index: int) -> StrategyInput | None:
    return StrategyInput(99.0, 102.0, 5)


def strategy() -> LiquiditySweepReversalStrategy:
    return LiquiditySweepReversalStrategy(
        StrategyConfig(
            min_confidence=0.70,
            min_rr=2.5,
            preferred_rr=3.0,
            max_stop_atr=2.0,
            max_sweep_depth_atr=1.5,
        )
    )


def test_signal_enters_on_next_bar_not_same_bar() -> None:
    report = StrategyBacktester(
        strategy(),
        BacktestConfig(
            initial_capital=10_000,
            spread_bps=0,
            slippage_bps=0,
            commission_bps=0,
        ),
    ).run(dataset(), context_provider)

    assert report.trade_count == 1
    trade = report.trades[0]
    assert trade.entry_index == 33
    assert trade.exit_reason == "target"
    assert trade.pnl > 0
    assert report.ending_capital > report.initial_capital


def test_same_bar_stop_and_target_uses_conservative_stop() -> None:
    bars = dataset()
    bars[33] = bar(33, 99.50, 110.0, 90.0, 102.60, 2400)

    report = StrategyBacktester(
        strategy(),
        BacktestConfig(
            initial_capital=10_000,
            spread_bps=0,
            slippage_bps=0,
            commission_bps=0,
        ),
    ).run(bars, context_provider)

    assert report.trade_count == 1
    assert report.trades[0].exit_reason == "stop_and_target_same_bar"
    assert report.trades[0].pnl < 0
