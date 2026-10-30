from __future__ import annotations

import logging
import time
import pytest
import requests
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

from alpaca.trading.enums import AssetStatus, AssetClass
from money_mintter.config import Settings
from money_mintter.core.live_bot import (
    ConnectivityError,
    discover_universe,
    LiveMarketBot,
    LivePortfolioManager,
)


class FakeAsset:
    def __init__(self, symbol: str, status: AssetStatus = AssetStatus.ACTIVE, tradable: bool = True):
        self.symbol = symbol
        self.status = status
        self.tradable = tradable
        self.asset_class = AssetClass.US_EQUITY
        self.name = symbol
        self.exchange = "NASDAQ"
        self.shortable = True
        self.fractionable = True
        self.marginable = True


@pytest.fixture(autouse=True)
def _mock_suitability(monkeypatch):
    from money_mintter.core.suitability import SuitabilityResult
    def mock_eval(self, eligible_symbols, now_utc=None, force_refresh=False):
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
    monkeypatch.setattr("money_mintter.core.suitability.DailySuitabilityEngine.evaluate_universe", mock_eval)


def test_discovery_applies_30s_timeout(monkeypatch):
    timeout_calls = []
    def mock_apply_timeout(client, connect, read):
        timeout_calls.append((connect, read))

    monkeypatch.setattr("money_mintter.core.live_bot.apply_default_timeout", mock_apply_timeout)
    client = MagicMock()
    client.get_all_assets.return_value = [FakeAsset(f"SYM{i}") for i in range(60)]
    logger = logging.getLogger("test_timeout")

    discover_universe(client, logger)
    assert len(timeout_calls) == 1
    assert timeout_calls[0][1] == 30.0


def test_discovery_retries_transient_and_429_retry_after(monkeypatch):
    attempts = [0]
    
    class FakeAPIError(Exception):
        def __init__(self, status_code, retry_after=None):
            self.status_code = status_code
            self.response = MagicMock()
            self.response.headers = {"Retry-After": str(retry_after)} if retry_after else {}
            super().__init__(f"HTTP {status_code}")

    def mock_get_assets(req):
        attempts[0] += 1
        if attempts[0] == 1:
            raise FakeAPIError(429, retry_after=1)
        elif attempts[0] == 2:
            raise FakeAPIError(503)
        elif attempts[0] == 3:
            raise requests.exceptions.ConnectionError("network blip")
        return [FakeAsset(f"SYM{i}") for i in range(60)]

    client = MagicMock()
    client.get_all_assets.side_effect = mock_get_assets
    logger = logging.getLogger("test_retry")

    monkeypatch.setattr(time, "sleep", lambda s: None)
    universe = discover_universe(client, logger)
    assert len(universe) == 60
    assert attempts[0] == 4


def test_discovery_non_retryable_aborts_immediately(monkeypatch):
    attempts = [0]
    class FakeAPIError(Exception):
        def __init__(self, status_code):
            self.status_code = status_code
            super().__init__(f"HTTP {status_code}")

    client = MagicMock()
    def mock_get_assets(req):
        attempts[0] += 1
        raise FakeAPIError(401)
    client.get_all_assets.side_effect = mock_get_assets
    logger = logging.getLogger("test_abort")

    with pytest.raises(ConnectivityError):
        discover_universe(client, logger)
    assert attempts[0] == 1


def test_startup_discovery_failure_no_positions_raises(monkeypatch):
    monkeypatch.setattr("money_mintter.core.live_bot.discover_universe", lambda client, logger, api_key: (_ for _ in ()).throw(ConnectivityError("UNIVERSE_DISCOVERY_FAILED")))
    
    trading_client = MagicMock()
    trading_client.get_all_positions.return_value = []
    monkeypatch.setattr("money_mintter.core.live_bot.TradingClient", lambda *args, **kwargs: trading_client)
    monkeypatch.setattr("money_mintter.core.live_bot.StockHistoricalDataClient", lambda *args, **kwargs: MagicMock())

    with pytest.raises(ConnectivityError, match="UNIVERSE_DISCOVERY_FAILED"):
        LiveMarketBot()


def test_safe_drain_transition_with_open_positions(monkeypatch):
    monkeypatch.setattr("money_mintter.core.live_bot.discover_universe", lambda client, logger, api_key: (_ for _ in ()).throw(ConnectivityError("UNIVERSE_DISCOVERY_FAILED")))
    
    trading_client = MagicMock()
    pos_mock = MagicMock()
    pos_mock.symbol = "AAPL"
    trading_client.get_all_positions.return_value = [pos_mock]
    monkeypatch.setattr("money_mintter.core.live_bot.TradingClient", lambda *args, **kwargs: trading_client)
    monkeypatch.setattr("money_mintter.core.live_bot.StockHistoricalDataClient", lambda *args, **kwargs: MagicMock())

    bot = LiveMarketBot()
    assert bot.safe_drain_mode is True

    # Run cycle while in SAFE_DRAIN with open positions
    now = datetime.now(timezone.utc)
    res = bot.run_cycle(now)
    assert res.get("safe_drain") is True
    assert res.get("orders_sent") == 0


def test_safe_drain_shutdown_when_broker_confirmed_flat(monkeypatch):
    monkeypatch.setattr("money_mintter.core.live_bot.discover_universe", lambda client, logger, api_key: (_ for _ in ()).throw(ConnectivityError("UNIVERSE_DISCOVERY_FAILED")))
    
    trading_client = MagicMock()
    trading_client.get_all_positions.side_effect = [
        [MagicMock(symbol="AAPL")],  # startup has positions -> enters SAFE_DRAIN
        []                           # subsequent check confirms flat
    ]
    monkeypatch.setattr("money_mintter.core.live_bot.TradingClient", lambda *args, **kwargs: trading_client)
    monkeypatch.setattr("money_mintter.core.live_bot.StockHistoricalDataClient", lambda *args, **kwargs: MagicMock())

    bot = LiveMarketBot()
    assert bot.safe_drain_mode is True

    now = datetime.now(timezone.utc)
    res = bot.run_cycle(now)
    assert res.get("halted") is True
    assert res.get("flat") is True


def test_safe_drain_recovery_on_successful_periodic_retry(monkeypatch):
    call_count = [0]
    def mock_discover(client, logger, api_key):
        call_count[0] += 1
        if call_count[0] == 1:
            raise ConnectivityError("fail")
        return [f"SYM{i}" for i in range(60)]

    monkeypatch.setattr("money_mintter.core.live_bot.discover_universe", mock_discover)

    trading_client = MagicMock()
    trading_client.get_all_positions.return_value = [MagicMock(symbol="AAPL")]
    monkeypatch.setattr("money_mintter.core.live_bot.TradingClient", lambda *args, **kwargs: trading_client)
    monkeypatch.setattr("money_mintter.core.live_bot.StockHistoricalDataClient", lambda *args, **kwargs: MagicMock())

    bot = LiveMarketBot()
    assert bot.safe_drain_mode is True

    # Simulate time passing so periodic retry triggers
    now = datetime.now(timezone.utc) + timedelta(seconds=65)
    universe = bot._get_or_refresh_universe(now)
    assert bot.safe_drain_mode is False
    assert len(universe) == 60
