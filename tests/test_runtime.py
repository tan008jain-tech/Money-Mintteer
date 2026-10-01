from datetime import datetime, timezone

from money_mintter.brokers.paper import PaperBroker
from money_mintter.core.health import HealthRegistry
from money_mintter.core.runtime import RuntimeSupervisor
from money_mintter.domain import WalletLedger
from money_mintter.execution.guard import ExecutionGuard
from money_mintter.storage.state import RuntimeSnapshot


class FakeStore:
    def __init__(self):
        self.snapshot = None
        self.events = []

    def save_snapshot(self, snapshot):
        self.snapshot = snapshot

    def load_latest(self):
        return self.snapshot

    def append_event(self, event_type, payload):
        self.events.append((event_type, payload))


def test_runtime_supervisor_fails_closed_without_snapshot() -> None:
    broker = PaperBroker(10_000)
    wallet = WalletLedger(10_000)
    health = HealthRegistry()
    store = FakeStore()
    guard = ExecutionGuard(live_enabled=True)
    supervisor = RuntimeSupervisor(
        broker=broker,
        wallet=wallet,
        trade_provider=lambda: {},
        store=store,
        health=health,
        execution_guard=guard,
    )

    now = datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)
    result = supervisor.reconcile_after_restart(now)
    assert not result.ok
    assert not guard.health_ok
    assert not supervisor.refresh_execution_guard(now).live_execution_allowed


def test_runtime_checkpoint_persists_snapshot_and_event() -> None:
    broker = PaperBroker(10_000)
    wallet = WalletLedger(10_000)
    health = HealthRegistry()
    store = FakeStore()
    supervisor = RuntimeSupervisor(
        broker=broker,
        wallet=wallet,
        trade_provider=lambda: {},
        store=store,
        health=health,
        execution_guard=ExecutionGuard(live_enabled=False),
    )

    supervisor.checkpoint()
    assert isinstance(store.snapshot, RuntimeSnapshot)
    assert store.events and store.events[0][0] == "runtime_checkpoint"
