from __future__ import annotations

import json
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from alpaca.trading.enums import OrderType

from money_mintter.core.order_journal import OrderLifecycleJournal, snapshot_order


def _fill(fill_id, order_id, symbol, side, qty, price, time):
    return {
        "fill_id": fill_id,
        "order_id": order_id,
        "symbol": symbol,
        "side": side,
        "qty": str(qty),
        "price": str(price),
        "transaction_time": time.isoformat(),
    }


def test_journal_appends_and_reads_versioned_jsonl_events(tmp_path):
    journal = OrderLifecycleJournal(tmp_path / "order_lifecycle.jsonl")
    created = journal.append("entry_intent", symbol="AAPL", value=1, at=datetime(2026, 11, 19, tzinfo=timezone.utc))

    events = journal.read_events()

    assert len(events) == 1
    assert events[0]["event_id"] == created["event_id"]
    assert events[0]["event_type"] == "entry_intent"
    assert events[0]["at"] == "2026-11-19T00:00:00+00:00"
    assert json.loads((tmp_path / "order_lifecycle.jsonl").read_text(encoding="utf-8"))["schema_version"] == 1


def test_snapshot_order_preserves_parent_child_role_and_identifiers():
    parent = SimpleNamespace(
        id="parent-id",
        client_order_id="mm-ict-abc",
        symbol="AAPL",
        side="buy",
        type="market",
        order_class="bracket",
        status="accepted",
        qty="10",
        filled_qty="0",
        group_id="group-id",
        group_role=None,
    )
    child = SimpleNamespace(
        id="tp-id",
        symbol="AAPL",
        side="sell",
        type="limit",
        status="new",
        qty="10",
        filled_qty="0",
        group_id="group-id",
        group_role="take_profit",
    )

    parent_snapshot = snapshot_order(parent, role_hint="entry")
    child_snapshot = snapshot_order(child, parent_order_id="parent-id")

    assert parent_snapshot["order_id"] == "parent-id"
    assert parent_snapshot["client_order_id"] == "mm-ict-abc"
    assert parent_snapshot["order_role"] == "entry"
    assert child_snapshot["order_id"] == "tp-id"
    assert child_snapshot["order_role"] == "take_profit"
    assert child_snapshot["parent_order_id"] == "parent-id"


def test_snapshot_order_normalizes_alpaca_string_enum_order_types():
    limit_order = SimpleNamespace(
        id="enum-tp",
        symbol="AAPL",
        side="sell",
        type=OrderType.LIMIT,
        order_class="bracket",
        status="new",
        qty="1",
        filled_qty="0",
        limit_price="105.00",
        stop_price=None,
        group_role=None,
    )
    stop_order = SimpleNamespace(
        id="enum-sl",
        symbol="AAPL",
        side="sell",
        type=OrderType.STOP,
        order_class="bracket",
        status="new",
        qty="1",
        filled_qty="0",
        limit_price=None,
        stop_price="95.00",
        group_role=None,
    )

    limit_snapshot = snapshot_order(limit_order, parent_order_id="parent-id")
    stop_snapshot = snapshot_order(stop_order, parent_order_id="parent-id")

    assert type(limit_snapshot["order_type"]) is str
    assert limit_snapshot["order_type"] == "limit"
    assert limit_snapshot["order_role"] == "take_profit"
    assert type(stop_snapshot["order_type"]) is str
    assert stop_snapshot["order_type"] == "stop"
    assert stop_snapshot["order_role"] == "stop_loss"


