from __future__ import annotations

import os
import json
import tempfile
import logging
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch
import pytest
import pandas as pd

from alpaca.data.enums import DataFeed
from money_mintter.config import Settings
from money_mintter.core.suitability import DailySuitabilityEngine, SuitabilityResult, SymbolSuitabilityMetrics
from money_mintter.core.eligibility import StructuralEligibilityEngine
from money_mintter.core.live_bot import MarketScanner, discover_universe


class MockBar:
    def __init__(self, timestamp: datetime, open: float, high: float, low: float, close: float, volume: float, vwap: float = 0.0):
        self.timestamp = timestamp
        self.open = open
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume
        self.vwap = vwap


def create_bars_for_symbol(
    base_price: float = 100.0,
    num_bars: int = 25,
    daily_volume: float = 200_000.0,
    volatility_pct: float = 0.02,
    start_date: datetime | None = None,
) -> list[MockBar]:
    start_date = start_date or (datetime.now(timezone.utc) - timedelta(days=40))
    bars = []
    current_dt = start_date
    price = base_price
    for i in range(num_bars):
        while current_dt.weekday() >= 5: # Saturday or Sunday
            current_dt += timedelta(days=1)
        
        high = price * (1.0 + volatility_pct)
        low = price * (1.0 - volatility_pct)
        close = price * (1.0 + 0.001 * (i % 3 - 1))
        open_p = (high + low) / 2.0
        volume = daily_volume

        bars.append(MockBar(timestamp=current_dt, open=open_p, high=high, low=low, close=close, volume=volume, vwap=close))
        current_dt += timedelta(days=1)
        price = close
    return bars


def test_result_interface_alignment() -> None:
    settings = Settings()
    with tempfile.TemporaryDirectory() as tmpdir:
        stock_client = MagicMock()
        bars = create_bars_for_symbol(base_price=100.0, num_bars=20, daily_volume=500_000.0)
        stock_client.get_stock_bars.return_value = MagicMock(data={"INTF": bars}, next_page_token=None)

        engine = DailySuitabilityEngine(settings=settings, stock_client=stock_client, cache_dir=tmpdir, historical_feed="sip")
        res = engine.evaluate_universe(["INTF"], force_refresh=True)

        assert hasattr(res, "data_source")
        assert hasattr(res, "actual_feed")
        assert hasattr(res, "requested_feed")
        assert hasattr(res, "stale")
        assert hasattr(res, "cached")
        assert hasattr(res, "generated_at")
        assert res.actual_feed == res.data_source


def test_no_raw_layer1_fallback_on_failure() -> None:
    settings = Settings()
    with tempfile.TemporaryDirectory() as tmpdir:
        stock_client = MagicMock()
        # API fetch raises exception and no cache exists
        stock_client.get_stock_bars.side_effect = RuntimeError("API down")

        engine = DailySuitabilityEngine(settings=settings, stock_client=stock_client, cache_dir=tmpdir, historical_feed="sip")
        with pytest.raises(RuntimeError, match="Layer 2 historical bar fetch failed and no valid compatible cached pool exists."):
            engine.evaluate_universe(["FAILSYM"], force_refresh=True)


def test_genuinely_empty_pool_remains_empty() -> None:
    settings = Settings(min_latest_close=1000.0) # 0 stocks pass
    with tempfile.TemporaryDirectory() as tmpdir:
        stock_client = MagicMock()
        bars = create_bars_for_symbol(base_price=50.0, num_bars=20, daily_volume=500_000.0)
        stock_client.get_stock_bars.return_value = MagicMock(data={"CHEAP": bars}, next_page_token=None)

        engine = DailySuitabilityEngine(settings=settings, stock_client=stock_client, cache_dir=tmpdir, historical_feed="sip")
        res = engine.evaluate_universe(["CHEAP"], force_refresh=True)

        assert res.pool == []
        assert res.stale is False


def test_explicit_sip_feed_and_no_batch_mixing() -> None:
    settings = Settings()
    with tempfile.TemporaryDirectory() as tmpdir:
        stock_client = MagicMock()
        bars = create_bars_for_symbol(base_price=100.0, num_bars=20, daily_volume=500_000.0)

        def mock_bars(req):
            assert req.feed == DataFeed.SIP
            return MagicMock(data={"SIPSYM": bars}, next_page_token=None)

        stock_client.get_stock_bars.side_effect = mock_bars

        engine = DailySuitabilityEngine(settings=settings, stock_client=stock_client, cache_dir=tmpdir, historical_feed="sip")
        res = engine.evaluate_universe(["SIPSYM"], force_refresh=True)

        assert "SIPSYM" in res.pool
        assert res.requested_feed == "SIP"
        assert res.actual_feed == "SIP"


