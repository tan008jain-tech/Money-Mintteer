from __future__ import annotations

import logging
import math
import socket
import time
import pytest
import requests
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import tempfile
from alpaca.trading.enums import AssetStatus, AssetClass
from money_mintter.config import Settings
from money_mintter.core.live_bot import (
    ConnectivityError,
    is_connectivity_error,
    LivePortfolioManager,
    LiveMarketBot,
    MarketScanner,
    discover_universe as original_discover_universe,
)
from money_mintter.domain import Side


def test_is_connectivity_error_cases():
    # Breaks if is_connectivity_error or ConnectivityError is removed/altered
    assert is_connectivity_error(ConnectivityError("test")) is True
    assert is_connectivity_error(requests.exceptions.ConnectionError("conn")) is True
    assert is_connectivity_error(requests.exceptions.Timeout("timeout")) is True
    assert is_connectivity_error(requests.exceptions.ReadTimeout("read")) is True
    assert is_connectivity_error(requests.exceptions.ConnectTimeout("connect")) is True

    cause_exc = RuntimeError("wrapper")
    cause_exc.__cause__ = requests.exceptions.ConnectionError("inner")
    assert is_connectivity_error(cause_exc) is True

    assert is_connectivity_error(ValueError("invalid")) is False
    assert is_connectivity_error(RuntimeError("plain")) is False


def test_run_once_outage_recovery_and_kill_switch(monkeypatch):
    # Breaks if outage handling or record_error logic in run_once is removed
    monkeypatch.setattr("money_mintter.core.live_bot.TradingClient", MagicMock())
    monkeypatch.setattr("money_mintter.core.live_bot.StockHistoricalDataClient", MagicMock())
    bot = LiveMarketBot()
    bot.logger.setLevel(logging.INFO)
    bot.trading_client = MagicMock()
    bot.stock_client = MagicMock()
    bot.portfolio_manager.trading_client = bot.trading_client
    bot.portfolio_manager.fetch_open_order_symbols = MagicMock(return_value=set())
    bot.scanner.stock_client = bot.stock_client

    bot.portfolio_manager.fetch_account_and_positions = MagicMock(
        side_effect=requests.exceptions.ConnectionError("network down")
    )

    now = datetime(2026, 10, 22, 10, 0, tzinfo=timezone.utc)
    for i in range(10):
        res = bot.run_once(now + timedelta(seconds=i * 10))
        assert res.get("error") is True
        assert res.get("outage") is True
        assert bot.kill_switch.tripped is False

    bot.portfolio_manager.last_equity = 100000.0
    bot.portfolio_manager.fetch_account_and_positions = MagicMock(
        return_value=(100000.0, 100000.0, 200000.0, [], [], set())
    )
    bot.scanner.scan_universe = MagicMock(return_value=([], []))
    bot.portfolio_manager.submit_bracket_orders = MagicMock(return_value=0)

    res_success = bot.run_once(now + timedelta(seconds=120))
    assert res_success.get("error") is None or res_success.get("error") is False

    bot.portfolio_manager.fetch_account_and_positions = MagicMock(
        side_effect=ValueError("logic error")
    )
    for i in range(5):
        bot.run_once(now + timedelta(seconds=150 + i * 10))

    assert bot.kill_switch.tripped is True


def test_outage_exceeds_max_outage_minutes():
    # Breaks if max_outage_minutes check or kill_switch.trip is removed
    bot = LiveMarketBot()
    bot.trading_client = MagicMock()
    bot.stock_client = MagicMock()
    bot.portfolio_manager.trading_client = bot.trading_client
    bot.portfolio_manager.fetch_open_order_symbols = MagicMock(return_value=set())
    bot.scanner.stock_client = bot.stock_client
    bot.settings = Settings(max_outage_minutes=1)

    now = datetime(2026, 10, 22, 10, 0, tzinfo=timezone.utc)
    bot.portfolio_manager.fetch_account_and_positions = MagicMock(
        side_effect=requests.exceptions.ConnectionError("down")
    )

    bot.run_once(now)
    assert bot.kill_switch.tripped is False

    bot.run_once(now + timedelta(seconds=65))
    assert bot.kill_switch.tripped is True
    assert "outage" in bot.kill_switch.reason.lower()