def test_owned_order_records_recovers_legacy_bracket_child_roles_conservatively(tmp_path):
    import pytest

    journal = OrderLifecycleJournal(tmp_path / "order_lifecycle.jsonl")
    cdns_parent = {
        "order_id": "cdns-entry",
        "client_order_id": "mm-ict-cdns",
        "symbol": "CDNS",
        "side": "buy",
        "order_class": "bracket",
        "status": "filled",
        "qty": "1",
        "filled_qty": "1",
        "time_in_force": "day",
        "order_role": "entry",
    }
    cdns_take_profit = {
        "order_id": "cdns-tp",
        "symbol": "CDNS",
        "side": "sell",
        "order_type": "limit",
        "order_class": "bracket",
        "status": "filled",
        "qty": "1",
        "filled_qty": "1",
        "limit_price": "291.27",
        "parent_order_id": "cdns-entry",
        "order_role": "unknown",
    }
    cdns_stop = {
        "order_id": "cdns-sl",
        "symbol": "CDNS",
        "side": "sell",
        "order_type": "stop",
        "order_class": "bracket",
        "status": "canceled",
        "qty": "1",
        "filled_qty": "0",
        "stop_price": "277.22",
        "parent_order_id": "cdns-entry",
        "order_role": "unknown",
    }
    unverified_limit = {
        "order_id": "unverified-limit",
        "symbol": "CDNS",
        "side": "sell",
        "order_type": "limit",
        "order_class": "simple",
        "status": "new",
        "qty": "1",
        "filled_qty": "0",
        "limit_price": "300.00",
        "parent_order_id": "cdns-entry",
        "order_role": "unknown",
    }
    journal.append(
        "entry_order_submitted",
        client_order_id="mm-ict-cdns",
        parent_order=cdns_parent,
        child_orders=[cdns_take_profit, cdns_stop, unverified_limit],
    )
    # A later legacy order_state snapshot also carried the incorrect role.
    journal.append(
        "order_state",
        order={**cdns_take_profit, "status": "filled", "filled_qty": "1", "order_role": "unknown"},
    )

    aph_parent = {
        "order_id": "aph-entry",
        "client_order_id": "mm-ict-aph",
        "symbol": "APH",
        "side": "sell",
        "order_class": "bracket",
        "status": "filled",
        "qty": "8",
        "filled_qty": "8",
        "time_in_force": "day",
        "order_role": "entry",
    }
    aph_take_profit = {
        "order_id": "aph-tp",
        "symbol": "APH",
        "side": "buy",
        "order_type": "limit",
        "order_class": "bracket",
        "status": "canceled",
        "qty": "8",
        "filled_qty": "0",
        "limit_price": "68.03",
        "parent_order_id": "aph-entry",
        "order_role": "unknown",
    }
    journal.append(
        "entry_order_submitted",
        client_order_id="mm-ict-aph",
        parent_order=aph_parent,
        child_orders=[aph_take_profit],
    )

    records = journal.owned_order_records()

    assert records["cdns-tp"]["order_role"] == "take_profit"
    assert records["cdns-tp"]["status"] == "filled"
    assert records["cdns-sl"]["order_role"] == "stop_loss"
    assert records["aph-tp"]["order_role"] == "take_profit"
    assert records["unverified-limit"]["order_role"] == "unknown"

    journal.append(
        "fill",
        **_fill(
            "unverified-fill",
            "unverified-limit",
            "CDNS",
            "sell",
            1,
            300.00,
            datetime.now(timezone.utc),
        ),
    )
    with pytest.raises(ValueError, match="unknown order role"):
        journal.owned_position_quantities()


def test_session_summary_matches_partial_long_fills_and_separates_fees(tmp_path):
    journal = OrderLifecycleJournal(tmp_path / "order_lifecycle.jsonl")
    start = datetime(2026, 11, 19, 14, 0, tzinfo=timezone.utc)
    end = start + timedelta(hours=2)

    journal.append(
        "entry_order_submitted",
        client_order_id="entry-client",
        parent_order={"order_id": "entry", "order_role": "entry"},
        child_orders=[
            {"order_id": "tp", "order_role": "take_profit"},
            {"order_id": "sl", "order_role": "stop_loss"},
        ],
    )
    journal.append(
        "fill", **_fill("f1", "entry", "AAPL", "buy", 10, 100, start - timedelta(minutes=1))
    )
    journal.append(
        "fill", **_fill("f2", "tp", "AAPL", "sell", 4, 105, start + timedelta(minutes=10))
    )
    journal.append(
        "fill", **_fill("f3", "tp", "AAPL", "sell", 6, 106, start + timedelta(minutes=11))
    )
    journal.append(
        "fee_activity",
        activity_id="fee-1",
        net_amount="-0.80",
        activity_type="FEE",
        created_at=(start + timedelta(minutes=12)).isoformat(),
    )

    summary = journal.summarize_session(start, end)

    assert summary["realized_gross_pnl_usd"] == 56.0
    assert summary["pnl_coverage_status"] == "complete_for_recorded_closed_trades"
    assert summary["open_entry_quantity_after_session"] == 0.0
    assert summary["unattributed_fill_count"] == 0
    assert summary["matched_exit_quantity"] == 10.0
    assert summary["unmatched_exit_quantity"] == 0.0
    assert summary["fee_activity_net_amount_usd"] == -0.8
    assert summary["fee_activity_count"] == 1
    assert summary["net_pnl_usd"] is None
    assert summary["net_pnl_status"] == "not_calculated"


