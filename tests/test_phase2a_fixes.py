from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
import pytest

from money_mintter.domain import Side
from money_mintter.config import Settings
from money_mintter.core.live_bot import LivePortfolioManager, normalize_symbol
from money_mintter.strategy.ict.integration import ICTExecutionInstruction, ICTDeduplicationTracker
from money_mintter.core.suitability import DailySuitabilityEngine, SuitabilityResult, SymbolSuitabilityMetrics


class RecordingBroker:
    def __init__(self):
        self.submitted = []

    def submit_order(self, req):
        self.submitted.append(req.symbol)
        return type(
            "Ord",
            (),
            {"order_class": "bracket", "legs": [1, 2], "id": f"ord_{req.symbol}"}
        )()


def test_fix_a_slot_starvation_and_filtering(monkeypatch: pytest.MonkeyPatch) -> None:
    """Comprehensive regression tests for FIX A: ICT instruction-slot starvation and filtering."""
    monkeypatch.setenv("MAX_PORTFOLIO_RISK_PCT", "15")
    logger = logging.getLogger("test")
    broker = RecordingBroker()
    pm = LivePortfolioManager(broker, logger)
    settings = Settings()
    tracker = ICTDeduplicationTracker()

    now = datetime.now(timezone.utc)
    bar_time = now - timedelta(minutes=5)
    processed_signals = set()

    # 1. Invalid slash symbol ("AAPL/USD")
    inst_slash = ICTExecutionInstruction(
        symbol="AAPL/USD",
        side=Side.BUY,
        entry_price=100.0,
        stop_price=98.0,
        target_price=105.0,
        risk_pts=2.0,
        timestamp=now,
        timestamp_ns=1,
        zone_type="disp_fvg",
        score=1.0,
        zone_top=101.0,
        zone_bot=99.0,
        reason="slash",
        is_diagnostic=False,
    )

    # 2. Locked symbol ("MSFT")
    inst_locked = ICTExecutionInstruction(
        symbol="MSFT",
        side=Side.BUY,
        entry_price=200.0,
        stop_price=196.0,
        target_price=210.0,
        risk_pts=4.0,
        timestamp=now,
        timestamp_ns=2,
        zone_type="disp_fvg",
        score=1.0,
        zone_top=202.0,
        zone_bot=198.0,
        reason="locked",
        is_diagnostic=False,
    )

    # 3. Pending duplicate in tracker ("GOOGL")
    inst_pending = ICTExecutionInstruction(
        symbol="GOOGL",
        side=Side.BUY,
        entry_price=150.0,
        stop_price=146.0,
        target_price=160.0,
        risk_pts=4.0,
        timestamp=now,
        timestamp_ns=3,
        zone_type="disp_fvg",
        score=1.2,
        zone_top=152.0,
        zone_bot=148.0,
        reason="pending_dup",
        is_diagnostic=False,
    )
    tracker.mark_pending("GOOGL", "bull", "disp_fvg", 152.0, 148.0)

    # 3b. Already-executed duplicate in tracker ("AAPL")
    inst_executed = ICTExecutionInstruction(
        symbol="AAPL",
        side=Side.BUY,
        entry_price=170.0,
        stop_price=166.0,
        target_price=180.0,
        risk_pts=4.0,
        timestamp=now,
        timestamp_ns=35,
        zone_type="disp_fvg",
        score=1.4,
        zone_top=172.0,
        zone_bot=168.0,
        reason="executed_dup",
        is_diagnostic=False,
    )
    tracker.mark_executed("AAPL", "bull", "disp_fvg", 172.0, 168.0)

    # 4. Processed signal duplicate ("AMZN" at bar_time)
    inst_processed = ICTExecutionInstruction(
        symbol="AMZN",
        side=Side.BUY,
        entry_price=180.0,
        stop_price=175.0,
        target_price=190.0,
        risk_pts=5.0,
        timestamp=now,
        timestamp_ns=4,
        zone_type="disp_fvg",
        score=1.3,
        zone_top=182.0,
        zone_bot=178.0,
        reason="processed",
        is_diagnostic=False,
    )
    processed_signals.add((normalize_symbol("AMZN"), bar_time))

    # 5. Valid candidates preserving priority ("TSLA", "NVDA", "META", "NFLX")
    inst_tsla = ICTExecutionInstruction(
        symbol="TSLA",
        side=Side.BUY,
        entry_price=250.0,
        stop_price=245.0,
        target_price=265.0,
        risk_pts=5.0,
        timestamp=now,
        timestamp_ns=5,
        zone_type="disp_fvg",
        score=1.5,
        zone_top=252.0,
        zone_bot=248.0,
        reason="tsla",
        is_diagnostic=False,
    )
    inst_nvda = ICTExecutionInstruction(
        symbol="NVDA",
        side=Side.BUY,
        entry_price=300.0,
        stop_price=290.0,
        target_price=320.0,
        risk_pts=10.0,
        timestamp=now,
        timestamp_ns=6,
        zone_type="disp_fvg",
        score=1.8,
        zone_top=305.0,
        zone_bot=295.0,
        reason="nvda",
        is_diagnostic=False,
    )
    inst_meta = ICTExecutionInstruction(
        symbol="META",
        side=Side.BUY,
        entry_price=400.0,
        stop_price=390.0,
        target_price=425.0,
        risk_pts=10.0,
        timestamp=now,
        timestamp_ns=7,
        zone_type="disp_fvg",
        score=1.6,
        zone_top=405.0,
        zone_bot=395.0,
        reason="meta",
        is_diagnostic=False,
    )

    # 3 positions plus one pending order occupy 4 of 5 slots -> 1 slot available.
    locked_symbols = {"MSFT", "LOCKED1", "LOCKED2"}
    instructions = [
        inst_slash,
        inst_locked,
        inst_pending,
        inst_executed,
        inst_processed,
        inst_tsla,
        inst_nvda,
        inst_meta,
    ]

    sent = pm.submit_ict_instructions(
        instructions=instructions,
        locked_symbols=locked_symbols,
        account_equity=100000.0,
        settings=settings,
        dedup_tracker=tracker,
        bar_time=bar_time,
        processed_signals=processed_signals,
    )

    # Verification:
    # - Invalid, locked, pending, executed, and processed instructions filtered out without consuming slots.
    # - More eligible candidates (TSLA, NVDA, META) than available slots (1).
    # - Original priority preserved (TSLA submitted first).
    # - Pending order consumes a slot; submission count stays within capacity.
    # - Actual submitted symbols verified via recording broker.
    assert sent == 1
    assert broker.submitted == ["TSLA"]
    assert len(broker.submitted) <= 1
    assert tracker.is_executed("TSLA", "bull", "disp_fvg", 252.0, 248.0)
    assert not tracker.is_executed("NVDA", "bull", "disp_fvg", 305.0, 295.0)