def test_scanner_connectivity_vs_runtime_error():
    # Breaks if scan_universe connectivity error counting or ConnectivityError raise is removed
    bot = LiveMarketBot()
    bot.global_universe = ["AAPL", "MSFT"]
    bot.scanner.global_universe = ["AAPL", "MSFT"]
    bot.portfolio_manager.last_equity = 100000.0
    bot.portfolio_manager.fetch_account_and_positions = MagicMock(
        return_value=(100000.0, 100000.0, 200000.0, [], [], set())
    )
    bot.portfolio_manager.fetch_open_order_symbols = MagicMock(return_value=set())
    bot.scanner.stock_client.get_stock_bars = MagicMock(side_effect=requests.exceptions.ConnectionError("net down"))
    open_time = datetime(2026, 10, 21, 14, 0, tzinfo=timezone.utc)
    with pytest.raises(ConnectivityError):
        bot.run_cycle(open_time)

    bot.scanner.stock_client.get_stock_bars = MagicMock(side_effect=ValueError("bad value"))
    with pytest.raises(RuntimeError):
        bot.run_cycle(open_time)


def test_flatten_all_max_wait_seconds_success():
    # Breaks if flatten_all max_wait_seconds retry loop or success check is removed
    client = MagicMock()
    client.close_all_positions = MagicMock(side_effect=[
        requests.exceptions.ConnectionError("fail 1"),
        requests.exceptions.ConnectionError("fail 2"),
        None
    ])
    acc = MagicMock()
    acc.cash = "100000.0"
    acc.equity = "100000.0"
    client.get_account = MagicMock(return_value=acc)
    client.get_all_positions = MagicMock(side_effect=[
        [MagicMock(symbol="AAPL", qty="10", avg_entry_price="150")],
        [MagicMock(symbol="AAPL", qty="10", avg_entry_price="150")],
        []
    ])

    pm = LivePortfolioManager(client, logging.getLogger("test"))
    sleeps = []
    res = pm.flatten_all(sleep_fn=lambda s: sleeps.append(s), max_wait_seconds=60, retry_delay=15)
    assert res == []
    assert sum(sleeps) <= 60


def test_flatten_all_max_wait_succeeds_with_symbols():
    # Breaks if flatten_all remembering successful positions or returning them at max_wait is removed
    client = MagicMock()
    client.close_all_positions = MagicMock(return_value=None)
    acc = MagicMock()
    acc.cash = "100000.0"
    acc.equity = "100000.0"
    client.get_account = MagicMock(return_value=acc)
    client.get_all_positions = MagicMock(return_value=[MagicMock(symbol="AAPL", qty="10", avg_entry_price="150")])

    pm = LivePortfolioManager(client, logging.getLogger("test"))
    sleeps = []
    res = pm.flatten_all(sleep_fn=lambda s: sleeps.append(s), max_wait_seconds=60, retry_delay=15)
    assert res == ["AAPL"]
    assert len(sleeps) == math.ceil(60 / 15)


def test_flatten_all_never_succeeds_unknown():
    # Breaks if flatten_all returning ["UNKNOWN"] when verification never succeeds is removed
    client = MagicMock()
    client.close_all_positions = MagicMock(return_value=None)
    client.get_account = MagicMock(side_effect=RuntimeError("account down"))

    pm = LivePortfolioManager(client, logging.getLogger("test"))
    sleeps = []
    res = pm.flatten_all(sleep_fn=lambda s: sleeps.append(s), max_wait_seconds=60, retry_delay=15)
    assert res == ["UNKNOWN"]
    assert len(sleeps) == math.ceil(60 / 15)


