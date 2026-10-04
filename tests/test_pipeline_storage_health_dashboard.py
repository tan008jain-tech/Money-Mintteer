from datetime import datetime, timezone

from money_mintter.core.health import HealthRegistry, HealthStatus, Watchdog, WatchdogAction
from money_mintter.core.pipeline import DeepAnalysisPipeline
from money_mintter.dashboard.app import DashboardSnapshot, render_dashboard_html
from money_mintter.domain import Candidate
from money_mintter.storage.state import PositionSnapshot, RuntimeSnapshot, TradeSnapshot
from money_mintter.strategy import MultiTimeframeSnapshot, Timeframe


def test_pipeline_rejects_low_quality_candidate_before_strategy() -> None:
    now = datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)
    candidate = Candidate(
        "ABC", "NYSE", 0.50, 0.70, 0.80, 0.80, now
    )
    snapshot = MultiTimeframeSnapshot(
        as_of=now,
        daily=(),
        four_hour=(),
        one_hour=(),
        fifteen_minute=(),
        entry=(),
        entry_timeframe=Timeframe.FIVE_MIN,
        spread_bps=5,
    )
    result = DeepAnalysisPipeline().evaluate(candidate, snapshot)
    assert not result.opportunity.eligible
    assert result.signal is None


def test_runtime_snapshot_round_trip() -> None:
    snapshot = RuntimeSnapshot(
        captured_at="2026-10-01T14:00:00+00:00",
        broker_cash=9000,
        trading_wallet=9500,
        profit_reserve=500,
        positions=(PositionSnapshot("ABC", 10, 100),),
        open_trades=(
            TradeSnapshot("ABC", "BUY", 10, 100, 98, 110, "2026-10-01T14:00:00+00:00", "entry-1"),
        ),
    )
    decoded = RuntimeSnapshot.from_json(snapshot.to_json())
    assert decoded == snapshot


def test_watchdog_fails_closed_on_stale_component() -> None:
    registry = HealthRegistry(stale_after_seconds=30)
    t0 = datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)
    registry.heartbeat("market_data", now=t0)
    assert Watchdog(registry).evaluate(t0) is WatchdogAction.RUN
    assert Watchdog(registry).evaluate(t0.replace(minute=1)) is WatchdogAction.HALT


def test_degraded_component_pauses() -> None:
    registry = HealthRegistry()
    t0 = datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)
    registry.heartbeat("broker", status=HealthStatus.DEGRADED, now=t0)
    assert Watchdog(registry).evaluate(t0) is WatchdogAction.PAUSE


def test_dashboard_renders_as_read_only_control_room() -> None:
    snapshot = DashboardSnapshot("RUN", 9500, 500, 9000, (), (), (), ())
    html = render_dashboard_html(snapshot)
    assert "Money Mintter Control Room" in html
    assert snapshot.system_action in html
