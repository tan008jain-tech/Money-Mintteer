from __future__ import annotations

from dataclasses import dataclass, replace
from math import isfinite

from money_mintter.domain import Side, TradeSignal
from money_mintter.strategy.candles import Candle


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    """Intraday two-sided liquidity-sweep reversal configuration."""

    lookback_bars: int = 120
    structure_lookback_bars: int = 8
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
    max_stop_atr: float = 2.0
    max_chase_atr: float = 0.75
    min_rr: float = 1.50
    preferred_rr: float = 2.50
    max_target_rr: float = 5.00
    min_confidence: float = 0.62

    def __post_init__(self) -> None:
        if self.lookback_bars <= 0 or self.structure_lookback_bars < 3:
            raise ValueError("invalid lookback configuration")
        if self.atr_period < 2 or self.sweep_search_bars < 3:
            raise ValueError("invalid ATR/search configuration")
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
            raise ValueError("stop/chase ATR limits must be positive")
        if self.min_rr <= 0 or self.preferred_rr < self.min_rr:
            raise ValueError("preferred RR must be >= minimum RR")
        if self.max_target_rr < self.preferred_rr:
            raise ValueError("maximum target RR must be >= preferred RR")
        if not 0 < self.min_confidence <= 1:
            raise ValueError("minimum confidence must be in (0, 1]")


