import os

import pytest

from money_mintter.config import Settings


def test_settings_keep_existing_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "INITIAL_CAPITAL",
        "BASE_CURRENCY",
        "PROFIT_RESERVE_PCT",
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
    assert settings.profit_reserve_pct == 0.20
    assert settings.risk_per_trade_pct == 0.005
    assert settings.max_position_pct == 0.20
    assert settings.max_portfolio_risk_pct == 0.02


def test_settings_read_percent_values_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROFIT_RESERVE_PCT", "15")
    monkeypatch.setenv("RISK_PER_TRADE_PCT", "0.4")
    monkeypatch.setenv("MAX_POSITION_PCT", "25")
    monkeypatch.setenv("MAX_PORTFOLIO_RISK_PCT", "3")

    settings = Settings()

    assert settings.profit_reserve_pct == 0.15
    assert settings.risk_per_trade_pct == 0.004
    assert settings.max_position_pct == 0.25
    assert settings.max_portfolio_risk_pct == 0.03


def test_settings_reject_invalid_numeric_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INITIAL_CAPITAL", "not-a-number")
    with pytest.raises(ValueError, match="INITIAL_CAPITAL"):
        Settings()
