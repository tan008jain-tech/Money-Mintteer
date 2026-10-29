from __future__ import annotations

from datetime import datetime, timezone, date
from money_mintter.brokers.paper import PaperBroker
from money_mintter.core.health import HealthRegistry
from money_mintter.core.runtime import RuntimeSupervisor
from money_mintter.domain import WalletLedger
from money_mintter.execution.guard import ExecutionGuard
from money_mintter.storage.state import RuntimeSnapshot
from money_mintter.strategy import ICTStrategy


class FakeStore:
    def __init__(self):
        self.snapshot = None
        self.events = []

    def save_snapshot(self, snapshot):
        # Test real JSON serialization/deserialization roundtrip
        json_str = snapshot.to_json()
        self.snapshot = RuntimeSnapshot.from_json(json_str)

    def load_latest(self):
        return self.snapshot

    def append_event(self, event_type, payload):
        self.events.append((event_type, payload))


def test_ict_strategy_persistence_roundtrip() -> None:
    strategy1 = ICTStrategy()
    zone_key = ("BUY", "disp_fvg", 105.5, 104.0)
    strategy1._used_zones.add(zone_key)
    strategy1._last_trading_date = date(2026, 10, 28)

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
        strategy=strategy1,
    )

    supervisor.checkpoint()
    assert store.snapshot is not None
    assert isinstance(store.snapshot.strategy_state, dict)
    assert len(store.snapshot.strategy_state["used_zones"]) == 1
    assert store.snapshot.strategy_state["last_trading_date"] == "2026-10-28"

    # Simulate restart with new strategy instance
    strategy2 = ICTStrategy()
    supervisor2 = RuntimeSupervisor(
        broker=broker,
        wallet=wallet,
        trade_provider=lambda: {},
        store=store,
        health=health,
        execution_guard=guard,
        strategy=strategy2,
    )

    now = datetime(2026, 10, 28, 15, 0, tzinfo=timezone.utc)
    result = supervisor2.reconcile_after_restart(now)
    assert result.ok

    assert zone_key in strategy2._used_zones
    assert len(list(zone_key)) == 4
    assert strategy2._last_trading_date == date(2026, 10, 28)


def test_legacy_snapshot_compatibility() -> None:
    # Legacy snapshot without strategy_state (or default empty tuple)
    now = datetime(2026, 10, 28, 15, 0, tzinfo=timezone.utc)
    legacy_snapshot = RuntimeSnapshot(
        captured_at=now.isoformat(),
        broker_cash=10_000.0,
        wallet=10_000.0,
        positions=(),
        open_trades=(),
        strategy_state=(),
    )

    store = FakeStore()
    store.snapshot = legacy_snapshot

    strategy = ICTStrategy()
    broker = PaperBroker(10_000)
    wallet = WalletLedger(10_000)
    health = HealthRegistry()
    guard = ExecutionGuard(live_enabled=True)

    supervisor = RuntimeSupervisor(
        broker=broker,
        wallet=wallet,
        trade_provider=lambda: {},
        store=store,
        health=health,
        execution_guard=guard,
        strategy=strategy,
    )

    result = supervisor.reconcile_after_restart(now)
    assert result.ok
    assert strategy._used_zones == set()
    assert strategy._last_trading_date is None


def test_no_snapshot_startup() -> None:
    store = FakeStore()
    strategy = ICTStrategy()
    broker = PaperBroker(10_000)
    wallet = WalletLedger(10_000)
    health = HealthRegistry()
    guard = ExecutionGuard(live_enabled=True)

    supervisor = RuntimeSupervisor(
        broker=broker,
        wallet=wallet,
        trade_provider=lambda: {},
        store=store,
        health=health,
        execution_guard=guard,
        strategy=strategy,
    )

    now = datetime(2026, 10, 28, 15, 0, tzinfo=timezone.utc)
    result = supervisor.reconcile_after_restart(now)
    assert not result.ok
    assert "no persisted runtime snapshot" in result.reasons
