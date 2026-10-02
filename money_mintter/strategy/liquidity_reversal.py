from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from money_mintter.domain import Side, TradeSignal
from money_mintter.strategy.candles import Candle
from money_mintter.strategy.timeframes import MultiTimeframeSnapshot, Timeframe


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    entry_timeframe: Timeframe = Timeframe.FIVE_MIN
    entry_lookback_bars: int = 120
    one_hour_level_lookback: int = 30
    fifteen_minute_level_lookback: int = 30
    structure_bars: int = 8
    sweep_search_bars: int = 30
    max_bars_after_sweep: int = 8
    max_bars_after_mss: int = 5
    atr_period: int = 14
    min_sweep_penetration_atr: float = 0.05
    min_sweep_penetration_pct: float = 0.0003
    max_sweep_depth_atr: float = 1.50
    min_rejection_wick_fraction: float = 0.25
    min_displacement_body_fraction: float = 0.50
    min_relative_volume: float = 1.00
    require_volume_confirmation: bool = True
    max_spread_bps: float = 20.0
    stop_buffer_atr: float = 0.05
    max_stop_atr: float = 2.00
    max_chase_atr: float = 0.75
    min_rr: float = 1.50
    preferred_rr: float = 2.50
    max_target_rr: float = 5.00
    min_confidence: float = 0.62

    def __post_init__(self) -> None:
        if self.entry_timeframe not in (Timeframe.THREE_MIN, Timeframe.FIVE_MIN):
            raise ValueError("entry timeframe must be 3m or 5m")
        if self.entry_lookback_bars < self.sweep_search_bars:
            raise ValueError("entry lookback must cover sweep search")
        if self.one_hour_level_lookback < 6 or self.fifteen_minute_level_lookback < 6:
            raise ValueError("higher-timeframe level lookback is too short")
        if self.structure_bars < 3 or self.atr_period < 2:
            raise ValueError("invalid structure/ATR configuration")
        if self.sweep_search_bars < 3 or self.max_bars_after_sweep < 1:
            raise ValueError("invalid sweep timing configuration")
        if self.max_bars_after_mss < 1:
            raise ValueError("confirmation window must be positive")
        if not 0 < self.min_sweep_penetration_atr <= self.max_sweep_depth_atr:
            raise ValueError("invalid sweep depth configuration")
        if not 0 < self.min_rejection_wick_fraction <= 1:
            raise ValueError("invalid rejection-wick threshold")
        if not 0 < self.min_displacement_body_fraction <= 1:
            raise ValueError("invalid displacement threshold")
        if self.min_relative_volume <= 0:
            raise ValueError("relative-volume threshold must be positive")
        if self.max_spread_bps <= 0 or self.stop_buffer_atr < 0:
            raise ValueError("invalid spread/stop-buffer configuration")
        if self.max_stop_atr <= 0 or self.max_chase_atr <= 0:
            raise ValueError("invalid stop/chase configuration")
        if self.min_rr <= 0 or self.preferred_rr < self.min_rr:
            raise ValueError("preferred RR must be >= minimum RR")
        if self.max_target_rr < self.preferred_rr:
            raise ValueError("maximum target RR must be >= preferred RR")
        if not 0 < self.min_confidence <= 1:
            raise ValueError("minimum confidence must be in (0, 1]")


@dataclass(frozen=True, slots=True)
class SetupAnalysis:
    passed: bool
    score: float
    reason: str
    sweep_index: int | None = None
    mss_index: int | None = None
    trigger_index: int | None = None
    entry_price: float | None = None
    stop_price: float | None = None
    target_price: float | None = None
    risk_per_share: float | None = None
    reward_risk: float | None = None
    confluences: tuple[str, ...] = ()
    vetoes: tuple[str, ...] = ()
    side: Side | None = None


