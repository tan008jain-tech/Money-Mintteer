from datetime import datetime, timezone

import pytest

from money_mintter.brokers.base import BrokerError
from money_mintter.brokers.paper import PaperBroker
from money_mintter.domain import Quote, Side


def quote(price: float, symbol: str = "ABC") -> Quote:
    now = datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)
    return Quote(symbol, price - 0.05, price + 0.05, price, now)


def test_paper_broker_applies_costs_and_realizes_pnl() -> None:
    broker = PaperBroker(1_000.0, slippage_bps=0.0, commission_bps=10.0)
    opened = broker.submit_market_order("ABC", Side.BUY, 5.0, quote(100.0))
    assert opened.price == 100.05
    assert broker.available_cash < 500.0

    closed = broker.submit_market_order("ABC", Side.SELL, 5.0, quote(110.0))
    assert closed.realized_pnl is not None
    assert closed.realized_pnl > 40.0
    assert broker.positions() == {}


def test_shorting_is_explicitly_rejected() -> None:
    broker = PaperBroker(1_000.0)
    with pytest.raises(BrokerError, match="short selling"):
        broker.submit_market_order("ABC", Side.SELL, 1.0, quote(100.0))