def test_session_summary_handles_short_pnl_and_unmatched_exit(tmp_path):
    journal = OrderLifecycleJournal(tmp_path / "order_lifecycle.jsonl")
    start = datetime(2026, 11, 19, 14, 0, tzinfo=timezone.utc)
    end = start + timedelta(hours=1)
    journal.append(
        "entry_order_submitted",
        parent_order={"order_id": "short-entry", "order_role": "entry"},
        child_orders=[{"order_id": "cover", "order_role": "stop_loss"}],
    )
    journal.append(
        "fill", **_fill("s1", "short-entry", "MSFT", "sell_short", 5, 200, start - timedelta(seconds=1))
    )
    journal.append(
        "fill", **_fill("s2", "cover", "MSFT", "buy", 5, 196, start + timedelta(seconds=5))
    )
    journal.append(
        "eod_close_order_submitted",
        order_id="unmatched-exit",
        symbol="NVDA",
        order_role="eod_liquidation",
    )
    journal.append(
        "fill", **_fill("s3", "unmatched-exit", "NVDA", "sell", 2, 50, start + timedelta(seconds=6))
    )

    summary = journal.summarize_session(start, end)

    assert summary["realized_gross_pnl_usd"] == 20.0
    assert summary["matched_exit_quantity"] == 5.0
    assert summary["unmatched_exit_quantity"] == 2.0


def test_exit_classification_uses_broker_leg_role_and_eod_context(tmp_path):
    journal = OrderLifecycleJournal(tmp_path / "order_lifecycle.jsonl")
    now = datetime.now(timezone.utc)
    journal.append(
        "entry_order_submitted",
        parent_order={"order_id": "parent", "order_role": "entry"},
        child_orders=[
            {"order_id": "tp", "order_role": "take_profit"},
            {"order_id": "sl", "order_role": "stop_loss"},
        ],
    )
    journal.append(
        "fill", **_fill("tp-fill", "tp", "AAPL", "sell", 1, 105, now)
    )
    assert journal.classify_exit("AAPL") == "take_profit"

    journal.append(
        "eod_close_order_submitted",
        order_id="eod",
        symbol="MSFT",
        order_role="eod_liquidation",
        exit_reason="eod_flatten",
    )
    journal.append(
        "fill", **_fill("eod-fill", "eod", "MSFT", "sell", 1, 50, now)
    )
    assert journal.classify_exit("MSFT") == "eod_liquidation"
    assert journal.classify_exit("NVDA") == "external_or_unattributed"


def test_incomplete_final_jsonl_record_is_ignored_but_interior_corruption_is_not(tmp_path):
    path = tmp_path / "order_lifecycle.jsonl"
    journal = OrderLifecycleJournal(path)
    journal.append("entry_intent", symbol="AAPL")
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"schema_version":1,"event_type":"partial')
    assert len(journal.read_events()) == 1

    path.write_text('{"schema_version":1}\nnot-json\n', encoding="utf-8")
    try:
        journal.read_events()
    except ValueError as exc:
        assert "invalid JSON" in str(exc)
    else:
        raise AssertionError("completed malformed record must fail closed")


def test_ict_submission_persists_intent_and_broker_parent_child_ids(tmp_path, monkeypatch):
    import logging
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from money_mintter.config import Settings
    from money_mintter.core.live_bot import LivePortfolioManager
    from money_mintter.domain import Side
    from money_mintter.strategy.ict.integration import ICTExecutionInstruction

    monkeypatch.setenv("MAX_PORTFOLIO_RISK_PCT", "3")
    journal = OrderLifecycleJournal(tmp_path / "order_lifecycle.jsonl")

    class Quotes:
        def get_stock_latest_quote(self, request):
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
        def __init__(self):
            self.request = None

        def submit_order(self, request):
            self.request = request
            existing = journal.read_events()
            assert existing and existing[-1]["event_type"] == "entry_intent"
            return SimpleNamespace(
                id="parent-order-id",
                client_order_id=request.client_order_id,
                order_class="bracket",
                legs=[
                    SimpleNamespace(
                        id="take-profit-id",
                        type="limit",
                        group_role="take_profit",
                        symbol="AAPL",
                        side="sell",
                        qty="10",
                    ),
                    SimpleNamespace(
                        id="stop-loss-id",
                        type="stop",
                        group_role="stop_loss",
                        symbol="AAPL",
                        side="sell",
                        qty="10",
                    ),
                ],
            )

    broker = Broker()
    manager = LivePortfolioManager(
        broker,
        logging.getLogger("order_journal_execution"),
        stock_client=Quotes(),
        order_journal=journal,
    )
    now = datetime.now(timezone.utc)
    instruction = ICTExecutionInstruction(
        symbol="AAPL",
        side=Side.BUY,
        entry_price=100.0,
        stop_price=98.0,
        target_price=105.0,
        risk_pts=2.0,
        timestamp=now,
        timestamp_ns=1_000_000,
        zone_type="disp_fvg",
        score=1,
        zone_top=101.0,
        zone_bot=99.0,
        reason="journal_test",
    )

    sent = manager.submit_ict_instructions(
        [instruction],
        set(),
        100_000.0,
        Settings(),
        execution_time_utc=now,
    )

    events = journal.read_events()
    assert sent == 1
    assert broker.request is not None
    assert broker.request.client_order_id.startswith("mm-ict-")
    assert [event["event_type"] for event in events] == [
        "entry_intent",
        "entry_order_submitted",
    ]
    assert events[0]["client_order_id"] == broker.request.client_order_id
    assert events[1]["parent_order"]["order_id"] == "parent-order-id"
    assert {leg["order_role"] for leg in events[1]["child_orders"]} == {
        "take_profit",
        "stop_loss",
    }


