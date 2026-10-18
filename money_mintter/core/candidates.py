from __future__ import annotations

from dataclasses import dataclass

from money_mintter.domain import Candidate


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    candidate: Candidate
    score: float


class CandidateRanker:
    """Rank externally screened candidates; deep monitoring is capped by policy."""

    def __init__(
        self,
        min_score: float = 0.60,
        max_candidates: int = 5,
        min_liquidity_score: float = 0.0,
        min_activity_score: float = 0.0,
        replacement_delta: float = 0.10,
    ) -> None:
        if not 0 <= min_score <= 1:
            raise ValueError("min_score must be in [0, 1]")
        if max_candidates <= 0:
            raise ValueError("max_candidates must be positive")
        if not 0 <= min_liquidity_score <= 1:
            raise ValueError("min_liquidity_score must be in [0, 1]")
        if not 0 <= min_activity_score <= 1:
            raise ValueError("min_activity_score must be in [0, 1]")
        if not 0 <= replacement_delta <= 1:
            raise ValueError("replacement_delta must be in [0, 1]")
        self.min_score = min_score
        self.max_candidates = max_candidates
        self.min_liquidity_score = min_liquidity_score
        self.min_activity_score = min_activity_score
        self.replacement_delta = replacement_delta

    @staticmethod
    def score(candidate: Candidate) -> float:
        return (
            0.30 * candidate.liquidity_score
            + 0.25 * candidate.volatility_score
            + 0.20 * candidate.activity_score
            + 0.25 * candidate.signal_quality_score
        )

    def rank(self, candidates: list[Candidate], existing_symbols: set[str] | None = None) -> list[RankedCandidate]:
        existing_symbols = existing_symbols or set()
        ranked_items = []
        for c in candidates:
            if (
                c.liquidity_score >= self.min_liquidity_score
                and c.activity_score >= self.min_activity_score
                and self.score(c) >= self.min_score
            ):
                raw_score = self.score(c)
                effective_score = raw_score + (self.replacement_delta if c.symbol in existing_symbols else 0.0)
                ranked_items.append((effective_score, raw_score, c))

        ranked_items.sort(key=lambda item: (-item[0], item[2].symbol))
        return [RankedCandidate(c, raw_score) for effective_score, raw_score, c in ranked_items[: self.max_candidates]]
