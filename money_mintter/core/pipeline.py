from __future__ import annotations

import logging
from dataclasses import dataclass

from money_mintter.core.analyzer import OpportunityAnalyzer, OpportunityAssessment
from money_mintter.domain import Candidate, TradeSignal
from money_mintter.strategy import ICTStrategy, LiquiditySweepReversalStrategy, MultiTimeframeSnapshot, SetupAnalysis
from money_mintter.strategy.trend_continuation import TrendContinuationStrategy, TrendContinuationAnalysis

logger = logging.getLogger("MoneyMintterMasterBlueprint")


@dataclass(frozen=True, slots=True)
class DeepAnalysisResult:
    candidate: Candidate
    opportunity: OpportunityAssessment
    setup: SetupAnalysis
    signal: TradeSignal | None
    trend_setup: TrendContinuationAnalysis | None = None
    trend_signal: TradeSignal | None = None


class DeepAnalysisPipeline:
    """Connect coarse candidate quality to multi-timeframe strategies (lsr.mtf.v3 and tc.mtf.v1)."""

    def __init__(
        self,
        analyzer: OpportunityAnalyzer | None = None,
        strategy: ICTStrategy | LiquiditySweepReversalStrategy | None = None,
        trend_strategy: TrendContinuationStrategy | None = None,
        enable_trend_continuation: bool = False,
    ) -> None:
        self.analyzer = analyzer or OpportunityAnalyzer()
        self.strategy = strategy or ICTStrategy()
        self.trend_strategy = trend_strategy or TrendContinuationStrategy()
        self.enable_trend_continuation = enable_trend_continuation

    def evaluate(
        self,
        candidate: Candidate,
        snapshot: MultiTimeframeSnapshot,
    ) -> DeepAnalysisResult:
        opportunity = self.analyzer.assess(candidate, snapshot.spread_bps)
        if not opportunity.eligible:
            return DeepAnalysisResult(
                candidate,
                opportunity,
                SetupAnalysis(
                    False,
                    0.0,
                    "opportunity gate rejected",
                    vetoes=opportunity.vetoes,
                ),
                None,
                None,
                None,
            )

        setup = self.strategy.analyze(snapshot)
        signal = self.strategy.signal(snapshot) if setup.passed else None

        trend_setup = None
        trend_signal = None
        if self.enable_trend_continuation:
            trend_setup = self.trend_strategy.analyze(snapshot)
            trend_signal = self.trend_strategy.signal(snapshot) if trend_setup.passed else None

            # Conflict resolution: if both signal and trend_signal exist
            if signal is not None and trend_signal is not None:
                if signal.side != trend_signal.side:
                    logger.warning(
                        "[Strategy Routing] Conflict detected between lsr.mtf.v3 and tc.mtf.v1 for symbol %s: side mismatch (%s vs %s). Suppressing both signals.",
                        candidate.symbol, signal.side, trend_signal.side
                    )
                    signal = None
                    trend_signal = None
                else:
                    if trend_signal.confidence >= signal.confidence:
                        signal = trend_signal

        return DeepAnalysisResult(candidate, opportunity, setup, signal, trend_setup, trend_signal)