class LiquiditySweepReversalStrategy:
    VERSION = "lsr.mtf.v2"

    def __init__(self, config: StrategyConfig | None = None) -> None:
        self.config = config or StrategyConfig()

    def analyze(self, snapshot: MultiTimeframeSnapshot) -> SetupAnalysis:
        long_result = self.analyze_side(snapshot, Side.BUY)
        short_result = self.analyze_side(snapshot, Side.SELL)
        passing = [result for result in (long_result, short_result) if result.passed]
        if passing:
            return max(passing, key=lambda result: result.score)
        best = max((long_result, short_result), key=lambda result: result.score)
        return SetupAnalysis(
            False,
            best.score,
            f"no actionable setup: long={long_result.reason}; short={short_result.reason}",
            vetoes=tuple(sorted(set(long_result.vetoes + short_result.vetoes))),
        )

    def analyze_side(self, snapshot: MultiTimeframeSnapshot, side: Side) -> SetupAnalysis:
        c = self.config
        if snapshot.entry_timeframe is not c.entry_timeframe:
            return SetupAnalysis(False, 0.0, "entry timeframe mismatch", vetoes=("entry_timeframe",), side=side)
        if snapshot.spread_bps > c.max_spread_bps:
            return SetupAnalysis(False, 0.0, "spread too wide", vetoes=("spread",), side=side)

        entry = snapshot.entry[-c.entry_lookback_bars:]
        if len(entry) < max(c.structure_bars + c.atr_period + 2, 16):
            return SetupAnalysis(False, 0.0, "insufficient entry-timeframe history", side=side)
        if len(snapshot.one_hour) < 6 or len(snapshot.fifteen_minute) < 6:
            return SetupAnalysis(False, 0.0, "insufficient 1H/15M context", side=side)

        daily_bias = _trend_bias(snapshot.daily)
        four_hour_bias = _trend_bias(snapshot.four_hour)
        one_hour_bias = _trend_bias(snapshot.one_hour)
        fifteen_bias = _trend_bias(snapshot.fifteen_minute)

        if _is_opposite(daily_bias, side) and _is_opposite(four_hour_bias, side):
            return SetupAnalysis(
                False,
                0.0,
                "4H and daily filters oppose trade direction",
                vetoes=("broad_trend_conflict",),
                side=side,
            )

        sweep_levels = _liquidity_levels(
            snapshot,
            side,
            one_hour_limit=c.one_hour_level_lookback,
            fifteen_limit=c.fifteen_minute_level_lookback,
        )
        if not sweep_levels:
            return SetupAnalysis(False, 0.0, "no 1H/15M liquidity levels available", vetoes=("no_context_liquidity",), side=side)

        atr_values = _atr_series(entry, c.atr_period)
        sweep_start = max(1, len(entry) - c.sweep_search_bars)
        candidates: list[tuple[int, float, str]] = []

        for index in range(sweep_start, len(entry)):
            atr = atr_values[index - 1] if index > 0 else atr_values[index]
            if atr is None:
                continue
            candle = entry[index]
            wick_fraction = (
                candle.lower_wick / candle.range
                if side is Side.BUY and candle.range > 0
                else candle.upper_wick / candle.range
                if side is Side.SELL and candle.range > 0
                else 0.0
            )
            for level, source in sweep_levels:
                penetration = level - candle.low if side is Side.BUY else candle.high - level
                threshold = max(atr * c.min_sweep_penetration_atr, level * c.min_sweep_penetration_pct)
                reclaimed = candle.close > level if side is Side.BUY else candle.close < level
                if penetration >= threshold and penetration <= atr * c.max_sweep_depth_atr and reclaimed and wick_fraction >= c.min_rejection_wick_fraction:
                    candidates.append((index, level, source))

        if not candidates:
            return SetupAnalysis(False, 0.0, "no entry-timeframe sweep of 1H/15M liquidity", vetoes=("no_entry_sweep",), side=side)

        newest_failure: SetupAnalysis | None = None
        for sweep_index, swept_level, source in reversed(candidates):
            result = self._evaluate_candidate(
                snapshot,
                entry,
                atr_values,
                sweep_index,
                swept_level,
                source,
                side,
                one_hour_bias,
                fifteen_bias,
                daily_bias,
                four_hour_bias,
            )
            if result.passed:
                if result.trigger_index == len(entry) - 1:
                    return result
                newest_failure = SetupAnalysis(
                    False,
                    result.score,
                    "setup completed on an earlier entry candle",
                    sweep_index=result.sweep_index,
                    mss_index=result.mss_index,
                    trigger_index=result.trigger_index,
                    confluences=result.confluences,
                    vetoes=result.vetoes + ("stale_trigger",),
                    side=side,
                )
            elif newest_failure is None:
                newest_failure = result

        return newest_failure or SetupAnalysis(False, 0.0, "no actionable setup", side=side)

    def signal(self, snapshot: MultiTimeframeSnapshot) -> TradeSignal | None:
        result = self.analyze(snapshot)
        if not result.passed or result.side is None or result.entry_price is None or result.stop_price is None or result.target_price is None:
            return None
        return TradeSignal(
            symbol=snapshot.entry[-1].symbol,
            side=result.side,
            entry_price=result.entry_price,
            stop_price=result.stop_price,
            target_price=result.target_price,
            confidence=result.score,
            timestamp=snapshot.as_of,
            reason=result.reason,
            strategy_version=self.VERSION,
        )

    def _evaluate_candidate(
        self,
        snapshot: MultiTimeframeSnapshot,
        entry: tuple[Candle, ...],
        atr_values: list[float | None],
        sweep_index: int,
        swept_level: float,
        source: str,
        side: Side,
        one_hour_bias: int,
        fifteen_bias: int,
        daily_bias: int,
        four_hour_bias: int,
    ) -> SetupAnalysis:
        c = self.config
        sweep = entry[sweep_index]
        atr = atr_values[sweep_index - 1] if sweep_index > 0 else atr_values[sweep_index]
        if atr is None:
            return SetupAnalysis(False, 0.0, "entry ATR unavailable", sweep_index=sweep_index, vetoes=("atr",), side=side)

        structure = entry[max(0, sweep_index - c.structure_bars):sweep_index]
        if len(structure) < c.structure_bars:
            return SetupAnalysis(False, 0.0, "insufficient local structure before sweep", sweep_index=sweep_index, vetoes=("structure_history",), side=side)

        structure_level = max(bar.high for bar in structure) if side is Side.BUY else min(bar.low for bar in structure)
        mss_end = min(len(entry) - 1, sweep_index + c.max_bars_after_sweep)
        mss_index = None
        for index in range(sweep_index + 1, mss_end + 1):
            if side is Side.BUY and entry[index].close > structure_level:
                mss_index = index
                break
            if side is Side.SELL and entry[index].close < structure_level:
                mss_index = index
                break
        if mss_index is None:
            return SetupAnalysis(False, 0.0, "sweep did not produce local MSS", sweep_index=sweep_index, vetoes=("no_local_mss",), side=side)

        mss = entry[mss_index]
        relative_volume = _relative_volume(entry, mss_index, 20)
        displacement = (
            (side is Side.BUY and mss.bullish and mss.close > structure_level)
            or (side is Side.SELL and mss.bearish and mss.close < structure_level)
        ) and mss.body_fraction >= c.min_displacement_body_fraction and (not c.require_volume_confirmation or relative_volume >= c.min_relative_volume)
        if not displacement:
            return SetupAnalysis(False, 0.0, "MSS lacked displacement", sweep_index=sweep_index, mss_index=mss_index, vetoes=("no_displacement",), side=side)

        trigger_end = min(len(entry) - 1, mss_index + c.max_bars_after_mss)
        trigger_index = None
        for index in range(mss_index + 1, trigger_end + 1):
            if side is Side.BUY and entry[index].high > mss.high:
                trigger_index = index
                break
            if side is Side.SELL and entry[index].low < mss.low:
                trigger_index = index
                break
        if trigger_index is None:
            return SetupAnalysis(False, 0.0, "entry confirmation trigger was not broken", sweep_index=sweep_index, mss_index=mss_index, vetoes=("no_confirmation",), side=side)

        trigger = entry[trigger_index]
        confirmed = (
            side is Side.BUY and trigger.bullish and trigger.close > mss.high
        ) or (
            side is Side.SELL and trigger.bearish and trigger.close < mss.low
        )
        if not confirmed:
            return SetupAnalysis(False, 0.0, "confirmation candle did not close through trigger", sweep_index=sweep_index, mss_index=mss_index, trigger_index=trigger_index, vetoes=("weak_confirmation",), side=side)

        chase = trigger.close - mss.high if side is Side.BUY else mss.low - trigger.close
        if chase > c.max_chase_atr * atr:
            return SetupAnalysis(False, 0.0, "entry is too extended after confirmation", sweep_index=sweep_index, mss_index=mss_index, trigger_index=trigger_index, vetoes=("chase",), side=side)

        entry_price = trigger.close
        if side is Side.BUY:
            stop_price = min(sweep.low, mss.low) - c.stop_buffer_atr * atr
            risk = entry_price - stop_price
            opposing_levels = _liquidity_levels(snapshot, Side.SELL, one_hour_limit=c.one_hour_level_lookback, fifteen_limit=c.fifteen_minute_level_lookback)
        else:
            stop_price = max(sweep.high, mss.high) + c.stop_buffer_atr * atr
            risk = stop_price - entry_price
            opposing_levels = _liquidity_levels(snapshot, Side.BUY, one_hour_limit=c.one_hour_level_lookback, fifteen_limit=c.fifteen_minute_level_lookback)

        if risk <= 0 or risk > c.max_stop_atr * atr:
            return SetupAnalysis(False, 0.0, "entry stop is too wide", sweep_index=sweep_index, mss_index=mss_index, trigger_index=trigger_index, vetoes=("stop_too_wide",), side=side)

        target_level = _nearest_opposing_level(entry_price, opposing_levels, side)
        available_rr = abs(target_level - entry_price) / risk if target_level is not None else c.preferred_rr
        if target_level is not None and available_rr < c.min_rr:
            return SetupAnalysis(False, 0.0, "nearest opposing liquidity leaves insufficient room", sweep_index=sweep_index, mss_index=mss_index, trigger_index=trigger_index, vetoes=("insufficient_room",), side=side)

        target_rr = min(c.max_target_rr, max(c.preferred_rr, available_rr))
        target_price = entry_price + target_rr * risk if side is Side.BUY else entry_price - target_rr * risk
        reward_risk = abs(target_price - entry_price) / risk

        alignment = _alignment_score(side, daily_bias, four_hour_bias, one_hour_bias, fifteen_bias)
        rejection_score = min(1.0, (sweep.lower_wick if side is Side.BUY else sweep.upper_wick) / max(sweep.range, 1e-9) / c.min_rejection_wick_fraction)
        displacement_score = min(1.0, mss.body_fraction / c.min_displacement_body_fraction)
        volume_score = min(1.0, relative_volume / c.min_relative_volume)
        rr_score = min(1.0, reward_risk / c.max_target_rr)

        confluences = [
            f"{source} liquidity sweep",
            "local MSS",
            f"{snapshot.entry_timeframe.value} displacement",
            f"{snapshot.entry_timeframe.value} confirmation",
        ]
        if one_hour_bias == _side_value(side):
            confluences.append("1H trend aligned")
        if fifteen_bias == _side_value(side):
            confluences.append("15M structure aligned")
        if four_hour_bias == _side_value(side):
            confluences.append("4H filter aligned")
        if daily_bias == _side_value(side):
            confluences.append("daily filter aligned")

        score = min(0.99, 0.18 * rejection_score + 0.27 * displacement_score + 0.15 * volume_score + 0.20 * rr_score + 0.20 * alignment)
        if score < c.min_confidence:
            return SetupAnalysis(False, score, "multi-timeframe setup quality below threshold", sweep_index=sweep_index, mss_index=mss_index, trigger_index=trigger_index, confluences=tuple(confluences), vetoes=("quality",), side=side)
        if not all(isfinite(value) for value in (entry_price, stop_price, target_price, score)):
            return SetupAnalysis(False, 0.0, "non-finite strategy output", vetoes=("finite",), side=side)

        return SetupAnalysis(
            True,
            score,
            f"{side.value} {snapshot.entry_timeframe.value} liquidity-sweep reversal with 1H/15M entry context",
            sweep_index,
            mss_index,
            trigger_index,
            entry_price,
            stop_price,
            target_price,
            risk,
            reward_risk,
            tuple(confluences),
            (),
            side,
        )