def test_pagination_and_later_batch_failure() -> None:
    settings = Settings()
    with tempfile.TemporaryDirectory() as tmpdir:
        stock_client = MagicMock()
        bars = create_bars_for_symbol(base_price=100.0, num_bars=20, daily_volume=500_000.0)

        # First evaluation succeeds and caches valid pool
        stock_client.get_stock_bars.return_value = MagicMock(data={"GOOD": bars}, next_page_token=None)
        engine = DailySuitabilityEngine(settings=settings, stock_client=stock_client, cache_dir=tmpdir, historical_feed="sip")
        r1 = engine.evaluate_universe(["GOOD"], force_refresh=True)
        assert "GOOD" in r1.pool

        # Second evaluation with same universe where batch fails -> should retain last valid pool as stale
        stock_client.get_stock_bars.side_effect = RuntimeError("Batch failure")
        r2 = engine.evaluate_universe(["GOOD"], force_refresh=True)
        assert "GOOD" in r2.pool
        assert r2.stale is True


def test_batch_bar_retrieval_and_cache_validation() -> None:
    settings = Settings()
    with tempfile.TemporaryDirectory() as tmpdir:
        stock_client = MagicMock()
        bars_sym1 = create_bars_for_symbol(base_price=100.0, num_bars=20, daily_volume=500_000.0)
        bars_sym2 = create_bars_for_symbol(base_price=5.0, num_bars=20, daily_volume=500_000.0)

        stock_client.get_stock_bars.return_value = MagicMock(data={"SYM1": bars_sym1, "SYM2": bars_sym2})

        engine = DailySuitabilityEngine(settings=settings, stock_client=stock_client, cache_dir=tmpdir, historical_feed="sip")
        res = engine.evaluate_universe(["SYM1", "SYM2"], force_refresh=True)

        assert "SYM1" in res.pool
        assert "SYM2" not in res.pool  # SYM2 close 5.0 < min_latest_close 10.0

        # Test cache reuse with valid config and universe signature
        res_cached = engine.evaluate_universe(["SYM1", "SYM2"], force_refresh=False)
        assert res_cached.cached is True
        assert "SYM1" in res_cached.pool


def test_cache_first_startup_layer2_integration() -> None:
    settings = Settings()
    with tempfile.TemporaryDirectory() as tmpdir:
        # Pre-seed Layer 1 cache and Layer 2 pool cache
        l1_engine = StructuralEligibilityEngine(cache_dir=tmpdir)
        l1_engine.save_persisted_cache(["CACHE_SYM"], {"CACHE_SYM": {}}, datetime.now(timezone.utc))

        stock_client = MagicMock()
        bars = create_bars_for_symbol(base_price=100.0, num_bars=20, daily_volume=500_000.0)
        stock_client.get_stock_bars.return_value = MagicMock(data={"CACHE_SYM": bars}, next_page_token=None)

        l2_engine = DailySuitabilityEngine(settings=settings, stock_client=stock_client, cache_dir=tmpdir, historical_feed="sip")
        # Evaluate to generate Layer 2 pool cache
        res_initial = l2_engine.evaluate_universe(["CACHE_SYM"], force_refresh=True)
        assert "CACHE_SYM" in res_initial.pool

        # Now test loading persisted pool cache via DailySuitabilityEngine
        loaded_res = l2_engine.load_persisted_pool()
        assert loaded_res is not None
        assert "CACHE_SYM" in loaded_res.pool


def test_calendar_aware_session_expiration() -> None:
    settings = Settings()
    with tempfile.TemporaryDirectory() as tmpdir:
        engine = DailySuitabilityEngine(settings=settings, cache_dir=tmpdir)
        now = datetime.now(timezone.utc)
        past_gen = now - timedelta(days=5)
        assert engine._is_session_expired(past_gen, now) is True


def test_stale_fallback_age_policy_rejection() -> None:
    settings = Settings()
    with tempfile.TemporaryDirectory() as tmpdir:
        engine = DailySuitabilityEngine(settings=settings, cache_dir=tmpdir)
        old_time = datetime.now(timezone.utc) - timedelta(days=10)
        res = SuitabilityResult(
            pool=["AAPL"],
            quarantined=[],
            metrics={},
            exclusion_counts={},
            requested_feed="SIP",
            actual_feed="SIP",
            data_source="SIP",
            generated_at=old_time.isoformat(),
            stale=False,
            cached=False,
            config_signature=engine._compute_config_signature(),
            universe_signature=engine._compute_universe_signature(["AAPL"]),
        )
        engine.save_persisted_pool(res)
        fallback = engine.load_stale_fallback_pool(max_stale_sessions=3)
        assert fallback is None


def test_successfully_evaluated_empty_pool_validity() -> None:
    settings = Settings()
    with tempfile.TemporaryDirectory() as tmpdir:
        engine = DailySuitabilityEngine(settings=settings, cache_dir=tmpdir)
        now = datetime.now(timezone.utc)
        res = SuitabilityResult(
            pool=[],
            quarantined=[],
            metrics={},
            exclusion_counts={"low_price": 5},
            requested_feed="SIP",
            actual_feed="SIP",
            data_source="SIP",
            generated_at=now.isoformat(),
            stale=False,
            cached=False,
            config_signature=engine._compute_config_signature(),
            universe_signature=engine._compute_universe_signature(["CHEAP"]),
        )
        engine.save_persisted_pool(res)
        loaded = engine.load_persisted_pool()
        assert loaded is not None
        assert loaded.pool == []
        assert loaded.stale is False


