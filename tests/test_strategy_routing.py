from __future__ import annotations

from datetime import datetime, timezone
import pytest

from money_mintter.core.pipeline import DeepAnalysisPipeline
from money_mintter.domain import Candidate, Side, TradeSignal
from money_mintter.strategy import MultiTimeframeSnapshot, Timeframe, SetupAnalysis
from money_mintter.strategy.trend_continuation import TrendContinuationStrategy


def test_pipeline_default_disabled_trend_continuation() -> None:
    pipeline = DeepAnalysisPipeline(enable_trend_continuation=False)
    assert not pipeline.enable_trend_continuation


def test_pipeline_conflict_resolution() -> None:
    pipeline = DeepAnalysisPipeline(enable_trend_continuation=True)
    # Mock strategies to return conflicting signals
    class MockRevStrategy:
        def analyze(self, snap):
            return SetupAnalysis(True, 0.8, "rev")
        def signal(self, snap):
            return TradeSignal("AAPL", Side.BUY, 100.0, 98.0, 104.0, 0.85, datetime.now(timezone.utc), "rev", "lsr.mtf.v3")

    class MockTrendStrategy:
        def analyze(self, snap):
            return SetupAnalysis(True, 0.8, "trend")
        def signal(self, snap):
            return TradeSignal("AAPL", Side.SELL, 100.0, 102.0, 96.0, 0.80, datetime.now(timezone.utc), "trend", "tc.mtf.v1")

    pipeline.strategy = MockRevStrategy()
    pipeline.trend_strategy = MockTrendStrategy()

    now = datetime.now(timezone.utc)
    candidate = Candidate("AAPL", "NASDAQ", 0.9, 0.9, 0.9, 0.9, now)
    snapshot = MultiTimeframeSnapshot(
        as_of=now,
        daily=(),
        four_hour=(),
        one_hour=(),
        fifteen_minute=(),
        entry=(),
        entry_timeframe=Timeframe.FIVE_MIN,
        spread_bps=5.0,
    )

    result = pipeline.evaluate(candidate, snapshot)
    # Conflicting sides should result in signal = None
    assert result.signal is None
