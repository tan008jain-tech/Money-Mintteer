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
    apply_discovery_timeout,
    UniverseDiscoveryWorker,
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
    val = timeout_calls[0][1]
    assert (val[0] if isinstance(val, list) else val) == 30.0


def test_safe_drain_worker_start_non_blocking_and_idempotent(monkeypatch):
    import threading
    event = threading.Event()
    def slow_discover(client, logger, api_key):
        event.wait(timeout=2.0)
        return []

    monkeypatch.setattr("money_mintter.core.live_bot.discover_universe", slow_discover)
    client = MagicMock()
    logger = logging.getLogger("test_worker")
    worker = UniverseDiscoveryWorker(client, logger, "key")

    assert worker.start() is True
    assert worker.state == "DISCOVERING"

    # Repeated/simultaneous start requests return False and do not duplicate threads
    assert worker.start() is False
    assert worker.start_if_needed() is False

    event.set()
    if worker._thread:
        worker._thread.join(timeout=2.0)


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


def test_1_successful_account_retrieval_returns_actual_balances():
    client = MagicMock()
    account_mock = MagicMock()
    account_mock.cash = "12345.67"
    account_mock.equity = "50000.0"
    account_mock.buying_power = "100000.0"
    client.get_account.return_value = account_mock
    client.get_all_positions.return_value = []

    manager = LivePortfolioManager(client, logging.getLogger("test"))
    cash, equity, bp, open_pos, dash_pos, locked = manager.fetch_account_and_positions()
    assert cash == 12345.67
    assert equity == 50000.0
    assert bp == 100000.0


def test_2_account_api_failure_never_creates_fabricated_balances():
    client = MagicMock()
    client.get_account.side_effect = RuntimeError("Account error")
    client.get_all_positions.return_value = []

    manager = LivePortfolioManager(client, logging.getLogger("test"))
    with pytest.raises(RuntimeError, match="Account error"):
        manager.fetch_account_and_positions()


def test_3_successful_position_retrieval_distinguishes_open_and_flat():
    client = MagicMock()
    p_mock = MagicMock()
    p_mock.symbol = "AAPL"
    p_mock.qty = "10"
    p_mock.avg_entry_price = "150.0"
    p_mock.current_price = "155.0"
    p_mock.unrealized_pl = "50.0"

    client.get_all_positions.return_value = [p_mock]
    manager = LivePortfolioManager(client, logging.getLogger("test"))
    status, open_pos, locked = manager.fetch_position_state()
    assert status == "POSITIONS_OPEN"
    assert len(open_pos) == 1

    client.get_all_positions.return_value = []
    status_flat, _, _ = manager.fetch_position_state()
    assert status_flat == "POSITIONS_CONFIRMED_FLAT"


def test_4_failed_position_retrieval_produces_unknown():
    client = MagicMock()
    client.get_all_positions.side_effect = RuntimeError("Positions error")
    manager = LivePortfolioManager(client, logging.getLogger("test"))
    status, open_pos, locked = manager.fetch_position_state()
    assert status == "POSITIONS_UNKNOWN"
    assert open_pos == []


def test_5_account_failure_does_not_make_flat_position_list_unknown():
    client = MagicMock()
    client.get_account.side_effect = RuntimeError("Account down")
    client.get_all_positions.return_value = []
    manager = LivePortfolioManager(client, logging.getLogger("test"))

    with pytest.raises(RuntimeError, match="Account down"):
        manager.fetch_account_and_positions()

    status, _, _ = manager.fetch_position_state()
    assert status == "POSITIONS_CONFIRMED_FLAT"


def test_6_and_7_assets_request_and_other_requests_timeouts():
    class FakeSess:
        def __init__(self):
            self.last_kwargs = {}
        def request(self, method, url, **kwargs):
            self.last_kwargs = kwargs
            return (method, url, kwargs)

    class FakeClient:
        def __init__(self):
            self._session = FakeSess()

    client = FakeClient()
    apply_discovery_timeout(client, 5.0, 30.0)

    client._session.request("GET", "https://api.alpaca.markets/v2/assets")
    assert client._session.last_kwargs.get("timeout") == (5.0, 30.0)

    client._session.request("GET", "https://api.alpaca.markets/v2/account")
    assert client._session.last_kwargs.get("timeout") == (5.0, 20.0)


def test_8_wrapper_installation_idempotent_and_updates_config():
    class FakeSess:
        def __init__(self):
            self.last_kwargs = {}
        def request(self, method, url, **kwargs):
            self.last_kwargs = kwargs
            return (method, url, kwargs)

    class FakeClient:
        def __init__(self):
            self._session = FakeSess()

    client = FakeClient()
    read_holder1 = [30.0]
    apply_discovery_timeout(client, 5.0, read_holder1)

    client._session.request("GET", "/v2/assets")
    assert client._session.last_kwargs.get("timeout") == (5.0, 30.0)

    read_holder2 = [45.0]
    apply_discovery_timeout(client, 5.0, read_holder2)

    client._session.request("GET", "/v2/assets")
    assert client._session.last_kwargs.get("timeout") == (5.0, 45.0)


def test_9_discovery_timeout_configuration_errors_fail_explicitly():
    class BadClient:
        pass

    with pytest.raises(RuntimeError, match="missing session"):
        apply_discovery_timeout(BadClient(), 5.0, 30.0)


def test_open_order_query_failure_blocks_new_entries():
    client = MagicMock()
    client.get_account.return_value = MagicMock(cash="100000", equity="100000", buying_power="200000", last_equity="100000")
    client.get_all_positions.return_value = []
    client.get_orders.side_effect = RuntimeError("Open orders query timeout")

    manager = LivePortfolioManager(client, logging.getLogger("test"))
    with pytest.raises(RuntimeError, match="Open orders query timeout"):
        manager.fetch_account_position_lists()

    with pytest.raises(RuntimeError, match="Open orders query timeout"):
        manager.fetch_open_order_symbols()

    # Exercise actual entry-submission path via LiveMarketBot run_once
    bot = LiveMarketBot.__new__(LiveMarketBot)
    bot.logger = logging.getLogger("test")
    bot.settings = Settings()
    bot.trading_client = client
    bot.portfolio_manager = manager
    bot.kill_switch = type("KillSwitch", (), {
        "tripped": False,
        "check_daily_loss": lambda *args: None,
        "record_error": lambda *args: None,
        "record_success": lambda *args: None,
    })()
    bot.global_universe = ["AAPL"]
    bot._cached_universe = ["AAPL"]
    bot.latest_state = {}
    bot.processed_signals = set()
    bot._outage_start = None
    bot._last_outage_log_time = None

    now_utc = datetime.now(timezone.utc)
    res = bot.run_once(now_utc)
    assert res.get("error") is True
    assert res.get("orders_sent", 0) == 0
    client.submit_order.assert_not_called()



def test_missing_protection_info_remains_unknown():
    client = MagicMock()
    p_mock = MagicMock()
    p_mock.symbol = "AAPL"
    p_mock.qty = "10"
    p_mock.avg_entry_price = "150.0"
    p_mock.current_price = "155.0"
    p_mock.unrealized_pl = "50.0"

    client.get_all_positions.return_value = [p_mock]
    client.get_orders.return_value = []  # No open orders or protection legs

    manager = LivePortfolioManager(client, logging.getLogger("test"))
    _, dashboard_pos, _ = manager.fetch_account_position_lists()

    assert len(dashboard_pos) == 1
    assert dashboard_pos[0]["stop_price"] is None
    assert dashboard_pos[0]["target_price"] is None