def test_ict_submission_fails_closed_when_intent_cannot_be_persisted(monkeypatch):
    import logging
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from money_mintter.config import Settings
    from money_mintter.core.live_bot import LivePortfolioManager
    from money_mintter.domain import Side
    from money_mintter.strategy.ict.integration import ICTExecutionInstruction

    monkeypatch.setenv("MAX_PORTFOLIO_RISK_PCT", "3")

    class BrokenJournal:
        def append(self, event_type, **payload):
            raise OSError("disk unavailable")

    class Broker:
        def __init__(self):
            self.submissions = 0

        def submit_order(self, request):
            self.submissions += 1
            return SimpleNamespace(order_class="bracket", legs=[1, 2])

    class Quotes:
        def get_stock_latest_quote(self, request):
            now = datetime.now(timezone.utc)
            return {
                symbol: SimpleNamespace(
                    timestamp=now,
                    bid_price=99.99,
                    ask_price=100.01,
                )
                for symbol in request.symbol_or_symbols
            }

    broker = Broker()
    manager = LivePortfolioManager(
        broker,
        logging.getLogger("order_journal_fail_closed"),
        stock_client=Quotes(),
        order_journal=BrokenJournal(),
    )
    now = datetime.now(timezone.utc)
    instruction = ICTExecutionInstruction(
        symbol="AAPL",
        side=Side.BUY,
        entry_price=100.0,
        stop_price=98.0,
        target_price=105.0,
        risk_pts=2.0,
        timestamp=now,
        timestamp_ns=1,
        zone_type="disp_fvg",
        score=1,
        zone_top=101.0,
        zone_bot=99.0,
        reason="journal_failure_test",
    )

    sent = manager.submit_ict_instructions(
        [instruction],
        set(),
        100_000.0,
        Settings(),
        execution_time_utc=now,
    )
    assert sent == 0
    assert broker.submissions == 0
    assert manager.last_submit_failures == 0


def test_broken_journal_blocks_new_ict_entries_but_does_not_require_broker_calls():
    import logging

    from datetime import datetime, timezone
    from types import SimpleNamespace

    from money_mintter.config import Settings
    from money_mintter.core.live_bot import LivePortfolioManager
    from money_mintter.domain import Side
    from money_mintter.strategy.ict.integration import ICTExecutionInstruction

    class BrokenJournal:
        def read_events(self):
            raise ValueError("corrupt journal")

        def append(self, event_type, **payload):
            raise OSError("journal unavailable")

    class Broker:
        def __init__(self):
            self.submissions = 0

        def submit_order(self, request):
            self.submissions += 1
            return SimpleNamespace(order_class="bracket", legs=[1, 2])

    class Quotes:
        def __init__(self):
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

    broker, quotes = Broker(), Quotes()
    manager = LivePortfolioManager(
        broker,
        logging.getLogger("order_journal_corrupt_store"),
        stock_client=quotes,
        order_journal=BrokenJournal(),
    )
    assert manager.reconcile_order_journal() is False
    assert manager.order_journal_healthy is False

    now = datetime.now(timezone.utc)
    instruction = __import__(
        "money_mintter.strategy.ict.integration",
        fromlist=["ICTExecutionInstruction"],
    ).ICTExecutionInstruction(
        symbol="AAPL",
        side=Side.BUY,
        entry_price=100.0,
        stop_price=98.0,
        target_price=105.0,
        risk_pts=2.0,
        timestamp=now,
        timestamp_ns=1,
        zone_type="disp_fvg",
        score=1,
        zone_top=101.0,
        zone_bot=99.0,
        reason="broken_journal_test",
    )

    sent = manager.submit_ict_instructions(
        [instruction],
        set(),
        100_000.0,
        Settings(),
        execution_time_utc=now,
    )
    assert sent == 0
    assert quotes.calls == 0
    assert broker.submissions == 0
    assert manager.last_submit_failures == 0


