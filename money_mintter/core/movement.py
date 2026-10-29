from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Sequence

from money_mintter.strategy.candles import Candle


def is_same_session(prev_bar: Candle, curr_bar: Candle, max_gap_minutes: float = 1.5) -> bool:
    """Check if two consecutive bars belong to the same continuous session without overnight or gap boundary.

    Limitation: Existing session helpers in sessions.py check exchange open state but do not track intraday bar gap calendars.
    Therefore, session identity is determined via timestamp delta threshold (max_gap_minutes).
    Intraday missing bars exceeding the threshold are treated as session boundaries (timestamp-gap fallback limitation).
    """
    delta = (curr_bar.timestamp - prev_bar.timestamp).total_seconds() / 60.0
    if delta <= 0 or delta > max_gap_minutes:
        return False
    return True


def compute_wilder_atr_14(bars: Sequence[Candle], up_to_index: int) -> float | None:
    """Compute Wilder ATR-14 strictly using completed bars prior to up_to_index (no lookahead),
    respecting session boundaries via timestamp-gap fallback.
    
    Returns None if fewer than 14 valid prior True Range observations exist (no partial ATR fallback).
    """
    if up_to_index < 14 or not bars:
        return None

    limit = min(up_to_index, len(bars))
    tr_list: list[float] = []

    for i in range(limit):
        b = bars[i]
        if i == 0:
            tr = b.high - b.low
        else:
            prev_b = bars[i - 1]
            if is_same_session(prev_b, b):
                tr = max(b.high - b.low, abs(b.high - prev_b.close), abs(b.low - prev_b.close))
            else:
                tr = b.high - b.low
        tr_list.append(tr)

    atr_period = 14
    if len(tr_list) < atr_period:
        return None

    initial_atr = sum(tr_list[:atr_period]) / atr_period
    current_atr = initial_atr
    for tr_val in tr_list[atr_period:]:
        current_atr = (current_atr * (atr_period - 1) + tr_val) / atr_period
    return current_atr


def compute_q1(bars: Sequence[Candle], t: int) -> float | None:
    """Single-bar movement ratio: q1 = (High[t] - Low[t]) / ATR14_before_t.
    Excludes bar t from its own ATR baseline. Requires at least 14 prior ATR bars (t >= 14).
    """
    if t < 14 or t >= len(bars):
        return None
    atr = compute_wilder_atr_14(bars, t)
    if atr is None or atr <= 0:
        return None
    bar = bars[t]
    return (bar.high - bar.low) / atr


def compute_q3(bars: Sequence[Candle], t: int) -> float | None:
    """Three-bar movement ratio: q3 = (max(High[t-2:t]) - min(Low[t-2:t])) / ATR14_before_t_minus_2.
    Requires t-2 >= 14 (so t >= 16), three consecutive valid same-session bars, and excludes the three-bar
    window (t-2, t-1, t) from the ATR baseline.
    """
    if t < 16 or t >= len(bars):
        return None

    b_t2 = bars[t - 2]
    b_t1 = bars[t - 1]
    b_t = bars[t]

    if not is_same_session(b_t2, b_t1) or not is_same_session(b_t1, b_t):
        return None

    atr = compute_wilder_atr_14(bars, t - 2)
    if atr is None or atr <= 0:
        return None

    h_max = max(b_t2.high, b_t1.high, b_t.high)
    l_min = min(b_t2.low, b_t1.low, b_t.low)
    three_bar_range = h_max - l_min

    return three_bar_range / atr
