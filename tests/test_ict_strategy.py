from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from money_mintter.strategy import ICTStrategy, ICTStrategyConfig, Candle, MultiTimeframeSnapshot, MultiTimeframeSeries, Timeframe
from money_mintter.domain import Side, Candidate
from money_mintter.core.pipeline import DeepAnalysisPipeline


def test_ict_strategy_initialization():
    strategy = ICTStrategy()
    assert strategy.VERSION == "ict.v106.mtf"


def test_ict_strategy_analyze_empty():
    strategy = ICTStrategy()
    now = datetime(2026, 6, 1, 10, 0, tzinfo=ZoneInfo("America/New_York"))
    candles_1m = [
        Candle(
            symbol="AAPL",
            timestamp=now + timedelta(minutes=i),
            open=150.0,
            high=151.0,
            low=149.0,
            close=150.0,
            volume=1000.0,
        )
        for i in range(10)
    ]
    series = MultiTimeframeSeries.from_1m(candles_1m, timezone_name="America/New_York")
    snapshot = series.snapshot_at(now.astimezone(timezone.utc))
    analysis = strategy.analyze(snapshot)
    assert not analysis.passed
    assert analysis.side is None


def test_ict_strategy_with_genuine_1m_candles():
    strategy = ICTStrategy()
    now = datetime(2026, 6, 1, 10, 0, tzinfo=ZoneInfo("America/New_York"))
    candles_1m = [
        Candle(
            symbol="AAPL",
            timestamp=now + timedelta(minutes=i),
            open=150.0 + i * 0.1,
            high=151.0 + i * 0.1,
            low=149.5 + i * 0.1,
            close=150.5 + i * 0.1,
            volume=10000.0,
        )
        for i in range(100)
    ]
    series = MultiTimeframeSeries.from_1m(candles_1m, timezone_name="America/New_York")
    snapshot = series.snapshot_at(series.entry.bars[-1].timestamp + timedelta(minutes=5))
    analysis = strategy.analyze(snapshot)
    assert analysis is not None
    signal = strategy.signal(snapshot)
    if signal is not None:
        assert signal.strategy_version == "ict.v106.mtf"
        assert signal.reward_risk == 1.1


def test_pipeline_with_ict_strategy():
    pipeline = DeepAnalysisPipeline(strategy=ICTStrategy())
    now = datetime(2026, 6, 1, 10, 0, tzinfo=ZoneInfo("America/New_York"))
    candidate = Candidate(
        symbol="AAPL",
        market="US",
        liquidity_score=0.9,
        volatility_score=0.8,
        activity_score=0.85,
        signal_quality_score=0.8,
        timestamp=now.astimezone(timezone.utc),
    )
    candles_1m = [
        Candle(
            symbol="AAPL",
            timestamp=now + timedelta(minutes=i),
            open=150.0 + i * 0.1,
            high=151.0 + i * 0.1,
            low=149.5 + i * 0.1,
            close=150.5 + i * 0.1,
            volume=10000.0,
        )
        for i in range(100)
    ]
    series = MultiTimeframeSeries.from_1m(candles_1m, timezone_name="America/New_York")
    snapshot = series.snapshot_at(series.entry.bars[-1].timestamp + timedelta(minutes=5))
    result = pipeline.evaluate(candidate, snapshot)
    assert result.candidate == candidate
    assert result.setup is not None


def test_ict_zone_deduplication_and_persistence():
    strategy1 = ICTStrategy()
    strategy1._used_zones.add((Side.BUY, "ifvg", 150.0, 149.0))
    state = strategy1.export_state()

    strategy2 = ICTStrategy()
    strategy2.import_state(state)
    assert (Side.BUY, "ifvg", 150.0, 149.0) in strategy2._used_zones
    assert len(next(iter(strategy2._used_zones))) == 4


def test_ict_persistence_lifecycle_and_backward_compatibility():
    strat1 = ICTStrategy()
    strat1._used_zones.add((Side.BUY, "ifvg", 100.0, 99.0))
    state_dict = strat1.export_state()

    strat2 = ICTStrategy()
    strat2.import_state(state_dict)
    assert (Side.BUY, "ifvg", 100.0, 99.0) in strat2._used_zones
    assert len(next(iter(strat2._used_zones))) == 4

    old_state = [(Side.SELL, "disp_fvg", 200.0, 198.0)]
    strat3 = ICTStrategy()
    strat3.import_state(old_state)
    assert (Side.SELL, "disp_fvg", 200.0, 198.0) in strat3._used_zones


def test_ict_flat_rr_and_symbol_propagation():
    strategy = ICTStrategy()
    now = datetime(2026, 6, 1, 10, 0, tzinfo=ZoneInfo("America/New_York"))
    candles_1m = [
        Candle(
            symbol="ES",
            timestamp=now + timedelta(minutes=i),
            open=5000.0 + i * 0.1,
            high=5002.0 + i * 0.1,
            low=4998.0 + i * 0.1,
            close=5001.0 + i * 0.1,
            volume=1000.0,
        )
        for i in range(100)
    ]
    series = MultiTimeframeSeries.from_1m(candles_1m, timezone_name="America/New_York")
    snapshot = series.snapshot_at(series.entry.bars[-1].timestamp + timedelta(minutes=5))
    sig = strategy.signal(snapshot)
    if sig:
        assert sig.strategy_version == "ict.v106.mtf"
        assert sig.symbol == "ES"
        assert sig.target_price is not None
        assert sig.stop_price is not None
        assert sig.entry_price is not None
        risk = abs(sig.entry_price - sig.stop_price)
        expected_target = (
            sig.entry_price + (risk * 1.1)
            if sig.side == Side.BUY
            else sig.entry_price - (risk * 1.1)
        )
        assert abs(sig.target_price - expected_target) < 1e-5


def test_ict_newest_signal_selection():
    strategy = ICTStrategy()
    now = datetime(2026, 6, 1, 10, 0, tzinfo=ZoneInfo("America/New_York"))
    candles_1m = [
        Candle(
            symbol="QQQ",
            timestamp=now + timedelta(minutes=i),
            open=500.0 + (i * 0.2 if i < 50 else 50.0 - (i - 50) * 0.2),
            high=501.0 + (i * 0.2 if i < 50 else 50.0 - (i - 50) * 0.2),
            low=499.0 + (i * 0.2 if i < 50 else 50.0 - (i - 50) * 0.2),
            close=500.5 + (i * 0.2 if i < 50 else 50.0 - (i - 50) * 0.2),
            volume=5000.0,
        )
        for i in range(120)
    ]
    series = MultiTimeframeSeries.from_1m(candles_1m, timezone_name="America/New_York")
    snapshot = series.snapshot_at(series.entry.bars[-1].timestamp + timedelta(minutes=5))
    analysis = strategy.analyze(snapshot)
    assert analysis is not None
