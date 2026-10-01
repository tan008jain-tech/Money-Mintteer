from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from money_mintter.domain import Side, TradeSignal
from money_mintter.strategy.candles import Candle


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    """Strict baseline parameters for the liquidity-sweep reversal model."""

    lookback_bars: int = 120
    structure_lookback_bars: int = 24
    sweep_search_bars: int = 18
    max_bars_after_sweep: int = 12
    max_bars_after_mss: int = 6
    atr_period: int = 14

    min_sweep_penetration_atr: float = 0.08
    min_sweep_penetration_pct: float = 0.0005
    max_sweep_depth_atr: float = 1.50
    min_rejection_wick_fraction: float = 0.35

    min_displacement_body_fraction: float = 0.60
    min_relative_volume: float = 1.10
    require_volume_confirmation: bool = True

    max_spread_bps: float = 20.0
    stop_buffer_atr: float = 0.05
    max_stop_atr: float = 2.0
    max_chase_atr: float = 0.75

    min_rr: float = 2.5
    preferred_rr: float = 4.0
    max_target_rr: float = 6.0
    min_confidence: float = 0.74

    def __post_init__(self) -> None:
        if self.lookback_bars <= 0 or self.structure_lookback_bars < 3:
            raise ValueError("invalid lookback configuration")
        if self.atr_period < 2 or self.sweep_search_bars <= 0:
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
    """Session-level context required by the first strategy version."""

    prior_day_low: float
    prior_day_high: float
    spread_bps: float = 0.0

    def __post_init__(self) -> None:
        if self.prior_day_low <= 0 or self.prior_day_high <= self.prior_day_low:
            raise ValueError("invalid prior-day levels")
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


