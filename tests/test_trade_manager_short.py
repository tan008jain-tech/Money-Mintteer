from datetime import datetime, timezone

from money_mintter.brokers.paper import PaperBroker
from money_mintter.core.risk import RiskEngine
from money_mintter.core.trade_manager import TradeManager
from money_mintter.domain import Quote, Side, TradeSignal, WalletLedger


def test_trade_manager_supports_short_lifecycle() -> None:
    t0 = datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)
    broker = PaperBroker(10_000.0, slippage_bps=0.0, commission_bps=0.0)
    wallet = WalletLedger(10_000.0)
    manager = TradeManager(broker, RiskEngine(), wallet)

    signal = TradeSignal("ABC", Side.SELL, 100.0, 102.0, 96.0, 0.90, t0, "short test")
    opened = manager.open_trade(
        signal,
        Quote("ABC", 99.95, 100.05, 100.0, t0),
    )
    assert opened.kind == "OPEN"
    assert manager.open_trades["ABC"].side is Side.SELL

    t1 = t0.replace(second=10)
    closed = manager.process_quote(
        Quote("ABC", 89.95, 90.05, 90.0, t1),
    )
    assert closed is not None
    assert closed.kind == "CLOSE"
    assert wallet.realized_profit > 0
