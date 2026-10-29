from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from money_mintter.domain import Side, TradeSignal
from money_mintter.strategy.candles import Candle
from money_mintter.strategy.timeframes import MultiTimeframeSnapshot, Timeframe
from money_mintter.core.regime import RegimeEventTracker, MarketRegime, PriceEventState, RegimeAssessment
from money_mintter.core.movement import compute_wilder_atr_14


@dataclass(frozen=True, slots=True)
class TrendContinuationConfig:
    entry_timeframe: Timeframe = Timeframe.FIVE_MIN
    entry_lookback_bars: int = 60
    atr_period: int = 14
    stop_buffer_atr: float = 0.05
    min_strategy_score: float = 0.70
    reward_risk_ratio: float = 2.0


@dataclass(frozen=True, slots=True)
class TrendContinuationAnalysis:
    passed: bool
    score: float
    reason: str
    side: Side | None = None
    entry_price: float | None = None
    stop_price: float | None = None
    target_price: float | None = None


class TrendContinuationStrategy:
    """Trend-continuation and breakout-continuation strategy (tc.mtf.v1)."""

    VERSION = "tc.mtf.v1"

    def __init__(self, config: TrendContinuationConfig | None = None, tracker: RegimeEventTracker | None = None) -> None:
        self.config = config or TrendContinuationConfig()
        self.tracker = tracker or RegimeEventTracker()

    def analyze(self, snapshot: MultiTimeframeSnapshot) -> TrendContinuationAnalysis:
        candles = snapshot.entry if hasattr(snapshot, "entry") else []
        if not candles and hasattr(snapshot, "candles"):
            candles = snapshot.candles.get(self.config.entry_timeframe, [])
        if not candles or len(candles) < 25:
            return TrendContinuationAnalysis(False, 0.0, "insufficient entry timeframe candles for trend continuation (< 25 bars)")

        symbol = candles[0].symbol
        assessment = self.tracker.assess(symbol, candles)

        # Eligibility check:
        # BREAKOUT_WATCH alone is monitoring-only and NEVER generates an entry.
        # Established trends (TRENDING_UP / TRENDING_DOWN) or BREAKOUT_ACCEPTED / PULLBACK_ACTIVE are eligible.
        eligible = False
        side: Side | None = None

        if assessment.regime in (MarketRegime.TRENDING_UP, MarketRegime.TRENDING_DOWN):
            eligible = True
            side = Side.BUY if assessment.regime == MarketRegime.TRENDING_UP else Side.SELL
        elif assessment.event_state in (PriceEventState.BREAKOUT_ACCEPTED, PriceEventState.PULLBACK_ACTIVE):
            eligible = True
            # Determine side from breakout direction
            recent_close = candles[-1].close
            if assessment.range_high is not None and recent_close >= assessment.range_high:
                side = Side.BUY
            elif assessment.range_low is not None and recent_close <= assessment.range_low:
                side = Side.SELL
            else:
                return TrendContinuationAnalysis(False, 0.0, "consolidation breakout side indeterminable")

        if not eligible or side is None:
            return TrendContinuationAnalysis(
                False,
                0.0,
                f"regime/event state not eligible for continuation: regime={assessment.regime.value}, event={assessment.event_state.value}",
            )

        last_candle = candles[-1]
        prev_candle = candles[-2]
        atr = assessment.atr_14 or compute_wilder_atr_14(list(candles), len(candles) - 1) or 1.0

        # Context A: Established Trend Continuation Rules (Local 5-bar breakout + pullback + resumption)
        if assessment.regime in (MarketRegime.TRENDING_UP, MarketRegime.TRENDING_DOWN):
            lookback_5 = candles[-6:-1] if len(candles) >= 6 else candles[:-1]
            if side is Side.BUY:
                highest_5 = max(c.high for c in lookback_5)
                local_breakout = last_candle.close > highest_5
                resumption = last_candle.close > prev_candle.high
                if not (local_breakout and resumption):
                    return TrendContinuationAnalysis(False, 0.0, "established long trend local breakout/resumption not met")
            else:
                lowest_5 = min(c.low for c in lookback_5)
                local_breakout = last_candle.close < lowest_5
                resumption = last_candle.close < prev_candle.low
                if not (local_breakout and resumption):
                    return TrendContinuationAnalysis(False, 0.0, "established short trend local breakout/resumption not met")

        # Context B: Consolidation Breakout Continuation Rules (Acceptance + pullback + resumption)
        if assessment.event_state in (PriceEventState.BREAKOUT_ACCEPTED, PriceEventState.PULLBACK_ACTIVE):
            if side is Side.BUY:
                resumption = last_candle.close > prev_candle.high
                if not resumption:
                    return TrendContinuationAnalysis(False, 0.0, "consolidation long breakout resumption not met")
            else:
                resumption = last_candle.close < prev_candle.low
                if not resumption:
                    return TrendContinuationAnalysis(False, 0.0, "consolidation short breakout resumption not met")

        entry_price = last_candle.close

        # Structural stop calculation from actual pullback extreme + ATR buffer
        pullback_bars = candles[-5:]
        if side is Side.BUY:
            structural_extreme = min(c.low for c in pullback_bars)
            stop_price = structural_extreme - (atr * self.config.stop_buffer_atr)
            if not (stop_price < entry_price):
                return TrendContinuationAnalysis(False, 0.0, "invalid long stop price >= entry price")
            target_price = entry_price + self.config.reward_risk_ratio * (entry_price - stop_price)
        else:
            structural_extreme = max(c.high for c in pullback_bars)
            stop_price = structural_extreme + (atr * self.config.stop_buffer_atr)
            if not (stop_price > entry_price):
                return TrendContinuationAnalysis(False, 0.0, "invalid short stop price <= entry price")
            target_price = entry_price - self.config.reward_risk_ratio * (stop_price - entry_price)

        if not (isfinite(entry_price) and isfinite(stop_price) and isfinite(target_price)):
            return TrendContinuationAnalysis(False, 0.0, "non-finite price levels generated")

        score = 0.82 if assessment.regime != MarketRegime.UNKNOWN else 0.72
        if score < self.config.min_strategy_score:
            return TrendContinuationAnalysis(False, score, "trend continuation score below minimum threshold")

        return TrendContinuationAnalysis(
            passed=True,
            score=score,
            reason=f"valid trend continuation setup ({assessment.regime.value} / {assessment.event_state.value})",
            side=side,
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
        )

    def signal(self, snapshot: MultiTimeframeSnapshot) -> TradeSignal | None:
        analysis = self.analyze(snapshot)
        if not analysis.passed or analysis.side is None or analysis.entry_price is None or analysis.stop_price is None or analysis.target_price is None:
            return None

        candles = snapshot.entry if hasattr(snapshot, "entry") else []
        if not candles and hasattr(snapshot, "candles"):
            candles = snapshot.candles.get(self.config.entry_timeframe, [])
        symbol = candles[0].symbol if candles else "UNKNOWN"

        return TradeSignal(
            symbol=symbol,
            side=analysis.side,
            entry_price=analysis.entry_price,
            stop_price=analysis.stop_price,
            target_price=analysis.target_price,
            confidence=analysis.score,
            timestamp=candles[-1].timestamp if candles else snapshot.as_of,
            reason=analysis.reason,
            strategy_version=self.VERSION,
        )
