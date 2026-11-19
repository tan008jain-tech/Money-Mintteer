from __future__ import annotations

import logging
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from money_mintter.core.live_bot import LiveMarketBot
from money_mintter.strategy.candles import Candle
from money_mintter.strategy.ict.adapter import _create_verified_acquisition_result
from money_mintter.strategy.ict.integration import generate_production_ict_signals


def _acquisition(feed: str):
    timestamp = datetime(2026, 11, 18, 15, 0, tzinfo=timezone.utc)
    candle = Candle(
        symbol="AAPL",
        timestamp=timestamp,
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=1_000.0,
    )
    return _create_verified_acquisition_result(
        symbol="AAPL",
        b1_candles=(candle,),
        b5_candles=(candle,),
        feed=feed,
        b1_success=True,
        b5_success=True,
    )


def test_live_bot_rejects_non_iex_realtime_feed_before_client_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALPACA_REALTIME_FEED", "sip")
    monkeypatch.setattr(LiveMarketBot, "setup_logging", lambda self: logging.getLogger("feed_policy_test"))

    with pytest.raises(ValueError, match="requires IEX"):
        LiveMarketBot()


def test_production_ict_rejects_trusted_sip_acquisition_before_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter_called = False

    def adapter_spy(*args, **kwargs):
        nonlocal adapter_called
        adapter_called = True
        raise AssertionError("generic adapter must not be called for SIP production data")

    monkeypatch.setattr(
        "money_mintter.strategy.ict.integration.adapt_market_data",
        adapter_spy,
    )

    with pytest.raises(ValueError, match="Production ICT requires IEX"):
        generate_production_ict_signals(
            _acquisition("sip"),
            datetime(2026, 11, 18, 15, 10, tzinfo=timezone.utc),
        )

    assert adapter_called is False


def test_production_ict_keeps_generic_iex_adapter_path(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = SimpleNamespace(dr5={},)
    monkeypatch.setattr(
        "money_mintter.strategy.ict.integration.adapt_market_data",
        lambda *args, **kwargs: adapter,
    )

    with pytest.raises(ValueError, match="Current session date"):
        generate_production_ict_signals(
            _acquisition("iex"),
            datetime(2026, 11, 18, 15, 10, tzinfo=timezone.utc),
        )


def test_generic_adapter_contract_is_not_globally_restricted() -> None:
    from money_mintter.strategy.ict.adapter import adapt_market_data

    acquired = _acquisition("sip")
    adapted = adapt_market_data(
        acquired,
        datetime(2026, 11, 18, 15, 10, tzinfo=timezone.utc),
    )
    assert adapted.feed == "sip"
