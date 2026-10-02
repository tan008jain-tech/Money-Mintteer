from datetime import datetime, timedelta, timezone

from money_mintter.domain import Side
from money_mintter.strategy import (
    Candle,
    LiquiditySweepReversalStrategy,
    MultiTimeframeSnapshot,
    StrategyConfig,
    Timeframe,
)


def make_candle(timestamp, close, *, high_pad=0.5, low_pad=0.5, volume=1000):
    return Candle(
        "ABC",
        timestamp,
        close,
        close + high_pad,
        close - low_pad,
        close,
        volume,
    )


def build_snapshot(side: Side) -> MultiTimeframeSnapshot:
    start = datetime(2026, 10, 1, 9, 30, tzinfo=timezone.utc)
    direction = 1 if side is Side.BUY else -1

    daily = tuple(
        make_candle(start - timedelta(days=8 - i), 100 + direction * i)
        for i in range(8)
    )
    four_hour = tuple(
        make_candle(start - timedelta(hours=32 - i * 4), 100 + direction * i * 0.5)
        for i in range(8)
    )
    one_hour = tuple(
        make_candle(
            start - timedelta(hours=12 - i),
            100 + direction * i * 0.7,
            high_pad=0.25,
            low_pad=0.25,
        )
        for i in range(12)
    )
    fifteen = tuple(
        make_candle(
            start - timedelta(minutes=180 - i * 15),
            100 + direction * i * 0.25,
            high_pad=0.15,
            low_pad=0.15,
        )
        for i in range(12)
    )

    entry = [
        make_candle(start + timedelta(minutes=5 * i), 100.0, volume=1000)
        for i in range(30)
    ]

    if side is Side.BUY:
        entry[-8] = Candle(
            "ABC",
            start + timedelta(minutes=5 * 22),
            100.0,
            100.2,
            99.0,
            99.8,
            2200,
        )
        entry[-7] = Candle(
            "ABC",
            start + timedelta(minutes=5 * 23),
            99.8,
            101.4,
            99.7,
            101.2,
            2500,
        )
        entry[-6] = Candle(
            "ABC",
            start + timedelta(minutes=5 * 24),
            101.2,
            101.8,
            101.0,
            101.6,
            1800,
        )
    else:
        entry[-8] = Candle(
            "ABC",
            start + timedelta(minutes=5 * 22),
            100.0,
            101.0,
            100.8,
            100.2,
            2200,
        )
        entry[-7] = Candle(
            "ABC",
            start + timedelta(minutes=5 * 23),
            100.2,
            100.3,
            98.6,
            98.8,
            2500,
        )
        entry[-6] = Candle(
            "ABC",
            start + timedelta(minutes=5 * 24),
            98.8,
            99.0,
            98.0,
            98.5,
            1800,
        )

    return MultiTimeframeSnapshot(
        as_of=start + timedelta(minutes=150),
        daily=daily,
        four_hour=four_hour,
        one_hour=one_hour,
        fifteen_minute=fifteen,
        entry=tuple(entry),
        entry_timeframe=Timeframe.FIVE_MIN,
        spread_bps=5,
    )


def test_long_entry_uses_1h_15m_context_and_5m_trigger():
    result = LiquiditySweepReversalStrategy().analyze(build_snapshot(Side.BUY))
    assert result.passed, result
    assert result.side is Side.BUY
    assert result.trigger_index == len(build_snapshot(Side.BUY).entry) - 1
    assert any("1H trend aligned" == item for item in result.confluences)


def test_short_entry_is_supported():
    result = LiquiditySweepReversalStrategy().analyze(build_snapshot(Side.SELL))
    assert result.passed, result
    assert result.side is Side.SELL
    assert result.target_price < result.entry_price < result.stop_price


def test_daily_and_4h_can_block_countertrend():
    snapshot = build_snapshot(Side.BUY)
    result = LiquiditySweepReversalStrategy().analyze_side(snapshot, Side.SELL)
    assert "broad_trend_conflict" in result.vetoes


def test_wrong_entry_timeframe_is_rejected():
    snapshot = build_snapshot(Side.BUY)
    strategy = LiquiditySweepReversalStrategy(
        StrategyConfig(entry_timeframe=Timeframe.THREE_MIN)
    )
    result = strategy.analyze(snapshot)
    assert not result.passed
    assert "entry_timeframe" in result.vetoes
