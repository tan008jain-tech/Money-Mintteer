from __future__ import annotations

import logging
import os
import pytest
from unittest.mock import MagicMock
from datetime import datetime, timezone

from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest, TakeProfitRequest, StopLossRequest

from money_mintter.config import Settings
from money_mintter.core.killswitch import KillSwitch
from money_mintter.core.live_bot import LivePortfolioManager, LiveMarketBot, apply_default_timeout
from money_mintter.domain import Side


class FakeOrder:
    def __init__(self, order_id="ord_123", symbol="AAPL", order_class=OrderClass.BRACKET, legs=None):
        self.id = order_id
        self.symbol = symbol
        self.order_class = order_class
        self.legs = legs if legs is not None else [MagicMock(), MagicMock()]


class FakeTradingClient:
    def __init__(self, order_to_return=None):
        self.submitted_requests = []
        self.cancelled_ids = []
        self.closed_symbols = []
        self.order_to_return = order_to_return or FakeOrder()

    def submit_order(self, order_req):
        self.submitted_requests.append(order_req)
        return self.order_to_return

    def cancel_order_by_id(self, order_id):
        self.cancelled_ids.append(order_id)

    def close_position(self, symbol):
        self.closed_symbols.append(symbol)

    def get_account(self):
        acc = MagicMock()
        acc.cash = "100000.0"
        acc.equity = "100000.0"
        acc.buying_power = "200000.0"
        acc.last_equity = "100000.0"
        return acc

    def get_all_positions(self):
        return []

    def get_orders(self, req):
        return []

    def get_clock(self):
        clock = MagicMock()
        clock.is_open = True
        clock.next_close = datetime.now(timezone.utc)
        return clock


class FakeSession:
    def __init__(self):
        self.last_kwargs = None

    def request(self, method, url, **kwargs):
        self.last_kwargs = kwargs
        return MagicMock(status_code=200)


class FakeClientWithSession:
    def __init__(self):
        self._session = FakeSession()


class FakeClientWithoutSession:
    pass


def test_submit_bracket_orders_request_structure():
    # a. MarketOrderRequest passed to submit_order has order_class BRACKET and take_profit & stop_loss for BUY and SELL.
    client = FakeTradingClient()
    logger = logging.getLogger("test_resilience")
    pm = LivePortfolioManager(client, logger)
    settings = Settings(min_candidate_score=0.5, risk_per_trade_pct=0.01)

    # Test BUY candidate
    top_new_buy = [("AAPL", 0.8, Side.BUY, "test", 150.0, 147.0, 157.5)]
    pm.submit_bracket_orders(top_new_buy, set(), 100000.0, settings)
    assert len(client.submitted_requests) == 1
    req_buy = client.submitted_requests[0]
    assert req_buy.order_class == OrderClass.BRACKET
    assert req_buy.take_profit is not None
    assert req_buy.stop_loss is not None

    # Test SELL candidate
    client.submitted_requests.clear()
    top_new_sell = [("TSLA", 0.8, Side.SELL, "test", 200.0, 204.0, 190.0)]
    pm.submit_bracket_orders(top_new_sell, set(), 100000.0, settings)
    assert len(client.submitted_requests) == 1
    req_sell = client.submitted_requests[0]
    assert req_sell.order_class == OrderClass.BRACKET
    assert req_sell.take_profit is not None
    assert req_sell.stop_loss is not None


def test_order_confirmed_bracket(caplog):
    # b. Fake order with order_class "bracket" and 2 legs: ORDER CONFIRMED logged, orders_sent 1, no close_position call.
    fake_ord = FakeOrder(order_class="bracket", legs=[MagicMock(), MagicMock()])
    client = FakeTradingClient(order_to_return=fake_ord)
    logger = logging.getLogger("test_resilience")
    logger.setLevel(logging.INFO)
    pm = LivePortfolioManager(client, logger)
    settings = Settings(min_candidate_score=0.5, risk_per_trade_pct=0.01)

    with caplog.at_level(logging.INFO):
        sent = pm.submit_bracket_orders([("AAPL", 0.8, Side.BUY, "test", 150.0, 147.0, 157.5)], set(), 100000.0, settings)

    assert sent == 1
    assert "ORDER CONFIRMED" in caplog.text
    assert len(client.closed_symbols) == 0


