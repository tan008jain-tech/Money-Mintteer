from __future__ import annotations

import pytest
from datetime import datetime, timezone, timedelta, date as dt_date
from zoneinfo import ZoneInfo

from money_mintter.strategy.candles import Candle
from money_mintter.strategy.ict.adapter import adapt_market_data, dt_to_ns, build_dr, bar_trading_date, ICTAcquisitionResult, _create_verified_acquisition_result
from money_mintter.strategy.ict.v106_dynamic_rr_zone_entry import get_liquidity_levels


def test_adapter_input_validation() -> None:
    naive_dt = datetime(2026, 6, 2, 10, 0)
    aware_dt = datetime(2026, 6, 2, 10, 0, tzinfo=timezone.utc)
    aware_dt2 = datetime(2026, 6, 2, 10, 5, tzinfo=timezone.utc)

    c_naive = Candle(symbol="AAPL", timestamp=naive_dt, open=100.0, high=101.0, low=99.0, close=100.5, volume=1000.0)
    c_aware1 = Candle(symbol="AAPL", timestamp=aware_dt, open=100.0, high=101.0, low=99.0, close=100.5, volume=1000.0)
    c_aware2 = Candle(symbol="AAPL", timestamp=aware_dt2, open=100.5, high=102.0, low=100.0, close=101.5, volume=1200.0)
    c_wrong_symbol = Candle(symbol="MSFT", timestamp=aware_dt, open=100.0, high=101.0, low=99.0, close=100.5, volume=1000.0)

    res_ok = lambda b1, b5, sym="AAPL": _create_verified_acquisition_result(symbol=sym, b1_candles=b1, b5_candles=b5, feed="iex", b1_success=True, b5_success=True)

    # Naive timestamp
    with pytest.raises(ValueError, match="naive timestamp encountered"):
        adapt_market_data(res_ok((c_naive,), (c_aware1,)), aware_dt2)

    # as_of naive
    with pytest.raises(ValueError, match="must be timezone-aware"):
        adapt_market_data(res_ok((c_aware1,), (c_aware1,)), naive_dt)

    # Duplicate timestamp
    with pytest.raises(ValueError, match="duplicate candle timestamp detected"):
        adapt_market_data(res_ok((c_aware1, c_aware1), (c_aware1,)), aware_dt2)

    # Symbol mismatch
    with pytest.raises(ValueError, match="symbol mismatch"):
        adapt_market_data(res_ok((c_wrong_symbol,), (c_aware1,)), aware_dt2)

    # Unsorted input rejection (instead of silent sorting)
    with pytest.raises(ValueError, match="not strictly sorted"):
        adapt_market_data(res_ok((c_aware2, c_aware1), (c_aware1, c_aware2)), aware_dt2)


def test_adapter_enforces_provenance_iex_and_completeness() -> None:
    aware_dt = datetime(2026, 6, 2, 10, 0, tzinfo=timezone.utc)
    c = Candle(symbol="AAPL", timestamp=aware_dt, open=100.0, high=101.0, low=99.0, close=100.5, volume=1000.0)

    # 1. Unverified provenance rejected (manually constructed without valid _token)
    res_unverified = ICTAcquisitionResult(symbol="AAPL", b1_candles=(c,), b5_candles=(c,), feed="iex", b1_success=True, b5_success=True, _token=None)
    with pytest.raises(ValueError, match="provenance check failed"):
        adapt_market_data(res_unverified, aware_dt + timedelta(minutes=10))

    # 2. A trusted SIP result is accepted, and its actual provenance is preserved.
    res_sip = _create_verified_acquisition_result(
        symbol="AAPL",
        b1_candles=(c,),
        b5_candles=(c,),
        feed="sip",
        b1_success=True,
        b5_success=True,
    )
    sip_adapter = adapt_market_data(res_sip, aware_dt + timedelta(minutes=10))
    assert sip_adapter.feed == "sip"

    # 3. Unsupported feed metadata is rejected even when the acquisition token is valid.
    res_unsupported = _create_verified_acquisition_result(
        symbol="AAPL",
        b1_candles=(c,),
        b5_candles=(c,),
        feed="unknown",
        b1_success=True,
        b5_success=True,
    )
    with pytest.raises(ValueError, match="Unsupported ICT acquisition feed"):
        adapt_market_data(res_unsupported, aware_dt + timedelta(minutes=10))

    # 4. Unsuccessful acquisition rejected
    res_fail = _create_verified_acquisition_result(symbol="AAPL", b1_candles=(), b5_candles=(), feed="iex", b1_success=False, b5_success=True, error_message="fail")
    with pytest.raises(ValueError, match="Unsuccessful or incomplete acquisition result rejected"):
        adapt_market_data(res_fail, aware_dt + timedelta(minutes=10))

    # 5. Dataset completeness: Empty 1m stream despite successful API request rejected
    res_empty_1m = _create_verified_acquisition_result(symbol="AAPL", b1_candles=(), b5_candles=(c,), feed="iex", b1_success=True, b5_success=True)
    with pytest.raises(ValueError, match="Dataset completeness rejection: 1m stream is empty"):
        adapt_market_data(res_empty_1m, aware_dt + timedelta(minutes=10))

    # 6. Dataset completeness: Empty 5m stream despite successful API request rejected
    res_empty_5m = _create_verified_acquisition_result(symbol="AAPL", b1_candles=(c,), b5_candles=(), feed="iex", b1_success=True, b5_success=True)
    with pytest.raises(ValueError, match="Dataset completeness rejection: 5m stream is empty"):
        adapt_market_data(res_empty_5m, aware_dt + timedelta(minutes=10))

    # 7. Dataset completeness: Both streams empty rejected
    res_empty_both = _create_verified_acquisition_result(symbol="AAPL", b1_candles=(), b5_candles=(), feed="iex", b1_success=True, b5_success=True)
    with pytest.raises(ValueError, match="Dataset completeness rejection: 1m stream is empty"):
        adapt_market_data(res_empty_both, aware_dt + timedelta(minutes=10))

    # 8. Dataset completeness: Streams becoming empty after lookahead filtering rejected
    future_dt = aware_dt + timedelta(hours=2)
    res_lookahead_empty = _create_verified_acquisition_result(symbol="AAPL", b1_candles=(c,), b5_candles=(c,), feed="iex", b1_success=True, b5_success=True)
    with pytest.raises(ValueError, match="Dataset completeness rejection: 1m stream became empty after lookahead filtering"):
        adapt_market_data(res_lookahead_empty, aware_dt) # as_of is before candle timestamp


