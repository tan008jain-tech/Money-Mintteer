from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from money_mintter.config import Settings
from money_mintter.core.live_bot import LivePortfolioManager
from money_mintter.domain import Side
from money_mintter.strategy.ict.integration import ICTExecutionInstruction, ICTDeduplicationTracker


class QuoteClient:
    def __init__(self) -> None:
        self.calls = 0

    def get_stock_latest_quote(self, request):
        self.calls += 1
        now = datetime.now(timezone.utc)
        return {
            symbol: SimpleNamespace(
                timestamp=now,
                bid_price=99.99,
                ask_price=100.01,
            )
            for symbol in request.symbol_or_symbols
        }


class Broker:
    def __init__(self) -> None:
        self.submissions = 0

    def submit_order(self, request):
        self.submissions += 1
        return SimpleNamespace(
            id="parent-order",
            client_order_id=getattr(request, "client_order_id", None),
            order_class="bracket",
            legs=[SimpleNamespace(id="tp"), SimpleNamespace(id="sl")],
        )


def _instruction(timestamp: datetime) -> ICTExecutionInstruction:
    return ICTExecutionInstruction(
        symbol="AAPL",
        side=Side.BUY,
        entry_price=100.0,
        stop_price=98.0,
        target_price=105.0,
        risk_pts=2.0,
        timestamp=timestamp,
        timestamp_ns=1_000_000,
        zone_type="disp_fvg",
        score=1,
        zone_top=101.0,
        zone_bot=99.0,
        reason="signal_freshness_test",
    )


def _manager():
    quotes = QuoteClient()
    broker = Broker()
    manager = LivePortfolioManager(
        broker,
        logging.getLogger("ict_signal_freshness"),
        stock_client=quotes,
    )
    return manager, quotes, broker


@pytest.mark.parametrize("age_seconds", [0.0, 120.0, 299.9])
def test_fresh_ict_instruction_can_reach_broker(age_seconds: float, monkeypatch) -> None:
    monkeypatch.setenv("MAX_PORTFOLIO_RISK_PCT", "3")
    now = datetime.now(timezone.utc)
    manager, quotes, broker = _manager()

    sent = manager.submit_ict_instructions(
        [_instruction(now - timedelta(seconds=age_seconds))],
        set(),
        100_000.0,
        Settings(),
        execution_time_utc=now,
    )

    assert sent == 1
    assert quotes.calls == 1
    assert broker.submissions == 1


@pytest.mark.parametrize("age_seconds", [300.0, 301.0, 900.0])
def test_expired_ict_instruction_is_rejected_before_quote_or_submit(
    age_seconds: float,
    monkeypatch,
) -> None:
    now = datetime.now(timezone.utc)
    manager, quotes, broker = _manager()

    sent = manager.submit_ict_instructions(
        [_instruction(now - timedelta(seconds=age_seconds))],
        set(),
        100_000.0,
        Settings(),
        execution_time_utc=now,
    )

    assert sent == 0
    assert quotes.calls == 0
    assert broker.submissions == 0


def test_future_ict_instruction_is_rejected_before_quote_or_submit() -> None:
    now = datetime.now(timezone.utc)
    manager, quotes, broker = _manager()

    sent = manager.submit_ict_instructions(
        [_instruction(now + timedelta(seconds=1))],
        set(),
        100_000.0,
        Settings(),
        execution_time_utc=now,
    )

    assert sent == 0
    assert quotes.calls == 0
    assert broker.submissions == 0


def test_naive_ict_instruction_timestamp_is_rejected_before_quote_or_submit() -> None:
    now = datetime.now(timezone.utc)
    manager, quotes, broker = _manager()

    sent = manager.submit_ict_instructions(
        [_instruction(now.replace(tzinfo=None))],
        set(),
        100_000.0,
        Settings(),
        execution_time_utc=now,
    )

    assert sent == 0
    assert quotes.calls == 0
    assert broker.submissions == 0


def test_naive_execution_time_fails_closed() -> None:
    manager, quotes, broker = _manager()

    sent = manager.submit_ict_instructions(
        [_instruction(datetime.now(timezone.utc))],
        set(),
        100_000.0,
        Settings(),
        execution_time_utc=datetime.now(),
    )

    assert sent == 0
    assert quotes.calls == 0
    assert broker.submissions == 0


def test_quote_rejection_does_not_mark_fresh_signal_executed_or_pending() -> None:
    now = datetime.now(timezone.utc)
    manager, quotes, broker = _manager()
    tracker = ICTDeduplicationTracker()

    # The quote is valid but too far from the signal's reference price.
    manager.stock_client.get_stock_latest_quote = lambda request: {
        "AAPL": SimpleNamespace(
            timestamp=datetime.now(timezone.utc),
            bid_price=120.0,
            ask_price=120.01,
        )
    }

    sent = manager.submit_ict_instructions(
        [_instruction(now)],
        set(),
        100_000.0,
        Settings(),
        dedup_tracker=tracker,
        execution_time_utc=now,
    )

    assert sent == 0
    assert broker.submissions == 0
    assert not tracker.is_executed("AAPL", "bull", "disp_fvg", 101.0, 99.0)
    assert not tracker.is_pending("AAPL", "bull", "disp_fvg", 101.0, 99.0)
