from datetime import datetime, timedelta, timezone

from money_mintter.domain import Side
from money_mintter.research.backtest import BacktestConfig, StrategyBacktester
from money_mintter.strategy import Candle, LiquiditySweepReversalStrategy, StrategyConfig, StrategyInput


def bar(index, open_price, high, low, close, volume=1000):
    return Candle(
        "ABC",
        datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc) + timedelta(minutes=index),
        open_price,
        high,
        low,
        close,
        volume,
    )


def long_dataset():
    return [
        *(bar(i, 99.2, 99.35, 99.0, 99.2) for i in range(30)),
        bar(30, 99.05, 99.25, 98.92, 99.12, 2200),
        bar(31, 99.12, 99.45, 99.05, 99.40, 2500),
        bar(32, 99.40, 99.60, 99.30, 99.50, 1800),
        bar(33, 99.50, 103.0, 99.30, 102.60, 2400),
        bar(34, 102.60, 103.0, 102.0, 102.50, 1200),
    ]


def short_dataset():
    return [
        *(bar(i, 100.8, 101.0, 100.65, 100.8) for i in range(30)),
        bar(30, 100.95, 101.25, 100.85, 100.88, 2200),
        bar(31, 100.88, 101.0, 100.50, 100.55, 2500),
        bar(32, 100.55, 100.70, 100.25, 100.40, 1800),
        bar(33, 100.40, 100.5, 96.5, 97.0, 2400),
        bar(34, 97.0, 97.5, 96.8, 97.2, 1200),
    ]


def context_long(_):
    return StrategyInput(99.0, 102.0, 0)


def context_short(_):
    return StrategyInput(98.0, 101.0, 0)


def strategy():
    return LiquiditySweepReversalStrategy(
        StrategyConfig(min_confidence=0.70, min_rr=2.5, preferred_rr=3.0)
    )


def config():
    return BacktestConfig(initial_capital=10_000, spread_bps=0, slippage_bps=0, commission_bps=0)


def test_long_backtest_still_works():
    report = StrategyBacktester(strategy(), config()).run(long_dataset(), context_long)
    assert report.trade_count == 1
    assert report.trades[0].side is Side.BUY
    assert report.trades[0].entry_index == 33
    assert report.trades[0].exit_reason == "target"
    assert report.trades[0].pnl > 0


def test_short_backtest_works():
    report = StrategyBacktester(strategy(), config()).run(short_dataset(), context_short)
    assert report.trade_count == 1
    assert report.trades[0].side is Side.SELL
    assert report.trades[0].entry_index == 33
    assert report.trades[0].exit_reason == "target"
    assert report.trades[0].pnl > 0


def test_both_sides_use_conservative_same_bar_collision():
    long_bars = long_dataset()
    long_bars[33] = bar(33, 99.50, 110.0, 90.0, 102.60, 2400)
    long_report = StrategyBacktester(strategy(), config()).run(long_bars, context_long)
    assert long_report.trades[0].exit_reason == "stop_and_target_same_bar"
    assert long_report.trades[0].pnl < 0

    short_bars = short_dataset()
    short_bars[33] = bar(33, 100.40, 102.0, 96.0, 97.0, 2400)
    short_report = StrategyBacktester(strategy(), config()).run(short_bars, context_short)
    assert short_report.trades[0].exit_reason == "stop_and_target_same_bar"
    assert short_report.trades[0].pnl < 0