def test_position_management_continuity_on_l2_failure() -> None:
    settings = Settings()
    client = MagicMock()
    acc = MagicMock()
    acc.cash = "50000.0"
    acc.equity = "100000.0"
    acc.buying_power = "100000.0"
    client.get_account.return_value = acc
    client.get_all_positions.return_value = [
        MagicMock(symbol="AAPL", qty="10", avg_entry_price="150.0", current_price="155.0", unrealized_pl="50.0")
    ]
    client.get_orders.return_value = []

    from money_mintter.core.live_bot import LivePortfolioManager
    logger = logging.getLogger("test_pos")
    pm = LivePortfolioManager(client, logger)

    cash, equity, bp, open_pos, dash_pos, locked_syms = pm.fetch_account_and_positions()
    assert equity == 100000.0
    assert "AAPL" in locked_syms
    assert len(open_pos) == 1

    orders_sent = pm.submit_bracket_orders([], locked_syms, equity, settings)
    assert orders_sent == 0


def test_bypass_paths_never_leak_raw_layer1() -> None:
    settings = Settings()
    with tempfile.TemporaryDirectory() as tmpdir:
        stock_client = MagicMock()
        stock_client.get_stock_bars.side_effect = RuntimeError("API error")
        engine = DailySuitabilityEngine(settings=settings, stock_client=stock_client, cache_dir=tmpdir)
        with pytest.raises(RuntimeError):
            engine.evaluate_universe(["RAW1", "RAW2"], force_refresh=True)
        cached = engine.load_persisted_pool()
        assert cached is None


def test_stale_fallback_fail_closed_without_eligible_symbols() -> None:
    settings = Settings()
    with tempfile.TemporaryDirectory() as tmpdir:
        engine = DailySuitabilityEngine(settings=settings, cache_dir=tmpdir)
        now = datetime.now(timezone.utc)
        res = SuitabilityResult(
            pool=["AAPL"],
            quarantined=[],
            metrics={},
            exclusion_counts={},
            requested_feed="sip",
            actual_feed="sip",
            data_source="sip",
            generated_at=now.isoformat(),
            stale=False,
            cached=False,
            config_signature=engine._compute_config_signature(),
            universe_signature=engine._compute_universe_signature(["AAPL"]),
        )
        engine.save_persisted_pool(res)
        # Calling without eligible_symbols must fail closed (return None)
        assert engine.load_stale_fallback_pool(eligible_symbols=None) is None
        assert engine.load_stale_fallback_pool(eligible_symbols=[]) is None


def test_stale_fallback_fail_closed_universe_mismatch() -> None:
    settings = Settings()
    with tempfile.TemporaryDirectory() as tmpdir:
        engine = DailySuitabilityEngine(settings=settings, cache_dir=tmpdir)
        now = datetime.now(timezone.utc)
        res = SuitabilityResult(
            pool=["AAPL"],
            quarantined=[],
            metrics={},
            exclusion_counts={},
            requested_feed="sip",
            actual_feed="sip",
            data_source="sip",
            generated_at=now.isoformat(),
            stale=False,
            cached=False,
            config_signature=engine._compute_config_signature(),
            universe_signature=engine._compute_universe_signature(["AAPL"]),
        )
        engine.save_persisted_pool(res)
        # Supplying a different eligible universe must fail closed (return None)
        assert engine.load_stale_fallback_pool(eligible_symbols=["MSFT", "GOOG"]) is None


def test_feed_retry_non_mixing_multibatch(monkeypatch) -> None:
    settings = Settings()
    with tempfile.TemporaryDirectory() as tmpdir:
        stock_client = MagicMock()
        bars = create_bars_for_symbol(base_price=100.0, num_bars=20, daily_volume=500_000.0)

        calls = []
        def mock_get_bars(req):
            calls.append(req.feed)
            if req.feed == DataFeed.SIP:
                raise RuntimeError("SIP batch failure")
            return MagicMock(data={"SYM1": bars}, next_page_token=None)

        stock_client.get_stock_bars.side_effect = mock_get_bars

        # With IEX fallback enabled via environment variable
        monkeypatch.setenv("ALPACA_ALLOW_IEX_FALLBACK", "true")
        engine = DailySuitabilityEngine(settings=settings, stock_client=stock_client, cache_dir=tmpdir, historical_feed="sip")
        
        # When SIP fails and fallback is enabled, evaluate_universe restarts entire evaluation with IEX
        res = engine.evaluate_universe(["SYM1"], force_refresh=True)
        assert res.actual_feed == "IEX"
        # Verify no mixing: tried SIP once, failed, restarted cleanly with IEX without mixing feeds in successful output
        assert calls == [DataFeed.SIP, DataFeed.IEX]


