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

    daily = tuple(
        candle(
            start - timedelta(days=8 - i),
            100 + (0.6 if bullish else -0.6) * i,
            101 + (0.6 if bullish else -0.6) * i,
            99 + (0.6 if bullish else -0.6) * i,
            100.3 + (0.6 if bullish else -0.6) * i,
        )
        for i in range(8)
    )
    four_hour = tuple(
        candle(
            start - timedelta(hours=32 - i * 4),
            100 + (0.4 if bullish else -0.4) * i,
            100.7 + (0.4 if bullish else -0.4) * i,
            99.3 + (0.4 if bullish else -0.4) * i,
            100.2 + (0.4 if bullish else -0.4) * i,
        )
        for i in range(8)
    )

    if bullish:
        one_hour_prices = [
            101.0, 100.5, 100.0, 99.0, 100.5,
            101.0, 101.3, 102.0, 101.4, 100.9,
            101.2, 101.5,
        ]
        fifteen_prices = [
            101.0, 100.4, 100.1, 99.05, 100.6,
            101.1, 101.3, 102.02, 101.4, 100.9,
            101.2, 101.5,
        ]
    else:
        one_hour_prices = [
            99.0, 99.5, 100.0, 101.0, 100.5,
            99.8, 98.2, 97.0, 98.0, 98.4,
            97.5, 98.2,
        ]
        fifteen_prices = [
            99.0, 99.6, 100.0, 100.95, 100.4,
            99.8, 98.2, 97.05, 98.1, 98.5,
            97.6, 98.2,
        ]

    one_hour = tuple(
        candle(
            start - timedelta(hours=12 - i),
            price,
            price + 0.15,
            price - 0.15,
            price + (0.05 if bullish else -0.05),
        )
        for i, price in enumerate(one_hour_prices)
    )
    fifteen = tuple(
        candle(
            start - timedelta(minutes=180 - i * 15),
            price,
            price + 0.10,
            price - 0.10,
            price + (0.04 if bullish else -0.04),
        )
        for i, price in enumerate(fifteen_prices)
    )

    entry = [
        candle(
            start + timedelta(minutes=5 * i),
            100.0,
            100.4,
            99.6,
            100.0,
            1000,
        )
        for i in range(30)
    ]

    if bullish:
        entry[-3] = candle(
            start + timedelta(minutes=135),
            100.0,
            100.2,
            98.7,
            99.7,
            2200,
        )
        entry[-2] = candle(
            start + timedelta(minutes=140),
            99.7,
            101.4,
            99.6,
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
            101.3,
            99.9,
            100.2,
            2200,
        )
        entry[-2] = candle(
            start + timedelta(minutes=140),
            100.2,
            100.3,
            98.5,
            98.8,
            2500,
        )
        entry[-1] = candle(
            start + timedelta(minutes=145),
            98.8,
            98.9,
            97.8,
            97.9,
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
    result = LiquiditySweepReversalStrategy(
        StrategyConfig(max_stop_atr=5.0)
    ).analyze(snapshot)
    assert result.passed, result
    assert result.side is Side.BUY
    assert result.trigger_index == len(snapshot.entry) - 1
    assert any("1H" in item for item in result.confluences)
    assert result.target_percent is not None and result.target_percent > 0


def test_short_entry_is_supported():
    snapshot = build_snapshot(Side.SELL)
    result = LiquiditySweepReversalStrategy(
        StrategyConfig(max_stop_atr=5.0)
    ).analyze(snapshot)
    assert result.passed, result
    assert result.side is Side.SELL
    assert result.target_price < result.entry_price < result.stop_price


def test_daily_and_4h_are_context_not_a_blunt_veto():
    snapshot = build_snapshot(Side.BUY)
    result = LiquiditySweepReversalStrategy().analyze_side(snapshot, Side.BUY)
    assert "broad_trend_conflict" not in result.vetoes


def test_liquidity_confluence_is_explicit():
    snapshot = build_snapshot(Side.BUY)
    result = LiquiditySweepReversalStrategy(
        StrategyConfig(max_stop_atr=5.0)
    ).analyze(snapshot)
    assert result.passed, result
    assert any(
        "15M + 1H liquidity confluence" in item
        for item in result.confluences
    )


def test_risk_and_target_are_measured_in_percent():
    snapshot = build_snapshot(Side.BUY)
    result = LiquiditySweepReversalStrategy(
        StrategyConfig(max_stop_atr=5.0)
    ).analyze(snapshot)
    assert result.passed, result
    assert result.target_percent is not None and result.target_percent > 0
    assert result.risk_percent is not None and result.risk_percent > 0
    assert result.reward_risk is not None and result.reward_risk > 0


def test_wrong_entry_timeframe_is_rejected():
    snapshot = build_snapshot(Side.BUY)
    strategy = LiquiditySweepReversalStrategy(
        StrategyConfig(entry_timeframe=Timeframe.THREE_MIN)
    )
    result = strategy.analyze(snapshot)
    assert not result.passed
    assert "entry_timeframe" in result.vetoes
