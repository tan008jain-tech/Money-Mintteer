from __future__ import annotations

from datetime import datetime, timezone, timedelta
import pytest

from money_mintter.domain import Side
from money_mintter.strategy.candles import Candle
from money_mintter.strategy.timeframes import MultiTimeframeSnapshot, Timeframe
from money_mintter.strategy.trend_continuation import TrendContinuationStrategy
from money_mintter.config import Settings


def test_trend_continuation_insufficient_candles() -> None:
    strategy = TrendContinuationStrategy()
    now = datetime.now(timezone.utc)
    snapshot = MultiTimeframeSnapshot(
        as_of=now,
        daily=(),
        four_hour=(),
        one_hour=(),
        fifteen_minute=(),
        entry=(),
        entry_timeframe=Timeframe.FIVE_MIN,
        spread_bps=5.0,
    )
    analysis = strategy.analyze(snapshot)
    assert not analysis.passed


def test_trend_continuation_genuine_uptrend_sequence() -> None:
    strategy = TrendContinuationStrategy()
    base_time = datetime(2026, 10, 27, 10, 0, tzinfo=timezone.utc)
    candles = []
    price = 100.0
    for i in range(30):
        price += 0.5
        t = base_time + timedelta(minutes=5 * i)
        candles.append(Candle("AAPL", t, price, price + 0.4, price - 0.2, price + 0.3, 1000.0))

    # Add 5-bar local breakout
    t_break = base_time + timedelta(minutes=150)
    candles.append(Candle("AAPL", t_break, 115.0, 117.0, 114.8, 116.5, 2000.0))

    # Add counter-trend pullback (bearish bar)
    t_pull = base_time + timedelta(minutes=155)
    candles.append(Candle("AAPL", t_pull, 116.5, 116.8, 115.0, 115.2, 1200.0))

    # Add resumption (bullish bar closing above previous high)
    t_res = base_time + timedelta(minutes=160)
    candles.append(Candle("AAPL", t_res, 115.2, 118.5, 115.0, 118.0, 3000.0))

    snapshot = MultiTimeframeSnapshot(
        as_of=t_res,
        daily=(),
        four_hour=(),
        one_hour=(),
        fifteen_minute=(),
        entry=tuple(candles),
        entry_timeframe=Timeframe.FIVE_MIN,
        spread_bps=5.0,
    )
    signal = strategy.signal(snapshot)
    assert signal is not None
    assert signal.side == Side.BUY
    assert signal.strategy_version == "tc.mtf.v1"
    assert signal.stop_price < signal.entry_price
    assert signal.target_price > signal.entry_price
    assert pytest.approx(signal.target_price - signal.entry_price, rel=1e-2) == 2.0 * (signal.entry_price - signal.stop_price)


def test_trend_continuation_negative_no_resumption() -> None:
    strategy = TrendContinuationStrategy()
    base_time = datetime(2026, 10, 27, 10, 0, tzinfo=timezone.utc)
    candles = []
    price = 100.0
    for i in range(32):
        price += 0.5
        t = base_time + timedelta(minutes=5 * i)
        candles.append(Candle("AAPL", t, price, price + 0.4, price - 0.2, price + 0.3, 1000.0))

    # Make last candle bearish (no resumption)
    last = candles[-1]
    candles[-1] = Candle("AAPL", last.timestamp, last.open, last.high, last.low - 2.0, last.open - 1.5, 1000.0)

    snapshot = MultiTimeframeSnapshot(
        as_of=base_time + timedelta(minutes=160),
        daily=(),
        four_hour=(),
        one_hour=(),
        fifteen_minute=(),
        entry=tuple(candles),
        entry_timeframe=Timeframe.FIVE_MIN,
        spread_bps=5.0,
    )
    signal = strategy.signal(snapshot)
    assert signal is None


def test_default_config_disabled() -> None:
    settings = Settings()
    assert settings.enable_trend_continuation is False
