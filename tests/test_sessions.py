from datetime import datetime, timezone

from money_mintter.core.sessions import SessionEngine


def test_ny_session_uses_timezone_conversion() -> None:
    engine = SessionEngine.default()
    # 14:00 UTC on 1 Oct 2026 is 10:00 in New York (EDT), inside NYSE hours.
    moment = datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)
    assert engine.is_market_open("NYSE", moment)


def test_ny_standard_time_is_also_converted() -> None:
    engine = SessionEngine.default()
    # 14:30 UTC on 5 Jan 2026 is 09:30 in New York (EST), exactly at the open.
    moment = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)
    assert engine.is_market_open("NYSE", moment)


def test_weekend_is_closed() -> None:
    engine = SessionEngine.default()
    saturday = datetime(2026, 10, 3, 14, 0, tzinfo=timezone.utc)
    assert engine.open_markets(saturday) == []
