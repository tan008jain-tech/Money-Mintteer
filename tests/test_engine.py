from datetime import datetime, timezone

from money_mintter.brokers.paper import PaperBroker
from money_mintter.core.candidates import CandidateRanker
from money_mintter.core.engine import MoneyMintterEngine
from money_mintter.core.risk import RiskEngine
from money_mintter.core.sessions import SessionEngine
from money_mintter.core.trade_manager import TradeManager
from money_mintter.domain import Candidate, Quote, Side, TradeSignal, WalletLedger


def test_end_to_end_open_close_without_cooldown() -> None:
    settings_capital = 10_000.0
    broker = PaperBroker(settings_capital, slippage_bps=0.0, commission_bps=0.0)
    wallet = WalletLedger(settings_capital, profit_reserve_pct=0.20)
    manager = TradeManager(broker, RiskEngine(0.005, 0.20, 0.02), wallet)
    engine = MoneyMintterEngine(broker, manager, SessionEngine.default(), CandidateRanker(0.60, 5))

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
    assert wallet.profit_reserve > 0


def test_immediate_reentry_is_allowed_after_confirmed_exit() -> None:
    capital = 10_000.0
    broker = PaperBroker(capital, slippage_bps=0.0, commission_bps=0.0)
    wallet = WalletLedger(capital, profit_reserve_pct=0.20)
    manager = TradeManager(broker, RiskEngine(0.005, 0.20, 0.02), wallet)
    engine = MoneyMintterEngine(broker, manager, SessionEngine.default(), CandidateRanker(0.60, 5))

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
