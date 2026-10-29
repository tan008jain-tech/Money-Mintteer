from __future__ import annotations

from datetime import datetime, timezone, timedelta
import pytest

from money_mintter.strategy.candles import Candle
from money_mintter.core.regime import RegimeEventTracker, MarketRegime, PriceEventState


def test_regime_insufficient_history() -> None:
    tracker = RegimeEventTracker(min_bars_required=20)
    now = datetime.now(timezone.utc)
    candles = [
        Candle("AAPL", now, 100.0, 101.0, 99.0, 100.5, 1000.0)
        for _ in range(10)
    ]
    assessment = tracker.assess("AAPL", candles)
    assert assessment.regime == MarketRegime.UNKNOWN
    assert assessment.event_state == PriceEventState.NO_SETUP
    assert "insufficient" in assessment.reason.lower()


def test_regime_event_state_machine_idempotency_breakout() -> None:
    tracker = RegimeEventTracker(min_bars_required=20)
    base_time = datetime(2026, 10, 27, 10, 0, tzinfo=timezone.utc)
    candles = []
    for i in range(20):
        t = base_time + timedelta(minutes=5 * i)
        candles.append(Candle("AAPL", t, 100.0, 101.0, 99.0, 100.0, 1000.0))

    # Add breakout candle
    t_break = base_time + timedelta(minutes=100)
    breakout_candle = Candle("AAPL", t_break, 100.0, 105.0, 100.0, 104.5, 2000.0)
    candles_with_breakout = candles + [breakout_candle]

    assessment1 = tracker.assess("AAPL", candles_with_breakout)
    assert assessment1.event_state == PriceEventState.BREAKOUT_WATCH

    # Re-evaluate with exact same breakout candle (same timestamp)
    assessment2 = tracker.assess("AAPL", candles_with_breakout)
    assert assessment2.event_state == PriceEventState.BREAKOUT_WATCH
    assert "Idempotent" in assessment2.reason


def test_regime_long_lifecycle() -> None:
    tracker = RegimeEventTracker(min_bars_required=20)
    base_time = datetime(2026, 10, 27, 10, 0, tzinfo=timezone.utc)
    candles = []
    for i in range(20):
        t = base_time + timedelta(minutes=5 * i)
        candles.append(Candle("AAPL", t, 100.0, 101.0, 99.0, 100.0, 1000.0))

    # 1. Breakout Watch (Close above range high 101.0)
    t1 = base_time + timedelta(minutes=100)
    c1 = Candle("AAPL", t1, 100.5, 102.5, 100.0, 102.0, 1500.0)
    res1 = tracker.assess("AAPL", candles + [c1])
    assert res1.event_state == PriceEventState.BREAKOUT_WATCH

    # 2. Breakout Accepted (Close higher or >= range high)
    t2 = base_time + timedelta(minutes=105)
    c2 = Candle("AAPL", t2, 102.0, 104.0, 101.5, 103.5, 2000.0)
    res2 = tracker.assess("AAPL", candles + [c1, c2])
    assert res2.event_state == PriceEventState.BREAKOUT_ACCEPTED

    # 3. Pullback Active (Bearish candle within long breakout)
    t3 = base_time + timedelta(minutes=110)
    c3 = Candle("AAPL", t3, 103.5, 103.8, 102.5, 102.8, 1200.0)
    res3 = tracker.assess("AAPL", candles + [c1, c2, c3])
    assert res3.event_state == PriceEventState.PULLBACK_ACTIVE

    # 4. Expiry after 5 pullback bars (p_bars > 4)
    current_candles = candles + [c1, c2, c3]
    res_n = None
    for step in range(1, 6):
        tn = base_time + timedelta(minutes=110 + 5 * step)
        # Wider high/low to prevent consolidation compressed fallback immediately
        cn = Candle("AAPL", tn, 102.8, 108.0, 95.0, 102.2, 1000.0)
        current_candles.append(cn)
        res_n = tracker.assess("AAPL", current_candles)

    assert res_n.event_state == PriceEventState.EXPIRED


def test_regime_short_false_breakout_lifecycle() -> None:
    tracker = RegimeEventTracker(min_bars_required=20)
    base_time = datetime(2026, 10, 27, 10, 0, tzinfo=timezone.utc)
    candles = []
    for i in range(20):
        t = base_time + timedelta(minutes=5 * i)
        candles.append(Candle("AAPL", t, 100.0, 101.0, 99.0, 100.0, 1000.0))

    # Range low is 99.0
    # 1. Short Breakout Watch (Close below 99.0)
    t1 = base_time + timedelta(minutes=100)
    c1 = Candle("AAPL", t1, 99.5, 99.5, 97.5, 98.0, 1500.0)
    res1 = tracker.assess("AAPL", candles + [c1])
    assert res1.event_state == PriceEventState.BREAKOUT_WATCH

    # 2. False Breakout (Price bounces back above stable high or opposite boundary)
    # If short breakout watch, closing above range high (101.0) triggers false breakout
    t2 = base_time + timedelta(minutes=105)
    c2 = Candle("AAPL", t2, 98.0, 102.0, 98.0, 101.5, 2000.0)
    res2 = tracker.assess("AAPL", candles + [c1, c2])
    assert res2.event_state == PriceEventState.FALSE_BREAKOUT

    # 3. Failed Breakout Reversal / Invalidation
    t3 = base_time + timedelta(minutes=110)
    c3 = Candle("AAPL", t3, 101.5, 103.0, 101.0, 102.5, 2000.0)
    res3 = tracker.assess("AAPL", candles + [c1, c2, c3])
    assert res3.event_state == PriceEventState.FAILED_BREAKOUT_REVERSAL


def test_symbol_isolation() -> None:
    tracker = RegimeEventTracker(min_bars_required=20)
    base_time = datetime(2026, 10, 27, 10, 0, tzinfo=timezone.utc)
    aapl_candles = [Candle("AAPL", base_time + timedelta(minutes=5*i), 100.0, 101.0, 99.0, 100.0, 1000.0) for i in range(25)]
    msft_candles = [Candle("MSFT", base_time + timedelta(minutes=5*i), 300.0, 302.0, 298.0, 301.0, 5000.0) for i in range(25)]

    # AAPL breakout long
    aapl_breakout = aapl_candles + [Candle("AAPL", base_time + timedelta(minutes=130), 100.0, 105.0, 100.0, 104.5, 2000.0)]
    # MSFT breakout short
    msft_breakout = msft_candles + [Candle("MSFT", base_time + timedelta(minutes=130), 301.0, 301.0, 295.0, 296.0, 6000.0)]

    res_aapl = tracker.assess("AAPL", aapl_breakout)
    res_msft = tracker.assess("MSFT", msft_breakout)

    assert res_aapl.event_state == PriceEventState.BREAKOUT_WATCH
    assert res_msft.event_state == PriceEventState.BREAKOUT_WATCH


def test_adx_dmi_calculation() -> None:
    tracker = RegimeEventTracker(min_bars_required=20)
    base_time = datetime(2026, 10, 27, 10, 0, tzinfo=timezone.utc)
    # Strong uptrend series
    candles = []
    price = 100.0
    for i in range(35):
        price += 1.5
        t = base_time + timedelta(minutes=5 * i)
        candles.append(Candle("AAPL", t, price, price + 1.0, price - 0.2, price + 0.8, 2000.0))

    assessment = tracker.assess("AAPL", candles)
    assert assessment.adx is not None
    assert assessment.plus_di is not None
    assert assessment.minus_di is not None
    assert assessment.regime == MarketRegime.TRENDING_UP
    assert assessment.plus_di > assessment.minus_di