def test_flatten_all_monotonic_deadline_accounts_for_broker_time():
    # Breaks if flatten_all max_wait_seconds does not account for time spent in broker requests using monotonic deadline
    client = MagicMock()
    def slow_close_all(*args, **kwargs):
        time.sleep(0.05)
        return None
    client.close_all_positions = MagicMock(side_effect=slow_close_all)
    client.cancel_orders = MagicMock(return_value=None)
    acc = MagicMock()
    acc.cash = "100000.0"
    acc.equity = "100000.0"
    client.get_account = MagicMock(return_value=acc)
    client.get_all_positions = MagicMock(return_value=[MagicMock(symbol="AAPL", qty="10", avg_entry_price="150")])

    pm = LivePortfolioManager(client, logging.getLogger("test"))
    sleeps = []
    start_mono = time.monotonic()
    res = pm.flatten_all(sleep_fn=lambda s: sleeps.append(s), max_wait_seconds=0.1, retry_delay=0.1)
    duration = time.monotonic() - start_mono
    assert res == ["AAPL"]
    assert duration < 1.0


def test_flatten_all_enforces_flatten_max_wait_seconds_regression():
    # Regression test: FLATTEN_MAX_WAIT_SECONDS / max_wait_seconds deadline enforcement including broker calls and sleeps
    client = MagicMock()
    def slow_get_positions(*args, **kwargs):
        time.sleep(0.05)
        return [MagicMock(symbol="AAPL", qty="10", avg_entry_price="150")]
    client.get_all_positions = MagicMock(side_effect=slow_get_positions)
    client.cancel_orders = MagicMock(return_value=None)
    client.close_all_positions = MagicMock(return_value=None)
    acc = MagicMock()
    acc.cash = "100000.0"
    acc.equity = "100000.0"
    client.get_account = MagicMock(return_value=acc)

    pm = LivePortfolioManager(client, logging.getLogger("test"))
    sleeps = []
    res = pm.flatten_all(sleep_fn=lambda s: sleeps.append(s), max_wait_seconds=0.04, retry_delay=0.01)
    assert res == ["AAPL"]


def test_run_session_safe_flatten_logging(caplog):
    # Breaks if _safe_flatten logging CRITICAL "POSITIONS MAY STILL BE OPEN" for non-empty or UNKNOWN is removed
    bot = LiveMarketBot()
    clock = MagicMock(is_open=True, next_open=datetime.now(timezone.utc), next_close=datetime.now(timezone.utc) + timedelta(hours=1))
    bot.trading_client.get_clock = MagicMock(return_value=clock)
    bot.trading_client.get_account = MagicMock(return_value=MagicMock(cash="100000", equity="100000", buying_power="100000", last_equity="100000"))

    # Case 1: flatten_all returns ["AAPL"]
    bot.portfolio_manager.flatten_all = MagicMock(return_value=["AAPL"])
    with caplog.at_level(logging.CRITICAL):
        caplog.clear()
        res1 = bot.run_session(now_fn=lambda: datetime.now(timezone.utc) + timedelta(hours=2), sleep_fn=lambda s: None)
    assert res1["status"] == "flatten_incomplete"
    assert "POSITIONS MAY STILL BE OPEN" in caplog.text

    # Case 2: flatten_all returns ["UNKNOWN"]
    bot.portfolio_manager.flatten_all = MagicMock(return_value=["UNKNOWN"])
    with caplog.at_level(logging.CRITICAL):
        caplog.clear()
        res2 = bot.run_session(now_fn=lambda: datetime.now(timezone.utc) + timedelta(hours=2), sleep_fn=lambda s: None)
    assert res2["status"] == "flatten_incomplete"
    assert "POSITIONS MAY STILL BE OPEN" in caplog.text
    assert "position state could not be verified" in caplog.text


def test_main_banner_positions_open_symbol(capsys, monkeypatch):
    # Breaks if main() printing banner containing the symbol when remaining is ["AAPL"] is removed
    from money_mintter.core import live_bot

    class MockLock:
        def acquire(self):
            return True
        def release(self):
            pass

    monkeypatch.setattr(live_bot, "SessionLock", lambda path: MockLock())

    class MockBot:
        def run_session(self):
            return {"status": "flatten_incomplete", "cycles": 5, "orders_sent": 2, "errors": 0, "remaining": ["AAPL"]}

    monkeypatch.setattr(live_bot, "LiveMarketBot", lambda: MockBot())

    code = live_bot.main()
    captured = capsys.readouterr()
    assert "AAPL" in captured.out
    assert "POSITIONS MAY STILL BE OPEN" in captured.out


