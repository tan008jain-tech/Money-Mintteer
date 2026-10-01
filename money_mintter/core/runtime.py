from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from money_mintter.brokers.base import Broker
from money_mintter.core.health import HealthRegistry, HealthStatus, Watchdog, WatchdogAction
from money_mintter.core.recovery import RecoveryManager, ReconciliationResult
from money_mintter.domain import ManagedTrade, WalletLedger
from money_mintter.execution.guard import ExecutionAction, ExecutionGuard
from money_mintter.storage.postgres import PostgresSnapshotStore


@dataclass(frozen=True, slots=True)
class RuntimeState:
    watchdog_action: WatchdogAction
    reconciliation: ReconciliationResult
    live_execution_allowed: bool


class RuntimeSupervisor:
    """Fail-closed coordination of health, persistence, restart reconciliation, and live gating."""

    def __init__(
        self,
        *,
        broker: Broker,
        wallet: WalletLedger,
        trade_provider,
        store: PostgresSnapshotStore,
        health: HealthRegistry,
        execution_guard: ExecutionGuard,
    ) -> None:
        self.broker = broker
        self.wallet = wallet
        self.trade_provider = trade_provider
        self.store = store
        self.health = health
        self.watchdog = Watchdog(health)
        self.execution_guard = execution_guard
        self.recovery = RecoveryManager()

    def checkpoint(self) -> None:
        snapshot = self.recovery.capture(
            broker=self.broker,
            wallet=self.wallet,
            open_trades=self.trade_provider(),
        )
        self.store.save_snapshot(snapshot)
        self.store.append_event("runtime_checkpoint", snapshot.to_dict())

    def reconcile_after_restart(self, now: datetime) -> ReconciliationResult:
        snapshot = self.store.load_latest()
        if snapshot is None:
            result = ReconciliationResult(False, ("no persisted runtime snapshot",))
        else:
            result = self.recovery.reconcile(snapshot, self.broker)

        self.health.heartbeat(
            "reconciliation",
            status=HealthStatus.HEALTHY if result.ok else HealthStatus.FAILED,
            detail="; ".join(result.reasons),
            now=now,
        )
        self.refresh_execution_guard(now)
        return result

    def refresh_execution_guard(self, now: datetime) -> RuntimeState:
        action = self.watchdog.evaluate(now)
        reconciliation_snapshot = self.health.snapshot(now)
        reconciliation_ok = any(
            component.name == "reconciliation" and component.status is HealthStatus.HEALTHY
            for component in reconciliation_snapshot
        )
        health_ok = all(
            component.status is HealthStatus.HEALTHY
            for component in reconciliation_snapshot
            if component.name != "reconciliation"
        )
        data_ok = any(
            component.name == "market_data" and component.status is HealthStatus.HEALTHY
            for component in reconciliation_snapshot
        )

        execution_action = (
            ExecutionAction.RUN
            if action is WatchdogAction.RUN and health_ok and reconciliation_ok and data_ok
            else ExecutionAction.PAUSE
            if action is WatchdogAction.PAUSE
            else ExecutionAction.HALT
        )
        self.execution_guard.update(
            now=now,
            health_ok=health_ok,
            reconciliation_ok=reconciliation_ok,
            data_ok=data_ok,
            watchdog_action=execution_action,
        )
        return RuntimeState(
            watchdog_action=action,
            reconciliation=(
                ReconciliationResult(True, ())
                if reconciliation_ok
                else ReconciliationResult(False, ("runtime gates not green",))
            ),
            live_execution_allowed=execution_action is ExecutionAction.RUN and self.execution_guard.live_enabled,
        )
