from __future__ import annotations

from dataclasses import dataclass

from money_mintter.core.analyzer import OpportunityAnalyzer, OpportunityAssessment
from money_mintter.domain import Candidate, TradeSignal
from money_mintter.strategy import Candle, LiquiditySweepReversalStrategy, SetupAnalysis, StrategyInput


@dataclass(frozen=True, slots=True)
class DeepAnalysisResult:
    candidate: Candidate
    opportunity: OpportunityAssessment
    setup: SetupAnalysis
    signal: TradeSignal | None


class DeepAnalysisPipeline:
    """Connect coarse candidate quality to the deep strategy without execution."""

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
        candles: list[Candle],
        context: StrategyInput,
    ) -> DeepAnalysisResult:
        opportunity = self.analyzer.assess(candidate, context.spread_bps)
        if not opportunity.eligible:
            return DeepAnalysisResult(
                candidate=candidate,
                opportunity=opportunity,
                setup=SetupAnalysis(False, 0.0, "opportunity gate rejected", vetoes=opportunity.vetoes),
                signal=None,
            )

        setup = self.strategy.analyze(candles, context)
        signal = self.strategy.signal(candles, context) if setup.passed else None
        return DeepAnalysisResult(candidate, opportunity, setup, signal)
