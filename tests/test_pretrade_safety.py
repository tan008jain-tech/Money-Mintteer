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


def _owned_journal(tmp_path, *, symbol="AAPL", side="buy", quantity="5"):
    from money_mintter.core.order_journal import OrderLifecycleJournal

    journal = OrderLifecycleJournal(tmp_path / "order_lifecycle.jsonl")
    journal.append(
        "entry_order_submitted",
        client_order_id="mm-ict-unit-entry",
        symbol=symbol,
        parent_order={
            "order_id": "mm-entry-order",
            "client_order_id": "mm-ict-unit-entry",
            "symbol": symbol,
            "side": side,
            "order_role": "entry",
            "status": "filled",
            "qty": quantity,
            "filled_qty": quantity,
        },
        child_orders=[
            {
                "order_id": "mm-take-profit",
                "symbol": symbol,
                "side": "sell" if side == "buy" else "buy",
                "order_role": "take_profit",
                "status": "new",
                "qty": quantity,
                "filled_qty": "0",
            },
            {
                "order_id": "mm-stop-loss",
                "symbol": symbol,
                "side": "sell" if side == "buy" else "buy",
                "order_role": "stop_loss",
                "status": "new",
                "qty": quantity,
                "filled_qty": "0",
            },
        ],
    )
    journal.append(
        "fill",
        fill_id="mm-entry-fill",
        order_id="mm-entry-order",
        symbol=symbol,
        side=side,
        qty=quantity,
        price="100",
        transaction_time=datetime.now(timezone.utc).isoformat(),
    )
    return journal


