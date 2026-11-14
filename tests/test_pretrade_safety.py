from __future__ import annotations

import logging
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from money_mintter.config import Settings
from money_mintter.core.live_bot import LivePortfolioManager
from money_mintter.domain import Side


class FakeQuoteClient:
    def __init__(self, bid: float, ask: float, age_seconds: float = 0.0) -> None:
        self.bid = bid
        self.ask = ask
        self.age_seconds = age_seconds

    def get_stock_latest_quote(self, request):
        from datetime import timedelta

        quote = SimpleNamespace(
            timestamp=datetime.now(timezone.utc) - timedelta(seconds=self.age_seconds),
            bid_price=self.bid,
            ask_price=self.ask,
        )
        return {request.symbol_or_symbols[0]: quote}


class FakeFlattenBroker:
    def __init__(self, original_order: SimpleNamespace | None = None) -> None:
        self.position_open = True
        self.original_order = original_order
        self.close_order = None
        self.bulk_close_responses = [
            SimpleNamespace(
                status=403,
                order_id=None,
                symbol="AAPL",
                body=SimpleNamespace(
                    code=40310000,
                    message="insufficient qty available for order",
                    available="0",
                    existing_qty="10",
                    held_for_orders="10",
                ),
            )
        ]
        self.get_orders_calls = 0
        self.get_order_by_id_calls: list[str] = []
        self.cancel_calls: list[str] = []
        self.close_calls: list[str] = []

    def get_account(self):
        return SimpleNamespace(
            cash="1000.00",
            equity="1000.00",
            buying_power="1000.00",
            last_equity="1000.00",
        )

    def get_all_positions(self):
        if not self.position_open:
            return []
        return [
            SimpleNamespace(
                symbol="AAPL",
                qty="10",
                avg_entry_price="100.00",
                current_price="100.00",
                unrealized_pl="0.00",
            )
        ]

    def close_all_positions(self, cancel_orders=True):
        assert cancel_orders is True
        return list(self.bulk_close_responses)

    def close_position(self, symbol):
        self.close_calls.append(symbol)
        self.close_order = SimpleNamespace(
            id="liquidation-order-1",
            symbol=symbol,
            status="accepted",
        )
        return self.close_order

    def cancel_order_by_id(self, order_id):
        self.cancel_calls.append(str(order_id))
        if self.original_order is not None and str(self.original_order.id) == str(order_id):
            self.original_order.status = "pending_cancel"

    def get_orders(self, request):
        self.get_orders_calls += 1

        if self.original_order is not None:
            if self.get_orders_calls == 1:
                return [self.original_order]
            if self.get_orders_calls == 2:
                self.original_order.status = "canceled"
                return []

        if self.close_order is not None:
            if self.close_order.status == "accepted":
                # The first post-submit snapshot sees the close order active.
                self._close_order_open_seen = getattr(self, "_close_order_open_seen", 0) + 1
                if self._close_order_open_seen == 1:
                    return [self.close_order]
                # The order fills after the position snapshot for this cycle.
                self.close_order.status = "filled"
                self.position_open = False
                return []

        return []

    def get_order_by_id(self, order_id):
        self.get_order_by_id_calls.append(str(order_id))
        if self.close_order is not None and str(order_id) == str(self.close_order.id):
            return self.close_order
        if self.original_order is not None and str(order_id) == str(self.original_order.id):
            return self.original_order
        raise KeyError(order_id)


def test_close_position_order_response_is_tracked_and_never_cancelled(caplog):
    client = FakeFlattenBroker()
    manager = LivePortfolioManager(client, logging.getLogger("flatten_order_response"))

    with caplog.at_level(logging.INFO):
        remaining = manager.flatten_all(
            sleep_fn=lambda _: None, max_wait_seconds=10, retry_delay=1
        )

    assert remaining == []
    assert client.close_calls == ["AAPL"]
    assert client.cancel_calls == []
    assert "individual close order tracked" in caplog.text
    assert "order_id=liquidation-order-1" in caplog.text
    assert "held_for_orders=10" in caplog.text
    assert "liquidation-order-1" in client.get_order_by_id_calls
    assert manager.last_flatten_errors == 1  # Initial per-position 403 preserved.