def _side_value(side: Side) -> int:
    return 1 if side is Side.BUY else -1


def _is_opposite(bias: int, side: Side) -> bool:
    return bias != 0 and bias != _side_value(side)


def _trend_bias(candles: tuple[Candle, ...]) -> int:
    if len(candles) < 6:
        return 0
    highs = _swing_points(candles, is_high=True)
    lows = _swing_points(candles, is_high=False)
    if len(highs) >= 2 and len(lows) >= 2:
        if highs[-1] > highs[-2] and lows[-1] > lows[-2]:
            return 1
        if highs[-1] < highs[-2] and lows[-1] < lows[-2]:
            return -1
    lookback = min(8, len(candles) - 1)
    change = candles[-1].close - candles[-1 - lookback].close
    return 1 if change > 0 else -1 if change < 0 else 0


def _swing_points(candles: tuple[Candle, ...], *, is_high: bool, span: int = 2) -> list[float]:
    points: list[float] = []
    for index in range(span, len(candles) - span):
        value = candles[index].high if is_high else candles[index].low
        neighbors = [
            (bar.high if is_high else bar.low)
            for bar in candles[index - span:index]
        ] + [
            (bar.high if is_high else bar.low)
            for bar in candles[index + 1:index + span + 1]
        ]
        if is_high and value > max(neighbors):
            points.append(value)
        if not is_high and value < min(neighbors):
            points.append(value)
    return points


