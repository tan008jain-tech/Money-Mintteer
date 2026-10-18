from datetime import datetime, timezone

from money_mintter.brokers.paper import PaperBroker
from money_mintter.core.analyzer import OpportunityAnalyzer
from money_mintter.core.candidates import CandidateRanker
from money_mintter.core.engine import MoneyMintterEngine
from money_mintter.core.risk import RiskEngine
from money_mintter.core.sessions import SessionEngine
from money_mintter.core.trade_manager import TradeManager
from money_mintter.domain import Candidate, Quote, Side, TradeSignal, WalletLedger


def test_end_to_end_open_close_without_cooldown() -> None:
    capital = 10_000.0
    broker = PaperBroker(capital, slippage_bps=0.0, commission_bps=0.0)
    wallet = WalletLedger(capital)
    manager = TradeManager(broker, RiskEngine(0.005, 0.20, 0.02), wallet)
    engine = MoneyMintterEngine(
        broker, manager, SessionEngine.default(), CandidateRanker(0.60, 5)
    )

    t0 = datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)
    candidates = [Candidate("ABC", "NYSE", 0.95, 0.9, 0.9, 0.9, t0)]
    q0 = Quote("ABC", 99.95, 100.05, 100.00, t0)
    signal = TradeSignal("ABC", Side.BUY, 100.0, 98.0, 104.0, 0.90, t0, "test")

    first = engine.cycle(t0, candidates, {"ABC": q0}, [signal])
    assert any(e.kind == "OPEN" for e in first.trade_events)
    assert "ABC" in manager.open_trades

    t1 = t0.replace(second=10)
    q1 = Quote("ABC", 104.0, 104.05, 104.0, t1)
    second = engine.cycle(t1, candidates, {"ABC": q1}, [])
    assert any(e.kind == "CLOSE" for e in second.trade_events)
    assert "ABC" not in manager.open_trades
    assert wallet.wallet > capital


def test_immediate_reentry_is_allowed_after_confirmed_exit() -> None:
    capital = 10_000.0
    broker = PaperBroker(capital, slippage_bps=0.0, commission_bps=0.0)
    wallet = WalletLedger(capital)
    manager = TradeManager(broker, RiskEngine(0.005, 0.20, 0.02), wallet)
    engine = MoneyMintterEngine(
        broker, manager, SessionEngine.default(), CandidateRanker(0.60, 5)
    )

    t0 = datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)
    candidates = [Candidate("ABC", "NYSE", 0.95, 0.9, 0.9, 0.9, t0)]
    opened = engine.cycle(
        t0,
        candidates,
        {"ABC": Quote("ABC", 99.95, 100.05, 100.0, t0)},
        [TradeSignal("ABC", Side.BUY, 100.0, 98.0, 104.0, 0.9, t0, "first entry")],
    )
    assert any(e.kind == "OPEN" for e in opened.trade_events)

    t1 = t0.replace(second=5)
    result = engine.cycle(
        t1,
        candidates,
        {"ABC": Quote("ABC", 104.0, 104.05, 104.0, t1)},
        [TradeSignal("ABC", Side.BUY, 104.0, 102.0, 108.0, 0.9, t1, "fresh confirmation")],
    )
    assert any(e.kind == "CLOSE" for e in result.trade_events)
    assert any(e.kind == "OPEN" for e in result.trade_events)
    assert "ABC" in manager.open_trades


def test_engine_applies_opportunity_analyzer_before_deep_rank() -> None:
    capital = 10_000.0
    broker = PaperBroker(capital, slippage_bps=0.0, commission_bps=0.0)
    wallet = WalletLedger(capital)
    manager = TradeManager(broker, RiskEngine(), wallet)
    engine = MoneyMintterEngine(
        broker,
        manager,
        SessionEngine.default(),
        CandidateRanker(0.60, 5),
        analyzer=OpportunityAnalyzer(),
    )

    t0 = datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)
    candidates = [
        Candidate("GOOD", "NYSE", 0.95, 0.70, 0.90, 0.90, t0),
        Candidate("BAD", "NYSE", 0.40, 0.70, 0.90, 0.90, t0),
    ]
    quotes = {
        "GOOD": Quote("GOOD", 99.99, 100.01, 100.0, t0),
        "BAD": Quote("BAD", 99.99, 100.01, 100.0, t0),
    }
    result = engine.cycle(t0, candidates, quotes, [])
    assert [item.candidate.symbol for item in result.deep_candidates] == ["GOOD"]


def test_engine_pins_active_open_trades_in_deep_candidates() -> None:
    capital = 10_000.0
    broker = PaperBroker(capital, slippage_bps=0.0, commission_bps=0.0)
    wallet = WalletLedger(capital)
    manager = TradeManager(broker, RiskEngine(0.005, 0.20, 0.02), wallet)
    engine = MoneyMintterEngine(
        broker, manager, SessionEngine.default(), CandidateRanker(0.60, 5)
    )

    t0 = datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)
    candidates = [Candidate("HELD", "NYSE", 0.95, 0.9, 0.9, 0.9, t0)]
    q0 = Quote("HELD", 99.95, 100.05, 100.0, t0)
    signal = TradeSignal("HELD", Side.BUY, 100.0, 98.0, 104.0, 0.90, t0, "test")

    # Open trade
    engine.cycle(t0, candidates, {"HELD": q0}, [signal])
    assert "HELD" in manager.open_trades

    # Next cycle with different candidate pool where HELD is not present
    t1 = t0.replace(second=10)
    other_candidates = [Candidate("OTHER", "NYSE", 0.95, 0.9, 0.9, 0.9, t1)]
    result = engine.cycle(t1, other_candidates, {"HELD": q0, "OTHER": q0}, [])
    
    # HELD should still be pinned in deep candidates because it has an open trade
    deep_symbols = {item.candidate.symbol for item in result.deep_candidates}
    assert "HELD" in deep_symbols


def test_engine_closes_trade_when_no_longer_legit() -> None:
    capital = 10_000.0
    broker = PaperBroker(capital, slippage_bps=0.0, commission_bps=0.0)
    wallet = WalletLedger(capital)
    manager = TradeManager(broker, RiskEngine(0.005, 0.20, 0.02), wallet)
    engine = MoneyMintterEngine(
        broker, manager, SessionEngine.default(), CandidateRanker(0.60, 5)
    )

    t0 = datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)
    candidates = [Candidate("BADTRADE", "NYSE", 0.95, 0.9, 0.9, 0.9, t0)]
    q0 = Quote("BADTRADE", 99.95, 100.05, 100.0, t0)
    signal = TradeSignal("BADTRADE", Side.BUY, 100.0, 98.0, 104.0, 0.90, t0, "test")

    # Open trade
    engine.cycle(t0, candidates, {"BADTRADE": q0}, [signal])
    assert "BADTRADE" in manager.open_trades

    # Next cycle where candidate metrics drop significantly so it is no longer legit
    t1 = t0.replace(second=10)
    bad_candidates = [Candidate("BADTRADE", "NYSE", 0.10, 0.1, 0.1, 0.1, t1)]
    q1 = Quote("BADTRADE", 99.50, 99.60, 99.55, t1)
    result = engine.cycle(t1, bad_candidates, {"BADTRADE": q1}, [])

    # Trade should be closed due to analysis invalidation
    assert any(e.kind == "CLOSE" and e.symbol == "BADTRADE" for e in result.trade_events)
    assert "BADTRADE" not in manager.open_trades