def test_summary_flags_open_inventory_and_unattributed_fills(tmp_path):
    journal = OrderLifecycleJournal(tmp_path / "order_lifecycle.jsonl")
    start = datetime(2026, 11, 19, 14, 0, tzinfo=timezone.utc)
    end = start + timedelta(hours=1)
    journal.append(
        "entry_order_submitted",
        parent_order={"order_id": "entry", "order_role": "entry"},
        child_orders=[{"order_id": "tp", "order_role": "take_profit"}],
    )
    journal.append(
        "fill", **_fill("entry-fill", "entry", "AAPL", "buy", 5, 100, start)
    )
    journal.append(
        "fill", **_fill("tp-fill", "tp", "AAPL", "sell", 2, 102, start + timedelta(minutes=5))
    )
    journal.append(
        "fill", **_fill("other-fill", "manual-order", "MSFT", "buy", 1, 50, start + timedelta(minutes=6))
    )

    summary = journal.summarize_session(start, end)

    assert summary["realized_gross_pnl_usd"] == 4.0
    assert summary["open_entry_quantity_after_session"] == 3.0
    assert summary["unattributed_fill_count"] == 1
    assert summary["pnl_coverage_status"] == "open_or_unmatched_records"
    assert summary["net_pnl_usd"] is None


def test_fee_activity_reconciliation_deduplicates_broker_records(tmp_path):
    import logging
    from datetime import datetime, timezone

    from money_mintter.core.live_bot import LivePortfolioManager

    journal = OrderLifecycleJournal(tmp_path / "order_lifecycle.jsonl")
    calls = []

    class Broker:
        def get(self, path, params):
            calls.append((path, params))
            return [{
                "id": "fee-activity-1",
                "activity_type": "FEE",
                "activity_sub_type": "REG",
                "net_amount": "-0.02",
                "date": "2026-11-19",
                "created_at": "2026-11-19T20:00:00Z",
                "description": "regulatory fee",
            }]

    manager = LivePortfolioManager(
        Broker(),
        logging.getLogger("fee_activity_reconciliation"),
        order_journal=journal,
    )
    start = datetime(2026, 11, 19, 14, 0, tzinfo=timezone.utc)
    end = datetime(2026, 11, 20, 0, 0, tzinfo=timezone.utc)

    first = manager.reconcile_fee_activities(start, end)
    second = manager.reconcile_fee_activities(start, end)

    fee_events = [
        event for event in journal.read_events()
        if event.get("event_type") == "fee_activity"
    ]
    assert first == 1
    assert second == 0
    assert len(fee_events) == 1
    assert len(calls) == 2
    assert all(call[0] == "/account/activities/FEE" for call in calls)


def test_exit_classification_does_not_reuse_old_trade_exit_for_new_entry(tmp_path):
    journal = OrderLifecycleJournal(tmp_path / "order_lifecycle.jsonl")
    first_entry = datetime(2026, 11, 19, 14, 0, tzinfo=timezone.utc)
    first_exit = first_entry + timedelta(minutes=5)
    second_entry = first_exit + timedelta(minutes=5)
    journal.append(
        "entry_order_submitted",
        parent_order={"order_id": "entry-1", "order_role": "entry"},
        child_orders=[{"order_id": "tp-1", "order_role": "take_profit"}],
    )
    journal.append(
        "fill", **_fill("entry-1-fill", "entry-1", "AAPL", "buy", 1, 100, first_entry)
    )
    journal.append(
        "fill", **_fill("tp-1-fill", "tp-1", "AAPL", "sell", 1, 105, first_exit)
    )
    assert journal.classify_exit("AAPL") == "take_profit"

    journal.append(
        "entry_order_submitted",
        parent_order={"order_id": "entry-2", "order_role": "entry"},
        child_orders=[{"order_id": "sl-2", "order_role": "stop_loss"}],
    )
    journal.append(
        "fill", **_fill("entry-2-fill", "entry-2", "AAPL", "buy", 1, 110, second_entry)
    )
    assert journal.classify_exit("AAPL") == "external_or_unattributed"

    emergency_time = second_entry + timedelta(minutes=1)
    journal.append(
        "flatten_close_order_submitted",
        order_id="emergency-close",
        symbol="AAPL",
        order_role="emergency_cleanup",
        exit_reason="emergency_cleanup",
    )
    journal.append(
        "fill", **_fill("emergency-fill", "emergency-close", "AAPL", "sell", 1, 108, emergency_time)
    )
    assert journal.classify_exit("AAPL") == "emergency_cleanup"


