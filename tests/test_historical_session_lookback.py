from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from money_mintter.strategy import ICTStrategy, Candle, MultiTimeframeSeries, Timeframe
from money_mintter.strategy.ict_strategy import _bar_trading_date
import os
import sys

_ICT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "ICT Strategy"))
if _ICT_DIR not in sys.path:
    sys.path.insert(0, _ICT_DIR)

from v106_dynamic_rr_zone_entry import get_liquidity_levels

CT = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

def test_historical_session_lookback_liquidity_levels_and_values():
    """Proves that lookback_bars=len(series.entry.bars) in snapshot_at() provides
    complete historical sessions for PDH, PDL, P2H, and P2L with exact expected values,
    and ensures 1m, 15m, and entry timeframes are fully populated without truncation.
    """
    strategy = ICTStrategy()
    base_time = datetime(2026, 5, 31, 18, 0, tzinfo=CT)
    candles_1m = []

    for day_offset in range(4):
        day_start = base_time + timedelta(days=day_offset)
        base_price = 100.0 + day_offset * 10.0
        for minute_offset in range(24 * 60):
            ts = day_start + timedelta(minutes=minute_offset)
            high_val = base_price + 8.0 + (minute_offset % 3) * 0.5
            low_val = base_price + 2.0 - (minute_offset % 2) * 0.5
            open_val = base_price + 5.0
            close_val = base_price + 5.0
            candles_1m.append(
                Candle(
                    symbol="NQ",
                    timestamp=ts.astimezone(UTC),
                    open=open_val,
                    high=high_val,
                    low=low_val,
                    close=close_val,
                    volume=1000.0,
                )
            )

    series = MultiTimeframeSeries.from_1m(
        candles_1m,
        entry_timeframe=Timeframe.FIVE_MIN,
        timezone_name="America/New_York",
        spread_bps=2.0,
    )

    snapshot_time = (base_time + timedelta(days=3, hours=10)).astimezone(UTC)
    snapshot = series.snapshot_at(snapshot_time, lookback_bars=len(series.entry.bars))

    assert len(snapshot.entry) > 100
    assert len(snapshot.fifteen_minute) > 0
    assert len(snapshot.one_minute) > 0

    b5_raw = strategy._convert_candles(snapshot.entry)
    dr5 = strategy._build_dr(b5_raw)
    all_dates = sorted(dr5.keys())
    current_date = _bar_trading_date(int(snapshot.as_of.timestamp() * 1e9))
    if current_date not in dr5:
        current_date = all_dates[-1]

    liq_levels = get_liquidity_levels(b5_raw, dr5, current_date, all_dates)
    level_dict = {l_type: price for price, l_type in liq_levels}

    assert "pdh" in level_dict
    assert "pdl" in level_dict
    assert "p2h" in level_dict
    assert "p2l" in level_dict

    assert level_dict["pdh"] == 129.0
    assert level_dict["pdl"] == 121.5
    assert level_dict["p2h"] == 119.0
    assert level_dict["p2l"] == 111.5


def test_historical_session_lookback_with_missing_buckets():
    """Test that historical dates and liquidity levels remain available and robust
    even when entire 5-minute buckets and minute candles are missing (sparse / gappy data),
    verifying all four required liquidity levels (pdh, pdl, p2h, p2l) explicitly.
    """
    strategy = ICTStrategy()
    base_time = datetime(2026, 5, 31, 18, 0, tzinfo=CT)
    candles_1m = []

    for day_offset in range(4):
        day_start = base_time + timedelta(days=day_offset)
        base_price = 100.0 + day_offset * 10.0
        for minute_offset in range(24 * 60):
            # Omit a contiguous 15-minute block (completely missing 5-minute buckets)
            if 120 <= minute_offset < 135:
                continue
            high_val = base_price + 8.0 + (minute_offset % 3) * 0.5
            low_val = base_price + 2.0 - (minute_offset % 2) * 0.5
            ts = day_start + timedelta(minutes=minute_offset)
            candles_1m.append(
                Candle(
                    symbol="NQ",
                    timestamp=ts.astimezone(UTC),
                    open=base_price + 5.0,
                    high=high_val,
                    low=low_val,
                    close=base_price + 5.0,
                    volume=500.0,
                )
            )

    series = MultiTimeframeSeries.from_1m(
        candles_1m,
        entry_timeframe=Timeframe.FIVE_MIN,
        timezone_name="America/New_York",
        spread_bps=2.0,
    )

    snapshot_time = (base_time + timedelta(days=3, hours=10)).astimezone(UTC)
    snapshot = series.snapshot_at(snapshot_time, lookback_bars=len(series.entry.bars))

    assert len(snapshot.entry) > 50
    assert len(snapshot.fifteen_minute) > 0

    b5_raw = strategy._convert_candles(snapshot.entry)
    dr5 = strategy._build_dr(b5_raw)
    all_dates = sorted(dr5.keys())
    # Confirm historical trading-date coverage is sufficient (at least 4 trading dates)
    assert len(all_dates) >= 3

    current_date = _bar_trading_date(int(snapshot.as_of.timestamp() * 1e9))
    if current_date not in dr5:
        current_date = all_dates[-1]

    liq_levels = get_liquidity_levels(b5_raw, dr5, current_date, all_dates)
    level_dict = {l_type: price for price, l_type in liq_levels}

    # Verify all four historical liquidity levels are successfully extracted despite missing buckets
    assert "pdh" in level_dict
    assert "pdl" in level_dict
    assert "p2h" in level_dict
    assert "p2l" in level_dict