class FakeOwnedBroker:
    ACTIVE_STATUSES = {
        "new", "accepted", "pending_new", "partially_filled",
        "pending_cancel", "pending_replace", "accepted_for_bidding", "stopped",
    }

    def __init__(
        self,
        *,
        actual_quantity="25",
        include_open_orders=True,
        fail_before_close=0,
        fail_close_responses=0,
        pending_close=False,
        raise_after_close=False,
    ):
        self.actual_quantity = str(actual_quantity)
        self.close_all_calls = []
        self.cancel_calls = []
        self.fail_before_close = fail_before_close
        self.fail_close_responses = fail_close_responses
        self.pending_close = pending_close
        self.raise_after_close = raise_after_close
        child_status = "new" if include_open_orders else "canceled"
        manual_status = "new" if include_open_orders else "canceled"
        self.manual_gtc_order = SimpleNamespace(
            id="manual-gtc-order",
            symbol="MSFT",
            side="buy",
            status=manual_status,
            time_in_force="gtc",
            client_order_id="manual-long-term-order",
        )
        self.orders = {
            "mm-entry-order": SimpleNamespace(
                id="mm-entry-order", symbol="AAPL", status="filled",
                filled_qty="5", client_order_id="mm-ict-unit-entry",
            ),
            "mm-take-profit": SimpleNamespace(
                id="mm-take-profit", symbol="AAPL", status=child_status, filled_qty="0",
            ),
            "mm-stop-loss": SimpleNamespace(
                id="mm-stop-loss", symbol="AAPL", status=child_status, filled_qty="0",
            ),
            "manual-gtc-order": self.manual_gtc_order,
        }
        self.close_order = None
        self.close_fill = None

    def get_account(self):
        return SimpleNamespace(
            cash="1000.00", equity="3000.00", buying_power="2000.00", last_equity="3000.00"
        )

    def get_all_positions(self):
        if float(self.actual_quantity) == 0:
            return []
        return [SimpleNamespace(
            symbol="AAPL",
            qty=self.actual_quantity,
            avg_entry_price="100.00",
            current_price="100.00",
            unrealized_pl="0.00",
        )]

    def get_order_by_id(self, order_id):
        return self.orders[str(order_id)]

    def get_orders(self, request):
        return [
            order for order in self.orders.values()
            if str(getattr(order, "status", "")).lower() in self.ACTIVE_STATUSES
        ]

    def cancel_order_by_id(self, order_id):
        order_id = str(order_id)
        self.cancel_calls.append(order_id)
        self.orders[order_id].status = "canceled"

    def _complete_close(self, quantity, close_side):
        self.actual_quantity = "0"
        if self.close_order is not None:
            self.close_order.status = "filled"
            self.close_order.filled_qty = quantity
        self.close_fill = {
            "id": "mm-close-fill",
            "order_id": "mm-close-order",
            "symbol": "AAPL",
            "side": close_side,
            "qty": quantity,
            "price": "101",
            "transaction_time": datetime.now(timezone.utc).isoformat(),
        }

    def complete_pending_close(self):
        if self.close_order is not None:
            quantity = self.close_order.qty
            close_side = "sell" if float(self.actual_quantity) > 0 else "buy"
            self._complete_close(quantity, close_side)

    def close_all_positions(self, cancel_orders=None):
        self.close_all_calls.append(cancel_orders)
        if cancel_orders:
            for order in self.orders.values():
                if str(getattr(order, "status", "")).lower() in self.ACTIVE_STATUSES:
                    order.status = "canceled"

        if self.fail_before_close:
            self.fail_before_close -= 1
            raise TimeoutError("temporary close-all request timeout")

        if self.fail_close_responses:
            self.fail_close_responses -= 1
            return [SimpleNamespace(
                status=403,
                symbol="AAPL",
                order_id=None,
                body=SimpleNamespace(message="position temporarily unavailable"),
            )]

        if float(self.actual_quantity) == 0:
            results = []
        else:
            quantity = str(abs(float(self.actual_quantity)))
            close_side = "sell" if float(self.actual_quantity) > 0 else "buy"
            self.close_order = SimpleNamespace(
                id="mm-close-order",
                symbol="AAPL",
                status="new" if self.pending_close else "filled",
                qty=quantity,
                filled_qty="0" if self.pending_close else quantity,
                side=close_side,
                time_in_force="day",
            )
            self.orders["mm-close-order"] = self.close_order
            if not self.pending_close:
                self._complete_close(quantity, close_side)
            results = [SimpleNamespace(
                order_id="mm-close-order",
                status=200,
                symbol="AAPL",
                body=self.close_order,
            )]

        if self.raise_after_close:
            self.raise_after_close = False
            raise TimeoutError("response timed out after Alpaca accepted the close")
        return results

    def get(self, path, params):
        assert path == "/account/activities/FILL"
        if params.get("order_id") == "mm-close-order" and self.close_fill:
            return [self.close_fill]
        return []


def test_flatten_uses_broker_quantity_and_cancels_all_open_orders(tmp_path):
    journal = _owned_journal(tmp_path, quantity="5")
    broker = FakeOwnedBroker(actual_quantity="25")
    manager = LivePortfolioManager(
        broker, logging.getLogger("broker_authoritative_flatten"), order_journal=journal
    )

    remaining = manager.flatten_all(
        sleep_fn=lambda _: None, max_wait_seconds=10, retry_delay=1
    )

    assert remaining == []
    assert broker.close_all_calls == [True]
    assert float(broker.actual_quantity) == 0.0
    assert broker.manual_gtc_order.status == "canceled"
    assert broker.orders["mm-take-profit"].status == "canceled"
    assert broker.orders["mm-stop-loss"].status == "canceled"
    close_events = [
        event for event in journal.read_events()
        if event["event_type"] == "flatten_close_order_submitted"
    ]
    assert len(close_events) == 1
    assert close_events[0]["order_id"] == "mm-close-order"
    assert close_events[0]["symbol"] == "AAPL"


def test_flatten_retries_after_uncertain_request_only_after_broker_check():
    broker = FakeOwnedBroker(actual_quantity="25", include_open_orders=False, fail_before_close=1)
    manager = LivePortfolioManager(broker, logging.getLogger("flatten_retry_after_check"))

    remaining = manager.flatten_all(
        sleep_fn=lambda _: None, max_wait_seconds=10, retry_delay=1
    )

    assert remaining == []
    assert broker.close_all_calls == [True, True]
    assert float(broker.actual_quantity) == 0.0


