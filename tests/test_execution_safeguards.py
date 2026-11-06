from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from typing import Any
import pytest

from money_mintter.config import Settings
from money_mintter.core.live_bot import LivePortfolioManager, LiveMarketBot, MarketScanner
from money_mintter.domain import Side, TradeSignal
from money_mintter.strategy import ICTStrategy, LiquiditySweepReversalStrategy, StrategyConfig, MultiTimeframeSnapshot, MultiTimeframeSeries, Candle
from alpaca.data.requests import StockLatestQuoteRequest
from alpaca.data.enums import DataFeed


class DummyQuote:
    def __init__(self, bid_price: float, ask_price: float, timestamp: datetime | None) -> None:
        self.bid_price = bid_price
        self.ask_price = ask_price
        self.timestamp = timestamp


class MockStockClient:
    def __init__(self, quotes: dict[str, Any] | None = None, raise_exc: bool = False) -> None:
        self.quotes = quotes or {}
        self.raise_exc = raise_exc
        self.requests: list[Any] = []

    def get_stock_latest_quote(self, request: StockLatestQuoteRequest) -> dict[str, Any]:
        self.requests.append(request)
        if self.raise_exc:
            raise RuntimeError("API quote connection error")
        return self.quotes


class FakeTradingClientForSafeguards:
    def __init__(self, order_status: str = "new", raise_on_lookup: bool = False) -> None:
        self.submitted_orders: list[Any] = []
        self.order_status = order_status
        self.raise_on_lookup = raise_on_lookup
        self.lookup_calls: list[str] = []

    def submit_order(self, order_req: Any) -> Any:
        self.submitted_orders.append(order_req)
        class DummyOrder:
            order_class = "bracket"
            legs = [object(), object()]
            id = "order-id-123"
        return DummyOrder()

    def get_order_by_client_id(self, client_id: str) -> Any:
        self.lookup_calls.append(client_id)
        if self.raise_on_lookup:
            raise RuntimeError("Order lookup failed")
        class QueriedOrder:
            def __init__(self, st: str) -> None:
                self.status = st
        return QueriedOrder(self.order_status)


def test_missing_stock_client_blocks_submission():
    settings = Settings()
    manager = LivePortfolioManager(
        trading_client=FakeTradingClientForSafeguards(),
        logger=logging.getLogger("test"),
        stock_client=None,  # Missing stock client
        settings=settings,
    )
    sent = manager.submit_bracket_orders([("AAPL", 0.9, Side.BUY, "test", 150.0, 147.0, 155.0, datetime.now(timezone.utc))], set(), 100000.0, settings)
    assert sent == 0


def test_missing_quote_timestamp_blocks_submission():
    now = datetime.now(timezone.utc)
    quote = DummyQuote(150.0, 150.5, timestamp=None)  # Missing timestamp
    stock_client = MockStockClient(quotes={"AAPL": quote})
    settings = Settings()
    manager = LivePortfolioManager(
        trading_client=FakeTradingClientForSafeguards(),
        logger=logging.getLogger("test"),
        stock_client=stock_client,
        settings=settings,
    )
    sent = manager.submit_bracket_orders([("AAPL", 0.9, Side.BUY, "test", 150.0, 147.0, 155.0, datetime.now(timezone.utc))], set(), 100000.0, settings)
    assert sent == 0


def test_future_quote_timestamp_rejected():
    now = datetime.now(timezone.utc)
    # Timestamp 10 seconds in the future (exceeds 2s future skew tolerance)
    quote = DummyQuote(150.0, 150.5, timestamp=now + timedelta(seconds=10))
    stock_client = MockStockClient(quotes={"AAPL": quote})
    settings = Settings()
    manager = LivePortfolioManager(
        trading_client=FakeTradingClientForSafeguards(),
        logger=logging.getLogger("test"),
        stock_client=stock_client,
        settings=settings,
    )
    sent = manager.submit_bracket_orders([("AAPL", 0.9, Side.BUY, "test", 150.0, 147.0, 155.0, datetime.now(timezone.utc))], set(), 100000.0, settings)
    assert sent == 0


