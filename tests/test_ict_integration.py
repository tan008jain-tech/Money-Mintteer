from __future__ import annotations

import pytest
from copy import deepcopy
from datetime import datetime, timezone, timedelta, date as dt_date
from zoneinfo import ZoneInfo

from money_mintter.domain import Side
from money_mintter.strategy.candles import Candle
from money_mintter.strategy.ict.adapter import ICTAcquisitionResult, _create_verified_acquisition_result
from money_mintter.strategy.ict.integration import run_ict_integration, DiagnosticICTSignal, generate_production_ict_signals, ICTExecutionInstruction, ICTDeduplicationTracker
import money_mintter.strategy.ict.backtest_entry_modes as bem
from money_mintter.strategy.ict.v106_dynamic_rr_zone_entry import _G, INST


def test_ict_integration_adapter_and_generation() -> None:
    tz_utc = timezone.utc
    t_start = datetime(2026, 6, 2, 13, 30, tzinfo=tz_utc) # 08:30 CDT

    b1_list = [
        Candle(symbol="AAPL", timestamp=t_start + timedelta(minutes=i), open=100.0 + i*0.1, high=101.0 + i*0.1, low=99.0 + i*0.1, close=100.5 + i*0.1, volume=1000.0)
        for i in range(60)
    ]
    b5_list = [
        Candle(symbol="AAPL", timestamp=t_start + timedelta(minutes=i*5), open=100.0 + i, high=102.0 + i, low=98.0 + i, close=101.0 + i, volume=5000.0)
        for i in range(12)
    ]

    acq = _create_verified_acquisition_result(
        symbol="AAPL",
        b1_candles=tuple(b1_list),
        b5_candles=tuple(b5_list),
        feed="iex",
        b1_success=True,
        b5_success=True,
    )

    as_of = datetime(2026, 6, 2, 15, 0, tzinfo=tz_utc)
    eval_dates = [dt_date(2026, 6, 2)]

    adapter_res, signals = run_ict_integration(
        acquisition_result=acq,
        as_of=as_of,
        evaluation_dates=eval_dates,
    )

    assert adapter_res.symbol == "AAPL"
    assert adapter_res.feed == "iex"
    assert len(adapter_res.b5) == 12
    for sig in signals:
        assert sig.symbol == "AAPL"
        assert sig.is_diagnostic is True


