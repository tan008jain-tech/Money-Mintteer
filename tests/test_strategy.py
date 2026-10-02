from datetime import datetime, timedelta, timezone

from money_mintter.domain import Side
from money_mintter.strategy import (
    Candle,
    LiquiditySweepReversalStrategy,
    MultiTimeframeSnapshot,
    StrategyConfig,
    Timeframe,
)


def candle(ts, open_price, high, low, close, volume=1000):
    return Candle("ABC", ts, open_price, high, low, close, volume)


def build_snapshot(side: Side) -> MultiTimeframeSnapshot:
    start = datetime(2026, 10, 1, 9, 30, tzinfo=timezone.utc)
    bullish = side is Side.BUY
    direction = 1 if bullish else -1

    daily = tuple(
        candle(
            start - timedelta(days=8 - i),
            100 + direction * i,
            101 + direction * i,
            99 + direction * i,
            100.2 + direction * i,
        )
        for i in range(8)
    )
    four_hour = tuple(
        candle(
            start - timedelta(hours=32 - i * 4),
            100 + direction * i * 0.4,
            100.5 + direction * i * 0.4,
            99.5 + direction * i * 0.4,
            100.2 + direction * i * 0.4,
        )
        for i in range(8)
    )

    if bullish:
        one_hour_prices = [100.0, 100.4, 99.3, 101.0, 101.4, 101.8, 102.1, 102.4, 102.8, 103.2, 103.6, 104.0]
        fifteen_prices = [100.0, 100.2, 99.5, 100.8, 101.0, 101.3, 101.6, 101.9, 102.2, 102.5, 102.8, 103.0]
    else:
        one_hour_prices = [100.0, 99.6, 100.7, 99.0, 98.6, 98.2, 97.9, 97.6, 97.2, 96.9, 96.5, 96.2]
        fifteen_prices = [100.0, 99.8, 100.5, 99.2, 99.0, 98.7, 98.4, 98.1, 97.8, 97.5, 97.2, 97.0]

    one_hour = tuple(
        candle(
            start - timedelta(hours=12 - i),
            price,
            price + 0.15,
            price - 0.15,
            price + direction * 0.08,
        )
        for i, price in enumerate(one_hour_prices)
    )
    fifteen = tuple(
        candle(
            start - timedelta(minutes=180 - i * 15),
            price,
            price + 0.10,
            price - 0.10,
            price + direction * 0.05,
        )
        for i, price in enumerate(fifteen_prices)
    )

    entry = [
        candle(
            start + timedelta(minutes=5 * i),
            100.0,
            100.5,
            99.5,
            100.0,
        )
        for i in range(30)
    ]

    if bullish:
        entry[-3] = candle(
            start + timedelta(minutes=135),
            100.0,
            100.2,
            99.4,
            99.8,
            2200,
        )
        entry[-2] = candle(
            start + timedelta(minutes=140),
            99.8,
            101.4,
            99.7,
            101.2,
            2500,
        )
        entry[-1] = candle(
            start + timedelta(minutes=145),
            101.2,
            101.8,
            101.0,
            101.6,
            1800,
        )
    else:
        entry[-3] = candle(
            start + timedelta(minutes=135),
            100.0,
            100.6,
            99.8,
            100.2,
            2200,
        )
        entry[-2] = candle(
            start + timedelta(minutes=140),
            100.2,
            100.3,
            98.6,
            98.8,
            2500,
        )
        entry[-1] = candle(
            start + timedelta(minutes=145),
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
    snapshot = build_snapshot(Side.BUY)
    result = LiquiditySweepReversalStrategy().analyze(snapshot)
    assert result.passed, result
    assert result.side is Side.BUY
    assert result.trigger_index == len(snapshot.entry) - 1
    assert "1H trend aligned" in result.confluences


def test_short_entry_is_supported():
    snapshot = build_snapshot(Side.SELL)
    result = LiquiditySweepReversalStrategy().analyze(snapshot)
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