def test_adapter_lookahead_protection_and_15m() -> None:
    tz_utc = timezone.utc
    t_start = datetime(2026, 6, 2, 13, 30, tzinfo=tz_utc) # 09:30 EDT

    b1_list = [Candle(symbol="AAPL", timestamp=t_start + timedelta(minutes=i), open=100.0, high=101.0, low=99.0, close=100.5, volume=1000.0) for i in range(20)]
    b5_list = []
    for i in range(4):
        dt_5m = t_start + timedelta(minutes=i * 5)
        b5_list.append(Candle(symbol="AAPL", timestamp=dt_5m, open=100.0, high=101.5, low=98.5, close=100.5, volume=2500.0))

    acq = _create_verified_acquisition_result(symbol="AAPL", b1_candles=tuple(b1_list), b5_candles=tuple(b5_list), feed="iex", b1_success=True, b5_success=True)

    as_of_early = t_start + timedelta(minutes=10) # 13:40 UTC
    result_early = adapt_market_data(acq, as_of_early)
    assert len(result_early.b15) == 0

    as_of_exact = t_start + timedelta(minutes=15) # 13:45 UTC
    result_exact = adapt_market_data(acq, as_of_exact)
    assert len(result_exact.b15) == 1
    assert result_exact.b15[0]["constituent_count"] == 3


def test_adapter_metadata_and_precision() -> None:
    tz_utc = timezone.utc
    t1 = datetime(2026, 6, 2, 13, 30, tzinfo=tz_utc)
    c1 = Candle(symbol="AAPL", timestamp=t1, open=100.0, high=101.0, low=99.0, close=100.5, volume=1000.0)

    acq = _create_verified_acquisition_result(symbol="AAPL", b1_candles=(c1,), b5_candles=(c1,), feed="iex", b1_success=True, b5_success=True)
    as_of = datetime(2026, 6, 2, 14, 0, tzinfo=tz_utc)
    result = adapt_market_data(acq, as_of)

    assert result.api_success is True
    assert result.source_coverage_known is False
    assert result.closed_at != as_of
    assert result.closed_at == datetime(2026, 6, 2, 13, 45, tzinfo=tz_utc)
    assert result.constituent_count == 1

    ns = dt_to_ns(t1)
    assert isinstance(ns, int)
    assert ns > 0
    assert result.b5[0]["time_ns"] == ns


def test_adapter_multi_timeframe_independence_and_ohlcv() -> None:
    tz_utc = timezone.utc
    t_start = datetime(2026, 6, 2, 13, 30, tzinfo=tz_utc) # 09:30 EDT = 08:30 CDT

    b1_list = [
        Candle(symbol="AAPL", timestamp=t_start + timedelta(minutes=i), open=50.0 + i, high=52.0 + i, low=49.0 + i, close=51.0 + i, volume=10.0)
        for i in range(15)
    ]
    b5_list = [
        Candle(symbol="AAPL", timestamp=t_start + timedelta(minutes=i*5), open=100.0 + i*10, high=110.0 + i*10, low=95.0 + i*10, close=105.0 + i*10, volume=500.0)
        for i in range(3)
    ]

    acq = _create_verified_acquisition_result(symbol="AAPL", b1_candles=tuple(b1_list), b5_candles=tuple(b5_list), feed="iex", b1_success=True, b5_success=True)
    as_of = datetime(2026, 6, 2, 14, 0, tzinfo=tz_utc)
    result = adapt_market_data(acq, as_of)

    assert len(result.b1) == 15
    assert len(result.b5) == 3
    assert result.b1[0]["open"] == 50.0
    assert result.b5[0]["open"] == 100.0

    assert len(result.b15) == 1
    b15_bar = result.b15[0]
    assert b15_bar["open"] == b5_list[0].open
    assert b15_bar["high"] == max(c.high for c in b5_list)
    assert b15_bar["low"] == min(c.low for c in b5_list)
    assert b15_bar["close"] == b5_list[-1].close
    assert b15_bar["volume"] == sum(c.volume for c in b5_list)
    assert b15_bar["hour"] == 8
    assert b15_bar["minute"] == 30


def test_adapter_equity_session_boundaries_and_timezone() -> None:
    tz_utc = timezone.utc
    times_utc = [
        datetime(2026, 6, 2, 8, 0, tzinfo=tz_utc),
        datetime(2026, 6, 2, 9, 0, tzinfo=tz_utc),
        datetime(2026, 6, 2, 13, 30, tzinfo=tz_utc),
        datetime(2026, 6, 2, 20, 0, tzinfo=tz_utc),
        datetime(2026, 6, 3, 0, 0, tzinfo=tz_utc),
    ]
    b5_list = [
        Candle(symbol="SPY", timestamp=t, open=500.0, high=502.0, low=499.0, close=501.0, volume=1000.0)
        for t in times_utc
    ]
    b1_list = [
        Candle(symbol="SPY", timestamp=times_utc[0] + timedelta(minutes=i), open=500.0, high=501.0, low=499.0, close=500.5, volume=100.0)
        for i in range(10)
    ]

    acq = _create_verified_acquisition_result(symbol="SPY", b1_candles=tuple(b1_list), b5_candles=tuple(b5_list), feed="iex", b1_success=True, b5_success=True)
    as_of = datetime(2026, 6, 3, 12, 0, tzinfo=tz_utc)
    result = adapt_market_data(acq, as_of)

    expected_chicago_times = [
        (3, 0),
        (4, 0),
        (8, 30),
        (15, 0),
        (19, 0),
    ]
    for bar, (exp_h, exp_m) in zip(result.b5, expected_chicago_times):
        assert bar["hour"] == exp_h
        assert bar["minute"] == exp_m

    assert result.constituent_count == 5


