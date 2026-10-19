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
        StrategyConfig(min_strategy_score=0.80, min_target_pct=0.2, min_liquidity_strength=2.0, max_stop_pct=5.0, max_stop_atr=5.0)
    ).analyze(snapshot)
    assert result.passed, result
    assert result.side is Side.BUY
    assert result.trigger_index == len(snapshot.entry) - 1
    assert any("1H" in item for item in result.confluences)
    assert result.target_percent is not None and result.target_percent > 0


def test_short_entry_is_supported():
    snapshot = build_snapshot(Side.SELL)
    result = LiquiditySweepReversalStrategy(
        StrategyConfig(min_strategy_score=0.80, min_target_pct=0.2, min_liquidity_strength=2.0, max_stop_pct=5.0, max_stop_atr=5.0)
    ).analyze(snapshot)
    assert result.passed, result
    assert result.side is Side.SELL
    assert result.target_price < result.entry_price < result.stop_price


def test_daily_and_4h_are_context_not_a_blunt_veto():
    snapshot = build_snapshot(Side.BUY)
    result = LiquiditySweepReversalStrategy(
        StrategyConfig(min_strategy_score=0.80, min_target_pct=0.2, min_liquidity_strength=2.0, max_stop_pct=5.0)
    ).analyze_side(snapshot, Side.BUY)
    assert "broad_trend_conflict" not in result.vetoes


def test_liquidity_confluence_is_explicit():
    snapshot = build_snapshot(Side.BUY)
    result = LiquiditySweepReversalStrategy(
        StrategyConfig(min_strategy_score=0.80, min_target_pct=0.2, min_liquidity_strength=2.0, max_stop_pct=5.0, max_stop_atr=5.0)
    ).analyze(snapshot)
    assert result.passed, result
    assert any(
        "15M + 1H liquidity confluence" in item
        for item in result.confluences
    )


def test_risk_and_target_are_measured_in_percent():
    snapshot = build_snapshot(Side.BUY)
    result = LiquiditySweepReversalStrategy(
        StrategyConfig(min_strategy_score=0.80, min_target_pct=0.2, min_liquidity_strength=2.0, max_stop_pct=5.0, max_stop_atr=5.0)
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


def test_confirmation_window_skips_failed_initial_breach():
    from money_mintter.strategy.liquidity_reversal import LiquidityZone
    config = StrategyConfig(
        entry_timeframe=Timeframe.FIVE_MIN,
        max_bars_after_sweep=8,
        max_bars_after_mss=5,
        min_displacement_body_fraction=0.1,
        min_strategy_score=0.60,
        min_target_pct=0.2,
        min_liquidity_strength=2.0,
        max_stop_pct=10.0,
        max_stop_atr=10.0,
    )
    strategy = LiquiditySweepReversalStrategy(config)
    now = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)
    entry_candles = [candle(now - timedelta(minutes=5 * i), 100, 101, 99, 100, 1000) for i in range(50)]
    entry_candles = list(reversed(entry_candles))

    entry_candles[40] = candle(now, 96.5, 96.5, 94.0, 96.0, 2000)
    for i in range(32, 40):
        entry_candles[i] = candle(now, 97.0, 98.0, 97.0, 97.5, 1000)
    entry_candles[41] = candle(now, 96.0, 98.5, 96.0, 98.5, 1500)
    entry_candles[42] = candle(now, 98.5, 99.0, 97.5, 98.0, 1000)
    entry_candles[43] = candle(now, 98.0, 99.5, 98.0, 99.5, 1200)

    snapshot = MultiTimeframeSnapshot(
        as_of=now,
        daily=tuple([candle(now, 100, 101, 99, 100)] * 8),
        four_hour=tuple([candle(now, 100, 101, 99, 100)] * 8),
        one_hour=tuple([candle(now, 100, 100, 95, 100, 1000)] * 10),
        fifteen_minute=tuple([candle(now, 100, 100, 95, 100, 1000)] * 10),
        entry=tuple(entry_candles[:44]),
        entry_timeframe=Timeframe.FIVE_MIN,
        spread_bps=1.0,
    )

    import money_mintter.strategy.liquidity_reversal as lr
    original_zones = lr._liquidity_zones
    try:
        lr._liquidity_zones = lambda *args, **kwargs: (LiquidityZone(95.0, ("1H",), 2.0), LiquidityZone(105.0, ("1H",), 2.0))
        result = strategy.analyze(snapshot)
        assert result.passed
        assert result.trigger_index == 43
    finally:
        lr._liquidity_zones = original_zones


