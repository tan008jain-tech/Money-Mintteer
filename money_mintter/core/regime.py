from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Sequence
from money_mintter.strategy.candles import Candle
from money_mintter.core.movement import compute_wilder_atr_14


class MarketRegime(str, Enum):
    TRENDING_UP = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    RANGING = "RANGING"
    TRANSITION = "TRANSITION"
    UNKNOWN = "UNKNOWN"


class PriceEventState(str, Enum):
    NO_SETUP = "NO_SETUP"
    CONSOLIDATION_COMPRESSED = "CONSOLIDATION_COMPRESSED"
    BREAKOUT_WATCH = "BREAKOUT_WATCH"
    BREAKOUT_ACCEPTED = "BREAKOUT_ACCEPTED"
    PULLBACK_ACTIVE = "PULLBACK_ACTIVE"
    FALSE_BREAKOUT = "FALSE_BREAKOUT"
    FAILED_BREAKOUT_REVERSAL = "FAILED_BREAKOUT_REVERSAL"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True, slots=True)
class RegimeAssessment:
    regime: MarketRegime
    event_state: PriceEventState
    adx: float | None
    plus_di: float | None
    minus_di: float | None
    range_high: float | None
    range_low: float | None
    atr_14: float | None
    reason: str


class RegimeEventTracker:
    """Tracks market regime and price-event state machine per symbol using completed candles with idempotency guards."""

    def __init__(self, min_bars_required: int = 20) -> None:
        self.min_bars_required = min_bars_required
        self.states: dict[str, dict[str, object]] = {}

    def assess(self, symbol: str, candles: Sequence[Candle]) -> RegimeAssessment:
        if not candles or len(candles) < self.min_bars_required:
            return RegimeAssessment(
                regime=MarketRegime.UNKNOWN,
                event_state=PriceEventState.NO_SETUP,
                adx=None,
                plus_di=None,
                minus_di=None,
                range_high=None,
                range_low=None,
                atr_14=None,
                reason="insufficient completed candles for regime warm-up (< 20 bars)",
            )

        completed = list(candles)
        adx, plus_di, minus_di = self._compute_dmi_adx(completed, period=14)
        atr_14 = compute_wilder_atr_14(completed, len(completed) - 1)

        # State machine tracking per symbol
        symbol_state = self.states.setdefault(symbol, {
            "event_state": PriceEventState.NO_SETUP.value,
            "stable_range_high": None,
            "stable_range_low": None,
            "pullback_bars": 0,
            "breakout_side": None,
            "false_breakout_bars": 0,
            "last_processed_timestamp": None,
        })

        last_candle = completed[-1]
        last_processed = symbol_state.get("last_processed_timestamp")

        current_event = PriceEventState(symbol_state["event_state"])
        stable_high = symbol_state["stable_range_high"]
        stable_low = symbol_state["stable_range_low"]

        # Determine regime state
        regime = MarketRegime.UNKNOWN
        if adx is not None and plus_di is not None and minus_di is not None:
            if adx > 25.0:
                if plus_di > minus_di:
                    regime = MarketRegime.TRENDING_UP
                elif minus_di > plus_di:
                    regime = MarketRegime.TRENDING_DOWN
                else:
                    regime = MarketRegime.TRANSITION
            elif adx < 20.0:
                regime = MarketRegime.RANGING
            else:
                regime = MarketRegime.TRANSITION
        else:
            span = (stable_high - stable_low) if (stable_high is not None and stable_low is not None) else 0.0
            mid = (stable_high + stable_low) / 2 if (stable_high is not None and stable_low is not None) else 1.0
            regime = MarketRegime.RANGING if (span / max(1e-8, mid)) < 0.05 else MarketRegime.TRANSITION

        # Idempotency guard: if exact same last candle timestamp is re-evaluated, return current state without advancing
        if last_processed == last_candle.timestamp:
            return RegimeAssessment(
                regime=regime,
                event_state=current_event,
                adx=adx,
                plus_di=plus_di,
                minus_di=minus_di,
                range_high=stable_high,
                range_low=stable_low,
                atr_14=atr_14,
                reason=f"Idempotent re-eval for candle timestamp {last_candle.timestamp}",
            )

        symbol_state["last_processed_timestamp"] = last_candle.timestamp

        # If NO_SETUP, INVALIDATED, or EXPIRED, establish stable range boundaries using preceding 20 completed bars
        if current_event in (PriceEventState.NO_SETUP, PriceEventState.INVALIDATED, PriceEventState.EXPIRED):
            recent_bars = completed[-21:-1] if len(completed) >= 21 else completed[:-1]
            if recent_bars:
                stable_high = max(b.high for b in recent_bars)
                stable_low = min(b.low for b in recent_bars)
                symbol_state["stable_range_high"] = stable_high
                symbol_state["stable_range_low"] = stable_low
                symbol_state["pullback_bars"] = 0
                symbol_state["breakout_side"] = None
                symbol_state["false_breakout_bars"] = 0

        prev_candle = completed[-2] if len(completed) >= 2 else last_candle

        # State transitions
        if current_event in (PriceEventState.NO_SETUP, PriceEventState.INVALIDATED, PriceEventState.EXPIRED):
            span = (stable_high - stable_low) if (stable_high is not None and stable_low is not None) else 0.0
            mid = (stable_high + stable_low) / 2 if (stable_high is not None and stable_low is not None) else 1.0
            if mid > 0 and (span / mid) < 0.08:
                current_event = PriceEventState.CONSOLIDATION_COMPRESSED

            if stable_high is not None and stable_low is not None:
                if last_candle.close > stable_high:
                    current_event = PriceEventState.BREAKOUT_WATCH
                    symbol_state["breakout_side"] = "long"
                elif last_candle.close < stable_low:
                    current_event = PriceEventState.BREAKOUT_WATCH
                    symbol_state["breakout_side"] = "short"

        elif current_event == PriceEventState.CONSOLIDATION_COMPRESSED:
            if stable_high is not None and stable_low is not None:
                if last_candle.close > stable_high:
                    current_event = PriceEventState.BREAKOUT_WATCH
                    symbol_state["breakout_side"] = "long"
                elif last_candle.close < stable_low:
                    current_event = PriceEventState.BREAKOUT_WATCH
                    symbol_state["breakout_side"] = "short"

        elif current_event == PriceEventState.BREAKOUT_WATCH:
            side = symbol_state["breakout_side"]
            if stable_high is not None and stable_low is not None:
                if side == "long":
                    if last_candle.close < stable_low:
                        current_event = PriceEventState.FALSE_BREAKOUT
                        symbol_state["false_breakout_bars"] = 1
                    elif last_candle.close >= stable_high:
                        # Acceptance confirmed
                        current_event = PriceEventState.BREAKOUT_ACCEPTED
                        symbol_state["pullback_bars"] = 0
                elif side == "short":
                    if last_candle.close > stable_high:
                        current_event = PriceEventState.FALSE_BREAKOUT
                        symbol_state["false_breakout_bars"] = 1
                    elif last_candle.close <= stable_low:
                        current_event = PriceEventState.BREAKOUT_ACCEPTED
                        symbol_state["pullback_bars"] = 0

        elif current_event == PriceEventState.FALSE_BREAKOUT:
            fb_bars = int(symbol_state["false_breakout_bars"])
            fb_bars += 1
            symbol_state["false_breakout_bars"] = fb_bars
            side = symbol_state["breakout_side"]
            if stable_high is not None and stable_low is not None:
                if side == "long" and last_candle.close < stable_low:
                    current_event = PriceEventState.FAILED_BREAKOUT_REVERSAL
                elif side == "short" and last_candle.close > stable_high:
                    current_event = PriceEventState.FAILED_BREAKOUT_REVERSAL
                elif fb_bars > 2:
                    current_event = PriceEventState.INVALIDATED

        elif current_event == PriceEventState.BREAKOUT_ACCEPTED:
            side = symbol_state["breakout_side"]
            p_bars = int(symbol_state["pullback_bars"])
            is_pullback = (last_candle.bearish if side == "long" else last_candle.bullish)
            if is_pullback:
                p_bars += 1
                symbol_state["pullback_bars"] = p_bars
                current_event = PriceEventState.PULLBACK_ACTIVE
                if p_bars > 4:
                    current_event = PriceEventState.EXPIRED
            else:
                current_event = PriceEventState.PULLBACK_ACTIVE

        elif current_event == PriceEventState.PULLBACK_ACTIVE:
            side = symbol_state["breakout_side"]
            p_bars = int(symbol_state["pullback_bars"])
            p_bars += 1
            symbol_state["pullback_bars"] = p_bars
            if p_bars > 4:
                current_event = PriceEventState.EXPIRED

        symbol_state["event_state"] = current_event.value

        return RegimeAssessment(
            regime=regime,
            event_state=current_event,
            adx=adx,
            plus_di=plus_di,
            minus_di=minus_di,
            range_high=stable_high,
            range_low=stable_low,
            atr_14=atr_14,
            reason=f"Regime: {regime.value}, Event: {current_event.value}, ADX: {adx}",
        )

    def _compute_dmi_adx(self, candles: list[Candle], period: int = 14) -> tuple[float | None, float | None, float | None]:
        if len(candles) < period + 1:
            return None, None, None

        trs: list[float] = []
        plus_dms: list[float] = []
        minus_dms: list[float] = []

        for i in range(1, len(candles)):
            c_curr = candles[i]
            c_prev = candles[i - 1]
            tr = max(c_curr.high - c_curr.low, abs(c_curr.high - c_prev.close), abs(c_curr.low - c_prev.close))
            trs.append(tr)

            up_move = c_curr.high - c_prev.high
            down_move = c_prev.low - c_curr.low

            plus_dm = up_move if (up_move > down_move and up_move > 0) else 0.0
            minus_dm = down_move if (down_move > up_move and down_move > 0) else 0.0

            plus_dms.append(plus_dm)
            minus_dms.append(minus_dm)

        if len(trs) < period:
            return None, None, None

        atr = sum(trs[:period]) / period
        plus_di_val = sum(plus_dms[:period]) / period
        minus_di_val = sum(minus_dms[:period]) / period

        if atr == 0:
            return 20.0, 50.0, 50.0

        p_di = (plus_di_val / atr) * 100
        m_di = (minus_di_val / atr) * 100

        dx_sum = 0.0
        dx_count = 0
        for i in range(period, len(trs)):
            atr = (atr * (period - 1) + trs[i]) / period
            p_val = (plus_dms[i] / atr) * 100 if atr > 0 else 0
            m_val = (minus_dms[i] / atr) * 100 if atr > 0 else 0
            denom = p_val + m_val
            dx = (abs(p_val - m_val) / denom) * 100 if denom > 0 else 0.0
            dx_sum += dx
            dx_count += 1

        adx = dx_sum / max(1, dx_count)
        return adx, p_di, m_di
