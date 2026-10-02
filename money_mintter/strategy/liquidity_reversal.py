from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from money_mintter.domain import Side, TradeSignal
from money_mintter.strategy.candles import Candle
from money_mintter.strategy.timeframes import MultiTimeframeSnapshot, Timeframe


_SOURCE_WEIGHT = {
    "15M": 1.0,
    "1H": 2.0,
    "4H": 3.0,
    "prior-day": 4.0,
    "daily": 4.0,
}


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    entry_timeframe: Timeframe = Timeframe.FIVE_MIN
    entry_lookback_bars: int = 120
    one_hour_level_lookback: int = 30
    fifteen_minute_level_lookback: int = 30
    four_hour_level_lookback: int = 12
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
    relative_volume_window: int = 20
    min_relative_volume: float = 1.00
    require_volume_confirmation: bool = False
    max_spread_bps: float = 20.0
    stop_buffer_atr: float = 0.05
    max_stop_atr: float = 2.00
    max_chase_atr: float = 0.75
    liquidity_zone_merge_pct: float = 0.0015
    min_liquidity_strength: float = 1.0

    def __post_init__(self) -> None:
        if self.entry_timeframe not in (Timeframe.THREE_MIN, Timeframe.FIVE_MIN):
            raise ValueError("entry timeframe must be 3m or 5m")
        if self.entry_lookback_bars < self.sweep_search_bars:
            raise ValueError("entry lookback must cover sweep search")
        if min(
            self.one_hour_level_lookback,
            self.fifteen_minute_level_lookback,
            self.four_hour_level_lookback,
        ) < 6:
            raise ValueError("higher-timeframe level lookbacks are too short")
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
        if self.relative_volume_window < 2 or self.min_relative_volume <= 0:
            raise ValueError("invalid relative-volume configuration")
        if self.max_spread_bps <= 0 or self.stop_buffer_atr < 0:
            raise ValueError("invalid spread/stop-buffer configuration")
        if self.max_stop_atr <= 0 or self.max_chase_atr <= 0:
            raise ValueError("invalid stop/chase configuration")
        if not 0 < self.liquidity_zone_merge_pct < 0.05:
            raise ValueError("liquidity-zone merge tolerance is invalid")
        if self.min_liquidity_strength <= 0:
            raise ValueError("minimum liquidity strength must be positive")


@dataclass(frozen=True, slots=True)
class LiquidityZone:
    price: float
    sources: tuple[str, ...]
    strength: float

    @property
    def has_15m(self) -> bool:
        return "15M" in self.sources

    @property
    def has_1h(self) -> bool:
        return "1H" in self.sources

    @property
    def higher_timeframe_confluence(self) -> bool:
        return any(source in self.sources for source in ("1H", "4H", "prior-day", "daily"))


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
    target_percent: float | None = None
    risk_percent: float | None = None
    liquidity_strength: float | None = None
    confluences: tuple[str, ...] = ()
    vetoes: tuple[str, ...] = ()
    side: Side | None = None


