from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from zoneinfo import ZoneInfo

from money_mintter.strategy.candles import Candle


class Timeframe(str, Enum):
    THREE_MIN = "3m"
    FIVE_MIN = "5m"
    FIFTEEN_MIN = "15m"
    ONE_HOUR = "1h"
    FOUR_HOUR = "4h"
    DAILY = "1d"

    @property
    def minutes(self) -> int:
        return {
            Timeframe.THREE_MIN: 3,
            Timeframe.FIVE_MIN: 5,
            Timeframe.FIFTEEN_MIN: 15,
            Timeframe.ONE_HOUR: 60,
            Timeframe.FOUR_HOUR: 240,
            Timeframe.DAILY: 1440,
        }[self]


@dataclass(frozen=True, slots=True)
class CandleSeries:
    timeframe: Timeframe
    bars: tuple[Candle, ...]
    close_times: tuple[datetime, ...]

    def closed_at(self, as_of: datetime, *, limit: int | None = None) -> tuple[Candle, ...]:
        count = bisect_right(self.close_times, as_of)
        if count == 0:
            return ()
        start = 0 if limit is None else max(0, count - limit)
        return self.bars[start:count]


@dataclass(frozen=True, slots=True)
class MultiTimeframeSnapshot:
    as_of: datetime
    daily: tuple[Candle, ...]
    four_hour: tuple[Candle, ...]
    one_hour: tuple[Candle, ...]
    fifteen_minute: tuple[Candle, ...]
    entry: tuple[Candle, ...]
    entry_timeframe: Timeframe
    spread_bps: float = 0.0

    @property
    def one_hour_levels(self) -> tuple[float, ...]:
        if not self.one_hour:
            return ()
        highs = [b.high for b in self.one_hour]
        lows = [b.low for b in self.one_hour]
        return (max(highs), min(lows), sum(highs) / len(highs), sum(lows) / len(lows))

    @property
    def fifteen_minute_levels(self) -> tuple[float, ...]:
        if not self.fifteen_minute:
            return ()
        highs = [b.high for b in self.fifteen_minute]
        lows = [b.low for b in self.fifteen_minute]
        return (max(highs), min(lows), sum(highs) / len(highs), sum(lows) / len(lows))

    @property
    def projected_levels(self) -> tuple[dict[str, Any], ...]:
        levels = []
        for p in self.one_hour_levels:
            levels.append({"source": "1H", "price": p})
        for p in self.fifteen_minute_levels:
            levels.append({"source": "15M", "price": p})
        return tuple(levels)

    @property
    def candle_bias_summary(self) -> str:
        if not self.entry:
            return "No 5M entry candles"
        recent = self.entry[-5:]
        bullish_count = sum(1 for c in recent if c.bullish)
        bearish_count = sum(1 for c in recent if c.bearish)
        total_vol = sum(c.volume for c in recent)
        avg_body = sum(c.body for c in recent) / len(recent)
        latest = recent[-1]
        pressure = "Buyer Control 🟢" if bullish_count >= bearish_count and latest.close >= latest.open else "Seller Control 🔴"
        return (
            f"5M Chart Bias: {pressure} | Bullish: {bullish_count}/5 | "
            f"Bearish: {bearish_count}/5 | Avg Body: ${avg_body:.2f} | "
            f"Volume: {total_vol:.0f} | Close: ${latest.close:.2f} "
            f"(Upper Wick: ${latest.upper_wick:.2f}, Lower Wick: ${latest.lower_wick:.2f})"
        )


