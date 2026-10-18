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


def test_candidate_liquidity_and_activity_filtering() -> None:
    ranker = CandidateRanker(min_score=0.5, max_candidates=5, min_liquidity_score=0.7, min_activity_score=0.6)
    # Candidate with high overall score but low liquidity
    bad_liq = Candidate("BAD_LIQ", "NYSE", 0.5, 0.9, 0.9, 0.9, datetime.now(timezone.utc))
    # Candidate with good liquidity and activity
    good = Candidate("GOOD", "NYSE", 0.8, 0.8, 0.8, 0.8, datetime.now(timezone.utc))
    result = ranker.rank([bad_liq, good])
    assert len(result) == 1
    assert result[0].candidate.symbol == "GOOD"


def test_candidate_hysteresis_preserves_existing() -> None:
    ranker = CandidateRanker(min_score=0.5, max_candidates=1, replacement_delta=0.10)
    existing = candidate("OLD", 0.70)
    new_c = candidate("NEW", 0.75) # raw score 0.75 > 0.70, but within 0.10 hysteresis delta

    # Without existing symbols, NEW wins (0.75 > 0.70)
    res_normal = ranker.rank([existing, new_c])
    assert res_normal[0].candidate.symbol == "NEW"

    # With existing symbol "OLD", OLD gets +0.10 effective score (0.80 vs 0.75), preserving OLD
    res_hysteresis = ranker.rank([existing, new_c], existing_symbols={"OLD"})
    assert res_hysteresis[0].candidate.symbol == "OLD"