def test_stale_session_edge_cases_boundary() -> None:
    settings = Settings(alpaca_data_feed="sip")
    with tempfile.TemporaryDirectory() as tmpdir:
        engine = DailySuitabilityEngine(settings=settings, cache_dir=tmpdir)
        # Test handling of generated_at timestamps and max stale sessions
        now = datetime.now(timezone.utc)
        res = SuitabilityResult(
            pool=["AAPL"],
            quarantined=[],
            metrics={},
            exclusion_counts={},
            requested_feed="sip",
            actual_feed="sip",
            data_source="sip",
            generated_at=(now - timedelta(hours=12)).isoformat(),
            stale=False,
            cached=False,
            config_signature=engine._compute_config_signature(),
            universe_signature=engine._compute_universe_signature(["AAPL"]),
        )
        engine.save_persisted_pool(res)
        fallback = engine.load_stale_fallback_pool(eligible_symbols=["AAPL"], max_stale_sessions=3)
        assert fallback is not None
        assert fallback.stale is True
        assert fallback.generated_at == res.generated_at


def test_compatibility_validation_independent_of_calendar_fallback() -> None:
    settings = Settings()
    with tempfile.TemporaryDirectory() as tmpdir:
        engine = DailySuitabilityEngine(settings=settings, cache_dir=tmpdir)
        now = datetime.now(timezone.utc)
        # Create result with mismatched config signature
        res = SuitabilityResult(
            pool=["AAPL"],
            quarantined=[],
            metrics={},
            exclusion_counts={},
            requested_feed="sip",
            actual_feed="sip",
            data_source="sip",
            generated_at=(now - timedelta(hours=2)).isoformat(), # Very recent (< 72 hours)
            stale=False,
            cached=False,
            config_signature="mismatched_sig",
            universe_signature=engine._compute_universe_signature(["AAPL"]),
        )
        engine.save_persisted_pool(res)
        # Even though generated 2 hours ago, config signature mismatch must reject immediately without falling through to calendar/age fallback
        fallback = engine.load_stale_fallback_pool(eligible_symbols=["AAPL"])
        assert fallback is None


def test_genuine_multibatch_feed_retry_51_symbols(monkeypatch) -> None:
    settings = Settings()
    with tempfile.TemporaryDirectory() as tmpdir:
        stock_client = MagicMock()
        fixed_now = datetime(2026, 6, 1, 16, 0, tzinfo=timezone.utc)
        fixed_start = fixed_now - timedelta(days=40)
        sip_bars = create_bars_for_symbol(base_price=100.0, num_bars=20, daily_volume=500_000.0, start_date=fixed_start)
        iex_bars = create_bars_for_symbol(base_price=200.0, num_bars=20, daily_volume=500_000.0, start_date=fixed_start)

        symbols = [f"SYM{i:02d}" for i in range(51)] # 51 symbols -> 2 batches (50 + 1)
        calls = []

        def mock_get_bars(req):
            feed = req.feed
            syms = req.symbol_or_symbols
            if isinstance(syms, str):
                batch_syms = [syms]
            else:
                batch_syms = list(syms)
            
            calls.append((feed, len(batch_syms)))
            if feed == DataFeed.SIP:
                if any(s == "SYM50" for s in batch_syms):
                    raise RuntimeError("SIP batch 2 failure")
                resp_data = {s: sip_bars for s in batch_syms}
            else:
                resp_data = {s: iex_bars for s in batch_syms}
            return MagicMock(data=resp_data, next_page_token=None)

        stock_client.get_stock_bars.side_effect = mock_get_bars

        monkeypatch.setenv("ALPACA_ALLOW_IEX_FALLBACK", "true")
        engine = DailySuitabilityEngine(settings=settings, stock_client=stock_client, cache_dir=tmpdir, historical_feed="sip")
        
        res = engine.evaluate_universe(symbols, now_utc=fixed_now, force_refresh=True)
        assert res.requested_feed == "SIP"
        assert res.actual_feed == "IEX"
        assert res.data_source == "IEX"
        assert len(res.pool) > 0

        # Run an equivalent reference evaluation using IEX data only to prove metrics match identically
        ref_client = MagicMock()
        def mock_ref_get_bars(req):
            syms = req.symbol_or_symbols
            batch_syms = [syms] if isinstance(syms, str) else list(syms)
            return MagicMock(data={s: iex_bars for s in batch_syms}, next_page_token=None)
        ref_client.get_stock_bars.side_effect = mock_ref_get_bars
        ref_engine = DailySuitabilityEngine(
            settings=settings,
            stock_client=ref_client,
            cache_dir=os.path.join(tmpdir, "ref_cache"),
            historical_feed="iex",
        )
        ref_res = ref_engine.evaluate_universe(symbols, now_utc=fixed_now, force_refresh=True)

        # Assert final suitability metrics and pool results match IEX-only reference evaluation
        assert res.pool == ref_res.pool
        assert res.quarantined == ref_res.quarantined
        assert len(res.metrics) == len(ref_res.metrics)
        for sym in symbols:
            m = res.metrics.get(sym)
            rm = ref_res.metrics.get(sym)
            assert m is not None
            assert rm is not None
            assert m.symbol == rm.symbol
            assert m.latest_close == rm.latest_close
            assert m.median_close_5 == rm.median_close_5
            assert m.median_dollar_volume_20 == rm.median_dollar_volume_20
            assert m.valid_bar_count == rm.valid_bar_count
            assert m.atr_14 == rm.atr_14
            assert m.atr_pct == rm.atr_pct
            assert m.status == rm.status
            assert m.exclusion_reason == rm.exclusion_reason
            assert m.data_source == "IEX"

        # Verify persisted cache records correct requested and actual feeds
        loaded_cache = engine.load_persisted_pool()
        assert loaded_cache is not None
        assert loaded_cache.requested_feed == "SIP"
        assert loaded_cache.actual_feed == "IEX"
        assert loaded_cache.data_source == "IEX"
        
        expected_call_pattern = [
            (DataFeed.SIP, 50),
            (DataFeed.SIP, 1),
            (DataFeed.IEX, 50),
            (DataFeed.IEX, 1),
        ]
        assert calls == expected_call_pattern


