import logging
import os
from typing import Any

import pytest

from money_mintter.config import Settings
from money_mintter.core.live_bot import LivePortfolioManager
from money_mintter.domain import Side


def test_settings_keep_existing_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "INITIAL_CAPITAL",
        "BASE_CURRENCY",
        "MAX_DEEP_CANDIDATES",
        "MIN_CANDIDATE_SCORE",
        "RISK_PER_TRADE_PCT",
        "MAX_POSITION_PCT",
        "MAX_PORTFOLIO_RISK_PCT",
        "PAPER_SLIPPAGE_BPS",
        "PAPER_COMMISSION_BPS",
    ):
        monkeypatch.delenv(key, raising=False)

    settings = Settings()
    assert settings.initial_capital == 10_000.0
    assert settings.risk_per_trade_pct == 0.005
    assert settings.max_position_pct == 0.20
    assert settings.max_portfolio_risk_pct == 0.02


def test_settings_read_percent_values_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RISK_PER_TRADE_PCT", "0.4")
    monkeypatch.setenv("MAX_POSITION_PCT", "25")
    monkeypatch.setenv("MAX_PORTFOLIO_RISK_PCT", "3")

    settings = Settings()

    assert settings.risk_per_trade_pct == 0.004
    assert settings.max_position_pct == 0.25
    assert settings.max_portfolio_risk_pct == 0.03


def test_settings_reject_invalid_numeric_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INITIAL_CAPITAL", "not-a-number")
    with pytest.raises(ValueError, match="INITIAL_CAPITAL"):
        Settings()


def test_max_position_pct_20_conversion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_POSITION_PCT", "20")
    settings = Settings()
    assert settings.max_position_pct == 0.20


def test_live_portfolio_manager_bracket_order_allocation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_POSITION_PCT", "20")
    settings = Settings()

    class FakeTradingClient:
        def __init__(self) -> None:
            self.submitted_orders: list[Any] = []

        def submit_order(self, order_req: Any) -> Any:
            self.submitted_orders.append(order_req)
            return object()

    fake_client = FakeTradingClient()
    manager = LivePortfolioManager(fake_client, logging.getLogger("test"))

    top_new = [("TEST", 0.9, Side.BUY, "r", 100.0, 98.5, 103.0)]
    manager.submit_bracket_orders(top_new, set(), 100_000.0, settings)

    assert len(fake_client.submitted_orders) == 1
    assert fake_client.submitted_orders[0].qty == 200.0
