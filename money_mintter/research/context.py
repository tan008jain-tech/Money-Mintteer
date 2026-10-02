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
    """Build causal prior-session and intraday liquidity levels."""

    def __init__(
        self,
        candles: list[Candle],
        *,
        timezone_name: str = "UTC",
        spread_bps: float = 0.0,
        intraday_lookback_bars: int = 30,
        swing_span: int = 2,
        max_intraday_levels: int = 6,
    ) -> None:
        if spread_bps < 0:
            raise ValueError("spread cannot be negative")
        if intraday_lookback_bars < 10:
            raise ValueError("intraday lookback must be at least 10 bars")
        if swing_span < 1:
            raise ValueError("swing span must be positive")
        if max_intraday_levels < 1:
            raise ValueError("max intraday levels must be positive")

        self.timezone = ZoneInfo(timezone_name)
        self.spread_bps = spread_bps
        self.intraday_lookback_bars = intraday_lookback_bars
        self.swing_span = swing_span
        self.max_intraday_levels = max_intraday_levels

        grouped: dict[date, list[Candle]] = {}
        for candle in candles:
            local_date = candle.timestamp.astimezone(self.timezone).date()
            grouped.setdefault(local_date, []).append(candle)

        ordered_dates = sorted(grouped)
        self._levels_by_date = {
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

    def __call__(
        self,
        index: int,
        candles: list[Candle],
    ) -> StrategyInput | None:
        if not 0 <= index < len(candles):
            raise IndexError("candle index out of range")

        local_date = candles[index].timestamp.astimezone(self.timezone).date()
        previous = self._previous_by_date.get(local_date)
        if previous is None:
            return None

        start = max(0, index - self.intraday_lookback_bars)
        prior_bars = candles[start:index]
        span = self.swing_span

        swing_lows: list[tuple[int, float]] = []
        swing_highs: list[tuple[int, float]] = []
        for pivot in range(start + span, index - span):
            left = candles[pivot - span : pivot]
            right = candles[pivot + 1 : pivot + span + 1]
            pivot_bar = candles[pivot]

            if all(pivot_bar.low < bar.low for bar in left + right):
                swing_lows.append((pivot, pivot_bar.low))
            if all(pivot_bar.high > bar.high for bar in left + right):
                swing_highs.append((pivot, pivot_bar.high))

        swing_lows = swing_lows[-self.max_intraday_levels:]
        swing_highs = swing_highs[-self.max_intraday_levels:]

        lows = [round(level, 8) for _, level in swing_lows]
        highs = [round(level, 8) for _, level in swing_highs]

        # Add current rolling extremes only when they are not already represented.
        if prior_bars:
            lows.append(round(min(bar.low for bar in prior_bars), 8))
            highs.append(round(max(bar.high for bar in prior_bars), 8))

        return StrategyInput(
            prior_day_low=previous.low,
            prior_day_high=previous.high,
            spread_bps=self.spread_bps,
            liquidity_lows=tuple(dict.fromkeys(lows)),
            liquidity_highs=tuple(dict.fromkeys(highs)),
        )
