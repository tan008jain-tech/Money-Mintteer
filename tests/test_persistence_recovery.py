from datetime import datetime, timezone

from money_mintter.brokers.paper import PaperBroker
from money_mintter.core.recovery import RecoveryManager
from money_mintter.domain import ManagedTrade, Quote, Side, WalletLedger


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.rows = []

    def execute(self, sql, params=None):
        self.connection.calls.append((sql, params))
        if sql.strip().startswith("SELECT snapshot"):
            self.rows = self.connection.select_rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def close(self):
        pass


class FakeConnection:
    def __init__(self):
        self.calls = []
        self.commits = 0
        self.rollbacks = 0
        self.select_rows = []

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_recovery_snapshot_matches_paper_broker() -> None:
    broker = PaperBroker(10_000, slippage_bps=0, commission_bps=0)
    wallet = WalletLedger(10_000)
    now = datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)
    broker.submit_market_order("ABC", Side.BUY, 10, Quote("ABC", 99.9, 100.1, 100, now))

    trade = ManagedTrade(
        symbol="ABC",
        side=Side.BUY,
        quantity=10,
        entry_price=100.1,
        stop_price=98,
        target_price=110,
        opened_at=now,
        entry_fill_id="entry-1",
    )
    snapshot = RecoveryManager().capture(
        broker=broker,
        wallet=wallet,
        open_trades={"ABC": trade},
    )
    result = RecoveryManager.reconcile(snapshot, broker)
    assert result.ok


def test_fake_postgres_store_uses_parameterized_inserts() -> None:
    from money_mintter.storage.postgres import PostgresSnapshotStore
    from money_mintter.storage.state import RuntimeSnapshot

    connection = FakeConnection()
    now = datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)
    snapshot = RuntimeSnapshot(
        captured_at=now.isoformat(),
        broker_cash=1000,
        trading_wallet=1000,
        profit_reserve=0,
        positions=(),
        open_trades=(),
    )
    store = PostgresSnapshotStore(lambda: connection)
    store.initialize()
    store.save_snapshot(snapshot)
    store.append_event("test_event", {"hello": "world"})

    assert connection.commits == 3
    sql_statements = [item[0] for item in connection.calls]
    assert all("%s" in sql for sql in sql_statements if "INSERT" in sql)
