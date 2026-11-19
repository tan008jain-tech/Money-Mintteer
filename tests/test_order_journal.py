from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from money_mintter.core.order_journal import OrderLifecycleJournal, snapshot_order


def _fill(fill_id, order_id, symbol, side, qty, price, time):
    return {
        "event_type": "fill",
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
