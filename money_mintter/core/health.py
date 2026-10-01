from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum


class HealthStatus(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class WatchdogAction(str, Enum):
    RUN = "RUN"
    PAUSE = "PAUSE"
    HALT = "HALT"


@dataclass(frozen=True, slots=True)
class ComponentHealth:
    name: str
    status: HealthStatus
    last_heartbeat: datetime
    detail: str = ""


class HealthRegistry:
    def __init__(self, stale_after_seconds: int = 30) -> None:
        if stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be positive")
        self.stale_after = timedelta(seconds=stale_after_seconds)
        self._components: dict[str, ComponentHealth] = {}

    def heartbeat(
        self,
        name: str,
        *,
        status: HealthStatus = HealthStatus.HEALTHY,
        detail: str = "",
        now: datetime | None = None,
    ) -> None:
        if not name.strip():
            raise ValueError("component name cannot be empty")
        moment = now or datetime.now(timezone.utc)
        if moment.tzinfo is None:
            raise ValueError("heartbeat time must be timezone-aware")
        self._components[name] = ComponentHealth(name, status, moment, detail)

    def snapshot(self, now: datetime | None = None) -> tuple[ComponentHealth, ...]:
        moment = now or datetime.now(timezone.utc)
        return tuple(
            ComponentHealth(
                component.name,
                (
                    HealthStatus.FAILED
                    if moment - component.last_heartbeat > self.stale_after
                    else component.status
                ),
                component.last_heartbeat,
                component.detail,
            )
            for component in sorted(self._components.values(), key=lambda item: item.name)
        )


class Watchdog:
    """Fail-closed runtime policy: degraded dependencies pause, hard failures halt."""

    def __init__(self, registry: HealthRegistry) -> None:
        self.registry = registry

    def evaluate(self, now: datetime | None = None) -> WatchdogAction:
        components = self.registry.snapshot(now)
        if not components:
            return WatchdogAction.HALT

        if any(item.status is HealthStatus.FAILED for item in components):
            return WatchdogAction.HALT

        if any(item.status in (HealthStatus.DEGRADED, HealthStatus.UNKNOWN) for item in components):
            return WatchdogAction.PAUSE

        return WatchdogAction.RUN
