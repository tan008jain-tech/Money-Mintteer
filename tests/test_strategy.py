from datetime import datetime, timedelta, timezone

from money_mintter.strategy import (
    Candle,
    LiquiditySweepReversalStrategy,
    StrategyInput,
)


def candle(
    symbol: str,
    index: int,
    open_price: float,
    high: float,
    low: float,
    close: float,
    volume: float = 1000.0,
) -> Candle:
    return Candle(
        symbol,
        datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc) + timedelta(minutes=index),
        open_price,
        high,
        low,
        close,
        volume,
    )


def build_setup() -> list[Candle]:
    bars = [candle("ABC", i, 99.2, 99.35, 99.0, 99.2) for i in range(30)]
    bars.extend(
        [
            candle("ABC", 30, 99.05, 99.25, 98.92, 99.12, 2200),
            candle("ABC", 31, 99.12, 99.45, 99.05, 99.40, 2500),
            candle("ABC", 32, 99.40, 99.60, 99.30, 99.50, 1800),
        ]
    )
    return bars


def test_detects_strict_long_setup() -> None:
    result = LiquiditySweepReversalStrategy().analyze(
        build_setup(),
        StrategyInput(99.0, 102.0, 5),
    )

    assert result.passed, result
    assert result.entry_price is not None
    assert result.stop_price is not None
    assert result.target_price is not None
    assert result.entry_price > result.stop_price
    assert result.target_price > result.entry_price
    assert result.reward_risk is not None and result.reward_risk >= 2.5
    assert (result.sweep_index, result.mss_index, result.trigger_index) == (30, 31, 32)


def test_rejects_without_structure_shift() -> None:
    bars = build_setup()
    bars[31] = candle("ABC", 31, 99.12, 99.24, 99.05, 99.20, 2500)
    result = LiquiditySweepReversalStrategy().analyze(
        bars,
        StrategyInput(99.0, 102.0, 5),
    )
    assert not result.passed


def test_rejects_wide_spread() -> None:
    result = LiquiditySweepReversalStrategy().analyze(
        build_setup(),
        StrategyInput(99.0, 102.0, 30),
    )
    assert not result.passed
    assert "spread" in result.vetoes


def test_rejects_overextended_breakout() -> None:
    bars = build_setup()
    bars[32] = candle("ABC", 32, 99.4, 102.0, 99.3, 101.9, 1800)
    result = LiquiditySweepReversalStrategy().analyze(
        bars,
        StrategyInput(99.0, 102.0, 5),
    )
    assert not result.passed
    assert "chase" in result.vetoes or "insufficient_room" in result.vetoes


def test_rejects_deep_breakdown_like_sweep() -> None:
    bars = build_setup()
    bars[30] = candle("ABC", 30, 99.05, 99.25, 95.0, 99.12, 2200)
    result = LiquiditySweepReversalStrategy().analyze(
        bars,
        StrategyInput(99.0, 102.0, 5),
    )
    assert not result.passed


def test_signal_builds_long_trade_only() -> None:
    signal = LiquiditySweepReversalStrategy().signal(
        build_setup(),
        StrategyInput(99.0, 102.0, 5),
    )
    assert signal is not None
    assert signal.side.value == "BUY"


def test_does_not_reemit_old_trigger() -> None:
    bars = build_setup()
    bars.append(candle("ABC", 33, 99.5, 99.55, 99.2, 99.4, 1600))
    result = LiquiditySweepReversalStrategy().analyze(
        bars,
        StrategyInput(99.0, 102.0, 5),
    )
    assert not result.passed
    assert "stale_trigger" in result.vetoes
