from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from zoneinfo import ZoneInfo

from money_mintter.strategy import Candle, StrategyInput


@dataclass(frozen=True, slots=True)
class SessionLevels:
    session_date: date
    high: float
    low: float


class PreviousSessionContext:
    """Derive prior available trading-session high/low levels from intraday candles."""

    def __init__(
        self,
        candles: list[Candle],
        *,
        timezone_name: str = "UTC",
        spread_bps: float = 0.0,
    ) -> None:
        if spread_bps < 0:
            raise ValueError("spread cannot be negative")
        self.timezone = ZoneInfo(timezone_name)
        self.spread_bps = spread_bps

        grouped: dict[date, list[Candle]] = {}
        for candle in candles:
            local_date = candle.timestamp.astimezone(self.timezone).date()
            grouped.setdefault(local_date, []).append(candle)

        ordered_dates = sorted(grouped)
        self._levels_by_date: dict[date, SessionLevels] = {
            session_date: SessionLevels(
                session_date,
                max(candle.high for candle in grouped[session_date]),
                min(candle.low for candle in grouped[session_date]),
            )
            for session_date in ordered_dates
        }
        self._previous_by_date: dict[date, SessionLevels | None] = {}
        previous: SessionLevels | None = None
        for session_date in ordered_dates:
            self._previous_by_date[session_date] = previous
            previous = self._levels_by_date[session_date]

    def __call__(self, index: int, candles: list[Candle]) -> StrategyInput | None:
        if not 0 <= index < len(candles):
            raise IndexError("candle index out of range")
        local_date = candles[index].timestamp.astimezone(self.timezone).date()
        previous = self._previous_by_date.get(local_date)
        if previous is None:
            return None
        return StrategyInput(
            prior_day_low=previous.low,
            prior_day_high=previous.high,
            spread_bps=self.spread_bps,
        )
