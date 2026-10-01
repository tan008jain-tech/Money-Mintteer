from datetime import datetime, timedelta, timezone

from money_mintter.research.backtest import BacktestConfig, StrategyBacktester
from money_mintter.strategy import (
    Candle,
    LiquiditySweepReversalStrategy,
    StrategyConfig,
    StrategyInput,
)


def bar(index: int, open_price: float, high: float, low: float, close: float, volume: float = 1000) -> Candle:
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
    bars = [
        bar(i, 100.0, 100.5, 99.5, 100.02 if i % 2 == 0 else 99.98)
        for i in range(30)
    ]
    bars.extend(
        [
            bar(30, 98.4, 99.1, 96.9, 98.7, 2200),
            bar(31, 98.7, 101.8, 98.5, 101.6, 2500),
            bar(32, 101.6, 102.8, 101.2, 102.4, 1800),
            bar(33, 102.4, 109.0, 102.0, 108.5, 2400),
            bar(34, 108.5, 109.0, 108.0, 108.7, 1200),
        ]
    )
    return bars


def context_provider(index: int) -> StrategyInput | None:
    return StrategyInput(98, 125, 5)


def test_signal_enters_on_next_bar_not_same_bar() -> None:
    strategy = LiquiditySweepReversalStrategy(
        StrategyConfig(
            min_confidence=0.70,
            min_rr=2.5,
            preferred_rr=3.0,
            max_stop_atr=5.0,
            max_sweep_depth_atr=5.0,
        )
    )
    report = StrategyBacktester(
        strategy,
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
    bars[33] = bar(33, 102.4, 120.0, 90.0, 108.5, 2400)

    strategy = LiquiditySweepReversalStrategy(
        StrategyConfig(
            min_confidence=0.70,
            min_rr=2.5,
            preferred_rr=3.0,
            max_stop_atr=5.0,
            max_sweep_depth_atr=5.0,
        )
    )
    report = StrategyBacktester(
        strategy,
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
