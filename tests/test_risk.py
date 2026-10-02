from money_mintter.core.risk import RiskEngine


def test_size_long_and_short_use_same_risk_budget() -> None:
    risk = RiskEngine(0.005, 0.20, 0.02)

    long_decision = risk.size_long(10_000, 100, 98, 0)
    short_decision = risk.size_short(10_000, 100, 102, 0)

    assert long_decision.approved
    assert short_decision.approved
    assert long_decision.capital_at_risk == short_decision.capital_at_risk


def test_rejects_invalid_short_stop() -> None:
    decision = RiskEngine().size_short(10_000, 100, 99, 0)
    assert not decision.approved
    assert decision.reason == "invalid short stop"
