from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum


class ExecutionBlocked(RuntimeError):
    """Order was blocked by a safety gate."""


class ExecutionAction(str, Enum):
    RUN = "RUN"
    PAUSE = "PAUSE"
    HALT = "HALT"


@dataclass(slots=True)
class ExecutionGuard:
    live_enabled: bool = False
    health_ok: bool = False
    reconciliation_ok: bool = False
    data_ok: bool = False
    watchdog_action: ExecutionAction = ExecutionAction.HALT
    updated_at: datetime | None = None

    def update(
        self,
        *,
        now: datetime,
        health_ok: bool,
        reconciliation_ok: bool,
        data_ok: bool,
        watchdog_action: ExecutionAction,
    ) -> None:
        self.health_ok = health_ok
        self.reconciliation_ok = reconciliation_ok
        self.data_ok = data_ok
        self.watchdog_action = watchdog_action
        self.updated_at = now

    def authorize(self, now: datetime, quantity: float) -> None:
        if not self.live_enabled:
            raise ExecutionBlocked("live execution is disabled")
        if quantity <= 0:
            raise ExecutionBlocked("order quantity must be positive")
        if self.updated_at is None or now - self.updated_at > timedelta(seconds=30):
            raise ExecutionBlocked("execution safety state is stale")
        if not self.health_ok:
            raise ExecutionBlocked("health gate is not green")
        if not self.reconciliation_ok:
            raise ExecutionBlocked("broker reconciliation is not green")
        if not self.data_ok:
            raise ExecutionBlocked("market-data gate is not green")
        if self.watchdog_action is not ExecutionAction.RUN:
            raise ExecutionBlocked(f"watchdog action is {self.watchdog_action.value}")
