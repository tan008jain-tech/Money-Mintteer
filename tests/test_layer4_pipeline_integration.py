from __future__ import annotations

from datetime import datetime, timezone, timedelta
import logging
import pytest

from money_mintter.strategy.candles import Candle
from money_mintter.strategy import LiquiditySweepReversalStrategy, StrategyConfig
from money_mintter.core.selector import select_layer4_candidates
from money_mintter.core.live_bot import MarketScanner
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit


def test_layer4_pipeline_integration_requirements(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeBar:
        def __init__(self, ts, o, h, l, c, v):
            self.timestamp = ts
            self.open = o
            self.high = h
            self.low = l
            self.close = c
            self.volume = v

    class FakeBarsResponse:
        def __init__(self, data):
            self.data = data

    requested_timeframes = []

    class FakeStockClient:
        def get_stock_bars(self, req):
            requested_timeframes.append(req.timeframe)
            end_dt = getattr(req, 'end', datetime.now(timezone.utc))
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
            # Alpaca start-of-bar: at now_utc 10:10:00, the latest completed bar starts at 10:09:00 (covers 10:09:00 - 10:10:00)
            bar_time = end_dt.replace(second=0, microsecond=0) - timedelta(minutes=1)
            symbols = req.symbol_or_symbols if isinstance(req.symbol_or_symbols, list) else [req.symbol_or_symbols]
            data = {}
            for sym in symbols:
                bars = [FakeBar(bar_time - timedelta(minutes=i), 100, 101, 99, 100, 1000) for i in range(50, 0, -1)]
                bars.append(FakeBar(bar_time, 100, 150, 50, 100, 1000))
                data[sym] = bars
            return FakeBarsResponse(data)

    monkeypatch.setattr(MarketScanner, "is_session_open", lambda sym, now: (True, "Open"))
    pool = ["SYM1", "SYM2", "SYM3", "SYM4", "SYM5", "SYM6", "SYM7", "SYM8", "SYM9", "SYM10"]
    scanner = MarketScanner(pool, FakeStockClient(), logging.getLogger("test"))
    now_utc = datetime(2026, 10, 18, 10, 10, 0, tzinfo=timezone.utc)

    analyzed, dashboard = scanner.scan_universe(now_utc, set(), LiquiditySweepReversalStrategy(StrategyConfig()), daily_pool=pool)

    assert len(requested_timeframes) > 0
    for tf in requested_timeframes:
        assert tf.amount == 1
    assert len(analyzed) <= 8


def test_layer4_in_progress_bar_exclusion(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeBar:
        def __init__(self, ts, o, h, l, c, v):
            self.timestamp = ts
            self.open = o
            self.high = h
            self.low = l
            self.close = c
            self.volume = v

    class FakeBarsResponse:
        def __init__(self, data):
            self.data = data

    class FakeStockClient:
        def get_stock_bars(self, req):
            end_dt = req.end
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
            bar_time = end_dt.replace(second=0, microsecond=0) # 10:10:00
            symbols = req.symbol_or_symbols if isinstance(req.symbol_or_symbols, list) else [req.symbol_or_symbols]
            data = {}
            for sym in symbols:
                bars = [FakeBar(bar_time - timedelta(minutes=i), 100, 101, 99, 100, 1000) for i in range(50, 1, -1)]
                # Completed bar at 10:09:00 (covers 10:09:00 to 10:10:00)
                bars.append(FakeBar(bar_time - timedelta(minutes=1), 100, 105, 98, 102, 2000))
                # In-progress bar at 10:10:00 (covers 10:10:00 to 10:11:00)
                bars.append(FakeBar(bar_time, 100, 120, 90, 110, 5000))
                data[sym] = bars
            return FakeBarsResponse(data)

    monkeypatch.setattr(MarketScanner, "is_session_open", lambda sym, now: (True, "Open"))
    pool = ["SYM1"]
    scanner = MarketScanner(pool, FakeStockClient(), logging.getLogger("test"))
    # Current time is 10:10:30 UTC -> 10:10:00 bar is in-progress (closes at 10:11:00), 10:09:00 bar is completed
    now_utc = datetime(2026, 10, 18, 10, 10, 30, tzinfo=timezone.utc)

    scanner.scan_universe(now_utc, set(), LiquiditySweepReversalStrategy(StrategyConfig()), daily_pool=pool)

    symbol_bars = scanner._candle_cache.get("SYM1", [])
    completed_bars = [c for c in symbol_bars if c.timestamp + timedelta(minutes=1) <= now_utc]

    # The in-progress bar at 10:10:00 must be excluded from completed bars
    assert completed_bars[-1].timestamp == datetime(2026, 10, 18, 10, 9, 0, tzinfo=timezone.utc)