def test_ambiguous_first_order_blocks_second_candidate():
    class FailingTradingClient:
        def __init__(self) -> None:
            self.submit_count = 0

        def submit_order(self, req: Any) -> Any:
            self.submit_count += 1
            raise RuntimeError("Submission failed")

        def get_order_by_client_id(self, client_id: str) -> Any:
            return None  # Order not found -> triggers safe uncertainty state

    now = datetime.now(timezone.utc)
    quote = DummyQuote(150.0, 150.5, now)
    stock_client = MockStockClient(quotes={"AAPL": quote, "MSFT": quote})
    settings = Settings()
    client = FailingTradingClient()
    manager = LivePortfolioManager(
        trading_client=client,
        logger=logging.getLogger("test"),
        stock_client=stock_client,
        settings=settings,
    )
    cands = [
        ("AAPL", 0.9, Side.BUY, "test", 150.0, 147.0, 155.0, now),
        ("MSFT", 0.85, Side.BUY, "test", 300.0, 294.0, 312.0, now),
    ]
    sent = manager.submit_bracket_orders(cands, set(), 100000.0, settings)
    assert sent == 0
    assert client.submit_count == 1  # Second candidate blocked after ambiguous first submission
    assert manager.safe_uncertainty_state is True


def test_unknown_order_status_enters_safe_uncertainty():
    class UnknownStatusTradingClient:
        def submit_order(self, req: Any) -> Any:
            raise RuntimeError("Submission failed")

        def get_order_by_client_id(self, client_id: str) -> Any:
            class Queried:
                status = "unknown_weird_status"
            return Queried()

    now = datetime.now(timezone.utc)
    quote = DummyQuote(150.0, 150.5, now)
    stock_client = MockStockClient(quotes={"AAPL": quote})
    settings = Settings()
    manager = LivePortfolioManager(
        trading_client=UnknownStatusTradingClient(),
        logger=logging.getLogger("test"),
        stock_client=stock_client,
        settings=settings,
    )
    sent = manager.submit_bracket_orders([("AAPL", 0.9, Side.BUY, "test", 150.0, 147.0, 155.0, datetime.now(timezone.utc))], set(), 100000.0, settings)
    assert sent == 0
    assert manager.safe_uncertainty_state is True


def test_known_terminal_status_handled_distinctly():
    class TerminalStatusTradingClient:
        def submit_order(self, req: Any) -> Any:
            raise RuntimeError("Submission failed")

        def get_order_by_client_id(self, client_id: str) -> Any:
            class Queried:
                status = "canceled"
            return Queried()

    now = datetime.now(timezone.utc)
    quote = DummyQuote(150.0, 150.5, now)
    stock_client = MockStockClient(quotes={"AAPL": quote})
    settings = Settings()
    manager = LivePortfolioManager(
        trading_client=TerminalStatusTradingClient(),
        logger=logging.getLogger("test"),
        stock_client=stock_client,
        settings=settings,
    )
    sent = manager.submit_bracket_orders([("AAPL", 0.9, Side.BUY, "test", 150.0, 147.0, 155.0, datetime.now(timezone.utc))], set(), 100000.0, settings)
    assert sent == 0
    assert manager.safe_uncertainty_state is False  # Terminal status does not trigger safe uncertainty state


def test_alternative_strategy_compatibility():
    # Verify LiquiditySweepReversalStrategy (alternative strategy class) compatibility with MarketScanner scan_universe
    strategy = LiquiditySweepReversalStrategy(StrategyConfig())
    stock_client = MockStockClient()
    scanner = MarketScanner(["AAPL"], stock_client, logging.getLogger("test"), data_feed=DataFeed.IEX)
    
    class MockSessionOpenScanner(MarketScanner):
        @staticmethod
        def is_session_open(sym: str, now_utc: datetime) -> tuple[bool, str]:
            return True, "Open"

    now = datetime.now(timezone.utc)
    # Mock stock client bars for scanner
    stock_client.get_stock_bars = lambda req: type("Resp", (), {"data": {"AAPL": [type("Bar", (), {"timestamp": now, "open": 100, "high": 105, "low": 99, "close": 102, "volume": 1000})()]}})()
    
    # scan_universe should execute without TypeErrors or dispatch issues
    analyzed, dashboard = MockSessionOpenScanner(["AAPL"], stock_client, logging.getLogger("test")).scan_universe(now, set(), strategy)
    assert isinstance(analyzed, list)
