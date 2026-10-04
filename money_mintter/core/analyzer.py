from __future__ import annotations

from dataclasses import dataclass

from money_mintter.domain import Candidate


@dataclass(frozen=True, slots=True)
class AnalyzerConfig:
    """Strict market-selection thresholds before deep strategy analysis."""

    min_liquidity: float = 0.80
    min_activity: float = 0.80
    min_volatility: float = 0.50
    max_volatility: float = 0.95
    min_signal_quality: float = 0.80
    min_score: float = 0.85
    max_spread_bps: float = 20.0

    def __post_init__(self) -> None:
        if not 0 <= self.min_liquidity <= 1:
            raise ValueError("min liquidity must be in [0, 1]")
        if not 0 <= self.min_activity <= 1:
            raise ValueError("min activity must be in [0, 1]")
        if not 0 <= self.min_volatility <= 1 or self.max_volatility > 1:
            raise ValueError("volatility thresholds must be in [0, 1]")
        if self.min_volatility > self.max_volatility:
            raise ValueError("minimum volatility cannot exceed maximum volatility")
        if not 0 <= self.min_signal_quality <= 1:
            raise ValueError("minimum signal quality must be in [0, 1]")
        if not 0 <= self.min_score <= 1:
            raise ValueError("minimum score must be in [0, 1]")
        if self.max_spread_bps <= 0:
            raise ValueError("maximum spread must be positive")


@dataclass(frozen=True, slots=True)
class OpportunityAssessment:
    eligible: bool
    score: float
    reasons: tuple[str, ...] = ()
    vetoes: tuple[str, ...] = ()


class OpportunityAnalyzer:
    """Reject untradeable candidates before expensive deep analysis.

    Candidate scores are provider-normalized inputs in [0, 1]. They are not
    treated as a trade signal. A candidate must pass every hard veto and clear
    the combined threshold before the strategy layer is allowed to act.
    """

    def __init__(self, config: AnalyzerConfig | None = None) -> None:
        self.config = config or AnalyzerConfig()

    def assess(
        self,
        candidate: Candidate,
        spread_bps: float | None = None,
    ) -> OpportunityAssessment:
        c = self.config
        vetoes: list[str] = []

        if candidate.liquidity_score < c.min_liquidity:
            vetoes.append("liquidity")
        if candidate.activity_score < c.min_activity:
            vetoes.append("activity")
        if candidate.volatility_score < c.min_volatility:
            vetoes.append("volatility_low")
        if candidate.volatility_score > c.max_volatility:
            vetoes.append("volatility_extreme")
        if candidate.signal_quality_score < c.min_signal_quality:
            vetoes.append("signal_quality")

        if spread_bps is not None:
            if spread_bps < 0:
                vetoes.append("invalid_spread")
                spread_score = 0.0
            else:
                spread_score = max(0.0, 1.0 - spread_bps / c.max_spread_bps)
                if spread_bps > c.max_spread_bps:
                    vetoes.append("spread")
        else:
            spread_score = 1.0

        score = (
            0.30 * candidate.liquidity_score
            + 0.20 * candidate.activity_score
            + 0.15 * candidate.volatility_score
            + 0.25 * candidate.signal_quality_score
            + 0.10 * spread_score
        )

        if score < c.min_score:
            vetoes.append("combined_score")

        reasons = (
            ()
            if vetoes
            else (
                "liquidity",
                "activity",
                "usable volatility",
                "signal quality",
                "spread discipline",
            )
        )
        return OpportunityAssessment(
            eligible=not vetoes,
            score=score,
            reasons=reasons,
            vetoes=tuple(vetoes),
        )
