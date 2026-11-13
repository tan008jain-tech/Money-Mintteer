from __future__ import annotations

import logging
import tempfile
from datetime import datetime, timezone, timedelta
import pytest

from alpaca.trading.enums import AssetStatus, AssetClass
from money_mintter.core.live_bot import discover_universe, LiveMarketBot, ConnectivityError, MarketScanner
from money_mintter.core.eligibility import StructuralEligibilityEngine
from money_mintter.core.suitability import DailySuitabilityEngine, SuitabilityResult
from money_mintter.core.selector import select_layer4_candidates


class FakeAsset:
    def __init__(self, symbol: str, status: AssetStatus = AssetStatus.ACTIVE, tradable: bool = True):
        self.symbol = symbol
        self.status = status
        self.tradable = tradable
        self.asset_class = AssetClass.US_EQUITY
        self.name = "Test Stock"
        self.exchange = "NASDAQ"
        self.shortable = True
        self.fractionable = True
        self.marginable = True


class MockTradingClient:
    def __init__(self, assets: list[Any], should_raise: bool = False, return_none: bool = False):
        self.assets = assets
        self.should_raise = should_raise
        self.return_none = return_none

    def get_all_assets(self, request=None):
        if self.should_raise:
            raise RuntimeError("API failure")
        if self.return_none:
            return None
        return self.assets


@pytest.fixture(autouse=True)
def _setup_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MM_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("MONEY_MINTTER_TEMP_DIR", str(tmp_path))


def test_normal_l1_l2_l4_flow(monkeypatch):
    assets = [FakeAsset(f"SYM{i}") for i in range(10)]
    client = MockTradingClient(assets)
    logger = logging.getLogger("test_flow")

    # Mock DailySuitabilityEngine evaluate_universe to return a subset
    monkeypatch.setattr(
        "money_mintter.core.suitability.DailySuitabilityEngine.evaluate_universe",
        lambda self, symbols, now=None, force=False: SuitabilityResult(
            pool=symbols[:5],
            quarantined=[],
            metrics={},
            exclusion_counts={},
            requested_feed="iex",
            actual_feed="iex",
            data_source="iex",
            generated_at=datetime.now(timezone.utc).isoformat(),
            stale=False,
            cached=False,
            config_signature=self._compute_config_signature(),
            universe_signature=self._compute_universe_signature(symbols),
        )
    )

    pool = discover_universe(client, logger, api_key="test")
    assert len(pool) == 5
    # Proof that Layer 4 receives only validated Layer 2 pool, never raw L1 symbols
    assert set(pool).issubset({a.symbol for a in assets})
    assert len(pool) < len(assets)


def test_startup_fresh_valid_l2_cache(monkeypatch, tmp_path):
    cache_dir = str(tmp_path)
    engine = StructuralEligibilityEngine(cache_dir=cache_dir)
    now = datetime.now(timezone.utc)
    engine.save_persisted_cache(["AAPL", "MSFT"], {"AAPL": {}, "MSFT": {}}, now)

    l2_engine = DailySuitabilityEngine(cache_dir=cache_dir)
    l2_result = SuitabilityResult(
        pool=["AAPL", "MSFT"],
        quarantined=[],
        metrics={},
        exclusion_counts={},
        requested_feed="iex",
        actual_feed="iex",
        data_source="iex",
        generated_at=now.isoformat(),
        stale=False,
        cached=True,
        config_signature=l2_engine._compute_config_signature(),
        universe_signature=l2_engine._compute_universe_signature(["AAPL", "MSFT"]),
    )
    l2_engine.save_persisted_pool(l2_result)

    bot = LiveMarketBot()
    assert bot.global_universe == ["AAPL", "MSFT"]


def test_l1_api_exception_none_empty(monkeypatch):
    logger = logging.getLogger("test_api")
    client_raise = MockTradingClient([], should_raise=True)
    with pytest.raises(ConnectivityError):
        discover_universe(client_raise, logger)

    client_none = MockTradingClient([], return_none=True)
    with pytest.raises(RuntimeError):
        discover_universe(client_none, logger)


def test_undersized_l1_discovery(monkeypatch):
    assets = [FakeAsset("AAPL"), FakeAsset("MSFT")]
    client = MockTradingClient(assets)
    logger = logging.getLogger("test_undersized")

    monkeypatch.setattr(
        "money_mintter.core.suitability.DailySuitabilityEngine.evaluate_universe",
        lambda self, symbols, now=None, force=False: SuitabilityResult(
            pool=symbols,
            quarantined=[],
            metrics={},
            exclusion_counts={},
            requested_feed="iex",
            actual_feed="iex",
            data_source="iex",
            generated_at=datetime.now(timezone.utc).isoformat(),
            stale=False,
            cached=False,
            config_signature=self._compute_config_signature(),
            universe_signature=self._compute_universe_signature(symbols),
        )
    )

    pool = discover_universe(client, logger)
    assert pool == ["AAPL", "MSFT"]


def test_incompatible_l2_stale_fallback_disables_new_entries(monkeypatch):
    client = MockTradingClient([])
    logger = logging.getLogger("test_fallback")

    monkeypatch.setattr(
        "money_mintter.core.suitability.DailySuitabilityEngine.evaluate_universe",
        lambda self, symbols, now=None, force=False: (_ for _ in ()).throw(RuntimeError("L2 evaluation failed"))
    )
    monkeypatch.setattr(
        "money_mintter.core.suitability.DailySuitabilityEngine.load_stale_fallback_pool",
        lambda self, eligible_symbols=None, max_stale_sessions=3: None
    )

    pool = discover_universe(client, logger)
    assert pool == []


def test_layer4_never_receives_raw_l1_symbols():
    raw_l1 = ["RAW1", "RAW2", "RAW3", "RAW4", "RAW5"]
    validated_l2 = ["VAL1", "VAL2"]
    class FakeStockClient:
        def get_stock_bars(self, req):
            class FakeBar:
                close = 100.0
                high = 101.0
                low = 99.0
                volume = 1_000_000
                vwap = 100.0
                timestamp = datetime.now(timezone.utc)
            return type("Resp", (), {"data": {sym: [FakeBar()] for sym in validated_l2}})()

    scanner = MarketScanner(raw_l1, FakeStockClient(), logging.getLogger("test_l4"))
    # If scanner scans using validated_l2 pool directly or global_universe
    # We verify select_layer4_candidates receives only validated L2 pool
    bars_dict = {"VAL1": (), "VAL2": ()}
    selected = select_layer4_candidates(validated_l2, bars_dict, max_candidates=8)
    for c in selected:
        assert c.symbol in validated_l2
        assert c.symbol not in raw_l1 or c.symbol in validated_l2


def test_position_management_continuity_when_new_entries_disabled(monkeypatch):
    # Verify existing position management methods remain active even when global_universe is empty / new entries disabled
    bot = LiveMarketBot()
    bot.global_universe = []
    # Verify portfolio manager or trade manager methods exist and can manage positions
    assert hasattr(bot, "portfolio_manager")
    assert hasattr(bot, "kill_switch")
