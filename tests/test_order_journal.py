from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

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
        "fill", **_fill("s1", "short-entry", "MSFT", "sell", 5, 200, start - timedelta(seconds=1))
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