def test_spread_friction_vetoes_small_targets():
    snapshot = build_snapshot(Side.BUY)
    snapshot = MultiTimeframeSnapshot(
        as_of=snapshot.as_of,
        daily=snapshot.daily,
        four_hour=snapshot.four_hour,
        one_hour=snapshot.one_hour,
        fifteen_minute=snapshot.fifteen_minute,
        entry=snapshot.entry,
        entry_timeframe=snapshot.entry_timeframe,
        spread_bps=15.0,
    )
    strategy = LiquiditySweepReversalStrategy(
        StrategyConfig(
            min_strategy_score=0.80,
            min_target_pct=0.25,
            max_spread_bps=20.0,
            min_reward_spread_multiple=20.0,
            min_liquidity_strength=2.0,
            max_stop_pct=5.0,
            max_stop_atr=5.0,
        )
    )
    result = strategy.analyze(snapshot)
    assert not result.passed
    assert "spread_friction" in result.vetoes


def test_strategy_cost_edge_and_reward_risk_gates() -> None:
    snapshot = build_snapshot(Side.BUY)

    # 1. High round_trip_cost_bps (100.0 * 2.0 = 2.0% threshold) -> target < 2.0% is vetoed with "cost_edge"
    strat_cost = LiquiditySweepReversalStrategy(
        StrategyConfig(
            min_strategy_score=0.80,
            min_target_pct=0.2,
            min_liquidity_strength=2.0,
            max_stop_pct=5.0,
            max_stop_atr=5.0,
            round_trip_cost_bps=100.0,
            min_net_edge_multiple=2.0,
        )
    )
    res_cost = strat_cost.analyze(snapshot)
    assert not res_cost.passed
    assert "cost_edge" in res_cost.vetoes

    # 2. Low round_trip_cost_bps (1.0 * 1.0 = 1.0% threshold or passing target) -> passes cost_edge gate
    strat_pass = LiquiditySweepReversalStrategy(
        StrategyConfig(
            min_strategy_score=0.80,
            min_target_pct=0.2,
            min_liquidity_strength=2.0,
            max_stop_pct=5.0,
            max_stop_atr=5.0,
            round_trip_cost_bps=1.0,
            min_net_edge_multiple=1.0,
        )
    )
    res_pass = strat_pass.analyze(snapshot)
    assert res_pass.passed
    assert "cost_edge" not in res_pass.vetoes

    # 3. min_reward_risk=20.0 vetoes setup with lower R:R
    strat_rr = LiquiditySweepReversalStrategy(
        StrategyConfig(
            min_strategy_score=0.80,
            min_target_pct=0.2,
            min_liquidity_strength=2.0,
            max_stop_pct=5.0,
            max_stop_atr=5.0,
            min_reward_risk=20.0,
        )
    )
    res_rr = strat_rr.analyze(snapshot)
    assert not res_rr.passed
    assert "reward_risk" in res_rr.vetoes

    # 4. min_reward_risk=None (disabled) does not veto
    strat_rr_dis = LiquiditySweepReversalStrategy(
        StrategyConfig(
            min_strategy_score=0.80,
            min_target_pct=0.2,
            min_liquidity_strength=2.0,
            max_stop_pct=5.0,
            max_stop_atr=5.0,
            min_reward_risk=None,
        )
    )
    res_rr_dis = strat_rr_dis.analyze(snapshot)
    assert res_rr_dis.passed
    assert "reward_risk" not in res_rr_dis.vetoes