@dataclass(frozen=True, slots=True)
class StrategyInput:
    """Point-in-time session and intraday liquidity context."""

    prior_day_low: float
    prior_day_high: float
    spread_bps: float = 0.0
    liquidity_lows: tuple[float, ...] = ()
    liquidity_highs: tuple[float, ...] = ()

    def __post_init__(self) -> None:
        if self.prior_day_low <= 0 or self.prior_day_high <= self.prior_day_low:
            raise ValueError("invalid prior-day levels")
        if any(level <= 0 for level in self.liquidity_lows + self.liquidity_highs):
            raise ValueError("liquidity levels must be positive")
        if self.spread_bps < 0:
            raise ValueError("spread cannot be negative")


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
    """Causal intraday liquidity-sweep reversal strategy, both directions."""

    VERSION = "lsr.v3"

    def __init__(self, config: StrategyConfig | None = None) -> None:
        self.config = config or StrategyConfig()

    def analyze(self, candles: list[Candle], context: StrategyInput) -> SetupAnalysis:
        long_result = self.analyze_side(candles, context, Side.BUY)
        short_result = self.analyze_side(candles, context, Side.SELL)
        passing = [result for result in (long_result, short_result) if result.passed]
        if passing:
            return max(passing, key=lambda result: result.score)

        best = max(
            (long_result, short_result),
            key=lambda result: (result.score, result.trigger_index or -1),
        )
        if long_result.reason == short_result.reason:
            return best

        return replace(
            best,
            reason=(
                "no actionable setup: "
                f"long={long_result.reason}; short={short_result.reason}"
            ),
            vetoes=tuple(sorted(set(long_result.vetoes + short_result.vetoes))),
        )

    def analyze_side(
        self,
        candles: list[Candle],
        context: StrategyInput,
        side: Side,
    ) -> SetupAnalysis:
        c = self.config
        minimum_history = max(c.atr_period + 5, c.structure_lookback_bars + 5)
        if len(candles) < minimum_history:
            return SetupAnalysis(False, 0.0, "insufficient candle history", side=side)
        if context.spread_bps > c.max_spread_bps:
            return SetupAnalysis(False, 0.0, "spread too wide", vetoes=("spread",), side=side)

        bars = candles[-c.lookback_bars:]
        atr_values = self._atr_series(bars, c.atr_period)
        sweep_start = max(
            1,
            len(bars)
            - c.sweep_search_bars
            - c.max_bars_after_sweep
            - c.max_bars_after_mss,
        )

        levels = (
            self._unique_levels((context.prior_day_low,) + context.liquidity_lows)
            if side is Side.BUY
            else self._unique_levels((context.prior_day_high,) + context.liquidity_highs)
        )

        sweep_candidates: list[tuple[int, float]] = []
        for index in range(sweep_start, len(bars)):
            atr = atr_values[index - 1] if index > 0 else atr_values[index]
            if atr is None:
                continue

            candle = bars[index]
            wick_fraction = (
                candle.lower_wick / candle.range
                if side is Side.BUY and candle.range > 0
                else candle.upper_wick / candle.range
                if side is Side.SELL and candle.range > 0
                else 0.0
            )

            for level in levels:
                penetration = level - candle.low if side is Side.BUY else candle.high - level
                reclaimed = candle.close > level if side is Side.BUY else candle.close < level
                threshold = max(
                    atr * c.min_sweep_penetration_atr,
                    level * c.min_sweep_penetration_pct,
                )
                if (
                    penetration >= threshold
                    and penetration <= atr * c.max_sweep_depth_atr
                    and reclaimed
                    and wick_fraction >= c.min_rejection_wick_fraction
                ):
                    sweep_candidates.append((index, level))

        if not sweep_candidates:
            return SetupAnalysis(
                False,
                0.0,
                "no valid sell-side liquidity sweep"
                if side is Side.BUY
                else "no valid buy-side liquidity sweep",
                vetoes=("no_long_sweep" if side is Side.BUY else "no_short_sweep",),
                side=side,
            )

        newest_failure: SetupAnalysis | None = None
        for sweep_index, swept_level in reversed(sweep_candidates):
            result = replace(
                self._evaluate_sweep(
                    bars, atr_values, context, sweep_index, swept_level, side
                ),
                side=side,
            )
            if result.passed:
                if result.trigger_index == len(bars) - 1:
                    return result
                newest_failure = replace(
                    result,
                    passed=False,
                    reason="setup completed on an earlier candle",
                    vetoes=result.vetoes + ("stale_trigger",),
                )
            elif newest_failure is None:
                newest_failure = result

        return newest_failure or SetupAnalysis(False, 0.0, "no actionable setup", side=side)

    def signal(self, candles: list[Candle], context: StrategyInput) -> TradeSignal | None:
        result = self.analyze(candles, context)
        if (
            not result.passed
            or result.side is None
            or result.entry_price is None
            or result.stop_price is None
            or result.target_price is None
        ):
            return None

        return TradeSignal(
            symbol=candles[-1].symbol,
            side=result.side,
            entry_price=result.entry_price,
            stop_price=result.stop_price,
            target_price=result.target_price,
            confidence=result.score,
            timestamp=candles[-1].timestamp,
            reason=result.reason,
            strategy_version=self.VERSION,
        )

    def _evaluate_sweep(
        self,
        bars: list[Candle],
        atr_values: list[float | None],
        context: StrategyInput,
        sweep_index: int,
        swept_level: float,
        side: Side,
    ) -> SetupAnalysis:
        c = self.config
        sweep = bars[sweep_index]
        atr = atr_values[sweep_index - 1] if sweep_index > 0 else atr_values[sweep_index]
        if atr is None:
            return SetupAnalysis(False, 0.0, "ATR unavailable", sweep_index=sweep_index, vetoes=("atr",))

        pre_start = max(0, sweep_index - c.structure_lookback_bars)
        if sweep_index - pre_start < 3:
            return SetupAnalysis(False, 0.0, "insufficient pre-sweep structure", sweep_index=sweep_index)

        structure_level = (
            max(bar.high for bar in bars[pre_start:sweep_index])
            if side is Side.BUY
            else min(bar.low for bar in bars[pre_start:sweep_index])
        )

        mss_end = min(len(bars) - 1, sweep_index + c.max_bars_after_sweep)
        mss_index: int | None = None
        for index in range(sweep_index + 1, mss_end + 1):
            if side is Side.BUY and bars[index].close > structure_level:
                mss_index = index
                break
            if side is Side.SELL and bars[index].close < structure_level:
                mss_index = index
                break

        if mss_index is None:
            return SetupAnalysis(
                False,
                0.0,
                "sweep did not produce bullish structure shift"
                if side is Side.BUY
                else "sweep did not produce bearish structure shift",
                sweep_index=sweep_index,
                vetoes=("no_mss_long" if side is Side.BUY else "no_mss_short",),
            )

        trigger_end = min(len(bars) - 1, mss_index + c.max_bars_after_mss)
        mss = bars[mss_index]
        relative_volume = self._relative_volume(bars, mss_index, 20)
        displacement = (
            (
                side is Side.BUY and mss.bullish and mss.close > structure_level
            )
            or (
                side is Side.SELL and mss.bearish and mss.close < structure_level
            )
        ) and (
            mss.body_fraction >= c.min_displacement_body_fraction
            and (
                not c.require_volume_confirmation
                or relative_volume >= c.min_relative_volume
            )
        )

        if not displacement:
            return SetupAnalysis(
                False,
                0.0,
                "structure shift lacks bullish displacement"
                if side is Side.BUY
                else "structure shift lacks bearish displacement",
                sweep_index=sweep_index,
                mss_index=mss_index,
                vetoes=("displacement_long" if side is Side.BUY else "displacement_short",),
            )

        trigger_index: int | None = None
        for index in range(mss_index + 1, trigger_end + 1):
            if side is Side.BUY and bars[index].high > mss.high:
                trigger_index = index
                break
            if side is Side.SELL and bars[index].low < mss.low:
                trigger_index = index
                break

        if trigger_index is None:
            return SetupAnalysis(
                False,
                0.0,
                "confirmation high was not broken"
                if side is Side.BUY
                else "confirmation low was not broken",
                sweep_index=sweep_index,
                mss_index=mss_index,
                vetoes=("no_trigger_long" if side is Side.BUY else "no_trigger_short",),
            )

        trigger = bars[trigger_index]
        confirmed = (
            (side is Side.BUY and trigger.bullish and trigger.close > mss.high)
            or (side is Side.SELL and trigger.bearish and trigger.close < mss.low)
        )
        if not confirmed:
            return SetupAnalysis(
                False,
                0.0,
                "breakout was not confirmed by the close"
                if side is Side.BUY
                else "breakdown was not confirmed by the close",
                sweep_index=sweep_index,
                mss_index=mss_index,
                trigger_index=trigger_index,
                vetoes=("weak_trigger_long" if side is Side.BUY else "weak_trigger_short",),
            )

        chase = trigger.close - mss.high if side is Side.BUY else mss.low - trigger.close
        if chase > c.max_chase_atr * atr:
            return SetupAnalysis(
                False,
                0.0,
                "breakout is too extended to chase"
                if side is Side.BUY
                else "breakdown is too extended to chase",
                sweep_index=sweep_index,
                mss_index=mss_index,
                trigger_index=trigger_index,
                vetoes=("chase_long" if side is Side.BUY else "chase_short",),
            )

        entry = trigger.close
        if side is Side.BUY:
            stop = min(sweep.low, mss.low) - c.stop_buffer_atr * atr
            risk = entry - stop
            next_liquidity = self._next_liquidity(
                entry, context.prior_day_high, context.liquidity_highs, side
            )
        else:
            stop = max(sweep.high, mss.high) + c.stop_buffer_atr * atr
            risk = stop - entry
            next_liquidity = self._next_liquidity(
                entry, context.prior_day_low, context.liquidity_lows, side
            )

        if risk <= 0 or risk > c.max_stop_atr * atr:
            return SetupAnalysis(
                False,
                0.0,
                "structural stop is too wide",
                sweep_index=sweep_index,
                mss_index=mss_index,
                trigger_index=trigger_index,
                vetoes=("stop_too_wide",),
            )

        available_rr = (
            (
                (next_liquidity - entry) / risk
                if side is Side.BUY
                else (entry - next_liquidity) / risk
            )
            if next_liquidity is not None
            else c.preferred_rr
        )
        if available_rr < c.min_rr:
            return SetupAnalysis(
                False,
                0.0,
                "nearby liquidity leaves insufficient reward room",
                sweep_index=sweep_index,
                mss_index=mss_index,
                trigger_index=trigger_index,
                vetoes=("insufficient_room",),
            )

        target_rr = min(c.max_target_rr, max(c.preferred_rr, available_rr))
        target = entry + target_rr * risk if side is Side.BUY else entry - target_rr * risk
        reward_risk = abs(target - entry) / risk

        fvg = self._has_fvg(
            bars,
            max(sweep_index + 1, mss_index - 2),
            mss_index,
            side,
        )
        wick_fraction = (
            sweep.lower_wick / max(sweep.range, 1e-9)
            if side is Side.BUY
            else sweep.upper_wick / max(sweep.range, 1e-9)
        )
        rejection_score = min(
            1.0, wick_fraction / max(c.min_rejection_wick_fraction, 1e-9)
        )
        body_score = min(
            1.0, mss.body_fraction / max(c.min_displacement_body_fraction, 1e-9)
        )
        volume_score = (
            min(1.0, relative_volume / max(c.min_relative_volume, 1e-9))
            if relative_volume > 0
            else 0.5
        )
        rr_score = min(1.0, reward_risk / c.max_target_rr)

        confluences = [
            "sell-side sweep" if side is Side.BUY else "buy-side sweep",
            "bullish MSS" if side is Side.BUY else "bearish MSS",
            "bullish displacement" if side is Side.BUY else "bearish displacement",
            "close-confirmed breakout" if side is Side.BUY else "close-confirmed breakdown",
            "prior-session liquidity"
            if swept_level in (context.prior_day_low, context.prior_day_high)
            else "intraday liquidity",
        ]
        if fvg:
            confluences.append(
                "bullish imbalance" if side is Side.BUY else "bearish imbalance"
            )

        score = min(
            0.99,
            0.22 * rejection_score
            + 0.26 * body_score
            + 0.16 * volume_score
            + 0.24 * rr_score
            + 0.12 * (1.0 if fvg else 0.65),
        )
        if score < c.min_confidence:
            return SetupAnalysis(
                False,
                score,
                "setup quality below strategy threshold",
                sweep_index=sweep_index,
                mss_index=mss_index,
                trigger_index=trigger_index,
                vetoes=("quality",),
                confluences=tuple(confluences),
            )

        if not all(isfinite(value) for value in (entry, stop, target, score)):
            return SetupAnalysis(False, 0.0, "non-finite strategy output", vetoes=("finite",))

        direction = "long" if side is Side.BUY else "short"
        return SetupAnalysis(
            True,
            score,
            (
                f"{direction} intraday liquidity-sweep reversal: "
                f"MSS, displacement, close-confirmed "
                f"{'breakout' if side is Side.BUY else 'breakdown'}; "
                f"RR={reward_risk:.2f}; score={score:.2f}"
            ),
            sweep_index,
            mss_index,
            trigger_index,
            entry,
            stop,
            target,
            risk,
            reward_risk,
            tuple(confluences),
            (),
        )

    @staticmethod
    def _unique_levels(levels: tuple[float, ...]) -> tuple[float, ...]:
        result: list[float] = []
        for level in levels:
            if not any(
                abs(level - existing) <= max(level, existing) * 1e-6
                for existing in result
            ):
                result.append(level)
        return tuple(result)

    @staticmethod
    def _next_liquidity(
        entry: float,
        session_level: float,
        intraday_levels: tuple[float, ...],
        side: Side,
    ) -> float | None:
        levels = (session_level,) + intraday_levels
        if side is Side.BUY:
            above = [level for level in levels if level > entry]
            return min(above) if above else None
        below = [level for level in levels if level < entry]
        return max(below) if below else None

    @staticmethod
    def _relative_volume(bars: list[Candle], index: int, window: int) -> float:
        start = max(0, index - window)
        baseline = [bar.volume for bar in bars[start:index] if bar.volume > 0]
        if not baseline or bars[index].volume <= 0:
            return 0.0
        average_volume = sum(baseline) / len(baseline)
        return bars[index].volume / average_volume if average_volume else 0.0

    @staticmethod
    def _has_fvg(
        bars: list[Candle],
        start: int,
        end: int,
        side: Side,
    ) -> bool:
        for index in range(max(2, start), min(len(bars), end + 1)):
            if side is Side.BUY and bars[index - 2].high < bars[index].low and bars[index].bullish:
                return True
            if side is Side.SELL and bars[index - 2].low > bars[index].high and bars[index].bearish:
                return True
        return False

    @staticmethod
    def _atr_series(
        bars: list[Candle],
        period: int,
    ) -> list[float | None]:
        values: list[float | None] = [None] * len(bars)
        true_ranges: list[float] = []
        for index, bar in enumerate(bars):
            previous_close = bars[index - 1].close if index else bar.close
            true_range = max(
                bar.high - bar.low,
                abs(bar.high - previous_close),
                abs(bar.low - previous_close),
            )
            true_ranges.append(true_range)
            if index + 1 >= period:
                window = true_ranges[index + 1 - period : index + 1]
                values[index] = sum(window) / period
        return values