class LiquiditySweepReversalStrategy:
    """Strict long-only translation of a liquidity-sweep reversal setup.

    The machine-testable sequence is:

    1. Price runs below the prior-day low and rejects back above it.
    2. The reversal breaks the pre-sweep structural high.
    3. The structure-shift candle must show real displacement and volume.
    4. A later candle must close above the displacement candle high.
    5. The structural stop must be tight enough and the next liquidity level
       must leave sufficient reward room.
    6. Only then is a long signal produced.

    The strategy intentionally refuses weak sweeps, deep breakdown-like moves,
    stale reversals, wide spreads, weak confirmation closes, and late chase entries.
    """

    def __init__(self, config: StrategyConfig | None = None) -> None:
        self.config = config or StrategyConfig()

    def analyze(self, candles: list[Candle], context: StrategyInput) -> SetupAnalysis:
        c = self.config
        minimum_history = max(c.atr_period + 5, c.structure_lookback_bars + 5)
        if len(candles) < minimum_history:
            return SetupAnalysis(False, 0.0, "insufficient candle history")
        if context.spread_bps > c.max_spread_bps:
            return SetupAnalysis(False, 0.0, "spread too wide", vetoes=("spread",))

        bars = candles[-c.lookback_bars :]
        atr_values = self._atr_series(bars, c.atr_period)

        sweep_start = max(
            1,
            len(bars) - c.sweep_search_bars - c.max_bars_after_sweep - c.max_bars_after_mss,
        )
        sweep_candidates: list[int] = []

        for index in range(sweep_start, len(bars)):
            atr = atr_values[index]
            if atr is None:
                continue

            candle = bars[index]
            penetration = context.prior_day_low - candle.low
            threshold = max(
                atr * c.min_sweep_penetration_atr,
                context.prior_day_low * c.min_sweep_penetration_pct,
            )
            if (
                penetration < threshold
                or penetration > atr * c.max_sweep_depth_atr
                or candle.close <= context.prior_day_low
            ):
                continue
            if candle.range <= 0 or candle.lower_wick / candle.range < c.min_rejection_wick_fraction:
                continue

            sweep_candidates.append(index)

        if not sweep_candidates:
            return SetupAnalysis(
                False,
                0.0,
                "no valid sell-side liquidity sweep",
                vetoes=("no_sweep",),
            )

        # Newest sweep first. A failed recent setup must not hide an older,
        # still-valid setup if that older setup completes cleanly.
        for sweep_index in reversed(sweep_candidates):
            result = self._evaluate_sweep(bars, atr_values, context, sweep_index)
            if result.passed:
                return result

        return self._evaluate_sweep(bars, atr_values, context, sweep_candidates[-1])

    def signal(self, candles: list[Candle], context: StrategyInput) -> TradeSignal | None:
        result = self.analyze(candles, context)
        if (
            not result.passed
            or result.entry_price is None
            or result.stop_price is None
            or result.target_price is None
        ):
            return None

        return TradeSignal(
            symbol=candles[-1].symbol,
            side=Side.BUY,
            entry_price=result.entry_price,
            stop_price=result.stop_price,
            target_price=result.target_price,
            confidence=result.score,
            timestamp=candles[-1].timestamp,
            reason=result.reason,
        )

    def _evaluate_sweep(
        self,
        bars: list[Candle],
        atr_values: list[float | None],
        context: StrategyInput,
        sweep_index: int,
    ) -> SetupAnalysis:
        c = self.config
        sweep = bars[sweep_index]
        atr = atr_values[sweep_index]

        if atr is None:
            return SetupAnalysis(
                False,
                0.0,
                "ATR unavailable",
                sweep_index=sweep_index,
                vetoes=("atr",),
            )

        pre_start = max(0, sweep_index - c.structure_lookback_bars)
        if sweep_index - pre_start < 3:
            return SetupAnalysis(
                False,
                0.0,
                "insufficient pre-sweep structure",
                sweep_index=sweep_index,
            )

        structure_high = max(bar.high for bar in bars[pre_start:sweep_index])

        mss_end = min(len(bars) - 1, sweep_index + c.max_bars_after_sweep)
        mss_index: int | None = None
        for index in range(sweep_index + 1, mss_end + 1):
            if bars[index].close > structure_high:
                mss_index = index
                break

        if mss_index is None:
            return SetupAnalysis(
                False,
                0.0,
                "sweep did not produce bullish structure shift",
                sweep_index=sweep_index,
                vetoes=("no_mss",),
            )

        trigger_end = min(len(bars) - 1, mss_index + c.max_bars_after_mss)
        mss = bars[mss_index]
        relative_volume = self._relative_volume(bars, mss_index, 20)

        displacement = (
            mss.bullish
            and mss.body_fraction >= c.min_displacement_body_fraction
            and mss.close > structure_high
            and (
                not c.require_volume_confirmation
                or relative_volume >= c.min_relative_volume
            )
        )
        if not displacement:
            return SetupAnalysis(
                False,
                0.0,
                "structure shift lacks bullish displacement",
                sweep_index=sweep_index,
                mss_index=mss_index,
                vetoes=("displacement",),
            )

        trigger_index: int | None = None
        for index in range(mss_index + 1, trigger_end + 1):
            if bars[index].high > mss.high:
                trigger_index = index
                break

        if trigger_index is None:
            return SetupAnalysis(
                False,
                0.0,
                "confirmation high was not broken",
                sweep_index=sweep_index,
                mss_index=mss_index,
                vetoes=("no_trigger",),
            )

        trigger = bars[trigger_index]
        if not trigger.bullish or trigger.close <= mss.high:
            return SetupAnalysis(
                False,
                0.0,
                "breakout was not confirmed by the close",
                sweep_index=sweep_index,
                mss_index=mss_index,
                trigger_index=trigger_index,
                vetoes=("weak_trigger",),
            )

        chase = trigger.close - mss.high
        if chase > c.max_chase_atr * atr:
            return SetupAnalysis(
                False,
                0.0,
                "breakout is too extended to chase",
                sweep_index=sweep_index,
                mss_index=mss_index,
                trigger_index=trigger_index,
                vetoes=("chase",),
            )

        # The signal becomes actionable only after the confirmation candle closes.
        # The production tick engine will later execute against the live ask, so this
        # value is the historical reference price rather than a guaranteed fill.
        entry = trigger.close
        stop = min(sweep.low, mss.low) - c.stop_buffer_atr * atr
        risk = entry - stop

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

        next_liquidity = self._next_resistance(
            bars,
            entry,
            context.prior_day_high,
            trigger_index,
        )
        if next_liquidity is not None and (next_liquidity - entry) / risk < c.min_rr:
            return SetupAnalysis(
                False,
                0.0,
                "nearby resistance leaves insufficient reward room",
                sweep_index=sweep_index,
                mss_index=mss_index,
                trigger_index=trigger_index,
                vetoes=("insufficient_room",),
            )

        available_rr = (
            (next_liquidity - entry) / risk if next_liquidity is not None else c.preferred_rr
        )
        target_rr = min(c.max_target_rr, max(c.preferred_rr, available_rr))
        target = entry + target_rr * risk
        reward_risk = (target - entry) / risk

        if reward_risk < c.min_rr:
            return SetupAnalysis(
                False,
                0.0,
                "reward-to-risk below minimum",
                sweep_index=sweep_index,
                mss_index=mss_index,
                trigger_index=trigger_index,
                vetoes=("rr",),
            )

        fvg = self._has_bullish_fvg(
            bars,
            max(sweep_index + 1, mss_index - 2),
            mss_index,
        )
        body_score = min(
            1.0,
            mss.body_fraction / max(c.min_displacement_body_fraction, 1e-9),
        )
        rejection_score = min(
            1.0,
            sweep.lower_wick
            / max(sweep.range, 1e-9)
            / max(c.min_rejection_wick_fraction, 1e-9),
        )
        volume_score = (
            min(1.0, relative_volume / max(c.min_relative_volume, 1e-9))
            if relative_volume > 0
            else 0.5
        )
        rr_score = min(1.0, reward_risk / c.max_target_rr)

        confluences = [
            "sell-side sweep",
            "bullish MSS",
            "displacement",
            "close-confirmed breakout",
        ]
        if fvg:
            confluences.append("bullish imbalance")

        score = min(
            0.99,
            0.22 * rejection_score
            + 0.28 * body_score
            + 0.18 * volume_score
            + 0.20 * rr_score
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
            return SetupAnalysis(
                False,
                0.0,
                "non-finite strategy output",
                vetoes=("finite",),
            )

        reason = (
            "long liquidity-sweep reversal: prior-day low swept, bullish MSS, "
            f"displacement, close-confirmed breakout; RR={reward_risk:.2f}; "
            f"score={score:.2f}"
        )
        return SetupAnalysis(
            True,
            score,
            reason,
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
    def _relative_volume(
        bars: list[Candle],
        index: int,
        window: int,
    ) -> float:
        start = max(0, index - window)
        baseline = [bar.volume for bar in bars[start:index] if bar.volume > 0]
        if not baseline or bars[index].volume <= 0:
            return 0.0
        average_volume = sum(baseline) / len(baseline)
        return bars[index].volume / average_volume if average_volume else 0.0

    @staticmethod
    def _next_resistance(
        bars: list[Candle],
        entry: float,
        prior_day_high: float,
        from_index: int,
    ) -> float | None:
        levels = [prior_day_high]
        lookback_start = max(0, from_index - 40)
        levels.extend(
            bar.high
            for bar in bars[lookback_start:from_index]
            if bar.high > entry
        )
        above = [level for level in levels if level > entry]
        return min(above) if above else None

    @staticmethod
    def _has_bullish_fvg(
        bars: list[Candle],
        start: int,
        end: int,
    ) -> bool:
        for index in range(max(2, start), min(len(bars), end + 1)):
            if bars[index - 2].high < bars[index].low and bars[index].bullish:
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
