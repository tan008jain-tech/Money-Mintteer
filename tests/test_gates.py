from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
import pytest

from money_mintter.config import Settings
from money_mintter.core.live_bot import LiveMarketBot, LivePortfolioManager
from money_mintter.domain import Side


class DummyQuote:
    def __init__(self, bid_price: float, ask_price: float, timestamp: datetime) -> None:
        self.bid_price = bid_price
        self.ask_price = ask_price
        self.timestamp = timestamp


class MockStockClient:
    def __init__(self, quotes: dict[str, Any] | None = None) -> None:
        self.quotes = quotes or {}

    def get_stock_latest_quote(self, request: Any) -> dict[str, Any]:
        res = {}
        syms = request.symbol_or_symbols if hasattr(request, 'symbol_or_symbols') else []
        for s in syms:
            q = self.quotes.get(s)
            if q is not None:
                res[s] = q
            else:
                p = 100.0
                res[s] = DummyQuote(p - 0.05, p + 0.05, datetime.now(timezone.utc))
        return res


class FakeTradingClient:
    def __init__(self) -> None:
        self.submitted_orders: list[Any] = []

    def submit_order(self, order_req: Any) -> Any:
        self.submitted_orders.append(order_req)
        class DummyOrder:
            order_class = "bracket"
            legs = [object(), object()]
            id = "test-id-1"
        return DummyOrder()


