from datetime import datetime, timezone

from money_mintter.brokers.paper import PaperBroker
from money_mintter.core.candidates import CandidateRanker
from money_mintter.core.engine import MoneyMintterEngine
from money_mintter.core.risk import RiskEngine
from money_mintter.core.sessions import SessionEngine
from money_mintter.core.trade_manager import TradeManager
from money_mintter.domain import Candidate, Quote, Side, TradeSignal, WalletLedger


def test_engine_supports_short_trade_lifecycle() -> None:
    capital = 10_000.0
    broker = PaperBroker(capital, slippage_bps=0.0, commission_bps=0.0)
    wallet = WalletLedger(capital)
    manager = TradeManager(broker, RiskEngine(), wallet)
    engine = MoneyMintterEngine(
        broker, manager, SessionEngine.default(), CandidateRanker(0.60, 5)
    )

    t0 = datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)
    candidate = Candidate("ABC", "NYSE", 0.95, 0.9, 0.9, 0.9, t0)
    signal = TradeSignal("ABC", Side.SELL, 100.0, 102.0, 96.0, 0.90, t0, "short test")

    opened = engine.cycle(
        t0,
        [candidate],
        {"ABC": Quote("ABC", 99.95, 100.05, 100.0, t0)},
        [signal],
    )
    assert any(event.kind == "OPEN" for event in opened.trade_events)

    t1 = t0.replace(second=10)
    closed = engine.cycle(
        t1,
        [candidate],
        {"ABC": Quote("ABC", 89.95, 90.05, 90.0, t1)},
        [],
    )
    assert any(event.kind == "CLOSE" for event in closed.trade_events)
    assert wallet.realized_profit > 0