def test_ambiguous_timeout_after_close_submission_does_not_duplicate():
    broker = FakeOwnedBroker(actual_quantity="25", raise_after_close=True)
    manager = LivePortfolioManager(broker, logging.getLogger("flatten_ambiguous_timeout"))

    remaining = manager.flatten_all(
        sleep_fn=lambda _: None, max_wait_seconds=10, retry_delay=1
    )

    assert remaining == []
    assert broker.close_all_calls == [True]
    assert float(broker.actual_quantity) == 0.0


def test_broker_quantity_mismatch_does_not_block_full_account_close(tmp_path):
    journal = _owned_journal(tmp_path, quantity="5")
    broker = FakeOwnedBroker(actual_quantity="3")
    manager = LivePortfolioManager(
        broker, logging.getLogger("broker_quantity_mismatch"), order_journal=journal
    )

    remaining = manager.flatten_all(
        sleep_fn=lambda _: None, max_wait_seconds=10, retry_delay=1
    )

    assert remaining == []
    assert broker.close_all_calls == [True]
    assert float(broker.actual_quantity) == 0.0


def test_unjournaled_broker_positions_and_manual_gtc_are_closed():
    broker = FakeOwnedBroker(actual_quantity="25")
    manager = LivePortfolioManager(broker, logging.getLogger("missing_journal"))

    remaining = manager.flatten_all(
        sleep_fn=lambda _: None, max_wait_seconds=10, retry_delay=1
    )

    assert remaining == []
    assert broker.close_all_calls == [True]
    assert float(broker.actual_quantity) == 0.0
    assert broker.manual_gtc_order.status == "canceled"


def test_flatten_closes_short_quantity_reported_by_alpaca(tmp_path):
    journal = _owned_journal(tmp_path, side="sell_short", quantity="5")
    broker = FakeOwnedBroker(actual_quantity="-25")
    manager = LivePortfolioManager(
        broker, logging.getLogger("broker_short_flatten"), order_journal=journal
    )

    remaining = manager.flatten_all(
        sleep_fn=lambda _: None, max_wait_seconds=10, retry_delay=1
    )

    assert remaining == []
    assert broker.close_all_calls == [True]
    assert float(broker.actual_quantity) == 0.0
    assert broker.close_order.side == "buy"


def test_failed_per_symbol_close_is_retried_after_no_active_orders_remain():
    broker = FakeOwnedBroker(
        actual_quantity="25", include_open_orders=False, fail_close_responses=1
    )
    manager = LivePortfolioManager(broker, logging.getLogger("flatten_partial_failure"))

    remaining = manager.flatten_all(
        sleep_fn=lambda _: None, max_wait_seconds=10, retry_delay=1
    )

    assert remaining == []
    assert broker.close_all_calls == [True, True]
    assert float(broker.actual_quantity) == 0.0
    assert manager.last_flatten_errors == 1


def test_pending_close_order_is_tracked_without_duplicate_submission():
    broker = FakeOwnedBroker(actual_quantity="25", pending_close=True)
    manager = LivePortfolioManager(broker, logging.getLogger("flatten_pending_close"))

    def complete_during_poll(_):
        broker.complete_pending_close()

    remaining = manager.flatten_all(
        sleep_fn=complete_during_poll, max_wait_seconds=10, retry_delay=1
    )

    assert remaining == []
    assert broker.close_all_calls == [True]
    assert float(broker.actual_quantity) == 0.0


def test_broken_journal_does_not_block_broker_liquidation():
    class BrokenJournal:
        def append(self, event_type, **payload):
            raise OSError("journal disk unavailable")

        def read_events(self):
            raise ValueError("journal corrupt")

    broker = FakeOwnedBroker(actual_quantity="25")
    manager = LivePortfolioManager(
        broker, logging.getLogger("flatten_broken_journal"), order_journal=BrokenJournal()
    )

    remaining = manager.flatten_all(
        sleep_fn=lambda _: None, max_wait_seconds=10, retry_delay=1
    )

    assert remaining == []
    assert broker.close_all_calls == [True]
    assert float(broker.actual_quantity) == 0.0