def test_sip_failure_no_fallback_fails_safely(monkeypatch) -> None:
    settings = Settings()
    with tempfile.TemporaryDirectory() as tmpdir:
        stock_client = MagicMock()
        sip_bars = create_bars_for_symbol(base_price=100.0, num_bars=20, daily_volume=500_000.0)

        symbols = [f"SYM{i:02d}" for i in range(51)]

        def mock_get_bars(req):
            syms = req.symbol_or_symbols
            batch_syms = [syms] if isinstance(syms, str) else list(syms)
            if any(s == "SYM50" for s in batch_syms):
                raise RuntimeError("SIP batch 2 failure")
            return MagicMock(data={s: sip_bars for s in batch_syms}, next_page_token=None)

        stock_client.get_stock_bars.side_effect = mock_get_bars

        monkeypatch.setenv("ALPACA_ALLOW_IEX_FALLBACK", "false")
        engine = DailySuitabilityEngine(settings=settings, stock_client=stock_client, cache_dir=tmpdir, historical_feed="sip")
        
        with pytest.raises(RuntimeError, match="Layer 2 historical bar fetch failed and no valid compatible cached pool exists."):
            engine.evaluate_universe(symbols, force_refresh=True)

        # Verify no partial data or successful pool was persisted
        assert engine.load_persisted_pool() is None


def test_cache_metadata_missing_requested_or_actual_feed_rejected() -> None:
    settings = Settings()
    with tempfile.TemporaryDirectory() as tmpdir:
        engine = DailySuitabilityEngine(settings=settings, cache_dir=tmpdir, historical_feed="sip")
        now = datetime.now(timezone.utc)
        
        # Base valid payload
        base_payload = {
            "pool": ["AAPL"],
            "quarantined": [],
            "metrics": {},
            "exclusion_counts": {},
            "requested_feed": "SIP",
            "actual_feed": "SIP",
            "data_source": "SIP",
            "generated_at": now.isoformat(),
            "stale": False,
            "config_signature": engine._compute_config_signature(),
            "universe_signature": engine._compute_universe_signature(["AAPL"]),
        }

        cache_path = engine.cache_file
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)

        # 1. Missing requested_feed
        payload_no_req = dict(base_payload)
        payload_no_req.pop("requested_feed")
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(payload_no_req, f)

        assert engine.load_persisted_pool() is None
        assert engine.load_stale_fallback_pool(eligible_symbols=["AAPL"]) is None
        # Normal cache reuse should bypass cache and attempt fetch (raising or falling back)
        stock_client = MagicMock()
        stock_client.get_stock_bars.side_effect = RuntimeError("API down")
        engine_fail = DailySuitabilityEngine(settings=settings, stock_client=stock_client, cache_dir=tmpdir, historical_feed="sip")
        with pytest.raises(RuntimeError):
            engine_fail.evaluate_universe(["AAPL"], force_refresh=False)

        # 2. Missing actual_feed
        payload_no_act = dict(base_payload)
        payload_no_act.pop("actual_feed")
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(payload_no_act, f)

        assert engine.load_persisted_pool() is None
        assert engine.load_stale_fallback_pool(eligible_symbols=["AAPL"]) is None


