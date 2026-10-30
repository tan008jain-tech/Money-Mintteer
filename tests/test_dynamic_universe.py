from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
import pytest
import requests

from alpaca.trading.enums import AssetStatus, AssetClass
from money_mintter.core.live_bot import LiveMarketBot, discover_universe, MarketScanner, ConnectivityError
from money_mintter.strategy import LiquiditySweepReversalStrategy, StrategyConfig


class FakeAsset:
    def __init__(self, symbol: str, status: AssetStatus, tradable: bool, asset_class: AssetClass = AssetClass.US_EQUITY):
        self.symbol = symbol
        self.status = status
        self.tradable = tradable
        self.asset_class = asset_class


class FakeTradingClient:
    def __init__(self, assets: list[Any], should_raise: bool = False):
        self.assets = assets
        self.should_raise = should_raise

    def get_all_assets(self, request=None):
        if self.should_raise:
            raise RuntimeError("API connection error")
        return self.assets


@pytest.fixture(autouse=True)
def _mock_suitability_evaluation(monkeypatch):
    from money_mintter.core.suitability import SuitabilityResult
    def mock_evaluate(self, eligible_symbols, now_utc=None, force_refresh=False):
        return SuitabilityResult(
            pool=eligible_symbols,
            quarantined=[],
            metrics={},
            exclusion_counts={},
            requested_feed="SIP",
            actual_feed="SIP",
            data_source="SIP",
            generated_at=datetime.now(timezone.utc).isoformat(),
            stale=False,
            cached=False,
            config_signature="mock",
            universe_signature="mock",
        )
    monkeypatch.setattr("money_mintter.core.suitability.DailySuitabilityEngine.evaluate_universe", mock_evaluate)


def test_discovery_24_assets(monkeypatch):
    monkeypatch.setenv("MIN_UNIVERSE_SIZE", "10")
    assets = [FakeAsset(f"SYM{i}", AssetStatus.ACTIVE, True) for i in range(24)]
    client = FakeTradingClient(assets)
    logger = logging.getLogger("test_disc")
    universe = discover_universe(client, logger, api_key="real_key")
    assert len(universe) == 24


def test_discovery_100_assets():
    assets = [FakeAsset(f"SYM{i}", AssetStatus.ACTIVE, True) for i in range(100)]
    client = FakeTradingClient(assets)
    logger = logging.getLogger("test_disc")
    universe = discover_universe(client, logger, api_key="real_key")
    assert len(universe) == 100


def test_discovery_filters_inactive_non_tradable_duplicates_and_slashes(monkeypatch):
    monkeypatch.setenv("MIN_UNIVERSE_SIZE", "2")
    assets = [
        FakeAsset("AAPL", AssetStatus.ACTIVE, True),
        FakeAsset("AAPL", AssetStatus.ACTIVE, True),  # Duplicate
        FakeAsset("INACT", AssetStatus.INACTIVE, True),  # Inactive
        FakeAsset("NOTRAD", AssetStatus.ACTIVE, False),  # Non-tradable
        FakeAsset("BAD/SYM", AssetStatus.ACTIVE, True),  # Slash symbol
        FakeAsset("MSFT", AssetStatus.ACTIVE, True),
    ]
    client = FakeTradingClient(assets)
    logger = logging.getLogger("test_disc")
    universe = discover_universe(client, logger, api_key="real_key")
    assert universe == ["AAPL", "MSFT"]


def test_discovery_api_failure_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("MONEY_MINTTER_TEMP_DIR", str(tmp_path))
    client = FakeTradingClient([], should_raise=True)
    logger = logging.getLogger("test_disc")
    with pytest.raises(ConnectivityError):
        discover_universe(client, logger, api_key="real_key")


def test_cached_universe_used_on_refresh_failure(monkeypatch):
    monkeypatch.setattr("money_mintter.core.live_bot.discover_universe", lambda client, logger, api_key: ["AAPL", "MSFT"])
    bot = LiveMarketBot()
    bot._cached_universe = ["AAPL", "MSFT"]
    bot._universe_last_fetched = datetime.now(timezone.utc) - timedelta(hours=5)  # Force refresh
    
    monkeypatch.setattr("money_mintter.core.live_bot.discover_universe", lambda client, logger, api_key: (_ for _ in ()).throw(RuntimeError("refresh failed")))

    universe = bot._get_or_refresh_universe(datetime.now(timezone.utc))
    assert universe == ["AAPL", "MSFT"]


