from datetime import datetime, timezone

from money_mintter.core.analyzer import OpportunityAnalyzer
from money_mintter.domain import Candidate


def candidate(
    liquidity: float,
    volatility: float,
    activity: float,
    signal_quality: float,
) -> Candidate:
    return Candidate(
        "ABC",
        "NYSE",
        liquidity,
        volatility,
        activity,
        signal_quality,
        datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc),
    )


def test_strict_candidate_gate_passes_good_candidate() -> None:
    assessment = OpportunityAnalyzer().assess(
        candidate(0.90, 0.70, 0.80, 0.80),
        spread_bps=5,
    )

    assert assessment.eligible
    assert assessment.score >= 0.74
    assert assessment.vetoes == ()


def test_extreme_volatility_is_hard_veto() -> None:
    assessment = OpportunityAnalyzer().assess(
        candidate(0.95, 1.00, 0.90, 0.90),
        spread_bps=5,
    )

    assert not assessment.eligible
    assert "volatility_extreme" in assessment.vetoes


def test_wide_spread_is_hard_veto() -> None:
    assessment = OpportunityAnalyzer().assess(
        candidate(0.95, 0.70, 0.90, 0.90),
        spread_bps=50,
    )

    assert not assessment.eligible
    assert "spread" in assessment.vetoes


def test_negative_spread_is_invalid() -> None:
    assessment = OpportunityAnalyzer().assess(
        candidate(0.95, 0.70, 0.90, 0.90),
        spread_bps=-1,
    )

    assert not assessment.eligible
    assert "invalid_spread" in assessment.vetoes
