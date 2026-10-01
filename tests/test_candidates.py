from datetime import datetime, timezone

from money_mintter.core.candidates import CandidateRanker
from money_mintter.domain import Candidate


def candidate(symbol: str, score: float) -> Candidate:
    return Candidate(symbol, "NYSE", score, score, score, score, datetime.now(timezone.utc))


def test_candidate_cap_and_threshold() -> None:
    ranker = CandidateRanker(min_score=0.60, max_candidates=5)
    result = ranker.rank([candidate(str(i), 0.9 - i * 0.01) for i in range(8)] + [candidate("LOW", 0.2)])
    assert len(result) == 5
    assert [x.candidate.symbol for x in result] == ["0", "1", "2", "3", "4"]


def test_candidate_ties_are_deterministic() -> None:
    ranker = CandidateRanker(min_score=0.5, max_candidates=5)
    result = ranker.rank([candidate("B", 0.7), candidate("A", 0.7)])
    assert [x.candidate.symbol for x in result] == ["A", "B"]