def test_gates_disabled_default_placement(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RISK_PER_TRADE_PCT", "0.5")
    monkeypatch.setenv("MAX_PORTFOLIO_RISK_PCT", "2")
    settings = Settings()
    assert settings.min_reward_risk == 0.0
    assert settings.min_target_pct == 0.0

    client = FakeTradingClient()
    manager = LivePortfolioManager(client, logging.getLogger("test"), stock_client=MockStockClient())
    candidate = [("AAPL", 0.9, Side.BUY, "test", 100.0, 98.0, 103.0, datetime.now(timezone.utc))]
    sent = manager.submit_bracket_orders(candidate, set(), 5000.0, settings)
    assert sent == 1
    assert len(client.submitted_orders) == 1
    assert manager.last_submit_failures == 0


def test_min_reward_risk_gate_rejects(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RISK_PER_TRADE_PCT", "0.5")
    monkeypatch.setenv("MAX_PORTFOLIO_RISK_PCT", "2")
    monkeypatch.setenv("MIN_REWARD_RISK", "1.0")
    settings = Settings()
    assert settings.min_reward_risk == 1.0

    client = FakeTradingClient()
    manager = LivePortfolioManager(client, logging.getLogger("test"), stock_client=MockStockClient())
    candidate = [("AAPL", 0.9, Side.BUY, "test", 100.0, 98.0, 101.5, datetime.now(timezone.utc))]
    sent = manager.submit_bracket_orders(candidate, set(), 5000.0, settings)
    assert sent == 0
    assert len(client.submitted_orders) == 0
    assert manager.last_submit_failures == 0


def test_min_target_pct_gate_skip_and_place(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RISK_PER_TRADE_PCT", "0.5")
    monkeypatch.setenv("MAX_PORTFOLIO_RISK_PCT", "2")
    monkeypatch.setenv("MIN_TARGET_PCT", "1.0")
    settings = Settings()

    client1 = FakeTradingClient()
    manager1 = LivePortfolioManager(client1, logging.getLogger("test"), stock_client=MockStockClient())
    candidate = [("AAPL", 0.9, Side.BUY, "test", 100.0, 99.0, 100.5, datetime.now(timezone.utc))]
    sent1 = manager1.submit_bracket_orders(candidate, set(), 5000.0, settings)
    assert sent1 == 0
    assert len(client1.submitted_orders) == 0

    monkeypatch.setenv("MIN_TARGET_PCT", "0.4")
    settings_loose = Settings()
    client2 = FakeTradingClient()
    manager2 = LivePortfolioManager(client2, logging.getLogger("test"), stock_client=MockStockClient())
    candidate_loose = [("AAPL", 0.9, Side.BUY, "test", 100.0, 99.0, 101.5, datetime.now(timezone.utc))]
    sent2 = manager2.submit_bracket_orders(candidate_loose, set(), 5000.0, settings_loose)
    assert sent2 == 1
    assert len(client2.submitted_orders) == 1


def test_stop_equal_to_entry_no_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RISK_PER_TRADE_PCT", "0.5")
    monkeypatch.setenv("MAX_PORTFOLIO_RISK_PCT", "2")
    settings = Settings()

    client = FakeTradingClient()
    manager = LivePortfolioManager(client, logging.getLogger("test"), stock_client=MockStockClient(quotes={"AAPL": DummyQuote(100.0, 100.0, datetime.now(timezone.utc))}))
    candidate = [("AAPL", 0.9, Side.BUY, "test", 100.0, 100.0, 102.0, datetime.now(timezone.utc))]
    sent = manager.submit_bracket_orders(candidate, set(), 5000.0, settings)
    assert sent == 0
    assert len(client.submitted_orders) == 0
    assert manager.last_submit_failures == 0


def test_settings_validation_and_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MIN_REWARD_RISK", raising=False)
    monkeypatch.delenv("MIN_TARGET_PCT", raising=False)
    s = Settings()
    assert s.min_reward_risk == 0.0
    assert s.min_target_pct == 0.0

    monkeypatch.setenv("MIN_REWARD_RISK", "-1.0")
    with pytest.raises(ValueError, match="min_reward_risk"):
        Settings()

    monkeypatch.delenv("MIN_REWARD_RISK", raising=False)
    monkeypatch.setenv("MIN_TARGET_PCT", "1.5")
    s2 = Settings()
    assert s2.min_target_pct == 1.5


def test_two_candidates_first_rejected_rr_second_gets_full_allocation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RISK_PER_TRADE_PCT", "0.5")
    monkeypatch.setenv("MAX_PORTFOLIO_RISK_PCT", "2")
    monkeypatch.setenv("MIN_REWARD_RISK", "1.0")
    settings = Settings()

    client_single = FakeTradingClient()
    manager_single = LivePortfolioManager(client_single, logging.getLogger("test"), stock_client=MockStockClient())
    cand_second = [("MSFT", 0.9, Side.BUY, "test", 100.0, 98.0, 103.0, datetime.now(timezone.utc))]
    manager_single.submit_bracket_orders(cand_second, set(), 5000.0, settings)
    qty_single = client_single.submitted_orders[0].qty

    client_two = FakeTradingClient()
    manager_two = LivePortfolioManager(client_two, logging.getLogger("test"), stock_client=MockStockClient())
    cands_two = [
        ("AAPL", 0.9, Side.BUY, "test", 100.0, 98.0, 101.0, datetime.now(timezone.utc)),
        ("MSFT", 0.9, Side.BUY, "test", 100.0, 98.0, 103.0, datetime.now(timezone.utc)),
    ]
    manager_two.submit_bracket_orders(cands_two, set(), 5000.0, settings)
    assert len(client_two.submitted_orders) == 1
    assert client_two.submitted_orders[0].symbol == "MSFT"
    assert client_two.submitted_orders[0].qty == qty_single


def test_caplog_candidate_and_config_gates(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setenv("MIN_REWARD_RISK", "2.0")
    monkeypatch.setenv("MIN_TARGET_PCT", "1.5")
    settings = Settings()

    client = FakeTradingClient()
    manager = LivePortfolioManager(client, logging.getLogger("test"), stock_client=MockStockClient())
    candidate = [("AAPL", 0.9, Side.BUY, "test", 100.0, 98.0, 103.0, datetime.now(timezone.utc))]
    with caplog.at_level(logging.INFO):
        manager.submit_bracket_orders(candidate, set(), 5000.0, settings)

    log_text = caplog.text
    assert "rr=" in log_text
    assert "target_pct=" in log_text

    monkeypatch.setenv("APCA_API_KEY_ID", "mock")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "mock")
    with caplog.at_level(logging.INFO):
        try:
            LiveMarketBot()
        except SystemExit:
            pass
    assert "[Config] gates:" in caplog.text
