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


def test_fractional_shares_and_leverage_market_open() -> None:
    risk = RiskEngine(0.005, 0.20, 0.02, leverage=4.0)
    # Market open: 4x leverage, fractional shares allowed.
    decision = risk.size_long(10_000, 150.0, 145.0, 0, active_candidates_count=5, market_open=True)
    assert decision.approved
    # Risk budget = 10,000 * 0.005 = 50. Risk per share = 5. Raw qty = 10.0.
    # With fractional rounding to 4 decimal places.
    assert isinstance(decision.quantity, float)
    assert decision.quantity == 10.0


def test_whole_shares_and_leverage_market_closed() -> None:
    risk = RiskEngine(0.005, 0.20, 0.02, leverage=4.0)
    # Market closed: 2x leverage, whole integer shares only.
    decision = risk.size_long(10_000, 150.0, 145.0, 0, active_candidates_count=5, market_open=False)
    assert decision.approved
    assert isinstance(decision.quantity, float)
    assert decision.quantity.is_integer()


def test_dynamic_capital_scaling_rounds_to_zero() -> None:
    risk = RiskEngine(0.005, 0.01, 0.02)
    # Very small capital and strict position cap -> rounds to zero.
    rej = risk.size_long(100.0, 5000.0, 4900.0, 0, active_candidates_count=5, market_open=False)
    assert not rej.approved
    assert rej.reason == "position size rounds to zero"

