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


def test_context_uses_only_previous_session_levels() -> None:
    candles = [
        candle(1, 0, 101, 99),
        candle(1, 1, 102, 98),
        candle(5, 0, 500, 1),
    ]
    context = PreviousSessionContext(candles, timezone_name="UTC")
    result = context(2, candles)

    assert result is not None
    assert result.prior_day_high == 102
    assert result.prior_day_low == 98


def test_negative_spread_is_rejected() -> None:
    try:
        PreviousSessionContext([], spread_bps=-1)
    except ValueError as exc:
        assert "spread" in str(exc)
    else:
        raise AssertionError("negative spread should fail")
