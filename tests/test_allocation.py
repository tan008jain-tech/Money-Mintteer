from __future__ import annotations

import logging
from typing import Any
import pytest

from money_mintter.config import Settings
from money_mintter.core.live_bot import LivePortfolioManager, LiveMarketBot
from money_mintter.domain import Side
from money_mintter.strategy import LiquiditySweepReversalStrategy, StrategyConfig
from alpaca.trading.enums import OrderSide, OrderClass


class FakeSubmittedOrder:
    def __init__(self, order_class=OrderClass.BRACKET, legs=None) -> None:
        self.order_class = order_class
        self.legs = legs or [object(), object()]
        self.id = "fake-order-id"


class FakeTradingClient:
    def __init__(self, positions=None) -> None:
        self.submitted_orders: list[Any] = []
        self._positions = positions or []
        self._account = type("Account", (), {"cash": 1000.0, "equity": 650.0, "buying_power": 1000.0, "last_equity": 650.0})()

    def submit_order(self, order_req: Any) -> Any:
        self.submitted_orders.append(order_req)
        return FakeSubmittedOrder()

    def get_account(self) -> Any:
        return self._account

    def get_all_positions(self) -> list[Any]:
        return self._positions

    def get_orders(self, req: Any) -> list[Any]:
        return []

    def get_clock(self) -> Any:
        return type("Clock", (), {"is_open": True, "next_open": None, "next_close": None})()


class FakePosition:
    def __init__(self, symbol: str, qty: float, current_price: float) -> None:
        self.symbol = symbol
        self.qty = qty
        self.avg_entry_price = current_price
        self.current_price = current_price
        self.unrealized_pl = 0.0


