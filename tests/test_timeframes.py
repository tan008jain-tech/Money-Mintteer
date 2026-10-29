from datetime import datetime, timedelta, timezone
import pytest

from money_mintter.strategy import Candle, MultiTimeframeSeries, Timeframe


def candle(index: int, price: float = 100.0) -> Candle:
    return Candle(
        "ABC",
        datetime(2026, 10, 1, 9, 30, tzinfo=timezone.utc) + timedelta(minutes=index * 5),
        price,
        price + 0.4,
        price - 0.4,
        price + 0.1,
        100,
    )


def test_five_minute_base_aggregation():
    candles = [candle(i, 100 + i * 0.1) for i in range(10)]
    series = MultiTimeframeSeries.from_5m(
        candles,
        entry_timeframe=Timeframe.FIVE_MIN,
        timezone_name="UTC",
    )
    assert len(series.entry.bars) == 10
    first = series.entry.bars[0]
    assert first.open == candles[0].open
    assert first.close == candles[0].close
    assert first.volume == 100


def test_one_minute_series_creation():
    candles = [Candle("ABC", datetime(2026, 10, 1, 9, 30, tzinfo=timezone.utc) + timedelta(minutes=i), 100.0+i*0.1, 101.0+i*0.1, 99.0+i*0.1, 100.5+i*0.1, 10) for i in range(30)]
    series = MultiTimeframeSeries.from_1m(
        candles,
        entry_timeframe=Timeframe.FIVE_MIN,
        timezone_name="UTC",
    )
    assert series.one_minute is not None
    assert len(series.one_minute.bars) == 30
    assert len(series.entry.bars) > 0


def test_mtf_levels_and_bias_summary():
    candles = [candle(i, 100 + i * 0.5) for i in range(20)]
    series = MultiTimeframeSeries.from_5m(
        candles,
        entry_timeframe=Timeframe.FIVE_MIN,
        timezone_name="UTC",
    )
    snapshot = series.snapshot_at(series.entry.bars[-1].timestamp + timedelta(minutes=5))
    assert snapshot.one_hour_levels
    assert snapshot.fifteen_minute_levels
    assert snapshot.projected_levels
    assert "5M Chart Bias" in snapshot.candle_bias_summary