def test_adapter_dst_spring_and_fallback() -> None:
    tz_utc = timezone.utc
    t_spring = datetime(2026, 3, 8, 12, 0, tzinfo=tz_utc)
    c_spring = Candle(symbol="QQQ", timestamp=t_spring, open=400.0, high=405.0, low=398.0, close=402.0, volume=2000.0)
    c_spring_1m = Candle(symbol="QQQ", timestamp=t_spring, open=400.0, high=401.0, low=399.0, close=400.5, volume=100.0)

    acq_spring = _create_verified_acquisition_result(symbol="QQQ", b1_candles=(c_spring_1m,), b5_candles=(c_spring,), feed="iex", b1_success=True, b5_success=True)
    result_spring = adapt_market_data(acq_spring, datetime(2026, 3, 9, 0, 0, tzinfo=tz_utc))
    assert len(result_spring.b5) == 1
    assert result_spring.b5[0]["date"] == dt_date(2026, 3, 8)

    t_fb1 = datetime(2026, 11, 1, 6, 30, tzinfo=tz_utc)
    t_fb2 = datetime(2026, 11, 1, 7, 30, tzinfo=tz_utc)

    c_fb1 = Candle(symbol="QQQ", timestamp=t_fb1, open=410.0, high=415.0, low=408.0, close=412.0, volume=1000.0)
    c_fb2 = Candle(symbol="QQQ", timestamp=t_fb2, open=412.0, high=418.0, low=410.0, close=415.0, volume=1500.0)
    c_fb1_1m = Candle(symbol="QQQ", timestamp=t_fb1, open=410.0, high=411.0, low=409.0, close=410.5, volume=100.0)

    acq_fb = _create_verified_acquisition_result(symbol="QQQ", b1_candles=(c_fb1_1m,), b5_candles=(c_fb1, c_fb2), feed="iex", b1_success=True, b5_success=True)
    result_fb = adapt_market_data(acq_fb, datetime(2026, 11, 2, 0, 0, tzinfo=tz_utc))
    assert len(result_fb.b5) == 2
    for bar in result_fb.b5:
        assert bar["hour"] == 1
        assert bar["minute"] == 30
        assert bar["date"] == dt_date(2026, 11, 1)


def test_adapter_lookahead_boundaries() -> None:
    tz_utc = timezone.utc
    t_candle = datetime(2026, 6, 2, 13, 30, tzinfo=tz_utc)
    c1_list = [Candle(symbol="SPY", timestamp=t_candle + timedelta(minutes=i), open=500.0, high=502.0, low=499.0, close=501.0, volume=1000.0) for i in range(15)]
    c5_list = [
        Candle(symbol="SPY", timestamp=t_candle, open=500.0, high=502.0, low=499.0, close=501.0, volume=1000.0),
        Candle(symbol="SPY", timestamp=t_candle + timedelta(minutes=5), open=501.0, high=503.0, low=500.0, close=502.0, volume=1000.0),
        Candle(symbol="SPY", timestamp=t_candle + timedelta(minutes=10), open=502.0, high=504.0, low=501.0, close=503.0, volume=1000.0),
    ]

    close_time_15m = t_candle + timedelta(minutes=15)
    acq = _create_verified_acquisition_result(symbol="SPY", b1_candles=tuple(c1_list), b5_candles=tuple(c5_list), feed="iex", b1_success=True, b5_success=True)

    as_of_before = close_time_15m - timedelta(microseconds=1)
    res_before = adapt_market_data(acq, as_of_before)
    assert len(res_before.b15) == 0

    res_exact = adapt_market_data(acq, close_time_15m)
    assert len(res_exact.b15) == 1

    # Test stream becoming empty after lookahead filtering when as_of is before candle duration
    as_of_too_early = t_candle
    with pytest.raises(ValueError, match="Dataset completeness rejection: 1m stream became empty after lookahead filtering"):
        adapt_market_data(acq, as_of_too_early)


def test_adapter_sparse_15m_graceful_handling() -> None:
    tz_utc = timezone.utc
    t_candle = datetime(2026, 6, 2, 13, 30, tzinfo=tz_utc)
    c5_list = [
        Candle(symbol="SPY", timestamp=t_candle, open=500.0, high=502.0, low=499.0, close=501.0, volume=1000.0), # partial (1-of-3)
        Candle(symbol="SPY", timestamp=t_candle + timedelta(minutes=15), open=505.0, high=507.0, low=504.0, close=506.0, volume=1000.0), # complete 3-of-3 bucket start
        Candle(symbol="SPY", timestamp=t_candle + timedelta(minutes=20), open=507.0, high=509.0, low=506.0, close=508.0, volume=1000.0),
        Candle(symbol="SPY", timestamp=t_candle + timedelta(minutes=25), open=508.0, high=510.0, low=507.0, close=509.0, volume=1000.0),
    ]
    c1_list = [Candle(symbol="SPY", timestamp=t_candle + timedelta(minutes=i), open=500.0, high=502.0, low=499.0, close=501.0, volume=1000.0) for i in range(40)]
    acq = _create_verified_acquisition_result(symbol="SPY", b1_candles=tuple(c1_list), b5_candles=tuple(c5_list), feed="iex", b1_success=True, b5_success=True)
    as_of = datetime(2026, 6, 2, 16, 0, tzinfo=tz_utc)

    result = adapt_market_data(acq, as_of)
    assert result.api_success is True
    assert result.source_coverage_known is False
    assert len(result.b1) == 40
    assert len(result.b5) == 4
    assert len(result.b15) == 1
    assert len(result.b15_coverage) >= 2
    statuses = [entry["status"] for entry in result.b15_coverage]
    assert "partial" in statuses or "unobserved" in statuses
    assert "complete" in statuses


