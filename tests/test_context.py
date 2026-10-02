from datetime import datetime, timezone

from money_mintter.research.context import PreviousSessionContext
from money_mintter.strategy import Candle


def candle(day: int, minute: int, high: float, low: float) -> Candle:
    return Candle(
        "ABC",
        datetime(2026, 10, day, 14, minute, tzinfo=timezone.utc),
        (high + low) / 2,
        high,
        low,
        (high + low) / 2,
        1000,
    )


def test_previous_session_context_uses_previous_available_day() -> None:
    candles = [
        candle(1, 0, 101, 99),
        candle(1, 1, 102, 98),
        candle(5, 0, 111, 108),
    ]
    context = PreviousSessionContext(candles, timezone_name="UTC")
    result = context(2, candles)

    assert result is not None
    assert result.prior_day_high == 102
    assert result.prior_day_low == 98


def test_intraday_levels_do_not_use_current_candle() -> None:
    candles = [candle(1, i, 100 + i, 99 - i) for i in range(35)]
    candles.append(candle(2, 0, 500, 1))

    context = PreviousSessionContext(candles, timezone_name="UTC")
    result = context(35, candles)

    assert result is not None
    assert 500 not in result.liquidity_highs
    assert 1 not in result.liquidity_lows


def test_intraday_levels_are_bounded() -> None:
    candles = [candle(1, i, 100 + (i % 5), 90 + (i % 5)) for i in range(50)]
    candles.append(candle(2, 0, 101, 89))

    context = PreviousSessionContext(
        candles,
        timezone_name="UTC",
        max_intraday_levels=4,
    )
    result = context(50, candles)

    assert result is not None
    assert len(result.liquidity_highs) <= 5
    assert len(result.liquidity_lows) <= 5