def _liquidity_levels(
    snapshot: MultiTimeframeSnapshot,
    side: Side,
    *,
    one_hour_limit: int,
    fifteen_limit: int,
) -> list[tuple[float, str]]:
    levels: list[tuple[float, str]] = []
    if snapshot.daily:
        prior_daily = snapshot.daily[-1]
        levels.append(
            (
                prior_daily.low if side is Side.BUY else prior_daily.high,
                "prior-day",
            )
        )

    for source, candles in (
        ("1H", snapshot.one_hour[-one_hour_limit:]),
        ("15M", snapshot.fifteen_minute[-fifteen_limit:]),
    ):
        for level in _swing_points(candles, is_high=side is Side.SELL):
            levels.append((level, source))

    result: list[tuple[float, str]] = []
    for level, source in levels:
        if not any(
            abs(level - existing) <= max(level, existing) * 1e-6
            for existing, _ in result
        ):
            result.append((level, source))
    return result


def _nearest_opposing_level(entry_price: float, levels: list[tuple[float, str]], side: Side) -> float | None:
    if side is Side.BUY:
        candidates = [level for level, _ in levels if level > entry_price]
        return min(candidates) if candidates else None
    candidates = [level for level, _ in levels if level < entry_price]
    return max(candidates) if candidates else None


