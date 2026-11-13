from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta, date as dt_date
from zoneinfo import ZoneInfo
from typing import Any, Sequence

from money_mintter.strategy.candles import Candle
from money_mintter.strategy.timeframes import MultiTimeframeSeries


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_CHICAGO_TZ = ZoneInfo("America/Chicago")
_TRUSTED_ACQUISITION_TOKEN = object()
_IEX_TOKEN = _TRUSTED_ACQUISITION_TOKEN  # Backward-compatible private alias.


@dataclass(frozen=True, slots=True)
class ICTAcquisitionResult:
    symbol: str
    b1_candles: tuple[Candle, ...]
    b5_candles: tuple[Candle, ...]
    feed: str
    b1_success: bool
    b5_success: bool
    error_message: str | None = None
    _token: Any = None


def _create_verified_acquisition_result(
    symbol: str,
    b1_candles: tuple[Candle, ...],
    b5_candles: tuple[Candle, ...],
    feed: str,
    b1_success: bool,
    b5_success: bool,
    error_message: str | None = None,
) -> ICTAcquisitionResult:
    """
    Internal factory for creating verified ICTAcquisitionResult with module-private token.
    Note: Python cannot provide absolute in-process security against deliberately malicious code
    inspecting private attributes or globals, but this prevents ordinary accidental bypass.
    """
    return ICTAcquisitionResult(
        symbol=symbol,
        b1_candles=b1_candles,
        b5_candles=b5_candles,
        feed=feed,
        b1_success=b1_success,
        b5_success=b5_success,
        error_message=error_message,
        _token=_TRUSTED_ACQUISITION_TOKEN,
    )


@dataclass(frozen=True, slots=True)
class ICTAdapterResult:
    symbol: str
    as_of: datetime
    feed: str
    b1: tuple[dict[str, Any], ...]
    b5: tuple[dict[str, Any], ...]
    b15: tuple[dict[str, Any], ...]
    dr5: dict[dt_date, tuple[int, int]]
    dr15: dict[dt_date, tuple[int, int]]
    all_dates: list[dt_date]
    closed_at: datetime
    constituent_count: int
    api_success: bool
    source_coverage_known: bool
    b15_coverage: tuple[dict[str, Any], ...] = ()


def dt_to_ns(dt: datetime) -> int:
    if dt.tzinfo is None:
        raise ValueError(f"naive datetime encountered: {dt}")
    dt_utc = dt.astimezone(timezone.utc)
    delta = dt_utc - _EPOCH
    total_us = (
        (delta.days * 86400 + delta.seconds) * 1_000_000
        + delta.microseconds
    )
    return total_us * 1000


def bar_trading_date(dt_chicago: datetime) -> dt_date:
    """
    Upstream trading date semantics:
    If time is at or after 17:00 CT, advance trading date by one calendar day.
    """
    d = dt_chicago.date()
    if dt_chicago.hour >= 17:
        d = d + timedelta(days=1)
    return d


def candle_to_ict_dict(candle: Candle) -> dict[str, Any]:
    time_ns = dt_to_ns(candle.timestamp)
    dt_chicago = candle.timestamp.astimezone(_CHICAGO_TZ)
    return {
        "time_ns": time_ns,
        "open": float(candle.open),
        "high": float(candle.high),
        "low": float(candle.low),
        "close": float(candle.close),
        "volume": float(candle.volume),
        "hour": dt_chicago.hour,
        "minute": dt_chicago.minute,
        "date": bar_trading_date(dt_chicago),
    }


def _validate_and_sort_candles(candles: Sequence[Candle], expected_symbol: str) -> tuple[Candle, ...]:
    if not candles:
        return ()
    
    seen = set()
    prev_dt = None
    for c in candles:
        if c.timestamp.tzinfo is None:
            raise ValueError(f"naive timestamp encountered in candle for {c.symbol}: {c.timestamp}")
        if c.symbol != expected_symbol:
            raise ValueError(f"symbol mismatch: expected {expected_symbol}, got {c.symbol}")
        if c.timestamp in seen:
            raise ValueError(f"duplicate candle timestamp detected: {c.timestamp}")
        seen.add(c.timestamp)
        
        if prev_dt is not None and c.timestamp <= prev_dt:
            raise ValueError(f"candles are not strictly sorted by timestamp: {prev_dt} >= {c.timestamp}")
        prev_dt = c.timestamp

    return tuple(candles)


def _apply_lookahead_filter(candles: tuple[Candle, ...], timeframe_minutes: int, as_of: datetime) -> tuple[Candle, ...]:
    filtered = []
    duration = timedelta(minutes=timeframe_minutes)
    for c in candles:
        if c.timestamp + duration <= as_of:
            filtered.append(c)
    return tuple(filtered)


def build_dr(bars: tuple[dict[str, Any], ...]) -> dict[dt_date, tuple[int, int]]:
    dr: dict[dt_date, list[int]] = {}
    for i, bar in enumerate(bars):
        d = bar["date"]
        if d not in dr:
            dr[d] = [i, i + 1]
        else:
            dr[d][1] = i + 1
    return {d: (indices[0], indices[1]) for d, indices in dr.items()}


