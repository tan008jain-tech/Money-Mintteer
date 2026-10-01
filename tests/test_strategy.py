from datetime import datetime, timedelta, timezone

from money_mintter.strategy import (
    Candle,
    LiquiditySweepReversalStrategy,
    StrategyConfig,
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
    bars: list[Candle] = []
    for index in range(30):
        close = 100.02 if index % 2 == 0 else 99.98
        bars.append(candle("ABC", index, 100.0, 100.5, 99.5, close))

    # Sell-side liquidity sweep under the prior-day low.
    bars.append(candle("ABC", 30, 98.4, 99.1, 96.9, 98.7, 2200))

    # Bullish structure shift with displacement and elevated volume.
    bars.append(candle("ABC", 31, 98.7, 101.8, 98.5, 101.6, 2500))

    # Confirmation candle closes above the displacement high.
    bars.append(candle("ABC", 32, 101.6, 102.8, 101.2, 102.4, 1800))
    return bars


def test_detects_strict_long_setup() -> None:
    strategy = LiquiditySweepReversalStrategy(
        StrategyConfig(
            min_confidence=0.70,
            min_rr=2.5,
            preferred_rr=3.0,
            max_stop_atr=5.0,
            max_sweep_depth_atr=5.0,
        )
    )
    result = strategy.analyze(
        build_setup(),
        StrategyInput(prior_day_low=98, prior_day_high=125, spread_bps=5),
    )

    assert result.passed, result
    assert result.entry_price is not None
    assert result.stop_price is not None
    assert result.target_price is not None
    assert result.entry_price > result.stop_price
    assert result.target_price > result.entry_price
    assert result.reward_risk is not None and result.reward_risk >= 2.5
    assert result.sweep_index == 30
    assert result.mss_index == 31
    assert result.trigger_index == 32


def test_rejects_without_structure_shift() -> None:
    bars = build_setup()
    bars[31] = candle("ABC", 31, 98.7, 99.7, 98.5, 99.6, 2500)

    result = LiquiditySweepReversalStrategy().analyze(
        bars,
        StrategyInput(98, 125, 5),
    )

    assert not result.passed
    assert "no_mss" in result.vetoes or "displacement" in result.vetoes


def test_rejects_wide_spread() -> None:
    result = LiquiditySweepReversalStrategy().analyze(
        build_setup(),
        StrategyInput(98, 125, 30),
    )

    assert not result.passed
    assert "spread" in result.vetoes


def test_rejects_overextended_breakout() -> None:
    bars = build_setup()
    bars[32] = candle("ABC", 32, 101.6, 106.0, 101.2, 105.9, 1800)

    strategy = LiquiditySweepReversalStrategy(
        StrategyConfig(
            max_stop_atr=5.0,
            max_sweep_depth_atr=5.0,
            min_confidence=0.70,
        )
    )
    result = strategy.analyze(bars, StrategyInput(98, 125, 5))

    assert not result.passed
    assert "chase" in result.vetoes or "insufficient_room" in result.vetoes


def test_rejects_deep_breakdown_like_sweep() -> None:
    bars = build_setup()
    bars[30] = candle("ABC", 30, 98.4, 99.1, 90.0, 98.7, 2200)

    result = LiquiditySweepReversalStrategy().analyze(
        bars,
        StrategyInput(98, 125, 5),
    )

    assert not result.passed
    assert "no_sweep" in result.vetoes or "stop_too_wide" in result.vetoes


def test_signal_builds_long_trade_only() -> None:
    strategy = LiquiditySweepReversalStrategy(
        StrategyConfig(
            min_confidence=0.70,
            min_rr=2.5,
            preferred_rr=3.0,
            max_stop_atr=5.0,
            max_sweep_depth_atr=5.0,
        )
    )
    signal = strategy.signal(
        build_setup(),
        StrategyInput(98, 125, 5),
    )

    assert signal is not None
    assert signal.side.value == "BUY"


def test_does_not_reemit_old_trigger() -> None:
    bars = build_setup()
    bars.append(candle("ABC", 33, 102.4, 102.6, 101.9, 102.3, 1600))

    strategy = LiquiditySweepReversalStrategy(
        StrategyConfig(
            min_confidence=0.70,
            min_rr=2.5,
            preferred_rr=3.0,
            max_stop_atr=5.0,
            max_sweep_depth_atr=5.0,
        )
    )
    result = strategy.analyze(bars, StrategyInput(98, 125, 5))

    assert not result.passed
    assert "stale_trigger" in result.vetoes
