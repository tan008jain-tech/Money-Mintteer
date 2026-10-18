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


def test_trade_manager_process_bar_thesis_invalidation() -> None:
    from money_mintter.strategy.candles import Candle

    t0 = datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)
    broker = PaperBroker(10_000.0, slippage_bps=0.0, commission_bps=0.0)
    wallet = WalletLedger(10_000.0)
    manager = TradeManager(broker, RiskEngine(), wallet)

    signal = TradeSignal("ABC", Side.BUY, 100.0, 95.0, 106.0, 0.90, t0, "buy test")
    manager.open_trade(signal, Quote("ABC", 99.95, 100.05, 100.0, t0))
    assert "ABC" in manager.open_trades

    t1 = t0.replace(second=10)
    bearish_candle = Candle("ABC", t1, 100.2, 100.4, 98.8, 99.0, 5000.0)
    q1 = Quote("ABC", 98.95, 99.05, 99.0, t1)

    event = manager.process_bar(bearish_candle, q1)
    assert event is not None
    assert event.kind == "CLOSE"
    assert event.message == "thesis_invalidation"
    assert "ABC" not in manager.open_trades
