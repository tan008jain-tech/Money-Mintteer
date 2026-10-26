from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Sequence

from money_mintter.strategy.candles import Candle
from money_mintter.core.movement import compute_q1, compute_q3, is_same_session


@dataclass(frozen=True, slots=True)
class Layer4CandidateScore:
    symbol: str
    score: float
    q1: float | None
    q3: float | None
    s_move: float
    s_act: float | None
    has_activity_baseline: bool


def compute_median_volume_baseline(bars: Sequence[Candle], t: int, window_size: int = 20) -> float | None:
    """Calculate the volume baseline as the median of the preceding `window_size` (default 20)
    completed same-session bars, excluding the evaluated bar at index t.
    Requires 20 consecutive valid same-session bars ending at t-1, and continuity between
    the evaluated bar at t and the preceding bar at t-1.
    """
    if t < window_size or not bars or t >= len(bars):
        return None

    start_idx = t - window_size
    # Check continuity among baseline bars
    for i in range(start_idx + 1, t):
        if not is_same_session(bars[i - 1], bars[i]):
            return None

    # Check continuity between the evaluated bar at t and the preceding baseline bar at t-1
    if not is_same_session(bars[t - 1], bars[t]):
        return None

    volumes: list[float] = []
    for i in range(start_idx, t):
        v = float(bars[i].volume)
        if not isfinite(v) or v < 0:
            return None
        volumes.append(v)

    if not volumes:
        return None

    sorted_v = sorted(volumes)
    mid = len(sorted_v) // 2
    if len(sorted_v) % 2 != 0:
        baseline_median = sorted_v[mid]
    else:
        baseline_median = (sorted_v[mid - 1] + sorted_v[mid]) / 2.0

    if not isfinite(baseline_median) or baseline_median <= 0:
        return None

    return baseline_median


def select_layer4_candidates(
    daily_pool: list[str],
    bars_dict: dict[str, Sequence[Candle]],
    max_candidates: int = 8,
) -> list[Layer4CandidateScore]:
    """Select and rank up to `max_candidates` (hard-capped at 8) new-entry candidates from the validated Layer 2
    daily pool using Layer 4 movement and activity criteria.
    
    Rules:
    - Never select a symbol outside input pool.
    - Deduplicate daily_pool preserving original order.
    - Movement eligibility: valid q1 >= 1.5 OR valid q3 >= 2.5.
    - S1 = min(1, q1/3), S3 = min(1, q3/5), S_move = max(valid S1, valid S3).
    - S_act = min(1, (current_volume / baseline_volume) / 4) when baseline is positive, finite and valid.
    - Global fallback: If any eligible candidate lacks a valid activity baseline, use range-only ranking
      consistently for the entire set: S_rank = S_move. Otherwise S_rank = 0.30 * S_act + 0.70 * S_move.
    - Rank descending by score, break ties alphabetically by symbol, return at most min(max_candidates, 8) without padding.
    """
    if max_candidates <= 0 or not daily_pool or not bars_dict:
        return []

    # Deduplicate daily_pool preserving original order
    unique_pool = list(dict.fromkeys(daily_pool))

    eligible_records: list[dict[str, object]] = []
    any_missing_activity_baseline = False

    for symbol in unique_pool:
        if symbol not in bars_dict:
            continue
        bars = bars_dict[symbol]
        if not bars:
            continue

        t = len(bars) - 1
        q1 = compute_q1(bars, t)
        q3 = compute_q3(bars, t)

        pass_q1 = q1 is not None and q1 >= 1.5
        pass_q3 = q3 is not None and q3 >= 2.5
        if not pass_q1 and not pass_q3:
            continue

        s1 = min(1.0, q1 / 3.0) if q1 is not None else None
        s3 = min(1.0, q3 / 5.0) if q3 is not None else None
        valid_s = [s for s in (s1, s3) if s is not None]
        if not valid_s:
            continue
        s_move = max(valid_s)

        baseline_vol = compute_median_volume_baseline(bars, t, window_size=20)
        has_act = False
        s_act = None
        if baseline_vol is not None and baseline_vol > 0:
            curr_vol = float(bars[t].volume)
            if isfinite(curr_vol) and curr_vol >= 0:
                s_act = min(1.0, (curr_vol / baseline_vol) / 4.0)
                if isfinite(s_act):
                    has_act = True

        if not has_act:
            any_missing_activity_baseline = True

        eligible_records.append({
            "symbol": symbol,
            "q1": q1,
            "q3": q3,
            "s_move": s_move,
            "s_act": s_act,
            "has_act": has_act,
        })

    if not eligible_records:
        return []

    results: list[Layer4CandidateScore] = []
    for rec in eligible_records:
        symbol = rec["symbol"]
        s_move = rec["s_move"]
        s_act = rec["s_act"]
        q1 = rec["q1"]
        q3 = rec["q3"]
        has_act = rec["has_act"]

        if any_missing_activity_baseline:
            score = s_move
        else:
            score = 0.30 * (s_act if s_act is not None else 0.0) + 0.70 * s_move

        results.append(
            Layer4CandidateScore(
                symbol=symbol,
                score=score,
                q1=q1,
                q3=q3,
                s_move=s_move,
                s_act=s_act,
                has_activity_baseline=has_act,
            )
        )

    results.sort(key=lambda item: (-item.score, item.symbol))

    hard_cap = min(max_candidates, 8)
    return results[:hard_cap]