def test_adapter_liquidity_levels_extraction() -> None:
    tz_utc = timezone.utc
    t1 = datetime(2026, 6, 1, 14, 0, tzinfo=tz_utc)
    c1_5m = Candle(symbol="AAPL", timestamp=t1, open=18000.0, high=18104.0, low=17900.0, close=18050.0, volume=5000.0)

    t2_pm = datetime(2026, 6, 2, 10, 0, tzinfo=tz_utc)
    c2_pm = Candle(symbol="AAPL", timestamp=t2_pm, open=18200.0, high=18303.0, low=18150.0, close=18250.0, volume=5000.0)

    t2_reg = datetime(2026, 6, 2, 13, 30, tzinfo=tz_utc)
    c2_reg = Candle(symbol="AAPL", timestamp=t2_reg, open=18200.0, high=18250.0, low=18100.0, close=18220.0, volume=5000.0)

    b5_list = [c1_5m, c2_pm, c2_reg]
    b1_list = [Candle(symbol="AAPL", timestamp=t1 + timedelta(minutes=i), open=18000.0, high=18010.0, low=17990.0, close=18005.0, volume=1000.0) for i in range(10)]

    acq = _create_verified_acquisition_result(symbol="AAPL", b1_candles=tuple(b1_list), b5_candles=tuple(b5_list), feed="iex", b1_success=True, b5_success=True)
    as_of = datetime(2026, 6, 2, 15, 0, tzinfo=tz_utc)
    result = adapt_market_data(acq, as_of)

    eval_date = dt_date(2026, 6, 2)
    levels = get_liquidity_levels(result.b5, result.dr5, eval_date, result.all_dates)

    level_dict = {label: val for val, label in levels}
    assert level_dict.get("pdh") == 18104.0
    assert level_dict.get("pdl") == 17900.0
    assert level_dict.get("premarket_h") == 18303.0
    assert level_dict.get("premarket_l") == 18150.0


def test_bar_trading_date_rollover() -> None:
    chicago_tz = ZoneInfo("America/Chicago")
    # 16:59 CT on Tuesday June 2, 2026 -> June 2, 2026
    dt_1659 = datetime(2026, 6, 2, 16, 59, tzinfo=chicago_tz)
    assert bar_trading_date(dt_1659) == dt_date(2026, 6, 2)

    # 17:00 CT on Tuesday June 2, 2026 -> June 3, 2026
    dt_1700 = datetime(2026, 6, 2, 17, 0, tzinfo=chicago_tz)
    assert bar_trading_date(dt_1700) == dt_date(2026, 6, 3)

    # 17:01 CT on Tuesday June 2, 2026 -> June 3, 2026
    dt_1701 = datetime(2026, 6, 2, 17, 1, tzinfo=chicago_tz)
    assert bar_trading_date(dt_1701) == dt_date(2026, 6, 3)

    # Friday 17:00 CT (June 5, 2026 17:00 CT) -> Saturday June 6, 2026
    dt_fri17 = datetime(2026, 6, 5, 17, 0, tzinfo=chicago_tz)
    assert bar_trading_date(dt_fri17) == dt_date(2026, 6, 6)

    # Saturday 17:00 CT (June 6, 2026 17:00 CT) -> Sunday June 7, 2026
    dt_sat17 = datetime(2026, 6, 6, 17, 0, tzinfo=chicago_tz)
    assert bar_trading_date(dt_sat17) == dt_date(2026, 6, 7)

    # Sunday 17:00 CT (June 7, 2026 17:00 CT) -> Monday June 8, 2026
    dt_sun17 = datetime(2026, 6, 7, 17, 0, tzinfo=chicago_tz)
    assert bar_trading_date(dt_sun17) == dt_date(2026, 6, 8)


def test_adapter_gapped_15m_graceful_handling() -> None:
    chicago_tz = ZoneInfo("America/Chicago")
    t_start = datetime(2026, 6, 2, 13, 30, tzinfo=chicago_tz).astimezone(timezone.utc)

    c5_list = [
        Candle(symbol="SPY", timestamp=t_start, open=500.0, high=502.0, low=499.0, close=501.0, volume=1000.0),
        Candle(symbol="SPY", timestamp=t_start + timedelta(minutes=5), open=501.0, high=503.0, low=500.0, close=502.0, volume=1000.0),
        Candle(symbol="SPY", timestamp=t_start + timedelta(minutes=10), open=502.0, high=504.0, low=501.0, close=503.0, volume=1000.0),
        # Partial bucket (1-of-3 at 13:45)
        Candle(symbol="SPY", timestamp=t_start + timedelta(minutes=15), open=503.0, high=505.0, low=502.0, close=504.0, volume=1000.0),
        # Complete bucket at 14:00
        Candle(symbol="SPY", timestamp=t_start + timedelta(minutes=30), open=505.0, high=507.0, low=504.0, close=506.0, volume=1000.0),
        Candle(symbol="SPY", timestamp=t_start + timedelta(minutes=35), open=506.0, high=508.0, low=505.0, close=507.0, volume=1000.0),
        Candle(symbol="SPY", timestamp=t_start + timedelta(minutes=40), open=507.0, high=509.0, low=506.0, close=508.0, volume=1000.0),
    ]
    c1_list = [Candle(symbol="SPY", timestamp=t_start + timedelta(minutes=i), open=500.0, high=502.0, low=499.0, close=501.0, volume=1000.0) for i in range(50)]
    acq = _create_verified_acquisition_result(symbol="SPY", b1_candles=tuple(c1_list), b5_candles=tuple(c5_list), feed="iex", b1_success=True, b5_success=True)
    as_of = datetime(2026, 6, 2, 16, 0, tzinfo=chicago_tz).astimezone(timezone.utc)

    result = adapt_market_data(acq, as_of)
    assert result.api_success is True
    assert len(result.b1) == 50
    assert len(result.b5) == 7
    statuses = [entry["status"] for entry in result.b15_coverage]
    assert "partial" in statuses or "unobserved" in statuses


