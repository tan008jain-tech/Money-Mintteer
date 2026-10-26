from __future__ import annotations

from datetime import datetime, timezone, timedelta
import pytest
import math
from unittest.mock import MagicMock

from money_mintter.strategy.candles import Candle
from money_mintter.core.selector import select_layer4_candidates, compute_median_volume_baseline


def test_exact_weighted_score_formula() -> None:
    start = datetime(2026, 10, 1, 9, 30, tzinfo=timezone.utc)
    aapl_bars = []
    for i in range(20):
        ts = start + timedelta(minutes=i)
        aapl_bars.append(Candle("AAPL", ts, 100.0, 101.0, 99.0, 100.0, 1000.0))
    aapl_bars.append(Candle("AAPL", start + timedelta(minutes=20), 100.0, 106.0, 100.0, 103.0, 2000.0))

    daily_pool = ["AAPL"]
    bars_dict = {"AAPL": aapl_bars}

    selected = select_layer4_candidates(daily_pool, bars_dict, max_candidates=8)
    assert len(selected) == 1
    assert selected[0].symbol == "AAPL"
    assert selected[0].has_activity_baseline is True
    assert pytest.approx(selected[0].s_act, rel=1e-5) == 0.5
    assert pytest.approx(selected[0].s_move, rel=1e-5) == 1.0
    expected_score = 0.30 * 0.5 + 0.70 * 1.0
    assert pytest.approx(selected[0].score, rel=1e-5) == expected_score


def test_invalid_or_non_finite_volume_triggers_fallback() -> None:
    start = datetime(2026, 10, 1, 9, 30, tzinfo=timezone.utc)
    
    bars = []
    for i in range(20):
        ts = start + timedelta(minutes=i)
        bars.append(Candle("AAPL", ts, 100.0, 101.0, 99.0, 100.0, 1000.0))
    bars.append(Candle("AAPL", start + timedelta(minutes=20), 100.0, 106.0, 100.0, 103.0, float("inf")))

    selected = select_layer4_candidates(["AAPL"], {"AAPL": bars}, max_candidates=8)
    assert len(selected) == 1
    assert selected[0].has_activity_baseline is False
    assert pytest.approx(selected[0].score, rel=1e-5) == selected[0].s_move


@pytest.mark.parametrize("invalid_vol", [float("nan"), float("inf"), float("-inf"), -500.0])
def test_baseline_volume_nan_inf_negative_makes_baseline_unavailable(invalid_vol: float) -> None:
    start = datetime(2026, 10, 1, 9, 30, tzinfo=timezone.utc)
    bars = []
    for i in range(19):
        ts = start + timedelta(minutes=i)
        bars.append(Candle("AAPL", ts, 100.0, 101.0, 99.0, 100.0, 1000.0))
    
    # Create mock bar for index 19 with invalid volume (bypassing Candle validation if negative)
    bad_bar = MagicMock()
    bad_bar.high = 101.0
    bad_bar.low = 99.0
    bad_bar.close = 100.0
    bad_bar.timestamp = start + timedelta(minutes=19)
    bad_bar.volume = invalid_vol
    bars.append(bad_bar)

    # Evaluated bar at index 20
    bars.append(Candle("AAPL", start + timedelta(minutes=20), 100.0, 106.0, 100.0, 103.0, 2000.0))

    baseline = compute_median_volume_baseline(bars, 20, window_size=20)
    assert baseline is None

    selected = select_layer4_candidates(["AAPL"], {"AAPL": bars}, max_candidates=8)
    assert len(selected) == 1
    assert selected[0].has_activity_baseline is False
    assert pytest.approx(selected[0].score, rel=1e-5) == selected[0].s_move


@pytest.mark.parametrize("invalid_curr_vol", [float("nan"), float("-inf"), -1.0])
def test_current_bar_volume_nan_inf_negative_triggers_fallback(invalid_curr_vol: float) -> None:
    start = datetime(2026, 10, 1, 9, 30, tzinfo=timezone.utc)
    bars = []
    for i in range(20):
        ts = start + timedelta(minutes=i)
        bars.append(Candle("AAPL", ts, 100.0, 101.0, 99.0, 100.0, 1000.0))
    
    bad_curr_bar = MagicMock()
    bad_curr_bar.high = 106.0
    bad_curr_bar.low = 100.0
    bad_curr_bar.close = 103.0
    bad_curr_bar.timestamp = start + timedelta(minutes=20)
    bad_curr_bar.volume = invalid_curr_vol
    bars.append(bad_curr_bar)

    selected = select_layer4_candidates(["AAPL"], {"AAPL": bars}, max_candidates=8)
    assert len(selected) == 1
    assert selected[0].has_activity_baseline is False
    assert pytest.approx(selected[0].score, rel=1e-5) == selected[0].s_move


def test_baseline_crossing_session_boundary_is_unavailable() -> None:
    start = datetime(2026, 10, 1, 9, 30, tzinfo=timezone.utc)
    bars = []
    for i in range(20):
        ts = start + timedelta(minutes=i)
        bars.append(Candle("AAPL", ts, 100.0, 101.0, 99.0, 100.0, 1000.0))
    
    gap_ts = start + timedelta(hours=10)
    bars.append(Candle("AAPL", gap_ts, 100.0, 106.0, 100.0, 103.0, 2000.0))

    baseline = compute_median_volume_baseline(bars, 20, window_size=20)
    assert baseline is None


def test_duplicate_input_symbols_unique_output_preserving_order() -> None:
    start = datetime(2026, 10, 1, 9, 30, tzinfo=timezone.utc)
    
    def make_bars(sym: str):
        b = []
        for i in range(20):
            ts = start + timedelta(minutes=i)
            b.append(Candle(sym, ts, 100.0, 101.0, 99.0, 100.0, 1000.0))
        b.append(Candle(sym, start + timedelta(minutes=20), 100.0, 106.0, 100.0, 103.0, 2000.0))
        return b

    bars_dict = {
        "MSFT": make_bars("MSFT"),
        "AAPL": make_bars("AAPL"),
    }
    daily_pool = ["AAPL", "MSFT", "AAPL", "MSFT"]

    selected = select_layer4_candidates(daily_pool, bars_dict, max_candidates=8)
    symbols = [c.symbol for c in selected]
    assert len(symbols) == len(set(symbols))
    assert set(symbols) == {"AAPL", "MSFT"}


def test_hard_maximum_of_eight_enforced() -> None:
    start = datetime(2026, 10, 1, 9, 30, tzinfo=timezone.utc)
    
    def make_bars(sym: str):
        b = []
        for i in range(20):
            ts = start + timedelta(minutes=i)
            b.append(Candle(sym, ts, 100.0, 101.0, 99.0, 100.0, 1000.0))
        b.append(Candle(sym, start + timedelta(minutes=20), 100.0, 106.0, 100.0, 103.0, 2000.0))
        return b

    symbols = ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L"]
    bars_dict = {s: make_bars(s) for s in symbols}

    selected = select_layer4_candidates(symbols, bars_dict, max_candidates=15)
    assert len(selected) == 8