class LiquiditySweepReversalStrategy:
    """Multi-timeframe liquidity sweep/reversal strategy for short intraday trades."""

    VERSION = "lsr.mtf.v3"

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

        zones = _liquidity_zones(snapshot, side, c)
        if not zones:
            return SetupAnalysis(False, 0.0, "no actionable 1H/15M liquidity zones", vetoes=("no_context_liquidity",), side=side)

        atr_values = _atr_series(entry, c.atr_period)
        sweep_start = max(1, len(entry) - c.sweep_search_bars)
        candidates: list[tuple[int, LiquidityZone]] = []

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

            for zone in zones:
                penetration = zone.price - candle.low if side is Side.BUY else candle.high - zone.price
                threshold = max(atr * c.min_sweep_penetration_atr, zone.price * c.min_sweep_penetration_pct)
                reclaimed = candle.close > zone.price if side is Side.BUY else candle.close < zone.price
                if (
                    zone.strength >= c.min_liquidity_strength
                    and penetration >= threshold
                    and penetration <= atr * c.max_sweep_depth_atr
                    and reclaimed
                    and wick_fraction >= c.min_rejection_wick_fraction
                ):
                    candidates.append((index, zone))

        if not candidates:
            return SetupAnalysis(False, 0.0, "no entry-timeframe sweep of mapped liquidity", vetoes=("no_entry_sweep",), side=side)

        newest_failure: SetupAnalysis | None = None
        for sweep_index, zone in reversed(candidates):
            result = self._evaluate_candidate(
                snapshot,
                entry,
                atr_values,
                sweep_index,
                zone,
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
        zone: LiquidityZone,
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
        mss_index: int | None = None
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
        relative_volume = _relative_volume(entry, mss_index, c.relative_volume_window)
        displacement = (
            (side is Side.BUY and mss.bullish and mss.close > structure_level)
            or (side is Side.SELL and mss.bearish and mss.close < structure_level)
        ) and (
            mss.body_fraction >= c.min_displacement_body_fraction
            and (
                not c.require_volume_confirmation
                or relative_volume >= c.min_relative_volume
            )
        )
        if not displacement:
            return SetupAnalysis(False, 0.0, "MSS lacked entry-timeframe displacement", sweep_index=sweep_index, mss_index=mss_index, vetoes=("no_displacement",), side=side)

        trigger_end = min(len(entry) - 1, mss_index + c.max_bars_after_mss)
        trigger_index: int | None = None
        for index in range(mss_index + 1, trigger_end + 1):
            if side is Side.BUY and entry[index].high > mss.high:
                trigger_index = index
                break
            if side is Side.SELL and entry[index].low < mss.low:
                trigger_index = index
                break

        if trigger_index is None:
            return SetupAnalysis(False, 0.0, "confirmation trigger was not broken", sweep_index=sweep_index, mss_index=mss_index, vetoes=("no_confirmation",), side=side)

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
            target_zones = _liquidity_zones(snapshot, Side.SELL, c)
        else:
            stop_price = max(sweep.high, mss.high) + c.stop_buffer_atr * atr
            target_zones = _liquidity_zones(snapshot, Side.BUY, c)

        risk = entry_price - stop_price if side is Side.BUY else stop_price - entry_price
        if risk <= 0 or risk > c.max_stop_atr * atr:
            return SetupAnalysis(False, 0.0, "entry stop is too wide", sweep_index=sweep_index, mss_index=mss_index, trigger_index=trigger_index, vetoes=("stop_too_wide",), side=side)

        target_zone = _nearest_opposing_zone(entry_price, target_zones, side)
        if target_zone is None:
            return SetupAnalysis(False, 0.0, "no meaningful opposing liquidity destination", sweep_index=sweep_index, mss_index=mss_index, trigger_index=trigger_index, vetoes=("no_target_liquidity",), side=side)

        target_price = target_zone.price
        target_percent = abs(target_price - entry_price) / entry_price * 100.0
        risk_percent = risk / entry_price * 100.0
        reward_risk = abs(target_price - entry_price) / risk

        alignment = _alignment_score(side, daily_bias, four_hour_bias, one_hour_bias, fifteen_bias)
        rejection_score = min(
            1.0,
            (sweep.lower_wick if side is Side.BUY else sweep.upper_wick)
            / max(sweep.range, 1e-9)
            / c.min_rejection_wick_fraction,
        )
        displacement_score = min(1.0, mss.body_fraction / c.min_displacement_body_fraction)
        volume_score = min(1.0, relative_volume / c.min_relative_volume)
        liquidity_score = min(1.0, zone.strength / 6.0)
        destination_score = min(1.0, target_zone.strength / 6.0)

        confluences = [
            f"liquidity sweep: {', '.join(zone.sources)}",
            f"{snapshot.entry_timeframe.value} local MSS",
            f"{snapshot.entry_timeframe.value} displacement",
            f"{snapshot.entry_timeframe.value} confirmation",
            f"target liquidity: {', '.join(target_zone.sources)}",
        ]
        if zone.has_15m and zone.has_1h:
            confluences.append("15M + 1H liquidity confluence")
        if zone.higher_timeframe_confluence:
            confluences.append("higher-timeframe liquidity reinforced")
        if one_hour_bias == _side_value(side):
            confluences.append("1H tendency aligned")
        if fifteen_bias == _side_value(side):
            confluences.append("15M tendency aligned")
        if four_hour_bias == _side_value(side):
            confluences.append("4H context aligned")
        if daily_bias == _side_value(side):
            confluences.append("daily context aligned")

        score = min(
            0.99,
            0.18 * rejection_score
            + 0.24 * displacement_score
            + 0.10 * volume_score
            + 0.20 * liquidity_score
            + 0.16 * destination_score
            + 0.12 * alignment,
        )

        if not all(
            isfinite(value)
            for value in (
                entry_price,
                stop_price,
                target_price,
                score,
                target_percent,
                risk_percent,
            )
        ):
            return SetupAnalysis(False, 0.0, "non-finite strategy output", vetoes=("finite",), side=side)

        return SetupAnalysis(
            True,
            score,
            f"{side.value} {snapshot.entry_timeframe.value} liquidity-sweep reversal toward opposing liquidity ({target_percent:.2f}%)",
            sweep_index=sweep_index,
            mss_index=mss_index,
            trigger_index=trigger_index,
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            risk_per_share=risk,
            reward_risk=reward_risk,
            target_percent=target_percent,
            risk_percent=risk_percent,
            liquidity_strength=zone.strength,
            confluences=tuple(confluences),
            side=side,
        )


def _liquidity_zones(
    snapshot: MultiTimeframeSnapshot,
    side: Side,
    config: StrategyConfig,
) -> tuple[LiquidityZone, ...]:
    raw_levels: list[tuple[float, str]] = []

    if snapshot.daily:
        prior = snapshot.daily[-1]
        raw_levels.append((prior.low if side is Side.BUY else prior.high, "prior-day"))

    if snapshot.four_hour:
        for level in _swing_points(
            snapshot.four_hour[-config.four_hour_level_lookback:],
            is_high=side is Side.SELL,
        ):
            raw_levels.append((level, "4H"))

    for source, candles, limit in (
        ("1H", snapshot.one_hour, config.one_hour_level_lookback),
        ("15M", snapshot.fifteen_minute, config.fifteen_minute_level_lookback),
    ):
        for level in _swing_points(
            candles[-limit:],
            is_high=side is Side.SELL,
        ):
            raw_levels.append((level, source))

    if not raw_levels:
        return ()

    raw_levels.sort(key=lambda item: item[0])
    clusters: list[list[tuple[float, str]]] = []

    for level, source in raw_levels:
        if not clusters:
            clusters.append([(level, source)])
            continue

        last_cluster = clusters[-1]
        center = sum(value for value, _ in last_cluster) / len(last_cluster)
        if abs(level - center) / center <= config.liquidity_zone_merge_pct:
            last_cluster.append((level, source))
        else:
            clusters.append([(level, source)])

    zones: list[LiquidityZone] = []
    for cluster in clusters:
        sources = tuple(
            sorted(
                set(source for _, source in cluster),
                key=lambda source: _SOURCE_WEIGHT[source],
            )
        )

        strength = sum(
            _SOURCE_WEIGHT[source]
            * min(sum(1 for _, item_source in cluster if item_source == source), 2)
            for source in sources
        )

        # Daily/4H alone are broad context. An actionable entry zone must
        # contain at least a 1H or 15M liquidity component.
        if not any(source in sources for source in ("1H", "15M")):
            continue

        zones.append(
            LiquidityZone(
                price=sum(value for value, _ in cluster) / len(cluster),
                sources=sources,
                strength=strength,
            )
        )

    return tuple(zones)


def _nearest_opposing_zone(
    entry_price: float,
    zones: tuple[LiquidityZone, ...],
    side: Side,
) -> LiquidityZone | None:
    if side is Side.BUY:
        candidates = [zone for zone in zones if zone.price > entry_price]
        return min(candidates, key=lambda zone: zone.price) if candidates else None

    candidates = [zone for zone in zones if zone.price < entry_price]
    return max(candidates, key=lambda zone: zone.price) if candidates else None


def _side_value(side: Side) -> int:
    return 1 if side is Side.BUY else -1


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


def _swing_points(
    candles: tuple[Candle, ...],
    *,
    is_high: bool,
    span: int = 2,
) -> list[float]:
    points: list[float] = []

    for index in range(span, len(candles) - span):
        value = candles[index].high if is_high else candles[index].low
        neighbors = [
            bar.high if is_high else bar.low
            for bar in candles[index - span:index]
        ] + [
            bar.high if is_high else bar.low
            for bar in candles[index + 1:index + span + 1]
        ]

        if is_high and value > max(neighbors):
            points.append(value)
        if not is_high and value < min(neighbors):
            points.append(value)

    return points


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


def _relative_volume(
    candles: tuple[Candle, ...],
    index: int,
    window: int,
) -> float:
    start = max(0, index - window)
    baseline = [
        candle.volume
        for candle in candles[start:index]
        if candle.volume > 0
    ]

    if not baseline or candles[index].volume <= 0:
        return 0.0

    average_volume = sum(baseline) / len(baseline)
    return candles[index].volume / average_volume if average_volume else 0.0


def _atr_series(
    candles: tuple[Candle, ...],
    period: int,
) -> list[float | None]:
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
            window = true_ranges[index + 1 - period:index + 1]
            values[index] = sum(window) / period

    return values