def test_legitimately_evaluated_empty_pool_accepted() -> None:
    settings = Settings()
    with tempfile.TemporaryDirectory() as tmpdir:
        engine = DailySuitabilityEngine(settings=settings, cache_dir=tmpdir, historical_feed="sip")
        now = datetime.now(timezone.utc)
        res = SuitabilityResult(
            pool=[], # Legitimately evaluated empty pool
            quarantined=[],
            metrics={},
            exclusion_counts={"no_bars": 1},
            requested_feed="SIP",
            actual_feed="SIP",
            data_source="SIP",
            generated_at=now.isoformat(),
            stale=False,
            cached=False,
            config_signature=engine._compute_config_signature(),
            universe_signature=engine._compute_universe_signature(["AAPL"]),
        )
        engine.save_persisted_pool(res)
        loaded = engine.load_persisted_pool()
        assert loaded is not None
        assert loaded.pool == []
        assert loaded.requested_feed == "SIP"
        assert loaded.actual_feed == "SIP"


def test_trading_session_boundary_sessions() -> None:
    settings = Settings(alpaca_data_feed="sip")
    with tempfile.TemporaryDirectory() as tmpdir:
        engine = DailySuitabilityEngine(settings=settings, cache_dir=tmpdir)
        
        t0 = pd.Timestamp("2026-06-01 20:00:00", tz="UTC")
        t1 = pd.Timestamp("2026-06-02 20:00:00", tz="UTC")
        t2 = pd.Timestamp("2026-06-03 20:00:00", tz="UTC")
        t3 = pd.Timestamp("2026-06-04 20:00:00", tz="UTC")
        t4 = pd.Timestamp("2026-06-05 20:00:00", tz="UTC")

        schedule_df = pd.DataFrame({
            "open": [
                pd.Timestamp("2026-06-01 13:30:00", tz="UTC"),
                pd.Timestamp("2026-06-02 13:30:00", tz="UTC"),
                pd.Timestamp("2026-06-03 13:30:00", tz="UTC"),
                pd.Timestamp("2026-06-04 13:30:00", tz="UTC"),
                pd.Timestamp("2026-06-05 13:30:00", tz="UTC"),
            ],
            "close": [t0, t1, t2, t3, t4]
        }, index=pd.Index(["2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04", "2026-06-05"]))

        mock_calendar = MagicMock()
        mock_calendar.schedule = schedule_df
        mock_cal_service = MagicMock()
        mock_cal_service._calendar.return_value = mock_calendar

        res = SuitabilityResult(
            pool=["AAPL"],
            quarantined=[],
            metrics={},
            exclusion_counts={},
            requested_feed="sip",
            actual_feed="sip",
            data_source="sip",
            generated_at=t0.isoformat(),
            stale=False,
            cached=False,
            config_signature=engine._compute_config_signature(),
            universe_signature=engine._compute_universe_signature(["AAPL"]),
        )
        engine.save_persisted_pool(res)

        with patch("money_mintter.core.suitability.ExchangeCalendarService", return_value=mock_cal_service):
            # max_stale_sessions = 2
            # Evaluation after t2 close (2 completed sessions: t1 and t2). Accepted at max_stale_sessions=2.
            eval_time_2 = t2 + timedelta(minutes=5)
            with patch("money_mintter.core.suitability.datetime") as mock_dt:
                mock_dt.now.return_value = eval_time_2
                mock_dt.fromisoformat = datetime.fromisoformat
                mock_dt.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
                
                fallback_2 = engine.load_stale_fallback_pool(eligible_symbols=["AAPL"], max_stale_sessions=2)
                assert fallback_2 is not None

            # Evaluation after t3 close (3 completed sessions: t1, t2, t3). Rejected when max_stale_sessions = 2.
            eval_time_3 = t3 + timedelta(minutes=5)
            with patch("money_mintter.core.suitability.datetime") as mock_dt:
                mock_dt.now.return_value = eval_time_3
                mock_dt.fromisoformat = datetime.fromisoformat
                mock_dt.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
                
                fallback_3 = engine.load_stale_fallback_pool(eligible_symbols=["AAPL"], max_stale_sessions=2)
                assert fallback_3 is None


