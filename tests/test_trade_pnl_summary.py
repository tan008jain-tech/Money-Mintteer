from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from money_mintter.core.live_bot import LiveMarketBot
from money_mintter.core.order_journal import OrderLifecycleJournal


def test_session_summary_reports_fill_gross_and_observed_fees_without_fabricating_net(
    tmp_path,
    caplog,
):
    journal = OrderLifecycleJournal(tmp_path / "order_lifecycle.jsonl")
    start = datetime(2026, 11, 19, 14, 0, tzinfo=timezone.utc)
    end = start + timedelta(hours=1)
    journal.append(
        "entry_order_submitted",
        parent_order={"order_id": "entry", "order_role": "entry"},
        child_orders=[{"order_id": "tp", "order_role": "take_profit"}],
    )
    journal.append(
        "fill",
        fill_id="entry-fill",
        order_id="entry",
        symbol="AAPL",
        side="buy",
        qty="2",
        price="100",
        transaction_time=(start + timedelta(minutes=1)).isoformat(),
    )
    journal.append(
        "fill",
        fill_id="exit-fill",
        order_id="tp",
        symbol="AAPL",
        side="sell",
        qty="2",
        price="103",
        transaction_time=(start + timedelta(minutes=2)).isoformat(),
    )
    journal.append(
        "fee_activity",
        activity_id="fee-1",
        net_amount="-0.04",
        created_at=(start + timedelta(minutes=3)).isoformat(),
    )

    class PortfolioManager:
        last_flatten_errors = 0
        last_equity = 100.0
        order_journal = journal

        def __init__(self):
            self.reconciled = 0
            self.fees_reconciled = []

        def reconcile_order_journal(self):
            self.reconciled += 1
            return True

        def reconcile_fee_activities(self, start_utc, end_utc):
            self.fees_reconciled.append((start_utc, end_utc))
            return 0

    bot = LiveMarketBot.__new__(LiveMarketBot)
    bot.logger = logging.getLogger("trade_pnl_summary_test")
    bot.portfolio_manager = PortfolioManager()
    bot.latest_state = {"equity": 103.0, "final_account_snapshot_verified": False}
    bot._session_start_utc = start

    with caplog.at_level(logging.INFO, logger="trade_pnl_summary_test"):
        bot._log_session_summary("completed", cycles=0, orders_sent=0, errors=0)

    pnl_lines = [record.getMessage() for record in caplog.records if "[Trade P&L]" in record.getMessage()]
    assert len(pnl_lines) == 1
    assert "realized_gross=$+6.00" in pnl_lines[0]
    assert "account_fee_activity_net=$-0.04" in pnl_lines[0]
    assert "net_pnl=NOT_CALCULATED" in pnl_lines[0]
    assert "coverage=complete_for_recorded_closed_trades" in pnl_lines[0]
    assert bot.portfolio_manager.reconciled == 1
    assert len(bot.portfolio_manager.fees_reconciled) == 1
    # The regular account-equity line remains separate and is still emitted on early return.
    assert any("[Session Summary]" in record.getMessage() for record in caplog.records)