def test_owned_position_quantities_uses_only_mm_prefixed_parent_and_linked_legs(tmp_path):
    journal = OrderLifecycleJournal(tmp_path / "owned-positions.jsonl")
    start = datetime(2026, 11, 20, 14, 30, tzinfo=timezone.utc)
    journal.append(
        "entry_order_submitted",
        client_order_id="mm-ict-owned-entry",
        parent_order={
            "order_id": "mm-entry",
            "client_order_id": "mm-ict-owned-entry",
            "symbol": "AAPL",
            "order_role": "entry",
            "status": "filled",
            "filled_qty": "5",
        },
        child_orders=[
            {"order_id": "mm-tp", "symbol": "AAPL", "order_role": "take_profit", "status": "filled", "filled_qty": "2"},
            {"order_id": "mm-sl", "symbol": "AAPL", "order_role": "stop_loss", "status": "canceled", "filled_qty": "0"},
        ],
    )
    journal.append("fill", **_fill("mm-entry-fill", "mm-entry", "AAPL", "buy", 5, 100, start))
    journal.append("fill", **_fill("mm-tp-fill", "mm-tp", "AAPL", "sell", 2, 102, start + timedelta(minutes=5)))

    journal.append(
        "entry_order_submitted",
        client_order_id="manual-investment-order",
        parent_order={
            "order_id": "manual-entry",
            "client_order_id": "manual-investment-order",
            "symbol": "AAPL",
            "order_role": "entry",
            "status": "filled",
            "filled_qty": "20",
        },
        child_orders=[],
    )
    journal.append("fill", **_fill("manual-fill", "manual-entry", "AAPL", "buy", 20, 90, start))

    assert journal.owned_position_quantities() == {"AAPL": Decimal("3")}


def test_owned_position_quantities_rejects_incomplete_fill_reconciliation(tmp_path):
    import pytest

    journal = OrderLifecycleJournal(tmp_path / "incomplete-owned-fills.jsonl")
    journal.append(
        "entry_order_submitted",
        client_order_id="mm-ict-incomplete",
        parent_order={
            "order_id": "mm-entry",
            "client_order_id": "mm-ict-incomplete",
            "symbol": "AAPL",
            "order_role": "entry",
            "status": "filled",
            "filled_qty": "5",
        },
        child_orders=[],
    )
    journal.append(
        "fill",
        **_fill("partial-fill", "mm-entry", "AAPL", "buy", 3, 100, datetime.now(timezone.utc)),
    )

    with pytest.raises(ValueError, match="not fully reconciled"):
        journal.owned_position_quantities()


def test_legacy_unowned_close_orders_are_not_adopted(tmp_path):
    journal = OrderLifecycleJournal(tmp_path / "legacy-close.jsonl")
    journal.append(
        "flatten_close_order_submitted",
        order_id="legacy-close-order",
        symbol="AAPL",
        order_role="emergency_cleanup",
        broker_status="accepted",
    )

    assert journal.owned_order_records() == {}
    assert journal.unowned_legacy_close_order_ids() == ["legacy-close-order"]

    journal.append(
        "order_state",
        order={
            "order_id": "legacy-close-order",
            "symbol": "AAPL",
            "order_role": "emergency_cleanup",
            "status": "filled",
            "filled_qty": "10",
        },
    )
    assert journal.unowned_legacy_close_order_ids() == []


def test_owned_order_records_excludes_gtc_orders_and_their_filled_shares(tmp_path):
    journal = OrderLifecycleJournal(tmp_path / "gtc-order.jsonl")
    journal.append(
        "entry_order_submitted",
        client_order_id="mm-ict-long-term-order",
        parent_order={
            "order_id": "mm-gtc-parent",
            "client_order_id": "mm-ict-long-term-order",
            "symbol": "AAPL",
            "order_role": "entry",
            "status": "filled",
            "time_in_force": "gtc",
            "filled_qty": "10",
        },
        child_orders=[],
    )
    journal.append(
        "fill",
        **_fill(
            "mm-gtc-fill",
            "mm-gtc-parent",
            "AAPL",
            "buy",
            10,
            100,
            datetime.now(timezone.utc),
        ),
    )

    assert journal.owned_order_records() == {}
    assert journal.owned_position_quantities() == {}