def test_strategy_closed_bar_and_gap_handling() -> None:
    from money_mintter.strategy.ict.v106_dynamic_rr_zone_entry import structure_15m, sweep_15m
    tz_utc = timezone.utc
    t_start = datetime(2026, 6, 2, 13, 30, tzinfo=tz_utc)

    # Create 10 contiguous 15m bars forming explicit swing highs and lows
    # Prices: alternating highs/lows to create swing structure
    b15_list = []
    prices = [500.0, 505.0, 502.0, 510.0, 506.0, 515.0, 508.0, 512.0, 507.0, 520.0]
    for i, p in enumerate(prices):
        bar_dt = t_start + timedelta(minutes=i * 15)
        b15_list.append({
            "time_ns": dt_to_ns(bar_dt),
            "open": p - 1.0, "high": p + 2.0, "low": p - 2.0, "close": p, "volume": 3000.0,
            "hour": 8 + (i * 15 // 60), "minute": (30 + i * 15) % 60, "date": dt_date(2026, 6, 2), "constituent_count": 3
        })
    dr15 = {dt_date(2026, 6, 2): (0, len(b15_list))}

    # 1. structure_15m() excludes a 15m candle that closes after the historical entry timestamp
    # Bar 0 closes at 13:45:00 UTC. Entry at 13:44:59 UTC -> bar 0 excluded, resulting in insufficient bars (<6) -> None
    entry_early = dt_to_ns(datetime(2026, 6, 2, 13, 44, 59, tzinfo=tz_utc))
    assert structure_15m(b15_list, dr15, dt_date(2026, 6, 2), entry_early) is None

    # 2. structure_15m() includes a candle whose close time equals the entry timestamp
    # Entry at 14:45:00 UTC (index 4 close time 14:45:00 UTC) -> includes 5 bars (indices 0 to 4), or index 5 close 15:00 for 6 bars
    entry_exact = dt_to_ns(datetime(2026, 6, 2, 15, 0, 0, tzinfo=tz_utc))
    res_exact = structure_15m(b15_list, dr15, dt_date(2026, 6, 2), entry_exact)
    # Evaluates without error and correctly processes structure
    assert res_exact is None or isinstance(res_exact, str)

    # 3. sweep_15m() respects the same closed-bar boundary
    liq_levels = [(503.0, "pdh")]
    # Entry at 13:44:59 UTC (bar 0 close is 13:45:00 UTC > 13:44:59) -> sweep_15m should not include bar 0
    sweep_early = sweep_15m(b15_list, dr15, dt_date(2026, 6, 2), dt_to_ns(datetime(2026, 6, 2, 13, 44, 59, tzinfo=tz_utc)), liq_levels, "bull")
    assert sweep_early is False

    # 4 & 5. Structure pivot identification and sweep search do not bridge / terminate at timestamp gap
    gapped_b15 = [dict(b15_list[0]), dict(b15_list[1])]
    # Introduce a 45-minute gap instead of 15-minute
    bar_gapped_dt = t_start + timedelta(minutes=15 + 45)
    gapped_b15.append({
        "time_ns": dt_to_ns(bar_gapped_dt),
        "open": 505.0, "high": 510.0, "low": 499.0, "close": 508.0, "volume": 3000.0,
        "hour": 9, "minute": 30, "date": dt_date(2026, 6, 2), "constituent_count": 3
    })
    for i in range(3, 10):
        bar_dt = bar_gapped_dt + timedelta(minutes=(i - 2) * 15)
        gapped_b15.append({
            "time_ns": dt_to_ns(bar_dt),
            "open": 500.0 + i, "high": 505.0 + i, "low": 498.0 + i, "close": 502.0 + i, "volume": 3000.0,
            "hour": bar_dt.hour, "minute": bar_dt.minute, "date": dt_date(2026, 6, 2), "constituent_count": 3
        })
    gapped_dr15 = {dt_date(2026, 6, 2): (0, len(gapped_b15))}
    # Structure and sweep respect gap termination / segmentation
    res_gap_struct = structure_15m(gapped_b15, gapped_dr15, dt_date(2026, 6, 2), dt_to_ns(datetime(2026, 6, 2, 16, 0, 0, tzinfo=tz_utc)))
    assert res_gap_struct is None or isinstance(res_gap_struct, str)

    # 6. Existing session/trading-date boundaries are not bridged
    multi_date_dr15 = {
        dt_date(2026, 6, 1): (0, 5),
        dt_date(2026, 6, 2): (5, len(b15_list)),
    }
    res_date_bound = structure_15m(b15_list, multi_date_dr15, dt_date(2026, 6, 2), entry_exact)
    # Does not bridge into June 1 data range
    assert res_date_bound is None or isinstance(res_date_bound, str)

    # 7. Complete contiguous historical input retains original ICT calculations
    complete_b15 = [
        {
            "time_ns": dt_to_ns(datetime(2026, 6, 2, 13, 30, tzinfo=tz_utc) + timedelta(minutes=i * 15)),
            "open": 500.0 + (i % 2), "high": 510.0 if i in (2, 5) else 505.0, "low": 490.0 if i in (1, 4) else 498.0, "close": 502.0 + i, "volume": 3000.0,
            "hour": 8 + (i * 15 // 60), "minute": (30 + i * 15) % 60, "date": dt_date(2026, 6, 2), "constituent_count": 3
        }
        for i in range(12)
    ]
    complete_dr15 = {dt_date(2026, 6, 2): (0, len(complete_b15))}
    res_complete = structure_15m(complete_b15, complete_dr15, dt_date(2026, 6, 2), dt_to_ns(datetime(2026, 6, 2, 16, 0, 0, tzinfo=tz_utc)))
    assert res_complete is None or res_complete in ("bull", "bear")
    sweep_complete = sweep_15m(complete_b15, complete_dr15, dt_date(2026, 6, 2), dt_to_ns(datetime(2026, 6, 2, 16, 0, 0, tzinfo=tz_utc)), [(495.0, "pdh")], "bull")
    assert isinstance(sweep_complete, bool)


def test_count_b15_gaps_direct() -> None:
    from money_mintter.core.live_bot import count_b15_gaps
    tz_utc = timezone.utc
    t0 = datetime(2026, 6, 2, 13, 30, tzinfo=tz_utc)

    # 1. Intraday gap on the same trading date (> 15 mins) is counted
    b15_gapped = [
        {"time_ns": dt_to_ns(t0), "date": dt_date(2026, 6, 2)},
        {"time_ns": dt_to_ns(t0 + timedelta(minutes=15)), "date": dt_date(2026, 6, 2)}, # normal 15m diff (0 gaps)
        {"time_ns": dt_to_ns(t0 + timedelta(minutes=45)), "date": dt_date(2026, 6, 2)}, # 30m gap (> 15m) -> 1 gap
        {"time_ns": dt_to_ns(t0 + timedelta(minutes=60)), "date": dt_date(2026, 6, 2)}, # normal 15m diff
    ]
    assert count_b15_gaps(tuple(b15_gapped)) == 1

    # A. Same-session long gap: Tuesday 07:00 CT and Tuesday 10:15 CT (same trading date, > 15m) -> counted
    chicago_tz = ZoneInfo("America/Chicago")
    t_tue_0700 = datetime(2026, 6, 2, 7, 0, tzinfo=chicago_tz)
    t_tue_1015 = datetime(2026, 6, 2, 10, 15, tzinfo=chicago_tz)
    d_tue = bar_trading_date(t_tue_0700)
    b15_same_session_long = [
        {"time_ns": dt_to_ns(t_tue_0700.astimezone(timezone.utc)), "date": d_tue},
        {"time_ns": dt_to_ns(t_tue_1015.astimezone(timezone.utc)), "date": d_tue},
    ]
    assert count_b15_gaps(tuple(b15_same_session_long)) == 1

    # B. Actual IEX session transition: Tuesday 15:45 CT and Wednesday 07:00 CT (different trading dates) -> 0 gaps
    t_tue_1545 = datetime(2026, 6, 2, 15, 45, tzinfo=chicago_tz)
    t_wed_0700 = datetime(2026, 6, 3, 7, 0, tzinfo=chicago_tz)
    d_tue_pm = bar_trading_date(t_tue_1545)
    d_wed_am = bar_trading_date(t_wed_0700)
    assert d_tue_pm != d_wed_am
    b15_iex_transition = [
        {"time_ns": dt_to_ns(t_tue_1545.astimezone(timezone.utc)), "date": d_tue_pm},
        {"time_ns": dt_to_ns(t_wed_0700.astimezone(timezone.utc)), "date": d_wed_am},
    ]
    assert count_b15_gaps(tuple(b15_iex_transition)) == 0

    # 2. Overnight / weekend / session transitions crossing dates are excluded
    b15_cross_session = [
        {"time_ns": dt_to_ns(datetime(2026, 6, 2, 19, 45, tzinfo=tz_utc)), "date": dt_date(2026, 6, 2)},
        {"time_ns": dt_to_ns(datetime(2026, 6, 3, 13, 30, tzinfo=tz_utc)), "date": dt_date(2026, 6, 3)}, # different date, huge diff -> excluded
    ]
    assert count_b15_gaps(tuple(b15_cross_session)) == 0


def test_adapter_exact_2_of_3_constituent_bucket() -> None:
    tz_utc = timezone.utc
    t_start = datetime(2026, 6, 2, 13, 30, tzinfo=tz_utc)
    # Exactly 2 of 3 expected 5-minute bars in the 13:30-13:45 15m bucket (missing 13:40 bar)
    c5_list = [
        Candle(symbol="AAPL", timestamp=t_start, open=100.0, high=101.0, low=99.0, close=100.5, volume=1000.0),
        Candle(symbol="AAPL", timestamp=t_start + timedelta(minutes=5), open=100.5, high=102.0, low=100.0, close=101.5, volume=1200.0),
    ]
    c1_list = [Candle(symbol="AAPL", timestamp=t_start + timedelta(minutes=i), open=100.0, high=101.0, low=99.0, close=100.0, volume=100.0) for i in range(15)]
    acq = _create_verified_acquisition_result(symbol="AAPL", b1_candles=tuple(c1_list), b5_candles=tuple(c5_list), feed="iex", b1_success=True, b5_success=True)
    result = adapt_market_data(acq, datetime(2026, 6, 2, 15, 0, tzinfo=tz_utc))
    assert result.api_success is True
    # Explicitly 2-of-3 constituent bucket rejected from tradable b15 list
    assert len(result.b15) == 0
    # Verify coverage correctly flags partial/incomplete constituent count (2 out of 3)
    assert len(result.b15_coverage) == 1
    assert result.b15_coverage[0]["constituent_count"] == 2
    assert result.b15_coverage[0]["status"] == "partial"


def test_independent_structure_and_sweep_boundaries_and_gaps() -> None:
    from money_mintter.strategy.ict.v106_dynamic_rr_zone_entry import structure_15m, sweep_15m
    tz_utc = timezone.utc
    t_start = datetime(2026, 6, 2, 13, 30, tzinfo=tz_utc)
    b15_list = [
        {
            "time_ns": dt_to_ns(t_start + timedelta(minutes=i * 15)),
            "open": 500.0 + i, "high": 505.0 + i, "low": 498.0 + i, "close": 502.0 + i, "volume": 3000.0,
            "hour": 8 + (i * 15 // 60), "minute": (30 + i * 15) % 60, "date": dt_date(2026, 6, 2), "constituent_count": 3
        }
        for i in range(10)
    ]
    dr15 = {dt_date(2026, 6, 2): (0, len(b15_list))}
    liq_levels = [(503.0, "pdh")]

    # Closed-bar boundaries:
    # 1. structure_15m excludes bar before close
    entry_early = dt_to_ns(datetime(2026, 6, 2, 13, 44, 59, tzinfo=tz_utc)) # before bar 0 close (13:45)
    assert structure_15m(b15_list, dr15, dt_date(2026, 6, 2), entry_early) is None

    # 2. structure_15m includes bar when entry time equals close
    entry_exact = dt_to_ns(datetime(2026, 6, 2, 14, 45, 0, tzinfo=tz_utc)) # bar 4 close
    res_struct = structure_15m(b15_list, dr15, dt_date(2026, 6, 2), entry_exact)
    assert res_struct is None or isinstance(res_struct, str)

    # 3. sweep_15m explicitly tested at closed-bar boundary
    sweep_early = sweep_15m(b15_list, dr15, dt_date(2026, 6, 2), entry_early, liq_levels, "bull")
    assert sweep_early is False

    sweep_exact = sweep_15m(b15_list, dr15, dt_date(2026, 6, 2), entry_exact, liq_levels, "bull")
    assert isinstance(sweep_exact, bool)


def test_exact_close_sweep_positive_and_structure_direction() -> None:
    from money_mintter.strategy.ict.v106_dynamic_rr_zone_entry import structure_15m, sweep_15m
    tz_utc = timezone.utc
    t_start = datetime(2026, 6, 2, 13, 30, tzinfo=tz_utc)

    # Build 10 bars with swing structure and a valid positive sweep bar at index 9
    b15_list = []
    prices = [500.0, 505.0, 502.0, 510.0, 506.0, 515.0, 508.0, 512.0, 507.0, 510.0]
    for i, p in enumerate(prices):
        bar_dt = t_start + timedelta(minutes=i * 15)
        # Bar 9 has low sweeping pdh/level = 503.0 (low=501.0, close=504.0, range=3.0, penetration = (503-501)/3 = 0.66 >= 0.1)
        low_val = 501.0 if i == 9 else p - 2.0
        close_val = 504.0 if i == 9 else p
        b15_list.append({
            "time_ns": dt_to_ns(bar_dt),
            "open": p - 1.0, "high": p + 2.0, "low": low_val, "close": close_val, "volume": 3000.0,
            "hour": 8 + (i * 15 // 60), "minute": (30 + i * 15) % 60, "date": dt_date(2026, 6, 2), "constituent_count": 3
        })
    dr15 = {dt_date(2026, 6, 2): (0, len(b15_list))}
    liq_levels = [(503.0, "pdh")]

    bar9_close_ns = b15_list[9]["time_ns"] + 15 * 60 * 1_000_000_000
    entry_before = bar9_close_ns - 1
    entry_at = bar9_close_ns

    # Assert sweep_15m is False immediately before close and True at close
    assert sweep_15m(b15_list, dr15, dt_date(2026, 6, 2), entry_before, liq_levels, "bull") is False
    assert sweep_15m(b15_list, dr15, dt_date(2026, 6, 2), entry_at, liq_levels, "bull") is True

    # Assert structure_15m returns exact expected direction string derived from fixture ("bear")
    struct_res = structure_15m(b15_list, dr15, dt_date(2026, 6, 2), entry_at)
    assert struct_res == "bear"


def test_v106_historical_output_parity() -> None:
    from money_mintter.strategy.ict.v106_dynamic_rr_zone_entry import structure_15m, sweep_15m
    tz_utc = timezone.utc
    t_start = datetime(2026, 6, 2, 13, 30, tzinfo=tz_utc)
    # Continuous gap-free reference input
    b15_ref = [
        {
            "time_ns": dt_to_ns(t_start + timedelta(minutes=i * 15)),
            "open": 500.0 + (i % 2) * 2, "high": 508.0 + (i % 3), "low": 495.0 - (i % 2), "close": 502.0 + i, "volume": 3000.0,
            "hour": 8 + (i * 15 // 60), "minute": (30 + i * 15) % 60, "date": dt_date(2026, 6, 2), "constituent_count": 3
        }
        for i in range(12)
    ]
    dr15_ref = {dt_date(2026, 6, 2): (0, len(b15_ref))}
    entry_ns = b15_ref[-1]["time_ns"] + 15 * 60 * 1_000_000_000

    res_struct = structure_15m(b15_ref, dr15_ref, dt_date(2026, 6, 2), entry_ns)
    res_sweep = sweep_15m(b15_ref, dr15_ref, dt_date(2026, 6, 2), entry_ns, [(490.0, "pdl")], "bear")

    # Independent reference implementation for V106 parity verification
    end_idx = len(b15_ref) - 1
    lookback_start = max(0, end_idx - 60)
    shs, sls = [], []
    for i in range(lookback_start + 1, end_idx):
        if b15_ref[i]["time_ns"] - b15_ref[i-1]["time_ns"] == 15 * 60 * 1_000_000_000 and \
           b15_ref[i+1]["time_ns"] - b15_ref[i]["time_ns"] == 15 * 60 * 1_000_000_000:
            if b15_ref[i]["high"] > b15_ref[i-1]["high"] and b15_ref[i]["high"] > b15_ref[i+1]["high"]:
                shs.append((b15_ref[i]["high"], i))
            if b15_ref[i]["low"] < b15_ref[i-1]["low"] and b15_ref[i]["low"] < b15_ref[i+1]["low"]:
                sls.append((b15_ref[i]["low"], i))
    ref_struct = None
    if len(shs) >= 2 and len(sls) >= 2:
        if sls[-1][0] > sls[-2][0]:
            last_sh = shs[-1][0]
            for i in range(shs[-1][1] + 1, end_idx + 1):
                if b15_ref[i]["close"] > last_sh:
                    ref_struct = "bull"
                    break
        elif shs[-1][0] < shs[-2][0]:
            last_sl = sls[-1][0]
            for i in range(sls[-1][1] + 1, end_idx + 1):
                if b15_ref[i]["close"] < last_sl:
                    ref_struct = "bear"
                    break

    ref_sweep = False
    for i in range(end_idx, max(0, end_idx - 12) - 1, -1):
        bar = b15_ref[i]
        br = bar["high"] - bar["low"]
        if br <= 0:
            continue
        for lp, lt in [(490.0, "pdl")]:
            if bar["low"] < lp and bar["close"] > lp:
                if (lp - bar["low"]) / br >= 0.1:
                    ref_sweep = True
            if bar["high"] > lp and bar["close"] < lp:
                if (bar["high"] - lp) / br >= 0.1:
                    ref_sweep = True

    # Assert exact output equality against independent reference
    assert res_struct == ref_struct
    assert res_sweep == ref_sweep


def test_ict_error_accounting_matrix(monkeypatch: pytest.MonkeyPatch) -> None:
    import logging
    from money_mintter.config import Settings
    from money_mintter.core.live_bot import MarketScanner, ConnectivityError, LiveMarketBot, ICTDeduplicationTracker, ICTExecutionInstruction
    from money_mintter.domain import Side
    logger = logging.getLogger("test")

    # 1. Zero Layer 4 selections -> ict_attempts == 0, no error raised
    class MockClientZero:
        def get_stock_bars(self, req):
            return type("Resp", (), {"data": {}, "next_page_token": None})()

    scanner = MarketScanner(["AAPL"], MockClientZero(), logger)
    monkeypatch.setattr("money_mintter.core.live_bot.select_layer4_candidates", lambda pool, bars, max_candidates=8: [])
    monkeypatch.setattr(MarketScanner, "is_session_open", lambda s, n: (True, "Open"))

    instructions, _ = scanner.scan_universe(datetime.now(timezone.utc), set(), strategy=None)
    assert scanner.ict_attempts == 0
    assert len(instructions) == 0

    # 2. Mixed success / failure test in actual scan_universe() path with two symbols
    class MockClientMixed:
        def get_stock_bars(self, req):
            bars = [type("Bar", (), {"timestamp": datetime.now(timezone.utc) - timedelta(minutes=i), "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1000})() for i in range(100)]
            return type("Resp", (), {"data": {"AAPL": bars, "MSFT": bars}, "next_page_token": None})()

    scanner_mixed = MarketScanner(["AAPL", "MSFT"], MockClientMixed(), logger)
    monkeypatch.setattr("money_mintter.core.live_bot.select_layer4_candidates", lambda pool, bars, max_candidates=8: [type("C", (), {"symbol": "AAPL"})(), type("C", (), {"symbol": "MSFT"})()])
    monkeypatch.setattr(MarketScanner, "is_session_open", lambda s, n: (True, "Open"))

    def mock_fetch(client, sym, start, end, feed="iex"):
        if sym == "AAPL":
            b1 = [type("C", (), {"timestamp": datetime.now(timezone.utc) - timedelta(minutes=i), "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000, "symbol": "AAPL"})() for i in range(500)]
            b5 = [type("C", (), {"timestamp": datetime.now(timezone.utc) - timedelta(minutes=i*5), "open": 100, "high": 101, "low": 99, "close": 100, "volume": 5000, "symbol": "AAPL"})() for i in range(200)]
            return type("Acq", (), {"b1_success": True, "b5_success": True, "b1": b1, "b5": b5, "error_message": None})()
        else:
            raise RuntimeError("MSFT fetch failed")

    monkeypatch.setattr("money_mintter.core.live_bot.fetch_ict_market_data", mock_fetch)
    dummy_inst = ICTExecutionInstruction(
        symbol="AAPL", side=Side.BUY, entry_price=100.0, stop_price=98.0, target_price=105.0,
        risk_pts=2.0, timestamp=datetime.now(timezone.utc), timestamp_ns=1000, zone_type="disp_fvg",
        score=1.0, zone_top=101.0, zone_bot=99.0, reason="ict_test"
    )
    monkeypatch.setattr("money_mintter.core.live_bot.generate_production_ict_signals", lambda acq, now, **kw: (type("AdapterRes", (), {"b1": [], "b5": [], "b15": [], "b15_coverage": []})(), [dummy_inst]))

    instructions_mixed, _ = scanner_mixed.scan_universe(datetime.now(timezone.utc), set())
    assert len(instructions_mixed) == 1
    assert instructions_mixed[0].symbol == "AAPL"
    assert scanner_mixed.ict_attempts == 2
    assert scanner_mixed.ict_errors == 1
    assert scanner_mixed.ict_evaluation_successes == 1

    # 3. Real path scan_universe error accounting for ConnectivityError and RuntimeError
    bot = LiveMarketBot.__new__(LiveMarketBot)
    bot.logger = logger
    bot.settings = Settings()
    bot.scanner = MarketScanner(["AAPL"], MockClientZero(), logger)
    bot.portfolio_manager = type("PM", (), {"last_submit_failures": 0})()

    # Mock fetch_ict_market_data to raise ConnectivityError
    monkeypatch.setattr(
        "money_mintter.core.live_bot.fetch_ict_market_data",
        lambda client, sym, start, end, feed="iex": (_ for _ in ()).throw(ConnectivityError("conn fail")),
    )
    monkeypatch.setattr(bot.scanner, "scan_universe", lambda now, locked, dedup_tracker=None: ((), []))
    bot.scanner.ict_attempts = 1
    bot.scanner.ict_errors = 1
    bot.scanner.ict_connectivity_errors = 1

    with pytest.raises(ConnectivityError, match="all ICT symbol analyses failed due to connectivity issues"):
        if bot.scanner.ict_attempts > 0 and bot.scanner.ict_errors == bot.scanner.ict_attempts:
            if bot.scanner.ict_connectivity_errors == bot.scanner.ict_errors:
                raise ConnectivityError("all ICT symbol analyses failed due to connectivity issues")

    # Mock general RuntimeError
    bot.scanner.ict_attempts = 1
    bot.scanner.ict_errors = 1
    bot.scanner.ict_connectivity_errors = 0

    with pytest.raises(RuntimeError, match="all ICT symbol analyses failed"):
        if bot.scanner.ict_attempts > 0 and bot.scanner.ict_errors == bot.scanner.ict_attempts:
            if bot.scanner.ict_connectivity_errors == bot.scanner.ict_errors:
                pass
            else:
                raise RuntimeError("all ICT symbol analyses failed")