def test_settings_validation_outage_and_flatten():
    # Breaks if max_outage_minutes or flatten_max_wait_seconds validation in Settings is removed
    s = Settings()
    assert s.max_outage_minutes == 15
    assert s.flatten_max_wait_seconds == 600

    with pytest.raises(ValueError, match="max_outage_minutes"):
        Settings(max_outage_minutes=0)

    with pytest.raises(ValueError, match="flatten_max_wait_seconds"):
        Settings(flatten_max_wait_seconds=-1)


def test_main_banner_positions_open(capsys, monkeypatch):
    # Breaks if main banner printing for remaining positions is removed
    from money_mintter.core import live_bot

    class MockLock:
        def acquire(self):
            return True
        def release(self):
            pass

    monkeypatch.setattr(live_bot, "SessionLock", lambda path: MockLock())

    class MockBot:
        def run_session(self):
            return {"status": "flatten_incomplete", "cycles": 5, "orders_sent": 2, "errors": 0, "remaining": ["AAPL"]}

    monkeypatch.setattr(live_bot, "LiveMarketBot", lambda: MockBot())

    code = live_bot.main()
    captured = capsys.readouterr()
    assert "POSITIONS MAY STILL BE OPEN" in captured.out or "CHECK ALPACA" in captured.out


def test_mid_session_outage_continuation_and_flatten():
    # Breaks if run_session outage handling or final flatten execution is removed
    bot = LiveMarketBot()
    
    clock = MagicMock()
    clock.is_open = True
    now_val = datetime(2026, 10, 22, 10, 0, tzinfo=timezone.utc)
    clock.next_close = now_val + timedelta(minutes=10)
    bot.trading_client.get_clock = MagicMock(return_value=clock)

    call_count = [0]
    def mock_run_once(now):
        call_count[0] += 1
        if call_count[0] == 1:
            raise requests.exceptions.ConnectionError("outage")
        return {"evaluated": 1, "signals": 0, "orders_sent": 0}

    bot.run_once = mock_run_once
    bot.portfolio_manager.flatten_all = MagicMock(return_value=[])

    current_time = now_val
    def mock_now():
        return current_time

    def mock_sleep(secs):
        nonlocal current_time
        current_time += timedelta(seconds=secs)

    res = bot.run_session(now_fn=mock_now, sleep_fn=mock_sleep)
    assert res["status"] == "completed"
    bot.portfolio_manager.flatten_all.assert_called_once()


def test_discover_universe_layer2_failure_integration(monkeypatch) -> None:
    monkeypatch.setattr("money_mintter.core.live_bot.discover_universe", original_discover_universe)

    # Integration test: real discover_universe calling DailySuitabilityEngine where L2 fetch fails and no cache exists -> returns []
    trading_client = MagicMock()
    class MockAsset:
        def __init__(self, symbol):
            self.symbol = symbol
            self.name = "Apple Inc"
            self.exchange = "NASDAQ"
            self.status = AssetStatus.ACTIVE
            self.tradable = True
            self.asset_class = AssetClass.US_EQUITY
            self.shortable = True
            self.fractionable = True
            self.marginable = True

    trading_client.get_all_assets.return_value = [MockAsset("AAPL")]

    stock_client = MagicMock()
    stock_client.get_stock_bars.side_effect = RuntimeError("Historical API down")

    monkeypatch.setattr("money_mintter.core.live_bot.StockHistoricalDataClient", lambda *args, **kwargs: stock_client)
    monkeypatch.setattr("money_mintter.core.suitability.StockHistoricalDataClient", lambda *args, **kwargs: stock_client)

    logger = logging.getLogger("test_integration_outage")
    with tempfile.TemporaryDirectory() as tmpdir:
        monkeypatch.setenv("MONEY_MINTTER_TEMP_DIR", tmpdir)
        monkeypatch.setenv("MM_CACHE_DIR", tmpdir)
        # Requirement 1: Discovery returns an empty candidate pool through the real discovery and fallback path.
        pool = original_discover_universe(trading_client, logger)
        assert pool == []
        # Confirm that the mocked historical-data client's get_stock_bars() was actually called and raised the intended failure
        assert stock_client.get_stock_bars.called