def test_missing_market_data_does_not_crash_scan(monkeypatch):
    class FakeStockClient:
        def get_stock_bars(self, req):
            class FakeResp:
                data = {}
            return FakeResp()

    logger = logging.getLogger("test_scan")
    scanner = MarketScanner(["AAPL", "MSFT"], FakeStockClient(), logger)
    now_utc = datetime(2026, 10, 18, 10, 10, 0, tzinfo=timezone.utc)
    
    monkeypatch.setattr(MarketScanner, "is_session_open", lambda sym, now: (True, "Open"))

    candidates, dashboard = scanner.scan_universe(now_utc, set(), LiquiditySweepReversalStrategy(StrategyConfig()))
    assert len(candidates) == 0


def test_partially_missing_market_data(monkeypatch):
    class FakeBar:
        def __init__(self, ts, o, h, l, c, v):
            self.timestamp = ts
            self.open = o
            self.high = h
            self.low = l
            self.close = c
            self.volume = v

    class FakeStockClient:
        def get_stock_bars(self, req):
            class FakeResp:
                def __init__(self, data):
                    self.data = data
            now_utc = datetime(2026, 10, 18, 10, 10, 0, tzinfo=timezone.utc)
            bars = [FakeBar(now_utc - timedelta(minutes=5 * i), 100, 101, 99, 100, 1000) for i in range(50, 0, -1)]
            # AAPL has bars, MSFT does not return bars
            return FakeResp({"AAPL": bars})

    logger = logging.getLogger("test_scan")
    scanner = MarketScanner(["AAPL", "MSFT"], FakeStockClient(), logger)
    now_utc = datetime(2026, 10, 18, 10, 10, 0, tzinfo=timezone.utc)
    
    monkeypatch.setattr(MarketScanner, "is_session_open", lambda sym, now: (True, "Open"))

    candidates, dashboard = scanner.scan_universe(now_utc, set(), LiquiditySweepReversalStrategy(StrategyConfig()))
    # Should not crash, processes AAPL (even if no signal) and handles MSFT missing data gracefully
    assert scanner.last_scan_attempts == 2


def test_connectivity_error_does_not_retry_symbol_by_symbol(monkeypatch):
    call_count = 0

    class FakeStockClient:
        def get_stock_bars(self, req):
            nonlocal call_count
            call_count += 1
            raise requests.exceptions.ConnectionError("network down")

    logger = logging.getLogger("test_scan")
    scanner = MarketScanner(["AAPL", "MSFT"], FakeStockClient(), logger)
    now_utc = datetime(2026, 10, 18, 10, 10, 0, tzinfo=timezone.utc)
    
    monkeypatch.setattr(MarketScanner, "is_session_open", lambda sym, now: (True, "Open"))

    scanner.scan_universe(now_utc, set(), LiquiditySweepReversalStrategy(StrategyConfig()))
    # With batching of 2 symbols, get_stock_bars called once for batch. Connectivity error must NOT retry 2 times individually!
    assert call_count == 1
    assert scanner.last_scan_connectivity_errors == 2


def test_incremental_caching_and_pagination(monkeypatch):
    requests_made = []

    class FakeBar:
        def __init__(self, ts, o, h, l, c, v):
            self.timestamp = ts
            self.open = o
            self.high = h
            self.low = l
            self.close = c
            self.volume = v

    class FakeStockClient:
        def get_stock_bars(self, req):
            requests_made.append(req)
            class FakeResp:
                def __init__(self, data, token=None):
                    self.data = data
                    self.next_page_token = token
            now_utc = datetime(2026, 10, 18, 10, 10, 0, tzinfo=timezone.utc)
            bars = [FakeBar(req.start + timedelta(minutes=5 * i), 100, 101, 99, 100, 1000) for i in range(10)]
            return FakeResp({"AAPL": bars})

    logger = logging.getLogger("test_scan")
    scanner = MarketScanner(["AAPL"], FakeStockClient(), logger)
    now_utc = datetime(2026, 10, 18, 10, 10, 0, tzinfo=timezone.utc)
    
    monkeypatch.setattr(MarketScanner, "is_session_open", lambda sym, now: (True, "Open"))

    # First scan cycle
    scanner.scan_universe(now_utc, set(), LiquiditySweepReversalStrategy(StrategyConfig()))
    assert len(requests_made) == 1
    first_req_start = requests_made[0].start

    # Second scan cycle (should use incremental cache start, which is later than 30 days ago)
    later_utc = now_utc + timedelta(minutes=5)
    scanner.scan_universe(later_utc, set(), LiquiditySweepReversalStrategy(StrategyConfig()))
    assert len(requests_made) == 2
    second_req_start = requests_made[1].start
    assert second_req_start > first_req_start

