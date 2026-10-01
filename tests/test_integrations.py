from datetime import datetime, timezone
import tempfile

from money_mintter.calendar.service import ExchangeCalendarService
from money_mintter.core.health import HealthRegistry, HealthStatus
from money_mintter.data.csv import CandleCsvLoader, CsvDataError
from money_mintter.execution.guard import ExecutionAction, ExecutionGuard


def test_execution_guard_fails_closed_until_all_gates_are_green() -> None:
    guard = ExecutionGuard(live_enabled=True)
    now = datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)

    try:
        guard.authorize(now, 1)
    except Exception as exc:
        assert "safety state" in str(exc)
    else:
        raise AssertionError("guard should block stale state")

    guard.update(
        now=now,
        health_ok=True,
        reconciliation_ok=True,
        data_ok=True,
        watchdog_action=ExecutionAction.RUN,
    )
    guard.authorize(now, 1)


def test_csv_loader_rejects_out_of_order_data() -> None:
    with tempfile.NamedTemporaryFile("w+", suffix=".csv", delete=False) as handle:
        handle.write("timestamp,open,high,low,close,volume\n")
        handle.write("2026-10-01T14:01:00+00:00,100,101,99,100,1000\n")
        handle.write("2026-10-01T14:00:00+00:00,100,101,99,100,1000\n")
        path = handle.name

    try:
        CandleCsvLoader().load(path, "ABC")
    except CsvDataError as exc:
        assert "ordered" in str(exc)
    else:
        raise AssertionError("out-of-order data should be rejected")


def test_calendar_service_requires_optional_dependency_only_when_used() -> None:
    service = ExchangeCalendarService({"NYSE": "XNYS"})
    now = datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)
    # Either the optional dependency is installed and returns a boolean,
    # or the service correctly explains that the optional dependency is absent.
    try:
        result = service.is_open("NYSE", now)
    except RuntimeError as exc:
        assert "exchange-calendars" in str(exc)
    else:
        assert isinstance(result, bool)


def test_health_registry_tracks_degraded_component() -> None:
    registry = HealthRegistry()
    now = datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)
    registry.heartbeat("database", status=HealthStatus.DEGRADED, now=now)
    assert registry.snapshot(now)[0].status is HealthStatus.DEGRADED
