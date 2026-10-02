from __future__ import annotations

from dataclasses import dataclass

from money_mintter.core.analyzer import OpportunityAnalyzer, OpportunityAssessment
from money_mintter.domain import Candidate, TradeSignal
from money_mintter.strategy import LiquiditySweepReversalStrategy, MultiTimeframeSnapshot, SetupAnalysis


@dataclass(frozen=True, slots=True)
class DeepAnalysisResult:
    candidate: Candidate
    opportunity: OpportunityAssessment
    setup: SetupAnalysis
    signal: TradeSignal | None


class DeepAnalysisPipeline:
    """Connect coarse candidate quality to the multi-timeframe strategy."""

    def __init__(
        self,
        analyzer: OpportunityAnalyzer | None = None,
        strategy: LiquiditySweepReversalStrategy | None = None,
    ) -> None:
        self.analyzer = analyzer or OpportunityAnalyzer()
        self.strategy = strategy or LiquiditySweepReversalStrategy()

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
            )

        setup = self.strategy.analyze(snapshot)
        signal = self.strategy.signal(snapshot) if setup.passed else None
        return DeepAnalysisResult(candidate, opportunity, setup, signal)
