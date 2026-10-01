from __future__ import annotations

from dataclasses import dataclass

from money_mintter.domain import Candidate


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    candidate: Candidate
    score: float


class CandidateRanker:
    """Rank externally screened candidates; deep monitoring is capped by policy."""

    def __init__(self, min_score: float = 0.60, max_candidates: int = 5) -> None:
        if not 0 <= min_score <= 1:
            raise ValueError("min_score must be in [0, 1]")
        if max_candidates <= 0:
            raise ValueError("max_candidates must be positive")
        self.min_score = min_score
        self.max_candidates = max_candidates

    @staticmethod
    def score(candidate: Candidate) -> float:
        return (
            0.30 * candidate.liquidity_score
            + 0.25 * candidate.volatility_score
            + 0.20 * candidate.activity_score
            + 0.25 * candidate.signal_quality_score
        )

    def rank(self, candidates: list[Candidate]) -> list[RankedCandidate]:
        ranked = [
            RankedCandidate(c, self.score(c))
            for c in candidates
            if self.score(c) >= self.min_score
        ]
        ranked.sort(key=lambda item: (-item.score, item.candidate.symbol))
        return ranked[: self.max_candidates]