def _session_bot_for_snapshot_test(
    *,
    account_snapshot_error: Exception | None = None,
):
    from money_mintter.core.live_bot import LiveMarketBot

    bot = LiveMarketBot.__new__(LiveMarketBot)
    bot.logger = logging.getLogger("final_snapshot_test")
    bot.settings = Settings()
    moment = datetime(2026, 11, 13, 20, 0, tzinfo=timezone.utc)
    bot.trading_client = SimpleNamespace(
        get_clock=lambda: SimpleNamespace(
            is_open=True,
            next_open=moment - __import__("datetime").timedelta(hours=6),
            next_close=moment + __import__("datetime").timedelta(minutes=5),
        )
    )

    class Portfolio:
        last_equity = 2071.08
        last_flatten_errors = 0

        def flatten_all(self, *args, **kwargs):
            return []

        def fetch_account_and_positions(self):
            if account_snapshot_error is not None:
                raise account_snapshot_error
            return (
                1200.00,
                2071.26,
                2071.26,
                [],
                [],
                set(),
            )

        def fetch_open_order_symbols(self):
            return set()

    bot.portfolio_manager = Portfolio()
    bot.kill_switch = SimpleNamespace(tripped=False, reason="")
    bot.latest_state = {
        "equity": 2071.08,
        "cash": 1000.00,
        "open_positions": [],
        "final_account_snapshot_verified": False,
    }
    return bot, moment


def test_session_summary_uses_post_flatten_broker_equity(caplog):
    bot, moment = _session_bot_for_snapshot_test()

    with caplog.at_level(logging.INFO):
        result = bot.run_session(
            now_fn=lambda: moment,
            sleep_fn=lambda _: None,
        )

    assert result["status"] == "completed"
    assert bot.latest_state["equity"] == pytest.approx(2071.26)
    assert bot.latest_state["cash"] == pytest.approx(1200.00)
    assert bot.latest_state["final_account_snapshot_verified"] is True
    assert "[Final Broker Snapshot] equity=$2071.26" in caplog.text
    assert "final_account_snapshot=verified" in caplog.text


def test_session_cannot_complete_when_post_flatten_snapshot_fails(caplog):
    bot, moment = _session_bot_for_snapshot_test(
        account_snapshot_error=RuntimeError("broker snapshot unavailable")
    )

    with caplog.at_level(logging.CRITICAL):
        result = bot.run_session(
            now_fn=lambda: moment,
            sleep_fn=lambda _: None,
        )

    assert result["status"] == "flatten_incomplete"
    assert result["remaining"] == ["UNKNOWN"]
    assert bot.latest_state["final_account_snapshot_verified"] is False
    assert bot.portfolio_manager.last_flatten_errors == 1
    assert "Unable to verify final broker account/positions/orders snapshot" in caplog.text


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

def test_owned_flatten_closes_only_mm_owned_short_quantity(tmp_path):
    journal = _owned_journal(tmp_path, side="sell_short", quantity="5")
    broker = FakeOwnedBroker(actual_quantity="-25")
    manager = LivePortfolioManager(
        broker, logging.getLogger("owned_short_flatten"), order_journal=journal
    )

    remaining = manager.flatten_all(
        sleep_fn=lambda _: None, max_wait_seconds=10, retry_delay=1
    )

    assert remaining == []
    assert broker.cancel_calls == ["mm-take-profit", "mm-stop-loss"]
    assert broker.close_calls == [("AAPL", "5")]
    assert float(broker.actual_quantity) == -20.0
    assert broker.manual_gtc_order.status == "new"
    assert "manual-gtc-order" not in broker.cancel_calls
    assert journal.owned_position_quantities() == {}