from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo


@dataclass(frozen=True, slots=True)
class MarketSession:
    name: str
    timezone: str
    open_time: time
    close_time: time

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def is_open(self, moment_utc: datetime) -> bool:
        if moment_utc.tzinfo is None:
            raise ValueError("moment must be timezone-aware")
        local = moment_utc.astimezone(self.tz)
        if local.weekday() >= 5:
            return False
        current = local.time().replace(tzinfo=None)
        return self.open_time <= current < self.close_time


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
            if self.calendar_service is not None and not self.calendar_service.is_open(session.name, moment_utc):
                continue
            result.append(session.name)
        return result

    def is_market_open(self, market: str, moment_utc: datetime) -> bool:
        return market in self.open_markets(moment_utc)

    @classmethod
    def default(cls) -> "SessionEngine":
        return cls(
            [
                MarketSession("NYSE", "America/New_York", time(9, 30), time(16, 0)),
                MarketSession("LSE", "Europe/London", time(8, 0), time(16, 30)),
                MarketSession("TSE", "Asia/Tokyo", time(9, 0), time(15, 0)),
            ]
        )