def test_owned_order_records_inherits_day_tif_for_legacy_ict_journal_entries(tmp_path):
    journal = OrderLifecycleJournal(tmp_path / "legacy-day-order.jsonl")
    journal.append(
        "entry_order_submitted",
        client_order_id="mm-ict-legacy-day",
        parent_order={
            "order_id": "mm-day-parent",
            "client_order_id": "mm-ict-legacy-day",
            "symbol": "AAPL",
            "order_role": "entry",
            "status": "filled",
            "filled_qty": "0",
        },
        child_orders=[
            {
                "order_id": "mm-day-tp",
                "symbol": "AAPL",
                "order_role": "take_profit",
                "status": "new",
                "filled_qty": "0",
            }
        ],
    )

    records = journal.owned_order_records()
    assert records["mm-day-parent"]["time_in_force"] == "day"
    assert records["mm-day-tp"]["time_in_force"] == "day"


def test_owned_position_quantities_accounts_for_short_sale_activity_and_cover(tmp_path):
    journal = OrderLifecycleJournal(tmp_path / "short-owned-positions.jsonl")
    start = datetime(2026, 11, 20, 14, 30, tzinfo=timezone.utc)
    journal.append(
        "entry_order_submitted",
        client_order_id="mm-ict-short-entry",
        parent_order={
            "order_id": "mm-short-entry",
            "client_order_id": "mm-ict-short-entry",
            "symbol": "CTVA",
            "order_role": "entry",
            "status": "filled",
            "filled_qty": "10",
        },
        child_orders=[
            {"order_id": "mm-short-tp", "symbol": "CTVA", "order_role": "take_profit", "status": "new", "filled_qty": "0"},
            {"order_id": "mm-short-sl", "symbol": "CTVA", "order_role": "stop_loss", "status": "new", "filled_qty": "0"},
        ],
    )
    journal.append(
        "fill",
        **_fill("short-entry-fill", "mm-short-entry", "CTVA", "sell_short", 10, 8.05, start),
    )

    # Alpaca reports short-sale activity as sell_short; it creates negative inventory.
    assert journal.owned_position_quantities() == {"CTVA": Decimal("-10")}

    journal.append(
        "flatten_close_order_submitted",
        owner="money_mintter",
        order_id="mm-short-cover",
        symbol="CTVA",
        order_role="emergency_cleanup",
        time_in_force="day",
        broker_status="filled",
    )
    journal.append(
        "fill",
        **_fill("short-cover-fill", "mm-short-cover", "CTVA", "buy", 10, 8.00, start + timedelta(minutes=10)),
    )

    assert journal.owned_position_quantities() == {}


def test_session_summary_handles_short_loss_from_sell_short_activity(tmp_path):
    journal = OrderLifecycleJournal(tmp_path / "short-loss.jsonl")
    start = datetime(2026, 11, 19, 14, 0, tzinfo=timezone.utc)
    end = start + timedelta(hours=1)
    journal.append(
        "entry_order_submitted",
        parent_order={"order_id": "short-entry", "order_role": "entry"},
        child_orders=[{"order_id": "cover", "order_role": "stop_loss"}],
    )
    journal.append(
        "fill",
        **_fill("short-loss-entry", "short-entry", "NVDA", "sell_short", 3, 200, start - timedelta(seconds=1)),
    )
    journal.append(
        "fill",
        **_fill("short-loss-cover", "cover", "NVDA", "buy", 3, 207, start + timedelta(seconds=5)),
    )

    summary = journal.summarize_session(start, end)

    assert summary["realized_gross_pnl_usd"] == -21.0
    assert summary["matched_exit_quantity"] == 3.0
    assert summary["unmatched_exit_quantity"] == 0.0
    assert summary["open_entry_quantity_after_session"] == 0.0
    assert summary["unattributed_fill_count"] == 0
    assert summary["pnl_coverage_status"] == "complete_for_recorded_closed_trades"


def test_session_summary_marks_unsupported_fill_side_as_incomplete(tmp_path):
    journal = OrderLifecycleJournal(tmp_path / "unsupported-side.jsonl")
    start = datetime(2026, 11, 19, 14, 0, tzinfo=timezone.utc)
    end = start + timedelta(hours=1)
    journal.append(
        "entry_order_submitted",
        parent_order={"order_id": "entry", "order_role": "entry"},
        child_orders=[{"order_id": "exit", "order_role": "stop_loss"}],
    )
    journal.append(
        "fill",
        **_fill("unsupported-fill", "entry", "AAPL", "cross", 2, 100, start + timedelta(seconds=1)),
    )

    summary = journal.summarize_session(start, end)

    assert summary["realized_gross_pnl_usd"] == 0.0
    assert summary["unattributed_fill_count"] == 1
    assert summary["pnl_coverage_status"] == "open_or_unmatched_records"


