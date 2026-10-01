from datetime import datetime, time, timezone

from money_mintter.core.sessions import MarketSession, SessionEngine


class FakeCalendar:
    def __init__(self, holidays):
        self.holidays = holidays

    def is_open(self, market, moment):
        return moment.date().isoformat() not in self.holidays


def test_market_session_respects_named_timezone() -> None:
    session = MarketSession("NYSE", "America/New_York", time(9, 30), time(16, 0))
    utc_open = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)
    assert session.is_open(utc_open)


def test_session_engine_can_overlay_exchange_calendar() -> None:
    sessions = [
        MarketSession("NYSE", "America/New_York", time(9, 30), time(16, 0))
    ]
    service = FakeCalendar({"2026-01-05"})
    engine = SessionEngine(sessions, calendar_service=service)
    moment = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)
    assert engine.open_markets(moment) == []
