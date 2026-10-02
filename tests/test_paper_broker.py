from datetime import datetime, timezone

from money_mintter.brokers.paper import PaperBroker
from money_mintter.domain import Quote, Side


def quote(price: float, symbol: str = "ABC") -> Quote:
    now = datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)
    return Quote(symbol, price - 0.05, price + 0.05, price, now)


def test_paper_broker_applies_costs_and_realizes_long_pnl() -> None:
    broker = PaperBroker(1_000.0, slippage_bps=0.0, commission_bps=10.0)
    opened = broker.submit_market_order("ABC", Side.BUY, 5.0, quote(100.0))
    assert opened.price == 100.05

    closed = broker.submit_market_order("ABC", Side.SELL, 5.0, quote(110.0))
    assert closed.realized_pnl is not None
    assert closed.realized_pnl > 40.0
    assert broker.positions() == {}


def test_paper_broker_supports_short_entry_and_cover() -> None:
    broker = PaperBroker(1_000.0, slippage_bps=0.0, commission_bps=0.0)

    opened = broker.submit_market_order("ABC", Side.SELL, 5.0, quote(100.0))
    assert opened.realized_pnl is None
    assert broker.positions()["ABC"].quantity == -5.0

    closed = broker.submit_market_order("ABC", Side.BUY, 5.0, quote(90.0))
    assert closed.realized_pnl is not None
    assert closed.realized_pnl > 40.0
    assert broker.positions() == {}


def test_paper_broker_marks_short_equity_using_ask() -> None:
    broker = PaperBroker(1_000.0, slippage_bps=0.0, commission_bps=0.0)
    broker.submit_market_order("ABC", Side.SELL, 5.0, quote(100.0))
    equity = broker.equity({"ABC": quote(90.0)})
    assert equity == 1_050.0
