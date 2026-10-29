from __future__ import annotations

from datetime import datetime, timezone, timedelta
import pytest

from money_mintter.strategy.candles import Candle
from money_mintter.core.movement import (
    compute_wilder_atr_14,
    compute_q1,
    compute_q3,
    is_same_session,
)


def test_candle_creation_and_malformed() -> None:
    t = datetime(2026, 10, 1, 9, 30, tzinfo=timezone.utc)
    c = Candle("AAPL", t, 100.0, 102.0, 99.0, 101.0, 1000.0)
    assert c.range == 3.0

    with pytest.raises(ValueError):
        Candle("AAPL", t, -100.0, 102.0, 99.0, 101.0, 1000.0)
    with pytest.raises(ValueError):
        Candle("AAPL", t, 100.0, 98.0, 99.0, 101.0, 1000.0)


def test_atr_availability_boundary() -> None:
    start = datetime(2026, 10, 1, 9, 30, tzinfo=timezone.utc)
    bars = []
    for i in range(20):
        ts = start + timedelta(minutes=i)
        bars.append(Candle("AAPL", ts, 100.0, 101.0, 99.0, 100.0, 1000.0))

    # ATR-14 is unavailable before 14 prior valid TR observations (< 14)
    assert compute_wilder_atr_14(bars, 13) is None
    # ATR-14 is available when exactly 14 prior valid TR observations exist (up_to_index = 14)
    assert compute_wilder_atr_14(bars, 14) is not None


def test_q1_availability_boundary() -> None:
    start = datetime(2026, 10, 1, 9, 30, tzinfo=timezone.utc)
    bars = []
    for i in range(20):
        ts = start + timedelta(minutes=i)
        bars.append(Candle("AAPL", ts, 100.0, 101.0, 99.0, 100.0, 1000.0))

    # q1 unavailable at index 13 (requires t >= 14)
    assert compute_q1(bars, 13) is None
    # q1 available at index 14
    assert compute_q1(bars, 14) is not None


def test_q3_availability_boundary() -> None:
    start = datetime(2026, 10, 1, 9, 30, tzinfo=timezone.utc)
    bars = []
    for i in range(25):
        ts = start + timedelta(minutes=i)
        bars.append(Candle("AAPL", ts, 100.0, 101.0, 99.0, 100.0, 1000.0))

    # q3 unavailable at index 15 (requires t >= 16 so t-2 >= 14)
    assert compute_q3(bars, 15) is None
    # q3 available at index 16 with sufficient history and valid 3-bar window
    assert compute_q3(bars, 16) is not None


def test_atr_initialization_and_smoothing() -> None:
    start = datetime(2026, 10, 1, 9, 30, tzinfo=timezone.utc)
    bars = []
    for i in range(20):
        ts = start + timedelta(minutes=i)
        bars.append(Candle("AAPL", ts, 100.0, 101.0, 99.0, 100.0, 1000.0))

    atr_14 = compute_wilder_atr_14(bars, 14)
    assert atr_14 is not None
    assert pytest.approx(atr_14, rel=1e-5) == 2.0

    bars[14] = Candle("AAPL", start + timedelta(minutes=14), 100.0, 103.0, 99.0, 101.0, 1000.0)
    atr_15 = compute_wilder_atr_14(bars, 15)
    assert atr_15 is not None
    expected = (2.0 * 13 + 4.0) / 14
    assert pytest.approx(atr_15, rel=1e-5) == expected


def test_movement_ratios_hand_calculated() -> None:
    start = datetime(2026, 10, 1, 9, 30, tzinfo=timezone.utc)
    bars = []
    for i in range(15):
        ts = start + timedelta(minutes=i)
        bars.append(Candle("AAPL", ts, 100.0, 101.0, 99.0, 100.0, 1000.0))

    bars.append(Candle("AAPL", start + timedelta(minutes=15), 100.0, 104.0, 98.0, 102.0, 1000.0))

    q1 = compute_q1(bars, 15)
    assert q1 is not None
    assert pytest.approx(q1, rel=1e-5) == 3.0


def test_exclusion_of_evaluated_event_from_baseline() -> None:
    start = datetime(2026, 10, 1, 9, 30, tzinfo=timezone.utc)
    bars = []
    for i in range(15):
        ts = start + timedelta(minutes=i)
        bars.append(Candle("AAPL", ts, 100.0, 101.0, 99.0, 100.0, 1000.0))

    bars.append(Candle("AAPL", start + timedelta(minutes=15), 100.0, 115.0, 95.0, 100.0, 1000.0))

    atr_before = compute_wilder_atr_14(bars, 15)
    assert atr_before is not None
    assert pytest.approx(atr_before, rel=1e-5) == 2.0


def test_three_bar_ratio() -> None:
    start = datetime(2026, 10, 1, 9, 30, tzinfo=timezone.utc)
    bars = []
    for i in range(20):
        ts = start + timedelta(minutes=i)
        bars.append(Candle("AAPL", ts, 100.0, 101.0, 99.0, 100.0, 1000.0))

    bars[14] = Candle("AAPL", start + timedelta(minutes=14), 100.0, 102.0, 98.0, 100.0, 1000.0)
    bars[15] = Candle("AAPL", start + timedelta(minutes=15), 100.0, 103.0, 97.0, 100.0, 1000.0)
    bars[16] = Candle("AAPL", start + timedelta(minutes=16), 100.0, 102.0, 99.0, 100.0, 1000.0)

    q3 = compute_q3(bars, 16)
    assert q3 is not None
    assert pytest.approx(q3, rel=1e-5) == 3.0


def test_session_boundary_timestamp_gap_fallback() -> None:
    start = datetime(2026, 10, 1, 15, 59, tzinfo=timezone.utc)
    bars = []
    for i in range(18):
        ts = start + timedelta(minutes=i)
        bars.append(Candle("AAPL", ts, 100.0, 101.0, 99.0, 100.0, 1000.0))

    # Bar 18 has a timestamp gap (> 1.5 minutes) simulating overnight/session boundary
    next_day_start = start + timedelta(hours=15)
    bars.append(Candle("AAPL", next_day_start, 150.0, 155.0, 145.0, 152.0, 1000.0))

    assert is_same_session(bars[17], bars[18]) is False

    atr = compute_wilder_atr_14(bars, 19)
    assert atr is not None
    
    # q3 across a session boundary gap should return None (not same session)
    assert compute_q3(bars, 18) is None