def test_fix_b_layer2_cache_reuse_and_fallback(tmp_path) -> None:
    """Comprehensive regression tests for FIX B: Layer 2 suitability cache reuse, fallback, and metadata validation."""
    cache_dir = str(tmp_path)
    settings = Settings()
    engine = DailySuitabilityEngine(settings=settings, cache_dir=cache_dir, historical_feed="iex")

    now_utc = datetime.now(timezone.utc)
    config_sig = engine._compute_config_signature()
    universe_sig = engine._compute_universe_signature(["AAPL", "MSFT"])

    # Mock stock client for tests where fresh cache is not reused
    class MockStockClient:
        def get_stock_bars(self, req):
            raise RuntimeError("Stock client should not be called during fresh cache reuse")

    engine.stock_client = MockStockClient()

    # 1. Valid fresh cache reuse (stale=False, non-empty pool, matching signatures)
    fresh_result = SuitabilityResult(
        pool=["AAPL", "MSFT"],
        quarantined=[],
        metrics={},
        exclusion_counts={},
        requested_feed="iex",
        actual_feed="iex",
        data_source="iex",
        generated_at=now_utc.isoformat(),
        stale=False,
        cached=False,
        config_signature=config_sig,
        universe_signature=universe_sig,
    )
    engine.save_persisted_pool(fresh_result)

    res_fresh = engine.evaluate_universe(["AAPL", "MSFT"], now_utc=now_utc, force_refresh=False)
    assert res_fresh is not None
    assert res_fresh.pool == ["AAPL", "MSFT"]
    assert res_fresh.stale is False
    assert res_fresh.cached is True
    assert res_fresh.config_signature == config_sig
    assert res_fresh.universe_signature == universe_sig

    # 2. Stale cache rejected from normal fresh reuse, but loaded via fallback with stale=True and meaningful metadata
    stale_result = SuitabilityResult(
        pool=["AAPL", "MSFT"],
        quarantined=[],
        metrics={},
        exclusion_counts={},
        requested_feed="iex",
        actual_feed="iex",
        data_source="iex",
        generated_at=now_utc.isoformat(),
        stale=True,
        cached=False,
        config_signature=config_sig,
        universe_signature=universe_sig,
    )
    engine.save_persisted_pool(stale_result)

    # Mock client that fails fetch so stale fallback is triggered
    class FailingStockClient:
        def get_stock_bars(self, req):
            raise RuntimeError("API fetch failure")

    engine.stock_client = FailingStockClient()

    res_stale_fallback = engine.evaluate_universe(["AAPL", "MSFT"], now_utc=now_utc, force_refresh=False)
    assert res_stale_fallback is not None
    assert res_stale_fallback.stale is True
    assert res_stale_fallback.cached is False
    assert res_stale_fallback.pool == ["AAPL", "MSFT"]
    assert res_stale_fallback.generated_at == now_utc.isoformat()
    assert res_stale_fallback.config_signature == config_sig
    assert res_stale_fallback.universe_signature == universe_sig

    # 3. Empty persisted pool rejected as fresh (rejection triggers fetch attempt -> fails -> fallback rejects empty pool -> raises RuntimeError)
    empty_pool_result = SuitabilityResult(
        pool=[],
        quarantined=[],
        metrics={},
        exclusion_counts={},
        requested_feed="iex",
        actual_feed="iex",
        data_source="iex",
        generated_at=now_utc.isoformat(),
        stale=False,
        cached=False,
        config_signature=config_sig,
        universe_signature=universe_sig,
    )
    engine.save_persisted_pool(empty_pool_result)

    with pytest.raises(RuntimeError, match="Layer 2 historical bar fetch failed"):
        engine.evaluate_universe(["AAPL", "MSFT"], now_utc=now_utc, force_refresh=False)

    # 4. Signature mismatch rejection (config signature mismatch rejects cache reuse and fallback, raising RuntimeError)
    bad_config_result = SuitabilityResult(
        pool=["AAPL", "MSFT"],
        quarantined=[],
        metrics={},
        exclusion_counts={},
        requested_feed="iex",
        actual_feed="iex",
        data_source="iex",
        generated_at=now_utc.isoformat(),
        stale=False,
        cached=False,
        config_signature="invalid_config_sig",
        universe_signature=universe_sig,
    )
    engine.save_persisted_pool(bad_config_result)

    with pytest.raises(RuntimeError, match="Layer 2 historical bar fetch failed"):
        engine.evaluate_universe(["AAPL", "MSFT"], now_utc=now_utc, force_refresh=False)

    # 5. Universe signature mismatch rejection
    bad_univ_result = SuitabilityResult(
        pool=["AAPL", "MSFT"],
        quarantined=[],
        metrics={},
        exclusion_counts={},
        requested_feed="iex",
        actual_feed="iex",
        data_source="iex",
        generated_at=now_utc.isoformat(),
        stale=False,
        cached=False,
        config_signature=config_sig,
        universe_signature="invalid_univ_sig",
    )
    engine.save_persisted_pool(bad_univ_result)

    with pytest.raises(RuntimeError, match="Layer 2 historical bar fetch failed"):
        engine.evaluate_universe(["AAPL", "MSFT"], now_utc=now_utc, force_refresh=False)