def adapt_market_data(
    acquisition_result: ICTAcquisitionResult,
    as_of: datetime,
) -> ICTAdapterResult:
    """Adapt validated single-feed ICT acquisition data with strict lookahead protection."""
    if as_of.tzinfo is None:
        raise ValueError("as_of timestamp must be timezone-aware")

    # Enforce acquisition provenance verification via module-private object-identity token
    if acquisition_result._token is not _TRUSTED_ACQUISITION_TOKEN:
        raise ValueError("ICT acquisition provenance check failed: result did not originate from the trusted acquisition path.")

    feed_value = (acquisition_result.feed or "").strip().lower()
    if feed_value not in ("iex", "sip"):
        raise ValueError(f"Unsupported ICT acquisition feed rejected by adapter: {acquisition_result.feed}")

    if not acquisition_result.b1_success or not acquisition_result.b5_success:
        raise ValueError(f"Unsuccessful or incomplete acquisition result rejected: b1_success={acquisition_result.b1_success}, b5_success={acquisition_result.b5_success}, error={acquisition_result.error_message}")

    # Dataset completeness validation: reject empty streams despite successful API response
    if not acquisition_result.b1_candles:
        raise ValueError("Dataset completeness rejection: 1m stream is empty despite successful acquisition.")
    if not acquisition_result.b5_candles:
        raise ValueError("Dataset completeness rejection: 5m stream is empty despite successful acquisition.")

    symbol = acquisition_result.symbol
    v_b1 = _validate_and_sort_candles(acquisition_result.b1_candles, symbol)
    v_b5 = _validate_and_sort_candles(acquisition_result.b5_candles, symbol)

    # Apply lookahead protection independently
    filtered_b1 = _apply_lookahead_filter(v_b1, 1, as_of)
    filtered_b5 = _apply_lookahead_filter(v_b5, 5, as_of)

    # Dataset completeness validation: reject streams becoming empty after lookahead filtering
    if not filtered_b1:
        raise ValueError("Dataset completeness rejection: 1m stream became empty after lookahead filtering.")
    if not filtered_b5:
        raise ValueError("Dataset completeness rejection: 5m stream became empty after lookahead filtering.")

    # Construct b15 from filtered b5 candles using MultiTimeframeSeries infrastructure
    b15_candles = tuple()
    if filtered_b5:
        mts = MultiTimeframeSeries.from_5m(list(filtered_b5), timezone_name="America/New_York")
        b15_candles = _apply_lookahead_filter(mts.fifteen_minute.bars, 15, as_of)

    # Convert to ICT dictionaries
    b1_dicts = tuple(candle_to_ict_dict(c) for c in filtered_b1)
    b5_dicts = tuple(candle_to_ict_dict(c) for c in filtered_b5)
    
    # Map 5m timestamps for fast constituency verification
    b5_timestamps = {c.timestamp for c in filtered_b5}

    # Track observed 5-minute constituent count and exact alignment per 15-minute bucket
    b15_coverage_list = []
    filtered_b15_list = []
    
    for c15 in b15_candles:
        d15 = candle_to_ict_dict(c15)
        bucket_start = c15.timestamp
        expected_ts = [
            bucket_start,
            bucket_start + timedelta(minutes=5),
            bucket_start + timedelta(minutes=10),
        ]
        constituents = [c for c in filtered_b5 if bucket_start <= c.timestamp < bucket_start + timedelta(minutes=15)]
        constituent_count = len(constituents)
        misaligned = any(ts not in b5_timestamps for ts in expected_ts) or (constituent_count != 3)
        
        status = "complete" if constituent_count == 3 and not misaligned else ("partial" if constituent_count > 0 else "unobserved")
        
        coverage_entry = {
            "bucket_start": bucket_start,
            "constituent_count": constituent_count,
            "status": status,
            "misaligned": misaligned,
        }
        b15_coverage_list.append(coverage_entry)

        if constituent_count == 3 and not misaligned:
            d15["constituent_count"] = constituent_count
            filtered_b15_list.append({k: v for k, v in d15.items() if k not in ("bucket_start", "misaligned")})

    b15_coverage = tuple(b15_coverage_list)
    b15_dicts = tuple(filtered_b15_list)
    dr5 = build_dr(b5_dicts)
    dr15 = build_dr(b15_dicts)
    all_dates = sorted(list(dr5.keys()))

    constituent_count = len(filtered_b5)

    max_closes = []
    if filtered_b1:
        max_closes.append(max(c.timestamp + timedelta(minutes=1) for c in filtered_b1))
    if filtered_b5:
        max_closes.append(max(c.timestamp + timedelta(minutes=5) for c in filtered_b5))
    if b15_candles:
        max_closes.append(max(c.timestamp + timedelta(minutes=15) for c in b15_candles))
    
    closed_at = max(max_closes) if max_closes else as_of

    return ICTAdapterResult(
        symbol=symbol,
        as_of=as_of,
        feed=feed_value,
        b1=b1_dicts,
        b5=b5_dicts,
        b15=b15_dicts,
        dr5=dr5,
        dr15=dr15,
        all_dates=all_dates,
        closed_at=closed_at,
        constituent_count=constituent_count,
        api_success=True,
        source_coverage_known=False,
        b15_coverage=b15_coverage,
    )