@dataclass(frozen=True, slots=True)
class MultiTimeframeSeries:
    source_5m: tuple[Candle, ...]
    daily: CandleSeries
    four_hour: CandleSeries
    one_hour: CandleSeries
    fifteen_minute: CandleSeries
    entry: CandleSeries
    timezone_name: str
    spread_bps: float = 0.0

    @classmethod
    def from_1m(
        cls,
        candles: list[Candle],
        *,
        entry_timeframe: Timeframe = Timeframe.FIVE_MIN,
        timezone_name: str = "America/New_York",
        spread_bps: float = 0.0,
    ) -> "MultiTimeframeSeries":
        raise ValueError("1-minute timeframe is strictly forbidden due to high noise. Use 5-minute base bars via from_5m.")

    @classmethod
    def from_5m(
        cls,
        candles: list[Candle],
        *,
        entry_timeframe: Timeframe = Timeframe.FIVE_MIN,
        timezone_name: str = "America/New_York",
        spread_bps: float = 0.0,
    ) -> "MultiTimeframeSeries":
        if not candles:
            raise ValueError("multi-timeframe series requires 5-minute candles")
        if entry_timeframe not in (Timeframe.THREE_MIN, Timeframe.FIVE_MIN):
            raise ValueError("entry timeframe must be 3m or 5m")
        timezone = ZoneInfo(timezone_name)
        ordered = tuple(sorted(candles, key=lambda candle: candle.timestamp))
        return cls(
            source_5m=ordered,
            daily=_resample(ordered, Timeframe.DAILY, timezone),
            four_hour=_resample(ordered, Timeframe.FOUR_HOUR, timezone),
            one_hour=_resample(ordered, Timeframe.ONE_HOUR, timezone),
            fifteen_minute=_resample(ordered, Timeframe.FIFTEEN_MIN, timezone),
            entry=_resample(ordered, entry_timeframe, timezone),
            timezone_name=timezone_name,
            spread_bps=spread_bps,
        )

    @property
    def source_end(self) -> datetime:
        return self.source_5m[-1].timestamp + timedelta(minutes=5)

    def snapshots(self, *, lookback_bars: int = 96) -> tuple[MultiTimeframeSnapshot, ...]:
        result: list[MultiTimeframeSnapshot] = []
        for entry_bar in self.entry.bars:
            as_of = _close_time(entry_bar.timestamp, self.entry.timeframe)
            if as_of > self.source_end:
                continue
            result.append(self.snapshot_at(as_of, lookback_bars=lookback_bars))
        return tuple(result)

    def snapshot_at(
        self,
        as_of: datetime,
        *,
        lookback_bars: int = 96,
    ) -> MultiTimeframeSnapshot:
        return MultiTimeframeSnapshot(
            as_of=as_of,
            daily=self.daily.closed_at(as_of, limit=lookback_bars),
            four_hour=self.four_hour.closed_at(as_of, limit=lookback_bars),
            one_hour=self.one_hour.closed_at(as_of, limit=lookback_bars),
            fifteen_minute=self.fifteen_minute.closed_at(as_of, limit=lookback_bars),
            entry=self.entry.closed_at(as_of, limit=lookback_bars),
            entry_timeframe=self.entry.timeframe,
            spread_bps=self.spread_bps,
        )


def _resample(
    candles: tuple[Candle, ...],
    timeframe: Timeframe,
    timezone: ZoneInfo,
) -> CandleSeries:
    buckets: dict[datetime, list[Candle]] = {}
    for candle in candles:
        bucket = _bucket_start(candle.timestamp, timeframe, timezone)
        buckets.setdefault(bucket, []).append(candle)

    bars: list[Candle] = []
    close_times: list[datetime] = []
    for bucket in sorted(buckets):
        source = buckets[bucket]
        bars.append(
            Candle(
                symbol=source[0].symbol,
                timestamp=bucket,
                open=source[0].open,
                high=max(candle.high for candle in source),
                low=min(candle.low for candle in source),
                close=source[-1].close,
                volume=sum(candle.volume for candle in source),
            )
        )
        close_times.append(_close_time(bucket, timeframe))

    return CandleSeries(timeframe, tuple(bars), tuple(close_times))


def _bucket_start(
    timestamp: datetime,
    timeframe: Timeframe,
    timezone: ZoneInfo,
) -> datetime:
    local = timestamp.astimezone(timezone)
    if timeframe is Timeframe.DAILY:
        return datetime(local.year, local.month, local.day, tzinfo=timezone)

    anchor = datetime(local.year, local.month, local.day, 9, 30, tzinfo=timezone)
    elapsed = int((local - anchor).total_seconds() // 60)
    bucket_number = elapsed // timeframe.minutes
    return anchor + timedelta(minutes=bucket_number * timeframe.minutes)


def _close_time(start: datetime, timeframe: Timeframe) -> datetime:
    if timeframe is Timeframe.DAILY:
        return start + timedelta(days=1)
    return start + timedelta(minutes=timeframe.minutes)