def test_market_timing_before_after_close_and_holiday() -> None:
    settings = Settings()
    with tempfile.TemporaryDirectory() as tmpdir:
        engine = DailySuitabilityEngine(settings=settings, cache_dir=tmpdir)

        weekend_schedule_df = pd.DataFrame({
            "open": [
                pd.Timestamp("2026-06-05 13:30:00", tz="UTC"),
                pd.Timestamp("2026-06-08 13:30:00", tz="UTC"),
            ],
            "close": [
                pd.Timestamp("2026-06-05 20:00:00", tz="UTC"), # Friday close
                pd.Timestamp("2026-06-08 20:00:00", tz="UTC"), # Monday close
            ]
        }, index=pd.Index(["2026-06-05", "2026-06-08"]))

        mock_calendar = MagicMock()
        mock_calendar.schedule = weekend_schedule_df
        mock_cal_service = MagicMock()
        mock_cal_service._calendar.return_value = mock_calendar

        with patch("money_mintter.core.suitability.ExchangeCalendarService", return_value=mock_cal_service):
            gen_dt_before = datetime(2026, 6, 5, 15, 0, tzinfo=timezone.utc)
            market_close = datetime(2026, 6, 5, 20, 0, tzinfo=timezone.utc)

            # 1. Immediately before market close
            before_close_utc = market_close - timedelta(minutes=1)
            assert engine._is_session_expired(gen_dt_before, before_close_utc) is False

            # 2. Immediately after market close
            after_close_utc = market_close + timedelta(minutes=1)
            assert engine._is_session_expired(gen_dt_before, after_close_utc) is True

            # 3. Weekend: Set cache generation to Friday, June 5, 2026, at 20:05 UTC (after Friday close)
            gen_dt_fri_close = datetime(2026, 6, 5, 20, 5, tzinfo=timezone.utc)

            # Verify _is_session_expired() returns False on Saturday
            saturday_dt = datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc)
            assert engine._is_session_expired(gen_dt_fri_close, saturday_dt) is False

            # Verify _is_session_expired() returns False on Monday before next close
            mon_pre_close = datetime(2026, 6, 8, 15, 0, tzinfo=timezone.utc)
            assert engine._is_session_expired(gen_dt_fri_close, mon_pre_close) is False

            # Verify _is_session_expired() returns True after Monday's close
            mon_post_close = datetime(2026, 6, 8, 20, 5, tzinfo=timezone.utc)
            assert engine._is_session_expired(gen_dt_fri_close, mon_post_close) is True

        # 4. US market holiday: controlled schedule containing July 2, 2026 trading session closing at 20:00 UTC and no July 3 session
        holiday_schedule_df = pd.DataFrame({
            "open": [
                pd.Timestamp("2026-07-02 13:30:00", tz="UTC"),
                pd.Timestamp("2026-07-06 13:30:00", tz="UTC"),
            ],
            "close": [
                pd.Timestamp("2026-07-02 20:00:00", tz="UTC"),
                pd.Timestamp("2026-07-06 20:00:00", tz="UTC"),
            ]
        }, index=pd.Index(["2026-07-02", "2026-07-06"])) # July 3 omitted (holiday)

        mock_holiday_cal = MagicMock()
        mock_holiday_cal.schedule = holiday_schedule_df
        mock_holiday_cal_service = MagicMock()
        mock_holiday_cal_service._calendar.return_value = mock_holiday_cal

        with patch("money_mintter.core.suitability.ExchangeCalendarService", return_value=mock_holiday_cal_service):
            # Set cache generation to July 2 at 20:05 UTC and evaluate on July 3
            gen_dt_july2 = datetime(2026, 7, 2, 20, 5, tzinfo=timezone.utc)
            july3_eval = datetime(2026, 7, 3, 15, 0, tzinfo=timezone.utc)

            expired_on_holiday = engine._is_session_expired(gen_dt_july2, july3_eval)
            assert expired_on_holiday is False


def test_calendar_failure_72_hour_fallback() -> None:
    settings = Settings(alpaca_data_feed="sip")
    with tempfile.TemporaryDirectory() as tmpdir:
        engine = DailySuitabilityEngine(settings=settings, cache_dir=tmpdir)
        now = datetime.now(timezone.utc)

        # 1. Within 72 hours with calendar failure
        recent_time = now - timedelta(hours=10)
        res_recent = SuitabilityResult(
            pool=["AAPL"],
            quarantined=[],
            metrics={},
            exclusion_counts={},
            requested_feed="sip",
            actual_feed="sip",
            data_source="sip",
            generated_at=recent_time.isoformat(),
            stale=False,
            cached=False,
            config_signature=engine._compute_config_signature(),
            universe_signature=engine._compute_universe_signature(["AAPL"]),
        )
        engine.save_persisted_pool(res_recent)

        with patch("money_mintter.core.suitability.ExchangeCalendarService") as mock_ecs:
            mock_ecs.side_effect = Exception("Calendar service down")
            fallback_recent = engine.load_stale_fallback_pool(eligible_symbols=["AAPL"])
            assert fallback_recent is not None
            assert fallback_recent.pool == ["AAPL"]

        # 2. Older than 72 hours with calendar failure
        old_time = now - timedelta(hours=75)
        res_old = SuitabilityResult(
            pool=["AAPL"],
            quarantined=[],
            metrics={},
            exclusion_counts={},
            requested_feed="sip",
            actual_feed="sip",
            data_source="sip",
            generated_at=old_time.isoformat(),
            stale=False,
            cached=False,
            config_signature=engine._compute_config_signature(),
            universe_signature=engine._compute_universe_signature(["AAPL"]),
        )
        engine.save_persisted_pool(res_old)

        with patch("money_mintter.core.suitability.ExchangeCalendarService") as mock_ecs:
            mock_ecs.side_effect = Exception("Calendar service down")
            fallback_old = engine.load_stale_fallback_pool(eligible_symbols=["AAPL"])
            assert fallback_old is None


