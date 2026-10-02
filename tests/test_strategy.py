from datetime import datetime, timedelta, timezone

from money_mintter.domain import Side
from money_mintter.strategy import Candle, LiquiditySweepReversalStrategy, StrategyInput


def candle(symbol, index, open_price, high, low, close, volume=1000.0):
    return Candle(
        symbol,
        datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc) + timedelta(minutes=index),
        open_price, high, low, close, volume,
    )


def build_long_setup():
    bars = [candle("ABC", i, 99.2, 99.35, 99.0, 99.2) for i in range(30)]
    bars.extend([
        candle("ABC", 30, 99.05, 99.25, 98.92, 99.12, 2200),
        candle("ABC", 31, 99.12, 99.45, 99.05, 99.40, 2500),
        candle("ABC", 32, 99.40, 99.60, 99.30, 99.50, 1800),
    ])
    return bars


def build_short_setup():
    bars = [candle("ABC", i, 100.8, 101.0, 100.65, 100.8) for i in range(30)]
    bars.extend([
        candle("ABC", 30, 100.95, 101.08, 100.85, 100.88, 2200),
        candle("ABC", 31, 100.88, 101.0, 100.50, 100.55, 2500),
        candle("ABC", 32, 100.55, 100.70, 100.25, 100.40, 1800),
    ])
    return bars


def test_detects_long_setup():
    result = LiquiditySweepReversalStrategy().analyze(
        build_long_setup(), StrategyInput(99.0, 102.0, 5)
    )
    assert result.passed, result
    assert result.side is Side.BUY
    assert result.reward_risk is not None and result.reward_risk >= 1.5


def test_detects_short_setup():
    result = LiquiditySweepReversalStrategy().analyze(
        build_short_setup(), StrategyInput(97.0, 101.0, 5)
    )
    assert result.passed, result
    assert result.side is Side.SELL
    assert result.target_price < result.entry_price < result.stop_price


def test_detects_local_intraday_liquidity_without_prior_day_sweep():
    bars = [candle("ABC", i, 100.0, 100.50, 99.50, 100.0) for i in range(30)]
    bars.extend([
        candle("ABC", 30, 100.0, 100.1, 99.20, 99.75, 2200),
        candle("ABC", 31, 99.75, 101.00, 99.70, 100.85, 2500),
        candle("ABC", 32, 100.85, 101.20, 100.70, 101.10, 1800),
    ])
    context = StrategyInput(
        98.0, 105.0, 5, liquidity_lows=(99.50,), liquidity_highs=(101.50,)
    )
    result = LiquiditySweepReversalStrategy().analyze(bars, context)
    assert result.passed, result
    assert result.side is Side.BUY
    assert "intraday liquidity" in result.confluences


def test_signals_both_sides():
    long_signal = LiquiditySweepReversalStrategy().signal(
        build_long_setup(), StrategyInput(99.0, 102.0, 5)
    )
    short_signal = LiquiditySweepReversalStrategy().signal(
        build_short_setup(), StrategyInput(97.0, 101.0, 5)
    )
    assert long_signal is not None and long_signal.side is Side.BUY
    assert short_signal is not None and short_signal.side is Side.SELL
    assert long_signal.strategy_version == "lsr.v3"
    assert short_signal.strategy_version == "lsr.v3"


def test_rejects_wide_spread():
    result = LiquiditySweepReversalStrategy().analyze(
        build_long_setup(), StrategyInput(99.0, 102.0, 30)
    )
    assert not result.passed
    assert "spread" in result.vetoes


def test_short_path_reports_missing_sweep():
    result = LiquiditySweepReversalStrategy().analyze_side(
        build_long_setup(), StrategyInput(99.0, 102.0, 5), Side.SELL
    )
    assert not result.passed
    assert "no_short_sweep" in result.vetoes


def test_does_not_reemit_old_trigger():
    bars = build_long_setup()
    bars.append(candle("ABC", 33, 99.5, 99.55, 99.2, 99.4, 1600))
    result = LiquiditySweepReversalStrategy().analyze(
        bars, StrategyInput(99.0, 102.0, 5)
    )
    assert not result.passed
    assert "stale_trigger" in result.vetoes


def test_invalid_liquidity_level_is_rejected():
    try:
        StrategyInput(99.0, 102.0, 5, liquidity_lows=(0.0,))
    except ValueError as exc:
        assert "positive" in str(exc)
    else:
        raise AssertionError("invalid liquidity level should fail")
