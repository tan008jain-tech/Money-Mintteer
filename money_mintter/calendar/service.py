from __future__ import annotations

from datetime import datetime, timezone


class CalendarUnavailable(RuntimeError):
    """Required exchange-calendar dependency is not installed."""


class ExchangeCalendarService:
    """Adapter around the open-source exchange_calendars package.

    The dependency is imported lazily so local unit tests do not require it.
    It provides regular-session schedules and holiday/early-close knowledge for
    many exchanges instead of maintaining fragile hand-written holiday tables.
    """

    def __init__(self, market_calendars: dict[str, str]) -> None:
        self.market_calendars = dict(market_calendars)
        self._loaded: dict[str, object] = {}

    def _calendar(self, market: str):
        calendar_name = self.market_calendars.get(market)
        if calendar_name is None:
            raise KeyError(f"no calendar configured for market {market}")
        if market in self._loaded:
            return self._loaded[market]

        try:
            import exchange_calendars as xcals
        except ImportError as exc:
            raise CalendarUnavailable(
                "install the optional exchange-calendars dependency"
            ) from exc

        calendar = xcals.get_calendar(calendar_name)
        self._loaded[market] = calendar
        return calendar

    def session_bounds(
        self,
        market: str,
        moment_utc: datetime,
    ) -> tuple[datetime, datetime] | None:
        if moment_utc.tzinfo is None:
            raise ValueError("moment must be timezone-aware")

        moment_utc = moment_utc.astimezone(timezone.utc)
        calendar = self._calendar(market)
        session_date = moment_utc.date().isoformat()

        schedule = calendar.schedule.loc[session_date:session_date]
        if schedule.empty:
            return None

        row = schedule.iloc[0]
        market_open = row["open"].to_pydatetime().astimezone(timezone.utc)
        market_close = row["close"].to_pydatetime().astimezone(timezone.utc)
        return market_open, market_close

    def is_open(self, market: str, moment_utc: datetime) -> bool:
        bounds = self.session_bounds(market, moment_utc)
        if bounds is None:
            return False
        market_open, market_close = bounds
        return market_open <= moment_utc.astimezone(timezone.utc) < market_close

    def is_trading_day(self, market: str, date_text: str) -> bool:
        calendar = self._calendar(market)
        schedule = calendar.schedule.loc[date_text:date_text]
        return not schedule.empty