def test_compatibility_safety_with_calendar_failure() -> None:
    settings = Settings()
    with tempfile.TemporaryDirectory() as tmpdir:
        engine = DailySuitabilityEngine(settings=settings, cache_dir=tmpdir)
        now = datetime.now(timezone.utc)
        recent_time = now - timedelta(hours=2)

        res_bad_config = SuitabilityResult(
            pool=["AAPL"],
            quarantined=[],
            metrics={},
            exclusion_counts={},
            requested_feed="sip",
            actual_feed="sip",
            data_source="sip",
            generated_at=recent_time.isoformat(),
            stale=False,
            cached=False,
            config_signature="invalid_config_signature",
            universe_signature=engine._compute_universe_signature(["AAPL"]),
        )
        engine.save_persisted_pool(res_bad_config)

        with patch("money_mintter.core.suitability.ExchangeCalendarService") as mock_ecs:
            mock_ecs.side_effect = Exception("Calendar service down")
            fallback = engine.load_stale_fallback_pool(eligible_symbols=["AAPL"])
            assert fallback is None


def test_stale_cached_empty_pool_triggers_fresh_evaluation() -> None:
    settings = Settings()
    with tempfile.TemporaryDirectory() as tmpdir:
        stock_client = MagicMock()
        bars = create_bars_for_symbol(base_price=100.0, num_bars=30, daily_volume=500_000.0)
        stock_client.get_stock_bars.return_value = MagicMock(data={"AAPL": bars}, next_page_token=None)

        engine = DailySuitabilityEngine(settings=settings, stock_client=stock_client, cache_dir=tmpdir, historical_feed="sip")
        now = datetime.now(timezone.utc)
        recent_time = now - timedelta(hours=1)

        res_stale_empty = SuitabilityResult(
            pool=[],
            quarantined=[],
            metrics={},
            exclusion_counts={"no_bars": 1},
            requested_feed="sip",
            actual_feed="sip",
            data_source="sip",
            generated_at=recent_time.isoformat(),
            stale=True,
            cached=False,
            config_signature=engine._compute_config_signature(),
            universe_signature=engine._compute_universe_signature(["AAPL"]),
        )
        engine.save_persisted_pool(res_stale_empty)

        result = engine.evaluate_universe(["AAPL"], now_utc=now, force_refresh=False)
        assert result.pool == ["AAPL"]
        assert result.stale is False
        stock_client.get_stock_bars.assert_called()


def test_stale_cached_non_empty_pool_triggers_fresh_evaluation() -> None:
    settings = Settings(min_latest_close=1000.0)
    with tempfile.TemporaryDirectory() as tmpdir:
        stock_client = MagicMock()
        bars = create_bars_for_symbol(base_price=50.0, num_bars=30, daily_volume=500_000.0)
        stock_client.get_stock_bars.return_value = MagicMock(data={"AAPL": bars}, next_page_token=None)

        engine = DailySuitabilityEngine(settings=settings, stock_client=stock_client, cache_dir=tmpdir, historical_feed="sip")
        now = datetime.now(timezone.utc)
        recent_time = now - timedelta(hours=1)

        res_stale_non_empty = SuitabilityResult(
            pool=["AAPL"],
            quarantined=[],
            metrics={},
            exclusion_counts={},
            requested_feed="sip",
            actual_feed="sip",
            data_source="sip",
            generated_at=recent_time.isoformat(),
            stale=True,
            cached=False,
            config_signature=engine._compute_config_signature(),
            universe_signature=engine._compute_universe_signature(["AAPL"]),
        )
        engine.save_persisted_pool(res_stale_non_empty)

        result = engine.evaluate_universe(["AAPL"], now_utc=now, force_refresh=False)
        assert result.pool == []
        assert result.stale is False
        stock_client.get_stock_bars.assert_called()


def test_non_stale_valid_cached_pool_is_reused() -> None:
    settings = Settings()
    with tempfile.TemporaryDirectory() as tmpdir:
        stock_client = MagicMock()
        engine = DailySuitabilityEngine(settings=settings, stock_client=stock_client, cache_dir=tmpdir, historical_feed="sip")
        now = datetime.now(timezone.utc)
        recent_time = now - timedelta(minutes=5)

        res_valid = SuitabilityResult(
            pool=["AAPL"],
            quarantined=[],
            metrics={},
            exclusion_counts={},
            requested_feed="sip",
            actual_feed="sip",
            data_source="sip",
            generated_at=recent_time.isoformat(),
            stale=False,
            cached=False,
            config_signature=engine._compute_config_signature(),
            universe_signature=engine._compute_universe_signature(["AAPL"]),
        )
        engine.save_persisted_pool(res_valid)

        result = engine.evaluate_universe(["AAPL"], now_utc=now, force_refresh=False)
        assert result.pool == ["AAPL"]
        assert result.stale is False
        stock_client.get_stock_bars.assert_not_called()