def test_allocation_a_single_sell(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RISK_PER_TRADE_PCT", "0.5")
    monkeypatch.setenv("MAX_PORTFOLIO_RISK_PCT", "2")
    settings = Settings()

    client = FakeTradingClient()
    manager = LivePortfolioManager(client, logging.getLogger("test"))

    top_new = [("AAPL", 0.9, Side.SELL, "reason", 553.12, 555.62, 548.94)]
    orders_sent = manager.submit_bracket_orders(top_new, set(), 650.0, settings)

    assert orders_sent == 1
    assert len(client.submitted_orders) == 1
    assert client.submitted_orders[0].qty == 1
    assert client.submitted_orders[0].side == OrderSide.SELL


def test_allocation_b_two_buy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RISK_PER_TRADE_PCT", "0.5")
    monkeypatch.setenv("MAX_PORTFOLIO_RISK_PCT", "2")
    settings = Settings()

    client = FakeTradingClient()
    manager = LivePortfolioManager(client, logging.getLogger("test"))

    top_new = [
        ("A", 0.9, Side.BUY, "r", 100.0, 99.0, 102.0),
        ("B", 0.8, Side.BUY, "r", 200.0, 198.0, 204.0),
    ]
    orders_sent = manager.submit_bracket_orders(top_new, set(), 650.0, settings)

    assert orders_sent == 2
    assert len(client.submitted_orders) == 2
    assert client.submitted_orders[0].qty == 3
    assert client.submitted_orders[1].qty == 1
    total_notional = client.submitted_orders[0].qty * 100.0 + client.submitted_orders[1].qty * 200.0
    assert total_notional <= 650.0


def test_allocation_c_entry_above_equity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RISK_PER_TRADE_PCT", "0.5")
    monkeypatch.setenv("MAX_PORTFOLIO_RISK_PCT", "2")
    settings = Settings()

    client = FakeTradingClient()
    manager = LivePortfolioManager(client, logging.getLogger("test"))

    top_new = [("AAPL", 0.9, Side.BUY, "r", 700.0, 690.0, 720.0)]
    orders_sent = manager.submit_bracket_orders(top_new, set(), 650.0, settings)

    assert orders_sent == 0
    assert len(client.submitted_orders) == 0
    assert manager.last_submit_failures == 0


def test_allocation_d_cost_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RISK_PER_TRADE_PCT", "0.5")
    monkeypatch.setenv("MAX_PORTFOLIO_RISK_PCT", "2")
    settings = Settings()

    client = FakeTradingClient()
    manager = LivePortfolioManager(client, logging.getLogger("test"))

    top_new = [("AAPL", 0.9, Side.BUY, "r", 92.69, 91.47, 93.28)]
    orders_sent = manager.submit_bracket_orders(top_new, set(), 650.0, settings)

    assert orders_sent == 0
    assert len(client.submitted_orders) == 0
    assert manager.last_submit_failures == 0


def test_allocation_e_open_exposure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RISK_PER_TRADE_PCT", "0.5")
    monkeypatch.setenv("MAX_PORTFOLIO_RISK_PCT", "2")
    settings = Settings()

    client = FakeTradingClient()
    manager = LivePortfolioManager(client, logging.getLogger("test"))

    top_new = [("AAPL", 0.9, Side.BUY, "r", 100.0, 99.0, 106.0)]
    orders_sent = manager.submit_bracket_orders(top_new, set(), 650.0, settings, open_exposure=500.0)

    assert orders_sent == 1
    assert len(client.submitted_orders) == 1
    assert client.submitted_orders[0].qty == 1


def test_allocation_f_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MAX_SINGLE_POSITION_PCT", raising=False)
    monkeypatch.delenv("COMMISSION_PER_ORDER_USD", raising=False)
    monkeypatch.delenv("COMMISSION_PER_SHARE_USD", raising=False)
    monkeypatch.delenv("MIN_GROSS_TO_COST_MULTIPLE", raising=False)

    s = Settings()
    assert s.max_single_position_pct == 0.90
    assert s.commission_per_order_usd == 0.35
    assert s.commission_per_share_usd == 0.0035
    assert s.min_gross_to_cost_multiple == 3.0

    monkeypatch.setenv("MAX_SINGLE_POSITION_PCT", "90")
    s2 = Settings()
    assert s2.max_single_position_pct == 0.90

    monkeypatch.setenv("MAX_SINGLE_POSITION_PCT", "0")
    with pytest.raises(ValueError, match="MAX_SINGLE_POSITION_PCT"):
        Settings()

    monkeypatch.delenv("MAX_SINGLE_POSITION_PCT", raising=False)
    monkeypatch.setenv("COMMISSION_PER_ORDER_USD", "-1")
    with pytest.raises(ValueError, match="COMMISSION_PER_ORDER_USD"):
        Settings()


def test_allocation_g_run_cycle_open_exposure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RISK_PER_TRADE_PCT", "0.5")
    monkeypatch.setenv("MAX_PORTFOLIO_RISK_PCT", "2")

    positions = [FakePosition("MSFT", 10.0, 50.0)]
    client = FakeTradingClient(positions=positions)

    recorded_kwargs = {}
    def mock_submit(self, top_new, locked_symbols, account_equity, settings, bar_time=None, processed_signals=None, locked_normalized=None, open_exposure=0.0):
        recorded_kwargs["open_exposure"] = open_exposure
        return 0

    monkeypatch.setattr(LivePortfolioManager, "submit_bracket_orders", mock_submit)

    bot = LiveMarketBot.__new__(LiveMarketBot)
    bot.logger = logging.getLogger("test")
    bot.settings = Settings()
    bot.trading_client = client
    bot.stock_client = type("StockClient", (), {})()
    bot.strategy = LiquiditySweepReversalStrategy(StrategyConfig())
    bot.scanner = type("Scanner", (), {"scan_universe": lambda *args: ([], []), "last_scan_attempts": 1, "last_scan_errors": 0})()
    bot.kill_switch = type("KillSwitch", (), {"tripped": False, "check_daily_loss": lambda *args: None})()
    bot.portfolio_manager = LivePortfolioManager(client, bot.logger)
    bot.global_universe = ["AAPL"]
    bot.latest_state = {}
    bot.processed_signals = set()

    import datetime
    bot.run_cycle(datetime.datetime.now(datetime.timezone.utc))

    assert recorded_kwargs.get("open_exposure") == 500.0
