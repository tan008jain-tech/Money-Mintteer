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

    # 2. Non-IEX feed rejected
    res_sip = _create_verified_acquisition_result(symbol="AAPL", b1_candles=(c,), b5_candles=(c,), feed="sip", b1_success=True, b5_success=True)
    with pytest.raises(ValueError, match="Non-IEX feed rejected"):
        adapt_market_data(res_sip, aware_dt + timedelta(minutes=10))

    # 3. Unsuccessful acquisition rejected
    res_fail = _create_verified_acquisition_result(symbol="AAPL", b1_candles=(), b5_candles=(), feed="iex", b1_success=False, b5_success=True, error_message="fail")
    with pytest.raises(ValueError, match="Unsuccessful or incomplete acquisition result rejected"):
        adapt_market_data(res_fail, aware_dt + timedelta(minutes=10))

    # 4. Dataset completeness: Empty 1m stream despite successful API request rejected
    res_empty_1m = _create_verified_acquisition_result(symbol="AAPL", b1_candles=(), b5_candles=(c,), feed="iex", b1_success=True, b5_success=True)
    with pytest.raises(ValueError, match="Dataset completeness rejection: 1m stream is empty"):
        adapt_market_data(res_empty_1m, aware_dt + timedelta(minutes=10))

    # 5. Dataset completeness: Empty 5m stream despite successful API request rejected
    res_empty_5m = _create_verified_acquisition_result(symbol="AAPL", b1_candles=(c,), b5_candles=(), feed="iex", b1_success=True, b5_success=True)
    with pytest.raises(ValueError, match="Dataset completeness rejection: 5m stream is empty"):
        adapt_market_data(res_empty_5m, aware_dt + timedelta(minutes=10))

    # 6. Dataset completeness: Both streams empty rejected
    res_empty_both = _create_verified_acquisition_result(symbol="AAPL", b1_candles=(), b5_candles=(), feed="iex", b1_success=True, b5_success=True)
    with pytest.raises(ValueError, match="Dataset completeness rejection: 1m stream is empty"):
        adapt_market_data(res_empty_both, aware_dt + timedelta(minutes=10))

    # 7. Dataset completeness: Streams becoming empty after lookahead filtering rejected
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


def test_adapter_15m_completeness_and_failures() -> None:
    tz_utc = timezone.utc
    t_candle = datetime(2026, 6, 2, 13, 30, tzinfo=tz_utc)
    c5_list = [
        Candle(symbol="SPY", timestamp=t_candle, open=500.0, high=502.0, low=499.0, close=501.0, volume=1000.0),
        Candle(symbol="SPY", timestamp=t_candle + timedelta(minutes=5), open=501.0, high=503.0, low=500.0, close=502.0, volume=1000.0),
        Candle(symbol="SPY", timestamp=t_candle + timedelta(minutes=10), open=502.0, high=504.0, low=501.0, close=503.0, volume=1000.0),
        Candle(symbol="SPY", timestamp=t_candle + timedelta(minutes=25), open=505.0, high=507.0, low=504.0, close=506.0, volume=1000.0),
        Candle(symbol="SPY", timestamp=t_candle + timedelta(minutes=30), open=507.0, high=509.0, low=506.0, close=508.0, volume=1000.0),
        Candle(symbol="SPY", timestamp=t_candle + timedelta(minutes=35), open=508.0, high=510.0, low=507.0, close=509.0, volume=1000.0),
        Candle(symbol="SPY", timestamp=t_candle + timedelta(minutes=40), open=509.0, high=511.0, low=508.0, close=510.0, volume=1000.0),
    ]
    c1_list = [Candle(symbol="SPY", timestamp=t_candle + timedelta(minutes=i), open=500.0, high=502.0, low=499.0, close=501.0, volume=1000.0) for i in range(50)]
    acq = _create_verified_acquisition_result(symbol="SPY", b1_candles=tuple(c1_list), b5_candles=tuple(c5_list), feed="iex", b1_success=True, b5_success=True)
    as_of = datetime(2026, 6, 2, 16, 0, tzinfo=tz_utc)

    with pytest.raises(ValueError, match="Internal missing or incomplete 15m bucket detected"):
        adapt_market_data(acq, as_of)


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


def test_adapter_entirely_absent_internal_bucket() -> None:
    chicago_tz = ZoneInfo("America/Chicago")
    t_start = datetime(2026, 6, 2, 13, 30, tzinfo=chicago_tz).astimezone(timezone.utc)
    
    c5_list = [
        Candle(symbol="SPY", timestamp=t_start, open=500.0, high=502.0, low=499.0, close=501.0, volume=1000.0),
        Candle(symbol="SPY", timestamp=t_start + timedelta(minutes=5), open=501.0, high=503.0, low=500.0, close=502.0, volume=1000.0),
        Candle(symbol="SPY", timestamp=t_start + timedelta(minutes=10), open=502.0, high=504.0, low=501.0, close=503.0, volume=1000.0),
        # Gap of 15 minutes (bucket 13:45-14:00 missing entirely)
        Candle(symbol="SPY", timestamp=t_start + timedelta(minutes=30), open=505.0, high=507.0, low=504.0, close=506.0, volume=1000.0),
        Candle(symbol="SPY", timestamp=t_start + timedelta(minutes=35), open=506.0, high=508.0, low=505.0, close=507.0, volume=1000.0),
        Candle(symbol="SPY", timestamp=t_start + timedelta(minutes=40), open=507.0, high=509.0, low=506.0, close=508.0, volume=1000.0),
    ]
    c1_list = [Candle(symbol="SPY", timestamp=t_start + timedelta(minutes=i), open=500.0, high=502.0, low=499.0, close=501.0, volume=1000.0) for i in range(50)]
    acq = _create_verified_acquisition_result(symbol="SPY", b1_candles=tuple(c1_list), b5_candles=tuple(c5_list), feed="iex", b1_success=True, b5_success=True)
    as_of = datetime(2026, 6, 2, 16, 0, tzinfo=chicago_tz).astimezone(timezone.utc)

    with pytest.raises(ValueError, match="Entirely absent internal 15m bucket gap detected"):
        adapt_market_data(acq, as_of)