@pytest.mark.parametrize(
    ("initial_status", "expected_cancel_calls"),
    [
        ("pending_cancel", 0),
        ("new", 1),
    ],
)
def test_existing_order_cancel_is_requested_at_most_once_and_reconciled(
    initial_status: str,
    expected_cancel_calls: int,
):
    original_order = SimpleNamespace(
        id="protective-order-1",
        symbol="AAPL",
        status=initial_status,
    )
    client = FakeFlattenBroker(original_order=original_order)
    manager = LivePortfolioManager(client, logging.getLogger("flatten_cancel_race"))

    remaining = manager.flatten_all(
        sleep_fn=lambda _: None, max_wait_seconds=10, retry_delay=1
    )

    assert remaining == []
    assert client.cancel_calls == (
        ["protective-order-1"] if expected_cancel_calls else []
    )
    assert client.close_calls == ["AAPL"]
    assert "protective-order-1" in client.get_order_by_id_calls
    assert client.cancel_calls.count("protective-order-1") <= 1


def test_missing_symbol_open_order_is_reconciled_by_id_before_flattening():
    class MissingSymbolBroker(FakeFlattenBroker):
        def __init__(self):
            super().__init__()
            self.unknown_order = SimpleNamespace(
                id="unknown-symbol-order",
                symbol=None,
                status="new",
            )
            self._unknown_cancelled = False

        def get_orders(self, request):
            if not self._unknown_cancelled:
                return [self.unknown_order]
            return super().get_orders(request)

        def cancel_order_by_id(self, order_id):
            if str(order_id) == self.unknown_order.id:
                self.cancel_calls.append(str(order_id))
                self.unknown_order.status = "canceled"
                self._unknown_cancelled = True
                return
            super().cancel_order_by_id(order_id)

        def get_order_by_id(self, order_id):
            if str(order_id) == self.unknown_order.id:
                self.get_order_by_id_calls.append(str(order_id))
                return self.unknown_order
            return super().get_order_by_id(order_id)

    client = MissingSymbolBroker()
    manager = LivePortfolioManager(client, logging.getLogger("flatten_unknown_order_symbol"))

    remaining = manager.flatten_all(
        sleep_fn=lambda _: None, max_wait_seconds=10, retry_delay=1
    )

    assert remaining == []
    assert client.cancel_calls.count("unknown-symbol-order") == 1
    assert client.get_order_by_id_calls.count("unknown-symbol-order") == 1
    assert client.close_calls == ["AAPL"]


def test_filled_unidentified_order_fails_closed():
    class FilledUnidentifiedBroker(FakeFlattenBroker):
        def __init__(self):
            super().__init__()
            self.unknown_order = SimpleNamespace(
                id="unknown-filled-order",
                symbol=None,
                status="filled",
            )

        def get_orders(self, request):
            return [self.unknown_order]

        def get_order_by_id(self, order_id):
            self.get_order_by_id_calls.append(str(order_id))
            return self.unknown_order

    client = FilledUnidentifiedBroker()
    manager = LivePortfolioManager(client, logging.getLogger("flatten_filled_unknown_order"))

    remaining = manager.flatten_all(sleep_fn=lambda _: None, max_wait_seconds=2)

    assert remaining == ["UNKNOWN"]
    assert "unknown-filled-order" in client.get_order_by_id_calls
    assert client.close_calls == []


def test_quote_spread_gate_rejects_wide_iex_reference_quote():
    manager = LivePortfolioManager(
        object(),
        logging.getLogger("wide_quote_spread"),
        stock_client=FakeQuoteClient(bid=99.50, ask=102.00),
    )

    # Signal is aligned with the buy-side ask, so only the independent spread
    # guard should reject this quote.
    result = manager._get_executable_price(
        "AAPL",
        Side.BUY,
        102.00,
        Settings(),
    )

    assert result is None


def test_quote_spread_gate_accepts_quote_at_configured_boundary():
    manager = LivePortfolioManager(
        object(),
        logging.getLogger("quote_spread_boundary"),
        stock_client=FakeQuoteClient(bid=99.50, ask=100.50),
    )

    result = manager._get_executable_price(
        "AAPL",
        Side.BUY,
        100.50,
        Settings(),
    )

    assert result == pytest.approx(100.50)


def test_quote_spread_limit_is_configurable_and_validated(monkeypatch):
    monkeypatch.setenv("MAX_QUOTE_SPREAD_PCT", "2")
    assert Settings().max_quote_spread_pct == pytest.approx(0.02)

    with pytest.raises(ValueError, match="MAX_QUOTE_SPREAD_PCT"):
        Settings(max_quote_spread_pct=0.0)

    with pytest.raises(ValueError, match="MAX_QUOTE_SPREAD_PCT"):
        Settings(max_quote_spread_pct=1.01)
