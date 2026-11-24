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
    def __init__(self, *, actual_quantity="25"):
        self.actual_quantity = actual_quantity
        self.cancel_calls = []
        self.close_calls = []
        self.manual_gtc_order = SimpleNamespace(
            id="manual-gtc-order",
            symbol="MSFT",
            side="buy",
            status="new",
            time_in_force="gtc",
            client_order_id="manual-long-term-order",
        )
        self.orders = {
            "mm-entry-order": SimpleNamespace(
                id="mm-entry-order", symbol="AAPL", status="filled",
                filled_qty="5", client_order_id="mm-ict-unit-entry",
            ),
            "mm-take-profit": SimpleNamespace(
                id="mm-take-profit", symbol="AAPL", status="new", filled_qty="0",
            ),
            "mm-stop-loss": SimpleNamespace(
                id="mm-stop-loss", symbol="AAPL", status="new", filled_qty="0",
            ),
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
            qty=str(self.actual_quantity),
            avg_entry_price="100.00",
            current_price="100.00",
            unrealized_pl="0.00",
        )]

    def get_order_by_id(self, order_id):
        order_id = str(order_id)
        if order_id == "mm-close-order":
            self.close_order.status = "filled"
            self.close_order.filled_qty = self.close_calls[-1][1]
            return self.close_order
        return self.orders[order_id]

    def cancel_order_by_id(self, order_id):
        order_id = str(order_id)
        self.cancel_calls.append(order_id)
        self.orders[order_id].status = "canceled"

    def close_position(self, symbol, close_options=None):
        quantity = str(close_options.qty)
        self.close_calls.append((symbol, quantity))
        current_quantity = float(self.actual_quantity)
        if current_quantity > 0:
            self.actual_quantity = str(current_quantity - float(quantity))
            close_side = "sell"
        else:
            self.actual_quantity = str(current_quantity + float(quantity))
            close_side = "buy"
        self.close_order = SimpleNamespace(
            id="mm-close-order",
            symbol=symbol,
            status="accepted",
            filled_qty="0",
        )
        self.orders["mm-close-order"] = self.close_order
        self.close_fill = {
            "id": "mm-close-fill",
            "order_id": "mm-close-order",
            "symbol": symbol,
            "side": close_side,
            "qty": quantity,
            "price": "101",
            "transaction_time": datetime.now(timezone.utc).isoformat(),
        }
        return self.close_order

    def get(self, path, params):
        assert path == "/account/activities/FILL"
        if params.get("order_id") == "mm-close-order":
            return [self.close_fill]
        return []

    def get_orders(self, request):
        return [self.manual_gtc_order]


def test_owned_flatten_cancels_mm_bracket_and_closes_only_mm_filled_quantity(tmp_path):
    from money_mintter.core.live_bot import LivePortfolioManager

    journal = _owned_journal(tmp_path, quantity="5")
    broker = FakeOwnedBroker(actual_quantity="25")
    manager = LivePortfolioManager(
        broker, logging.getLogger("owned_flatten"), order_journal=journal
    )

    remaining = manager.flatten_all(
        sleep_fn=lambda _: None, max_wait_seconds=10, retry_delay=1
    )

    assert remaining == []
    assert broker.cancel_calls == ["mm-take-profit", "mm-stop-loss"]
    assert broker.close_calls == [("AAPL", "5")]
    assert float(broker.actual_quantity) == 20.0
    assert broker.manual_gtc_order.status == "new"
    assert "manual-gtc-order" not in broker.cancel_calls
    assert journal.owned_position_quantities() == {}


def test_owned_flatten_retries_transient_reconciliation_then_closes_only_mm_quantity(tmp_path):
    journal = _owned_journal(tmp_path, quantity="5")
    broker = FakeOwnedBroker(actual_quantity="25")
    original_get_order = broker.get_order_by_id
    timed_out = False

    def get_order_with_one_transient_timeout(order_id):
        nonlocal timed_out
        if order_id == "mm-take-profit" and not timed_out:
            timed_out = True
            raise TimeoutError("temporary broker order lookup timeout")
        return original_get_order(order_id)

    broker.get_order_by_id = get_order_with_one_transient_timeout
    manager = LivePortfolioManager(
        broker, logging.getLogger("owned_flatten_retry"), order_journal=journal
    )

    remaining = manager.flatten_all(
        sleep_fn=lambda _: None, max_wait_seconds=10, retry_delay=1
    )

    assert timed_out is True
    assert remaining == []
    assert broker.cancel_calls == ["mm-take-profit", "mm-stop-loss"]
    assert broker.close_calls == [("AAPL", "5")]
    assert float(broker.actual_quantity) == 20.0
    assert broker.manual_gtc_order.status == "new"
    assert journal.owned_position_quantities() == {}


def test_owned_flatten_leaves_untracked_manual_holdings_and_gtc_orders_untouched(tmp_path):
    from money_mintter.core.live_bot import LivePortfolioManager
    from money_mintter.core.order_journal import OrderLifecycleJournal

    journal = OrderLifecycleJournal(tmp_path / "empty-order-lifecycle.jsonl")
    broker = FakeOwnedBroker(actual_quantity="25")
    manager = LivePortfolioManager(
        broker, logging.getLogger("manual_holdings"), order_journal=journal
    )

    remaining = manager.flatten_all(
        sleep_fn=lambda _: None, max_wait_seconds=2, retry_delay=1
    )

    assert remaining == []
    assert broker.close_calls == []
    assert broker.cancel_calls == []
    assert broker.actual_quantity == "25"
    assert broker.manual_gtc_order.status == "new"


def test_owned_flatten_fails_closed_when_account_quantity_conflicts_with_mm_ledger(tmp_path):
    from money_mintter.core.live_bot import LivePortfolioManager

    journal = _owned_journal(tmp_path, quantity="5")
    broker = FakeOwnedBroker(actual_quantity="3")
    manager = LivePortfolioManager(
        broker, logging.getLogger("owned_quantity_mismatch"), order_journal=journal
    )

    remaining = manager.flatten_all(
        sleep_fn=lambda _: None, max_wait_seconds=2, retry_delay=1
    )

    assert remaining == ["AAPL"]
    assert broker.close_calls == []
    assert broker.actual_quantity == "3"


def test_unjournaled_positions_and_manual_gtc_are_not_touched(tmp_path):
    from money_mintter.core.live_bot import LivePortfolioManager

    broker = FakeOwnedBroker(actual_quantity="25")
    manager = LivePortfolioManager(broker, logging.getLogger("missing_journal"))

    remaining = manager.flatten_all(
        sleep_fn=lambda _: None, max_wait_seconds=2, retry_delay=1
    )

    assert remaining == ["AAPL", "MSFT"]
    assert broker.close_calls == []
    assert broker.cancel_calls == []
    assert broker.actual_quantity == "25"
    assert broker.manual_gtc_order.status == "new"


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