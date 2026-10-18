from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo


@dataclass(frozen=True, slots=True)
class MarketSession:
    name: str
    timezone: str
    open_time: time
    close_time: time
    always_open: bool = False
    futures_hours: bool = False

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def is_open(self, moment_utc: datetime) -> bool:
        if moment_utc.tzinfo is None:
            raise ValueError("moment must be timezone-aware")
        local = moment_utc.astimezone(self.tz)
        if self.always_open:
            return True
        if self.futures_hours:
            weekday = local.weekday()  # 0=Mon, ..., 5=Sat, 6=Sun
            current_time = local.time().replace(tzinfo=None)
            if weekday == 5:  # Saturday - closed
                return False
            if weekday == 6:  # Sunday - open after 18:00
                return current_time >= time(18, 0)
            if weekday == 4:  # Friday - close at 17:00
                return current_time < time(17, 0)
            # Monday - Thursday: maintenance break 17:00 - 18:00
            if time(17, 0) <= current_time < time(18, 0):
                return False
            return True

        if local.weekday() >= 5:
            return False
        current = local.time().replace(tzinfo=None)
        return self.open_time <= current < self.close_time

    def is_core_open(
        self,
        moment_utc: datetime,
        opening_buffer_minutes: int = 15,
        closing_buffer_minutes: int = 15,
    ) -> bool:
        if self.always_open:
            return True
        if not self.is_open(moment_utc):
            return False
        local = moment_utc.astimezone(self.tz)
        current = local.time().replace(tzinfo=None)
        open_dt = datetime.combine(local.date(), self.open_time) + timedelta(minutes=opening_buffer_minutes)
        close_dt = datetime.combine(local.date(), self.close_time) - timedelta(minutes=closing_buffer_minutes)
        current_dt = datetime.combine(local.date(), current)
        return open_dt.time() <= current <= close_dt.time()


class SessionEngine:
    """Timezone-aware session gate with optional exchange-calendar support."""

    def __init__(self, sessions: list[MarketSession], calendar_service=None) -> None:
        self.sessions = tuple(sessions)
        self.calendar_service = calendar_service

    def open_markets(self, moment_utc: datetime) -> list[str]:
        result: list[str] = []
        for session in self.sessions:
            if not session.is_open(moment_utc):
                continue
            if self.calendar_service is not None and not session.always_open and not session.futures_hours and not self.calendar_service.is_open(session.name, moment_utc):
                continue
            result.append(session.name)
        return result

    def is_market_open(self, market: str, moment_utc: datetime) -> bool:
        return market in self.open_markets(moment_utc)

    def is_market_core_open(
        self,
        market: str,
        moment_utc: datetime,
        opening_buffer_minutes: int = 15,
        closing_buffer_minutes: int = 15,
    ) -> bool:
        for session in self.sessions:
            if session.name == market:
                if not session.is_open(moment_utc):
                    return False
                if self.calendar_service is not None and not session.always_open and not session.futures_hours and not self.calendar_service.is_open(session.name, moment_utc):
                    return False
                return session.is_core_open(moment_utc, opening_buffer_minutes, closing_buffer_minutes)
        return False

    @classmethod
    def default(cls) -> "SessionEngine":
        return cls(
            [
                MarketSession("NYSE", "America/New_York", time(9, 30), time(16, 0)),
                MarketSession("NASDAQ", "America/New_York", time(9, 30), time(16, 0)),
                MarketSession("LSE", "Europe/London", time(8, 0), time(16, 30)),
                MarketSession("TSE", "Asia/Tokyo", time(9, 0), time(15, 0)),
                MarketSession("CRYPTO", "UTC", time(0, 0), time(23, 59), always_open=True),
                MarketSession("COMMODITY", "America/New_York", time(18, 0), time(17, 0), futures_hours=True),
            ]
        )
