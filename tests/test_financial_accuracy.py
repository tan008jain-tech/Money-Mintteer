from __future__ import annotations

import logging
from typing import Any
import pytest
from datetime import datetime, timezone

from money_mintter.config import Settings
from money_mintter.core.live_bot import LivePortfolioManager, LiveMarketBot
from money_mintter.domain import Side
from alpaca.trading.enums import OrderSide, OrderClass


class FakeAccountClient:
    def __init__(self, cash: str, equity: str, buying_power: str, last_equity: str) -> None:
        self.cash = cash
        self.equity = equity
        self.buying_power = buying_power
        self.last_equity = last_equity


class FakeTradingClientForFinancials:
    def __init__(self, account: FakeAccountClient, positions: list[Any] | None = None) -> None:
        self._account = account
        self._positions = positions or []
        self.submitted_orders: list[Any] = []
        self.fail_get_account = False

    def get_account(self) -> Any:
        if self.fail_get_account:
            raise RuntimeError("API failure")
        return self._account

    def get_all_positions(self) -> list[Any]:
        return self._positions

    def get_orders(self, req: Any) -> list[Any]:
        return []

    def submit_order(self, order_req: Any) -> Any:
        self.submitted_orders.append(order_req)
        return type("Order", (), {"id": "test-id", "order_class": OrderClass.BRACKET, "legs": [object(), object()]})()


def test_equity_snapshot_and_last_equity_reconciliation() -> None:
    account = FakeAccountClient(cash="50000.0", equity="653.74", buying_power="50000.0", last_equity="649.96")
    client = FakeTradingClientForFinancials(account)
    logger = logging.getLogger("test_fin")
    manager = LivePortfolioManager(client, logger)

    cash, equity, bp, open_pos, dash_pos, locked = manager.fetch_account_and_positions()

    assert cash == 50000.0
    assert equity == 653.74
    assert manager.last_cash == 50000.0
    assert manager.last_equity_snapshot == 653.74
    assert manager.last_equity == 649.96
    assert manager.last_snapshot_time is not None
    
    diff = equity - manager.last_equity
    pct = (diff / manager.last_equity) * 100.0
    assert round(diff, 2) == 3.78
    assert round(pct, 2) == 0.58


def test_sequential_account_snapshots() -> None:
    account = FakeAccountClient(cash="50000.0", equity="650.00", buying_power="50000.0", last_equity="649.96")
    client = FakeTradingClientForFinancials(account)
    logger = logging.getLogger("test_fin")
    manager = LivePortfolioManager(client, logger)

    # First fetch sets baseline last_equity and snapshot
    c1, eq1, _, _, _, _ = manager.fetch_account_and_positions()
    assert eq1 == 650.00
    assert manager.last_equity == 649.96
    t1 = manager.last_snapshot_time

    # Subsequent sequential snapshot update
    account.equity = "653.74"
    account.cash = "49500.0"
    c2, eq2, _, _, _, _ = manager.fetch_account_and_positions()
    assert eq2 == 653.74
    assert manager.last_cash == 49500.0
    assert manager.last_equity_snapshot == 653.74
    # Baseline last_equity must remain stable across updates
    assert manager.last_equity == 649.96
    assert manager.last_snapshot_time is not None


def test_failed_account_refresh_handling() -> None:
    account = FakeAccountClient(cash="50000.0", equity="650.00", buying_power="50000.0", last_equity="649.96")
    client = FakeTradingClientForFinancials(account)
    logger = logging.getLogger("test_fin")
    manager = LivePortfolioManager(client, logger)

    # Successful initial fetch
    manager.fetch_account_and_positions()
    t_prev = manager.last_snapshot_time
    eq_prev = manager.last_equity_snapshot

    # Failed refresh
    client.fail_get_account = True
    with pytest.raises(RuntimeError, match="API failure"):
        manager.fetch_account_and_positions()

    # Verify previous successful snapshot timestamp and values are preserved without updating
    assert manager.last_snapshot_time == t_prev
    assert manager.last_equity_snapshot == eq_prev


def test_session_summary_unavailable_state(caplog: pytest.LogCaptureFixture) -> None:
    settings = Settings()
    account = FakeAccountClient(cash="10000.0", equity="653.74", buying_power="10000.0", last_equity="649.96")
    client = FakeTradingClientForFinancials(account)
    logger = logging.getLogger("MoneyMintterMasterBlueprint")
    logger.setLevel(logging.INFO)

    bot = LiveMarketBot.__new__(LiveMarketBot)
    bot.logger = logger
    bot.settings = settings
    bot.trading_client = client
    bot.portfolio_manager = LivePortfolioManager(client, logger)
    # Do NOT fetch account/positions (no successful snapshot available)

    with caplog.at_level(logging.INFO):
        bot._log_session_summary("completed", cycles=10, orders_sent=5, errors=0)

    log_output = caplog.text
    assert "timestamp=unavailable" in log_output
    assert "cash=unavailable" in log_output
    assert "equity=unavailable" in log_output


def test_session_summary_logging_snapshot_and_source(caplog: pytest.LogCaptureFixture) -> None:
    settings = Settings()
    account = FakeAccountClient(cash="10000.0", equity="653.74", buying_power="10000.0", last_equity="649.96")
    client = FakeTradingClientForFinancials(account)
    logger = logging.getLogger("MoneyMintterMasterBlueprint")
    logger.setLevel(logging.INFO)

    bot = LiveMarketBot.__new__(LiveMarketBot)
    bot.logger = logger
    bot.settings = settings
    bot.trading_client = client
    bot.portfolio_manager = LivePortfolioManager(client, logger)
    bot.portfolio_manager.fetch_account_and_positions()

    with caplog.at_level(logging.INFO):
        bot._log_session_summary("completed", cycles=10, orders_sent=5, errors=0)

    log_output = caplog.text
    assert "source=Alpaca Account API" in log_output
    assert "timestamp=" in log_output
    assert "equity=$653.74" in log_output
    assert "cash=$10000.00" in log_output
    assert "change=$+3.78 (+0.58%) vs baseline_equity=$649.96" in log_output