def _alignment_score(
    side: Side,
    daily_bias: int,
    four_hour_bias: int,
    one_hour_bias: int,
    fifteen_bias: int,
) -> float:
    target = _side_value(side)
    score = 0.50
    for bias, weight in (
        (daily_bias, 0.10),
        (four_hour_bias, 0.15),
        (one_hour_bias, 0.25),
        (fifteen_bias, 0.15),
    ):
        if bias == target:
            score += weight
        elif bias == -target:
            score -= weight
    return max(0.0, min(1.0, score))


def _has_fvg(
    candles: tuple[Candle, ...],
    start: int,
    end: int,
    side: Side,
) -> bool:
    for index in range(max(2, start), min(len(candles), end + 1)):
        if (
            side is Side.BUY
            and candles[index - 2].high < candles[index].low
            and candles[index].bullish
        ):
            return True
        if (
            side is Side.SELL
            and candles[index - 2].low > candles[index].high
            and candles[index].bearish
        ):
            return True
    return False


def _relative_volume(candles: tuple[Candle, ...], index: int, window: int) -> float:
    start = max(0, index - window)
    baseline = [candle.volume for candle in candles[start:index] if candle.volume > 0]
    if not baseline or candles[index].volume <= 0:
        return 0.0
    average_volume = sum(baseline) / len(baseline)
    return candles[index].volume / average_volume if average_volume else 0.0


def _atr_series(candles: tuple[Candle, ...], period: int) -> list[float | None]:
    values: list[float | None] = [None] * len(candles)
    true_ranges: list[float] = []
    for index, candle in enumerate(candles):
        previous_close = candles[index - 1].close if index else candle.close
        true_range = max(
            candle.high - candle.low,
            abs(candle.high - previous_close),
            abs(candle.low - previous_close),
        )
        true_ranges.append(true_range)
        if index + 1 >= period:
            values[index] = sum(true_ranges[index + 1 - period:index + 1]) / period
    return values
