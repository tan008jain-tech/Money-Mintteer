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


def test_market_session_core_open_filters_opening_noise() -> None:
    session = MarketSession("NYSE", "America/New_York", time(9, 30), time(16, 0))
    engine = SessionEngine([session])
    # 9:30 AM ET is 14:30 UTC
    open_moment = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)
    assert session.is_open(open_moment)
    # With 15 min opening buffer, 9:35 AM ET (14:35 UTC) is not core open
    early_moment = datetime(2026, 1, 5, 14, 35, tzinfo=timezone.utc)
    assert not session.is_core_open(early_moment, opening_buffer_minutes=15)
    assert not engine.is_market_core_open("NYSE", early_moment, opening_buffer_minutes=15)

    # 9:50 AM ET (14:50 UTC) is core open
    core_moment = datetime(2026, 1, 5, 14, 50, tzinfo=timezone.utc)
    assert session.is_core_open(core_moment, opening_buffer_minutes=15)
    assert engine.is_market_core_open("NYSE", core_moment, opening_buffer_minutes=15)


def test_crypto_always_open_session() -> None:
    engine = SessionEngine.default()
    # Crypto should be open on weekends and any time
    weekend_moment = datetime(2026, 1, 10, 3, 0, tzinfo=timezone.utc)  # Saturday
    assert engine.is_market_open("CRYPTO", weekend_moment)


def test_commodity_futures_session() -> None:
    session = MarketSession("COMMODITY", "America/New_York", time(18, 0), time(17, 0), futures_hours=True)
    engine = SessionEngine([session])
    # Saturday should be closed
    sat_moment = datetime(2026, 1, 10, 15, 0, tzinfo=timezone.utc)
    assert not engine.is_market_open("COMMODITY", sat_moment)
    # Sunday evening (after 18:00 ET = 23:00 UTC) should be open
    sun_moment = datetime(2026, 1, 11, 23, 30, tzinfo=timezone.utc)
    assert engine.is_market_open("COMMODITY", sun_moment)