def test_unprotected_order_handling(caplog):
    # c. Fake order with order_class "simple": cancel_order_by_id and close_position called once, orders_sent 0, last_submit_failures 1, CRITICAL logged, on_unprotected called once.
    fake_ord = FakeOrder(order_class="simple", legs=[])
    client = FakeTradingClient(order_to_return=fake_ord)
    logger = logging.getLogger("test_resilience")
    logger.setLevel(logging.CRITICAL)
    pm = LivePortfolioManager(client, logger)
    
    cb_calls = []
    pm.on_unprotected = lambda reason: cb_calls.append(reason)
    settings = Settings(min_candidate_score=0.5, risk_per_trade_pct=0.01)

    with caplog.at_level(logging.CRITICAL):
        sent = pm.submit_bracket_orders([("AAPL", 0.8, Side.BUY, "test", 150.0, 147.0, 157.5)], set(), 100000.0, settings)

    assert sent == 0
    assert pm.last_submit_failures == 1
    assert len(client.cancelled_ids) == 1
    assert len(client.closed_symbols) == 1
    assert "UNPROTECTED ORDER" in caplog.text
    assert len(cb_calls) == 1


def test_order_missing_attributes(caplog):
    # d. Order object without order_class: WARNING logged, orders_sent 1.
    class PlainOrder:
        def __init__(self):
            self.id = "p_1"

    client = FakeTradingClient(order_to_return=PlainOrder())
    logger = logging.getLogger("test_resilience")
    logger.setLevel(logging.WARNING)
    pm = LivePortfolioManager(client, logger)
    settings = Settings(min_candidate_score=0.5, risk_per_trade_pct=0.01)

    with caplog.at_level(logging.WARNING):
        sent = pm.submit_bracket_orders([("AAPL", 0.8, Side.BUY, "test", 150.0, 147.0, 157.5)], set(), 100000.0, settings)

    assert sent == 1
    assert "cannot verify order class" in caplog.text


def test_kill_switch_trip():
    # e. KillSwitch.trip: tripped, reason stored, stays tripped after record_success.
    ks = KillSwitch()
    assert not ks.tripped
    ks.trip("manual alert")
    assert ks.tripped
    assert ks.reason == "manual alert"
    ks.record_success()
    assert ks.tripped
    assert ks.reason == "manual alert"


def test_bot_wiring_unprotected_halts(monkeypatch):
    # f. Bot wiring: after an unprotected order, bot.kill_switch.tripped is True and the next run_cycle returns halted.
    monkeypatch.setenv("APCA_API_KEY_ID", "mock")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "mock")
    bot = LiveMarketBot()
    # Force an unprotected order callback
    bot.portfolio_manager.on_unprotected("test unprotected")
    assert bot.kill_switch.tripped
    res = bot.run_cycle(datetime.now(timezone.utc))
    assert res.get("halted") is True


def test_apply_default_timeout():
    # g. apply_default_timeout on a fake client with a recording _session.request: timeout (5, 20) added; explicit timeout not overridden; client without _session raises RuntimeError.
    client = FakeClientWithSession()
    apply_default_timeout(client, 5.0, 20.0)
    assert getattr(client._session.request, "_mm_timeout_applied", True)

    client._session.request("GET", "https://api.example.com")
    assert client._session.last_kwargs.get("timeout") == (5.0, 20.0)

    # Explicit timeout not overridden
    client._session.request("GET", "https://api.example.com", timeout=(2.0, 10.0))
    assert client._session.last_kwargs.get("timeout") == (2.0, 10.0)

    # Client without session raises RuntimeError
    bad_client = FakeClientWithoutSession()
    with pytest.raises(RuntimeError, match="cannot apply HTTP timeout"):
        apply_default_timeout(bad_client, 5.0, 20.0)


def test_settings_timeouts():
    # h. Settings: ALPACA_HTTP_READ_TIMEOUT=0 raises ValueError; defaults are 5 and 20.
    settings_default = Settings()
    assert settings_default.http_connect_timeout_seconds == 5.0
    assert settings_default.http_read_timeout_seconds == 20.0

    os.environ["ALPACA_HTTP_READ_TIMEOUT"] = "0"
    try:
        with pytest.raises(ValueError, match="http_read_timeout_seconds"):
            Settings()
    finally:
        os.environ.pop("ALPACA_HTTP_READ_TIMEOUT", None)


def test_live_market_bot_timeout_applied(monkeypatch):
    # i. A LiveMarketBot() built normally has the wrapper on both clients (check _mm_timeout_applied).
    monkeypatch.setenv("APCA_API_KEY_ID", "mock")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "mock")
    bot = LiveMarketBot()
    assert getattr(bot.trading_client._session.request, "_mm_timeout_applied", False) is True
    assert getattr(bot.stock_client._session.request, "_mm_timeout_applied", False) is True