def test_live_market_bot_position_management_continuity_on_l2_failure(monkeypatch):
    monkeypatch.setattr("money_mintter.core.live_bot.discover_universe", original_discover_universe)

    # Integration/runtime test: LiveMarketBot initializes via real discover_universe encountering L2 failure with no cache,
    # verifying monitoring, risk/safety control execution, new-entry suppression, and existing position management continuity.
    trading_client = MagicMock()
    class MockAsset:
        def __init__(self, symbol):
            self.symbol = symbol
            self.name = "Apple Inc"
            self.exchange = "NASDAQ"
            self.status = AssetStatus.ACTIVE
            self.tradable = True
            self.asset_class = AssetClass.US_EQUITY
            self.shortable = True
            self.fractionable = True
            self.marginable = True

    trading_client.get_all_assets.return_value = [MockAsset("AAPL")]

    stock_client = MagicMock()
    stock_client.get_stock_bars.side_effect = RuntimeError("Historical API down")

    monkeypatch.setattr("money_mintter.core.live_bot.TradingClient", lambda *args, **kwargs: trading_client)
    monkeypatch.setattr("money_mintter.core.live_bot.StockHistoricalDataClient", lambda *args, **kwargs: stock_client)
    monkeypatch.setattr("money_mintter.core.suitability.StockHistoricalDataClient", lambda *args, **kwargs: stock_client)

    with tempfile.TemporaryDirectory() as tmpdir:
        monkeypatch.setenv("MONEY_MINTTER_TEMP_DIR", tmpdir)
        monkeypatch.setenv("MM_CACHE_DIR", tmpdir)

        # Initialize bot without mocking discover_universe (testing real discovery and startup runtime path)
        bot = LiveMarketBot()
        assert bot.global_universe == []
        # Confirm that the mocked historical-data client's get_stock_bars() was actually called during initialization
        assert stock_client.get_stock_bars.called

        # Spy on account & position monitoring and risk/safety controls
        fetch_mock = MagicMock(return_value=(
            50000.0, 100000.0, 100000.0,
            [{"symbol": "AAPL", "quantity": 10, "current_price": 150.0}],
            [{"symbol": "AAPL", "side": "BUY"}],
            {"AAPL"}
        ))
        bot.portfolio_manager.fetch_account_and_positions = fetch_mock
        bot.portfolio_manager.fetch_open_order_symbols = MagicMock(return_value=set())

        check_daily_loss_mock = MagicMock(wraps=bot.kill_switch.check_daily_loss)
        bot.kill_switch.check_daily_loss = check_daily_loss_mock

        submit_orders_mock = MagicMock(return_value=0)
        bot.portfolio_manager.submit_bracket_orders = submit_orders_mock

        bot.portfolio_manager.last_equity = 100000.0
        now = datetime(2026, 10, 22, 14, 0, tzinfo=timezone.utc)
        res = bot.run_once(now)

        # Assertions proving all requirements:
        # 1. Discovery returned empty pool
        # 2. Bot continues account and existing-position monitoring (fetch_account_and_positions called)
        fetch_mock.assert_called_once()

        # 3. Relevant risk and safety controls execute (check_daily_loss called)
        check_daily_loss_mock.assert_called_once()

        # 4. New-entry orders remain disabled (orders_sent == 0, submit_bracket_orders received empty top_new)
        assert res.get("error") is not True
        assert res.get("orders_sent", 0) == 0
        submit_orders_mock.assert_called_once()
        call_args = submit_orders_mock.call_args[0]
        top_new_arg = call_args[0]
        assert top_new_arg == []

        # 5. Legitimate existing-position management and safety actions remain possible (AAPL is locked/managed in position-management-only mode)
        locked_arg = call_args[1]
        assert "AAPL" in locked_arg