def test_session_summary_marks_fill_without_parseable_timestamp_as_incomplete(tmp_path):
    journal = OrderLifecycleJournal(tmp_path / "untimed-fill.jsonl")
    start = datetime(2026, 11, 19, 14, 0, tzinfo=timezone.utc)
    end = start + timedelta(hours=1)
    journal.append(
        "entry_order_submitted",
        parent_order={"order_id": "entry", "order_role": "entry"},
        child_orders=[{"order_id": "exit", "order_role": "take_profit"}],
    )
    journal.append(
        "fill",
        **_fill("entry-fill", "entry", "AAPL", "buy", 2, 100, start - timedelta(seconds=1)),
    )
    journal.append(
        "fill",
        **_fill("exit-fill", "exit", "AAPL", "sell", 2, 105, start + timedelta(seconds=5)),
    )
    journal.append(
        "fill",
        fill_id="untimed-fill",
        order_id="manual-or-unknown",
        symbol="MSFT",
        side="buy",
        qty="1",
        price="50",
        transaction_time="not-a-timestamp",
    )

    summary = journal.summarize_session(start, end)

    assert summary["realized_gross_pnl_usd"] == 10.0
    assert summary["matched_exit_quantity"] == 2.0
    assert summary["unmatched_exit_quantity"] == 0.0
    assert summary["unattributed_fill_count"] == 1
    assert summary["pnl_coverage_status"] == "open_or_unmatched_records"


def test_order_journal_reconciliation_recovers_and_appends_fill_once(tmp_path):
    import logging

    from money_mintter.core.live_bot import LivePortfolioManager

    journal = OrderLifecycleJournal(tmp_path / "order_lifecycle.jsonl")
    journal.append(
        "entry_order_submitted",
        client_order_id="mm-ict-aapl",
        parent_order={
            "order_id": "entry-order",
            "client_order_id": "mm-ict-aapl",
            "symbol": "AAPL",
            "side": "buy",
            "order_type": "market",
            "order_class": "bracket",
            "order_role": "entry",
            "status": "accepted",
            "qty": "1",
            "filled_qty": "0",
            "time_in_force": "day",
        },
        child_orders=[
            {"order_id": "tp-order", "symbol": "AAPL", "order_role": "take_profit", "status": "new"},
            {"order_id": "sl-order", "symbol": "AAPL", "order_role": "stop_loss", "status": "new"},
        ],
    )

    class Broker:
        def __init__(self):
            self.order_lookups = 0
            self.activity_lookups = 0

        def get_order_by_id(self, order_id):
            self.order_lookups += 1
            if self.order_lookups == 1:
                raise TimeoutError("temporary order lookup timeout")
            return SimpleNamespace(
                id=order_id,
                client_order_id="mm-ict-aapl",
                symbol="AAPL",
                side="buy",
                type="market",
                order_class="bracket",
                order_role="entry",
                status="filled",
                qty="1",
                filled_qty="1",
                filled_avg_price="100.00",
                submitted_at=None,
                filled_at=None,
                time_in_force="day",
                group_id=None,
                group_role=None,
                limit_price=None,
                stop_price=None,
            )

        def get(self, path, params):
            assert path == "/account/activities/FILL"
            assert params["order_id"] == "entry-order"
            self.activity_lookups += 1
            return [{
                "id": "entry-fill-1",
                "order_id": "entry-order",
                "symbol": "AAPL",
                "side": "buy",
                "qty": "1",
                "price": "100.00",
                "transaction_time": datetime(2026, 11, 27, 15, 0, tzinfo=timezone.utc),
                "type": "fill",
                "order_status": "filled",
                "cum_qty": "1",
                "leaves_qty": "0",
            }]

    broker = Broker()
    manager = LivePortfolioManager(
        broker, logging.getLogger("order_journal_reconciliation_test"), order_journal=journal
    )

    assert manager.reconcile_order_journal() is False
    assert manager.order_journal_healthy is False
    assert manager.reconcile_order_journal() is True
    assert manager.order_journal_healthy is True
    assert manager.reconcile_order_journal() is True

    events = journal.read_events()
    fills = [event for event in events if event["event_type"] == "fill"]
    assert broker.order_lookups == 2
    assert broker.activity_lookups == 1
    assert len(fills) == 1
    assert fills[0]["fill_id"] == "entry-fill-1"
    assert journal.owned_position_quantities() == {"AAPL": Decimal("1")}
