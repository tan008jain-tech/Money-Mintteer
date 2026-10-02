from datetime import datetime, timedelta, timezone

from money_mintter.strategy import Candle, MultiTimeframeSeries, Timeframe


def candle(index: int, price: float = 100.0) -> Candle:
    return Candle(
        "ABC",
        datetime(2026, 10, 1, 9, 30, tzinfo=timezone.utc) + timedelta(minutes=index),
        price,
        price + 0.4,
        price - 0.4,
        price + 0.1,
        100,
    )


def test_five_minute_aggregation():
    candles = [candle(i, 100 + i * 0.1) for i in range(10)]
    series = MultiTimeframeSeries.from_1m(
        candles,
        entry_timeframe=Timeframe.FIVE_MIN,
        timezone_name="UTC",
    )
    assert len(series.entry.bars) == 2
    first = series.entry.bars[0]
    assert first.open == candles[0].open
    assert first.close == candles[4].close
    assert first.high == max(c.high for c in candles[:5])
    assert first.low == min(c.low for c in candles[:5])
    assert first.volume == 500


def test_snapshot_excludes_unclosed_entry_bar():
    candles = [candle(i) for i in range(7)]
    series = MultiTimeframeSeries.from_1m(
        candles,
        entry_timeframe=Timeframe.FIVE_MIN,
        timezone_name="UTC",
    )
    snapshots = series.snapshots()
    assert len(snapshots) == 1
    assert snapshots[0].entry[-1].close == candles[4].close
    assert snapshots[0].as_of == series.entry.bars[0].timestamp + timedelta(minutes=5)


def test_three_minute_entry_is_supported():
    candles = [candle(i) for i in range(9)]
    series = MultiTimeframeSeries.from_1m(
        candles,
        entry_timeframe=Timeframe.THREE_MIN,
        timezone_name="UTC",
    )
    assert len(series.entry.bars) == 3