def test_ict_integration_state_restoration_success(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = {"sentinel": "original_state_object"}
    monkeypatch.setitem(_G, "NQ", sentinel)
    inst_before = deepcopy(INST)

    tz_utc = timezone.utc
    t_start = datetime(2026, 6, 2, 13, 30, tzinfo=tz_utc)
    b1_list = [Candle(symbol="SPY", timestamp=t_start + timedelta(minutes=i), open=500.0, high=501.0, low=499.0, close=500.5, volume=1000.0) for i in range(10)]
    b5_list = [Candle(symbol="SPY", timestamp=t_start + timedelta(minutes=i*5), open=500.0, high=502.0, low=498.0, close=501.0, volume=5000.0) for i in range(2)]

    acq = _create_verified_acquisition_result(
        symbol="SPY",
        b1_candles=tuple(b1_list),
        b5_candles=tuple(b5_list),
        feed="iex",
        b1_success=True,
        b5_success=True,
    )

    as_of = datetime(2026, 6, 2, 14, 0, tzinfo=tz_utc)
    run_ict_integration(acq, as_of, [dt_date(2026, 6, 2)])

    assert _G["NQ"] is sentinel
    assert INST == inst_before


def test_ict_integration_state_restoration_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = "original_sentinel_object"
    monkeypatch.setitem(_G, "NQ", sentinel)
    inst_before = deepcopy(INST)

    tz_utc = timezone.utc
    t_start = datetime(2026, 6, 2, 13, 30, tzinfo=tz_utc)
    b1_list = [Candle(symbol="MSFT", timestamp=t_start + timedelta(minutes=i), open=400.0, high=401.0, low=399.0, close=400.5, volume=1000.0) for i in range(5)]
    b5_list = [Candle(symbol="MSFT", timestamp=t_start + timedelta(minutes=i*5), open=400.0, high=402.0, low=398.0, close=401.0, volume=5000.0) for i in range(1)]

    acq = _create_verified_acquisition_result(
        symbol="MSFT",
        b1_candles=tuple(b1_list),
        b5_candles=tuple(b5_list),
        feed="iex",
        b1_success=True,
        b5_success=True,
    )

    def mock_generate_all(dates):
        raise RuntimeError("simulated strategy failure")

    monkeypatch.setattr("money_mintter.strategy.ict.integration.generate_all", mock_generate_all)

    as_of = datetime(2026, 6, 2, 14, 0, tzinfo=tz_utc)
    with pytest.raises(RuntimeError, match="simulated strategy failure"):
        run_ict_integration(acq, as_of, [dt_date(2026, 6, 2)])

    assert _G["NQ"] is sentinel
    assert INST == inst_before


def test_ict_integration_sequential_symbols() -> None:
    tz_utc = timezone.utc
    t_start = datetime(2026, 6, 2, 13, 30, tzinfo=tz_utc)

    b1_aapl = [Candle(symbol="AAPL", timestamp=t_start + timedelta(minutes=i), open=200.0, high=201.0, low=199.0, close=200.5, volume=1000.0) for i in range(10)]
    b5_aapl = [Candle(symbol="AAPL", timestamp=t_start + timedelta(minutes=i*5), open=200.0, high=202.0, low=198.0, close=201.0, volume=5000.0) for i in range(2)]

    b1_spy = [Candle(symbol="SPY", timestamp=t_start + timedelta(minutes=i), open=500.0, high=501.0, low=499.0, close=500.5, volume=1000.0) for i in range(10)]
    b5_spy = [Candle(symbol="SPY", timestamp=t_start + timedelta(minutes=i*5), open=500.0, high=502.0, low=498.0, close=501.0, volume=5000.0) for i in range(2)]

    acq_aapl = _create_verified_acquisition_result(symbol="AAPL", b1_candles=tuple(b1_aapl), b5_candles=tuple(b5_aapl), feed="iex", b1_success=True, b5_success=True)
    acq_spy = _create_verified_acquisition_result(symbol="SPY", b1_candles=tuple(b1_spy), b5_candles=tuple(b5_spy), feed="iex", b1_success=True, b5_success=True)

    as_of = datetime(2026, 6, 2, 14, 0, tzinfo=tz_utc)

    _, sigs_aapl = run_ict_integration(acq_aapl, as_of, [dt_date(2026, 6, 2)])
    _, sigs_spy = run_ict_integration(acq_spy, as_of, [dt_date(2026, 6, 2)])

    for s in sigs_aapl:
        assert s.symbol == "AAPL"
    for s in sigs_spy:
        assert s.symbol == "SPY"
    assert "NQ" not in _G


def test_ict_integration_mock_signal_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_raw_signal = {
        "sym": "NQ",
        "date": dt_date(2026, 6, 2),
        "side": "bull",
        "t": 1234567890000000,
        "e": 100.5,
        "sp": 98.0,
        "risk": 2.5,
        "score": 4,
        "zt": "ifvg",
        "rej": True,
        "hour": 9,
        "cisd": True,
        "struct_15m": True,
        "sw15": True,
    }

    def mock_generate_all(dates):
        return [mock_raw_signal]

    monkeypatch.setattr("money_mintter.strategy.ict.integration.generate_all", mock_generate_all)

    tz_utc = timezone.utc
    t_start = datetime(2026, 6, 2, 13, 30, tzinfo=tz_utc)
    b1_list = [Candle(symbol="TSLA", timestamp=t_start + timedelta(minutes=i), open=200.0, high=201.0, low=199.0, close=200.5, volume=1000.0) for i in range(5)]
    b5_list = [Candle(symbol="TSLA", timestamp=t_start + timedelta(minutes=i*5), open=200.0, high=202.0, low=198.0, close=201.0, volume=5000.0) for i in range(1)]
    acq = _create_verified_acquisition_result(symbol="TSLA", b1_candles=tuple(b1_list), b5_candles=tuple(b5_list), feed="iex", b1_success=True, b5_success=True)

    as_of = datetime(2026, 6, 2, 14, 0, tzinfo=tz_utc)

    _, signals = run_ict_integration(acq, as_of, [dt_date(2026, 6, 2)])

    assert len(signals) == 1
    sig = signals[0]
    assert sig.symbol == "TSLA"
    assert sig.date == dt_date(2026, 6, 2)
    assert sig.side == "bull"
    assert sig.timestamp_ns == 1234567890000000
    assert sig.entry_price == 100.5
    assert sig.stop_loss == 98.0
    assert sig.risk == 2.5
    assert sig.score == 4
    assert sig.zone_type == "ifvg"
    assert sig.rejection is True
    assert sig.hour == 9
    assert sig.cisd_passed is True
    assert sig.structure_15m_passed is True
    assert sig.sweep_15m is True
    assert sig.is_diagnostic is True

    with pytest.raises(Exception):
        sig.symbol = "EXCHANGE_ORDER"


def test_ict_diagnostic_signal_immutability_and_non_executable() -> None:
    tz_utc = timezone.utc
    t_start = datetime(2026, 6, 2, 13, 30, tzinfo=tz_utc)
    b1_list = [Candle(symbol="GOOG", timestamp=t_start + timedelta(minutes=i), open=150.0, high=151.0, low=149.0, close=150.5, volume=1000.0) for i in range(10)]
    b5_list = [Candle(symbol="GOOG", timestamp=t_start + timedelta(minutes=i*5), open=150.0, high=152.0, low=148.0, close=151.0, volume=5000.0) for i in range(2)]
    acq = _create_verified_acquisition_result(symbol="GOOG", b1_candles=tuple(b1_list), b5_candles=tuple(b5_list), feed="iex", b1_success=True, b5_success=True)

    as_of = datetime(2026, 6, 2, 14, 0, tzinfo=tz_utc)
    _, signals = run_ict_integration(acq, as_of, [dt_date(2026, 6, 2)])

    for sig in signals:
        assert sig.is_diagnostic is True
        with pytest.raises(Exception):
            sig.symbol = "EXCHANGE_ORDER"


def test_generate_production_ict_signals_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_raw_signal = {
        "side": "bull",
        "ns": 1780400000000000000,
        "ep_close": 100.0,
        "ep_zone": 100.0,
        "sp": 98.0,
        "sp_struct": 98.0,
        "score": 3,
        "zt": "disp_fvg",
        "zone_top": 101.0,
        "zone_bot": 99.0,
        "time": datetime(2026, 6, 2, 10, 0, tzinfo=ZoneInfo("America/Chicago")),
        "close_confirmed": True,
        "c1_b1_idx": 0,
        "bar_idx": 0,
        "c1_close": 100.0,
        "c1_high": 101.0,
        "c1_low": 99.0,
        "c1_open": 99.5,
    }

    def mock_gen_sweep_entries(*args, **kwargs):
        return [mock_raw_signal]

    monkeypatch.setattr("money_mintter.strategy.ict.integration.gen_sweep_entries_enriched", mock_gen_sweep_entries)

    tz_utc = timezone.utc
    t_start = datetime(2026, 6, 2, 13, 30, tzinfo=tz_utc)
    b1_list = [Candle(symbol="AAPL", timestamp=t_start + timedelta(minutes=i), open=100.0, high=101.0, low=99.0, close=100.5, volume=1000.0) for i in range(10)]
    b5_list = [Candle(symbol="AAPL", timestamp=t_start + timedelta(minutes=i*5), open=100.0, high=102.0, low=98.0, close=101.0, volume=5000.0) for i in range(2)]
    acq = _create_verified_acquisition_result(symbol="AAPL", b1_candles=tuple(b1_list), b5_candles=tuple(b5_list), feed="iex", b1_success=True, b5_success=True)

    as_of = datetime(2026, 6, 2, 15, 0, tzinfo=tz_utc)
    adapter_res, instructions = generate_production_ict_signals(acq, as_of, [dt_date(2026, 6, 2)], _allow_historical_evaluation=True)

    assert adapter_res.symbol == "AAPL"
    assert len(instructions) == 1
    inst = instructions[0]
    assert inst.symbol == "AAPL"
    assert inst.side == Side.BUY
    assert inst.entry_price == 100.0
    assert inst.stop_price == 98.0
    # 1.1 RR target: entry + risk_pts * 1.1 * direction = 100.0 + 2.0 * 1.1 * 1.0 = 102.2
    assert inst.target_price == pytest.approx(102.2)
    assert inst.risk_pts == 2.0
    assert inst.zone_type == "disp_fvg"
    assert inst.is_diagnostic is False


def test_production_ict_ifvg_time_restriction(monkeypatch: pytest.MonkeyPatch) -> None:
    # IFVG before 9:00 CT (e.g. 08:30 CT) should be excluded
    early_ns = int(datetime(2026, 6, 2, 8, 30, tzinfo=ZoneInfo("America/Chicago")).timestamp() * 1e9)
    late_ns = int(datetime(2026, 6, 2, 9, 30, tzinfo=ZoneInfo("America/Chicago")).timestamp() * 1e9)

    raw_early = {
        "side": "bull",
        "ns": early_ns,
        "ep_close": 100.0,
        "ep_zone": 100.0,
        "sp": 98.0,
        "sp_struct": 98.0,
        "score": 3,
        "zt": "ifvg",
        "zone_top": 101.0,
        "zone_bot": 99.0,
        "time": datetime(2026, 6, 2, 8, 30, tzinfo=ZoneInfo("America/Chicago")),
        "close_confirmed": True,
        "c1_b1_idx": 0,
        "bar_idx": 0,
        "c1_close": 100.0,
        "c1_high": 101.0,
        "c1_low": 99.0,
        "c1_open": 99.5,
    }
    raw_late = {
        "side": "bull",
        "ns": late_ns,
        "ep_close": 100.0,
        "ep_zone": 100.0,
        "sp": 98.0,
        "sp_struct": 98.0,
        "score": 3,
        "zt": "ifvg",
        "zone_top": 101.0,
        "zone_bot": 99.0,
        "time": datetime(2026, 6, 2, 9, 30, tzinfo=ZoneInfo("America/Chicago")),
        "close_confirmed": True,
        "c1_b1_idx": 0,
        "bar_idx": 0,
        "c1_close": 100.0,
        "c1_high": 101.0,
        "c1_low": 99.0,
        "c1_open": 99.5,
    }

    def mock_gen_sweep_entries(*args, **kwargs):
        return [raw_early, raw_late]

    monkeypatch.setattr("money_mintter.strategy.ict.integration.gen_sweep_entries_enriched", mock_gen_sweep_entries)

    tz_utc = timezone.utc
    t_start = datetime(2026, 6, 2, 13, 30, tzinfo=tz_utc)
    b1_list = [Candle(symbol="MSFT", timestamp=t_start + timedelta(minutes=i), open=100.0, high=101.0, low=99.0, close=100.5, volume=1000.0) for i in range(10)]
    b5_list = [Candle(symbol="MSFT", timestamp=t_start + timedelta(minutes=i*5), open=100.0, high=102.0, low=98.0, close=101.0, volume=5000.0) for i in range(2)]
    acq = _create_verified_acquisition_result(symbol="MSFT", b1_candles=tuple(b1_list), b5_candles=tuple(b5_list), feed="iex", b1_success=True, b5_success=True)

    as_of = datetime(2026, 6, 2, 16, 0, tzinfo=tz_utc)
    _, instructions = generate_production_ict_signals(acq, as_of, [dt_date(2026, 6, 2)], _allow_historical_evaluation=True)

    # Only late_ns (9:30 CT) should be retained, early_ns (8:30 CT) excluded
    assert len(instructions) == 1
    assert instructions[0].timestamp_ns == late_ns


def test_persistent_deduplication_tracker(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_raw_signal = {
        "side": "bull",
        "ns": 1780400000000000000,
        "ep_close": 100.0,
        "ep_zone": 100.0,
        "sp": 98.0,
        "sp_struct": 98.0,
        "score": 3,
        "zt": "disp_fvg",
        "zone_top": 101.0,
        "zone_bot": 99.0,
        "time": datetime(2026, 6, 2, 10, 0, tzinfo=ZoneInfo("America/Chicago")),
        "close_confirmed": True,
        "c1_b1_idx": 0,
        "bar_idx": 0,
        "c1_close": 100.0,
        "c1_high": 101.0,
        "c1_low": 99.0,
        "c1_open": 99.5,
    }

    def mock_gen_sweep_entries(*args, **kwargs):
        return [mock_raw_signal]

    monkeypatch.setattr("money_mintter.strategy.ict.integration.gen_sweep_entries_enriched", mock_gen_sweep_entries)

    tz_utc = timezone.utc
    t_start = datetime(2026, 6, 2, 13, 30, tzinfo=tz_utc)
    b1_list = [Candle(symbol="AAPL", timestamp=t_start + timedelta(minutes=i), open=100.0, high=101.0, low=99.0, close=100.5, volume=1000.0) for i in range(10)]
    b5_list = [Candle(symbol="AAPL", timestamp=t_start + timedelta(minutes=i*5), open=100.0, high=102.0, low=98.0, close=101.0, volume=5000.0) for i in range(2)]
    acq = _create_verified_acquisition_result(symbol="AAPL", b1_candles=tuple(b1_list), b5_candles=tuple(b5_list), feed="iex", b1_success=True, b5_success=True)

    as_of = datetime(2026, 6, 2, 15, 0, tzinfo=tz_utc)
    tracker = ICTDeduplicationTracker()

    _, instructions1 = generate_production_ict_signals(acq, as_of, [dt_date(2026, 6, 2)], dedup_tracker=tracker, _allow_historical_evaluation=True)
    assert len(instructions1) == 1

    # Call again with same tracker without marking executed -> unsubmitted signal remains recoverable
    _, instructions2 = generate_production_ict_signals(acq, as_of, [dt_date(2026, 6, 2)], dedup_tracker=tracker, _allow_historical_evaluation=True)
    assert len(instructions2) == 1

    # Mark zone as executed -> deduplicated (0 new instructions)
    tracker.mark_executed("AAPL", "bull", "disp_fvg", 101.0, 99.0)
    _, instructions3 = generate_production_ict_signals(acq, as_of, [dt_date(2026, 6, 2)], dedup_tracker=tracker, _allow_historical_evaluation=True)
    assert len(instructions3) == 0


def test_deduplication_lifecycle_executed_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_raw_signal = {
        "side": "bull",
        "ns": 1780400000000000000,
        "ep_close": 100.0,
        "ep_zone": 100.0,
        "sp": 98.0,
        "sp_struct": 98.0,
        "score": 3,
        "zt": "disp_fvg",
        "zone_top": 101.0,
        "zone_bot": 99.0,
        "time": datetime(2026, 6, 2, 10, 0, tzinfo=ZoneInfo("America/Chicago")),
        "close_confirmed": True,
        "c1_b1_idx": 0,
        "bar_idx": 0,
        "c1_close": 100.0,
        "c1_high": 101.0,
        "c1_low": 99.0,
        "c1_open": 99.5,
    }

    def mock_gen_sweep_entries(*args, **kwargs):
        return [mock_raw_signal]

    monkeypatch.setattr("money_mintter.strategy.ict.integration.gen_sweep_entries_enriched", mock_gen_sweep_entries)

    tz_utc = timezone.utc
    t_start = datetime(2026, 6, 2, 13, 30, tzinfo=tz_utc)
    b1_list = [Candle(symbol="AAPL", timestamp=t_start + timedelta(minutes=i), open=100.0, high=101.0, low=99.0, close=100.5, volume=1000.0) for i in range(10)]
    b5_list = [Candle(symbol="AAPL", timestamp=t_start + timedelta(minutes=i*5), open=100.0, high=102.0, low=98.0, close=101.0, volume=5000.0) for i in range(2)]
    acq = _create_verified_acquisition_result(symbol="AAPL", b1_candles=tuple(b1_list), b5_candles=tuple(b5_list), feed="iex", b1_success=True, b5_success=True)

    as_of = datetime(2026, 6, 2, 15, 0, tzinfo=tz_utc)
    tracker = ICTDeduplicationTracker()
    # Mark zone as executed prior to generation
    tracker.mark_executed("AAPL", "bull", "disp_fvg", 101.0, 99.0)

    _, instructions = generate_production_ict_signals(acq, as_of, [dt_date(2026, 6, 2)], dedup_tracker=tracker, _allow_historical_evaluation=True)
    assert len(instructions) == 0


def test_no_global_nq_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    slip_before = bem.SLIP
    max_risk_before = bem.MAX_RISK

    mock_raw_signal = {
        "side": "bull",
        "ns": 1780400000000000000,
        "ep_close": 100.0,
        "ep_zone": 100.0,
        "sp": 98.0,
        "sp_struct": 98.0,
        "score": 3,
        "zt": "disp_fvg",
        "zone_top": 101.0,
        "zone_bot": 99.0,
        "time": datetime(2026, 6, 2, 10, 0, tzinfo=ZoneInfo("America/Chicago")),
        "close_confirmed": True,
        "c1_b1_idx": 0,
        "bar_idx": 0,
        "c1_close": 100.0,
        "c1_high": 101.0,
        "c1_low": 99.0,
        "c1_open": 99.5,
    }

    def mock_gen_sweep_entries(*args, **kwargs):
        return [mock_raw_signal]

    monkeypatch.setattr("money_mintter.strategy.ict.integration.gen_sweep_entries_enriched", mock_gen_sweep_entries)

    tz_utc = timezone.utc
    t_start = datetime(2026, 6, 2, 13, 30, tzinfo=tz_utc)
    b1_list = [Candle(symbol="AAPL", timestamp=t_start + timedelta(minutes=i), open=100.0, high=101.0, low=99.0, close=100.5, volume=1000.0) for i in range(10)]
    b5_list = [Candle(symbol="AAPL", timestamp=t_start + timedelta(minutes=i*5), open=100.0, high=102.0, low=98.0, close=101.0, volume=5000.0) for i in range(2)]
    acq = _create_verified_acquisition_result(symbol="AAPL", b1_candles=tuple(b1_list), b5_candles=tuple(b5_list), feed="iex", b1_success=True, b5_success=True)

    as_of = datetime(2026, 6, 2, 15, 0, tzinfo=tz_utc)
    generate_production_ict_signals(acq, as_of, [dt_date(2026, 6, 2)], _allow_historical_evaluation=True)

    assert bem.SLIP == slip_before
    assert bem.MAX_RISK == max_risk_before


def test_current_session_resolution_and_fail_closed() -> None:
    tz_utc = timezone.utc
    t_start = datetime(2026, 6, 2, 13, 30, tzinfo=tz_utc)
    b1_list = [Candle(symbol="AAPL", timestamp=t_start + timedelta(minutes=i), open=100.0, high=101.0, low=99.0, close=100.5, volume=1000.0) for i in range(10)]
    b5_list = [Candle(symbol="AAPL", timestamp=t_start + timedelta(minutes=i*5), open=100.0, high=102.0, low=98.0, close=101.0, volume=5000.0) for i in range(2)]
    acq = _create_verified_acquisition_result(symbol="AAPL", b1_candles=tuple(b1_list), b5_candles=tuple(b5_list), feed="iex", b1_success=True, b5_success=True)

    as_of = datetime(2026, 6, 5, 15, 0, tzinfo=tz_utc)
    with pytest.raises(ValueError, match="Current session date .* cannot be resolved or validated"):
        generate_production_ict_signals(acq, as_of, evaluation_dates=None)


def test_production_historical_override_rejection() -> None:
    tz_utc = timezone.utc
    t_start = datetime(2026, 6, 2, 13, 30, tzinfo=tz_utc)
    b1_list = [Candle(symbol="AAPL", timestamp=t_start + timedelta(minutes=i), open=100.0, high=101.0, low=99.0, close=100.5, volume=1000.0) for i in range(10)]
    b5_list = [Candle(symbol="AAPL", timestamp=t_start + timedelta(minutes=i*5), open=100.0, high=102.0, low=98.0, close=101.0, volume=5000.0) for i in range(2)]
    acq = _create_verified_acquisition_result(symbol="AAPL", b1_candles=tuple(b1_list), b5_candles=tuple(b5_list), feed="iex", b1_success=True, b5_success=True)

    as_of = datetime(2026, 6, 2, 15, 0, tzinfo=tz_utc)
    with pytest.raises(ValueError, match="Historical multi-date evaluation .* restricted to testing-only"):
        generate_production_ict_signals(acq, as_of, evaluation_dates=[dt_date(2026, 6, 2)], _allow_historical_evaluation=False)
