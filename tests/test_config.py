import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from alpaca.trading.enums import OrderSide
from money_mintter.config import Settings
from money_mintter.core.live_bot import LiveMarketBot, LivePortfolioManager, MarketScanner, next_bar_boundary, normalize_symbol
from money_mintter.domain import Side, TradeSignal


def test_settings_keep_existing_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "INITIAL_CAPITAL",
        "BASE_CURRENCY",
        "MAX_DEEP_CANDIDATES",
        "MIN_CANDIDATE_SCORE",
        "RISK_PER_TRADE_PCT",
        "MAX_POSITION_PCT",
        "MAX_PORTFOLIO_RISK_PCT",
        "PAPER_SLIPPAGE_BPS",
        "PAPER_COMMISSION_BPS",
    ):
        monkeypatch.delenv(key, raising=False)

    settings = Settings()
    assert settings.initial_capital == 10_000.0
    assert settings.risk_per_trade_pct == 0.005
    assert settings.max_position_pct == 0.20
    assert settings.max_portfolio_risk_pct == 0.02


def test_settings_read_percent_values_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RISK_PER_TRADE_PCT", "0.4")
    monkeypatch.setenv("MAX_POSITION_PCT", "25")
    monkeypatch.setenv("MAX_PORTFOLIO_RISK_PCT", "3")

    settings = Settings()

    assert settings.risk_per_trade_pct == 0.004
    assert settings.max_position_pct == 0.25
    assert settings.max_portfolio_risk_pct == 0.03


def test_settings_reject_invalid_numeric_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INITIAL_CAPITAL", "not-a-number")
    with pytest.raises(ValueError, match="INITIAL_CAPITAL"):
        Settings()


def test_max_position_pct_20_conversion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_POSITION_PCT", "20")
    settings = Settings()
    assert settings.max_position_pct == 0.20


def test_live_portfolio_manager_bracket_order_allocation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RISK_PER_TRADE_PCT", "0.5")
    monkeypatch.setenv("MAX_POSITION_PCT", "20")
    monkeypatch.setenv("MAX_PORTFOLIO_RISK_PCT", "2")
    settings = Settings()

    class FakeTradingClient:
        def __init__(self) -> None:
            self.submitted_orders: list[Any] = []

        def submit_order(self, order_req: Any) -> Any:
            self.submitted_orders.append(order_req)
            return object()

    fake_client = FakeTradingClient()
    manager = LivePortfolioManager(fake_client, logging.getLogger("test"))

    top_new = [("TEST", 0.9, Side.BUY, "r", 100.0, 99.0, 103.0)]
    manager.submit_bracket_orders(top_new, set(), 100_000.0, settings)

    assert len(fake_client.submitted_orders) == 1
    assert fake_client.submitted_orders[0].qty == 200


def test_live_portfolio_manager_sell_bracket_order_allocation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RISK_PER_TRADE_PCT", "0.5")
    monkeypatch.setenv("MAX_POSITION_PCT", "20")
    monkeypatch.setenv("MAX_PORTFOLIO_RISK_PCT", "2")
    settings = Settings()

    class FakeTradingClient:
        def __init__(self) -> None:
            self.submitted_orders: list[Any] = []

        def submit_order(self, order_req: Any) -> Any:
            self.submitted_orders.append(order_req)
            return object()

    fake_client = FakeTradingClient()
    manager = LivePortfolioManager(fake_client, logging.getLogger("test"))

    top_new = [("TEST", 0.9, Side.SELL, "r", 100.0, 101.0, 97.0)]
    manager.submit_bracket_orders(top_new, set(), 100_000.0, settings)

    assert len(fake_client.submitted_orders) == 1
    assert fake_client.submitted_orders[0].qty == 200
    assert fake_client.submitted_orders[0].side == OrderSide.SELL


def test_fetch_account_and_positions_raises_on_failure() -> None:
    class FailingTradingClient:
        def get_account(self):
            raise RuntimeError("API failure")

    client = FailingTradingClient()
    manager = LivePortfolioManager(client, logging.getLogger("test"))
    with pytest.raises(RuntimeError, match="API failure"):
        manager.fetch_account_and_positions()


def test_fetch_positions_raises_on_failure() -> None:
    class FailingPositionsClient:
        def get_account(self):
            class Acc:
                cash = "100000"
                equity = "100000"
                buying_power = "200000"
            return Acc()

        def get_all_positions(self):
            raise RuntimeError("positions failure")

    client = FailingPositionsClient()
    manager = LivePortfolioManager(client, logging.getLogger("test"))
    with pytest.raises(RuntimeError, match="positions failure"):
        manager.fetch_account_and_positions()


def test_market_scanner_with_fake_strategy_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_POSITION_PCT", "20")
    settings = Settings()

    class FakeBar:
        def __init__(self, ts, o, h, l, c, v):
            self.timestamp = ts
            self.open = o
            self.high = h
            self.low = l
            self.close = c
            self.volume = v

    class FakeBarsResponse:
        def __init__(self, data):
            self.data = data

    class FakeStockClient:
        def get_stock_bars(self, req):
            now = datetime.now(timezone.utc)
            bars = [FakeBar(now - timedelta(minutes=5 * i), 100, 101, 99, 100, 1000) for i in range(50, 0, -1)]
            return FakeBarsResponse({req.symbol_or_symbols: bars})

    class FakeStrategyWithSignal:
        def analyze(self, snapshot):
            class Analysis:
                passed = True
                reason = "ok"
            return Analysis()

        def signal(self, snapshot):
            return TradeSignal(
                symbol="AAPL",
                side=Side.BUY,
                entry_price=100.0,
                stop_price=98.0,
                target_price=101.0,
                confidence=0.85,
                timestamp=snapshot.as_of,
                reason="test signal",
            )

    class FakeTradingClient:
        def __init__(self) -> None:
            self.submitted_orders: list[Any] = []

        def submit_order(self, order_req: Any) -> Any:
            self.submitted_orders.append(order_req)
            return object()

    monkeypatch.setattr(MarketScanner, "is_session_open", lambda sym, now: (True, "Open"))
    scanner = MarketScanner(["AAPL"], FakeStockClient(), FakeStockClient(), logging.getLogger("test"))
    now_utc = datetime.now(timezone.utc)
    analyzed, _ = scanner.scan_universe(now_utc, set(), FakeStrategyWithSignal())

    assert len(analyzed) == 1
    assert analyzed[0][5] == 98.0  # stop_price
    assert analyzed[0][6] == 101.0 # target_price

    fake_client = FakeTradingClient()
    portfolio_manager = LivePortfolioManager(fake_client, logging.getLogger("test"))
    portfolio_manager.submit_bracket_orders(analyzed, set(), 100_000.0, settings)

    assert len(fake_client.submitted_orders) == 1
    assert fake_client.submitted_orders[0].stop_loss.stop_price == 98.0
    assert fake_client.submitted_orders[0].take_profit.limit_price == 101.0


def test_market_scanner_with_no_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings()

    class FakeBar:
        def __init__(self, ts, o, h, l, c, v):
            self.timestamp = ts
            self.open = o
            self.high = h
            self.low = l
            self.close = c
            self.volume = v

    class FakeBarsResponse:
        def __init__(self, data):
            self.data = data

    class FakeStockClient:
        def get_stock_bars(self, req):
            now = datetime.now(timezone.utc)
            bars = [FakeBar(now - timedelta(minutes=5 * i), 100, 101, 99, 100, 1000) for i in range(50, 0, -1)]
            return FakeBarsResponse({req.symbol_or_symbols: bars})

    class FakeStrategyNoSignal:
        def analyze(self, snapshot):
            class Analysis:
                passed = False
                reason = "no setup"
            return Analysis()

        def signal(self, snapshot):
            return None

    class FakeTradingClient:
        def __init__(self) -> None:
            self.submitted_orders: list[Any] = []

        def submit_order(self, order_req: Any) -> Any:
            self.submitted_orders.append(order_req)
            return object()

    monkeypatch.setattr(MarketScanner, "is_session_open", lambda sym, now: (True, "Open"))
    scanner = MarketScanner(["AAPL"], FakeStockClient(), FakeStockClient(), logging.getLogger("test"))
    now_utc = datetime.now(timezone.utc)
    analyzed, _ = scanner.scan_universe(now_utc, set(), FakeStrategyNoSignal())

    assert len(analyzed) == 0

    fake_client = FakeTradingClient()
    portfolio_manager = LivePortfolioManager(fake_client, logging.getLogger("test"))
    portfolio_manager.submit_bracket_orders(analyzed, set(), 100_000.0, settings)

    assert len(fake_client.submitted_orders) == 0


def test_next_bar_boundary() -> None:
    t1 = datetime(2026, 10, 18, 10, 3, 20, tzinfo=timezone.utc)
    assert next_bar_boundary(t1) == datetime(2026, 10, 18, 10, 5, 5, tzinfo=timezone.utc)

    t2 = datetime(2026, 10, 18, 10, 5, 6, tzinfo=timezone.utc)
    assert next_bar_boundary(t2) == datetime(2026, 10, 18, 10, 10, 5, tzinfo=timezone.utc)

    t3 = datetime(2026, 10, 18, 10, 59, 59, tzinfo=timezone.utc)
    assert next_bar_boundary(t3) == datetime(2026, 10, 18, 11, 0, 5, tzinfo=timezone.utc)


def test_run_cycle_counts_and_single_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_POSITION_PCT", "20")

    class FakeAccount:
        cash = "100000"
        equity = "100000"
        buying_power = "200000"

    class FakeTradingClient:
        def __init__(self) -> None:
            self.submitted_orders: list[Any] = []

        def get_account(self):
            return FakeAccount()

        def get_all_positions(self):
            return []

        def get_orders(self, req):
            return []

        def submit_order(self, order_req: Any) -> Any:
            self.submitted_orders.append(order_req)
            return object()

    class FakeBar:
        def __init__(self, ts, o, h, l, c, v):
            self.timestamp = ts
            self.open = o
            self.high = h
            self.low = l
            self.close = c
            self.volume = v

    class FakeBarsResponse:
        def __init__(self, data):
            self.data = data

    class FakeStockClient:
        def get_stock_bars(self, req):
            now = datetime.now(timezone.utc)
            bars = [FakeBar(now - timedelta(minutes=5 * i), 100, 101, 99, 100, 1000) for i in range(50, 0, -1)]
            return FakeBarsResponse({req.symbol_or_symbols: bars})

    class FakeStrategy:
        def analyze(self, snapshot):
            class Analysis:
                passed = True
                reason = "ok"
            return Analysis()

        def signal(self, snapshot):
            return TradeSignal(
                symbol="AAPL",
                side=Side.BUY,
                entry_price=100.0,
                stop_price=99.0,
                target_price=103.0,
                confidence=0.9,
                timestamp=snapshot.as_of,
                reason="signal",
            )

    bot = LiveMarketBot()
    bot.global_universe = ["AAPL"]
    bot.trading_client = FakeTradingClient()
    bot.portfolio_manager = LivePortfolioManager(bot.trading_client, bot.logger)
    bot.scanner = MarketScanner(bot.global_universe, FakeStockClient(), FakeStockClient(), bot.logger)
    bot.strategy = FakeStrategy()

    scan_calls = 0
    original_scan = bot.scanner.scan_universe
    def tracked_scan(now_utc, locked_symbols, strategy):
        nonlocal scan_calls
        scan_calls += 1
        return original_scan(now_utc, locked_symbols, strategy)
    monkeypatch.setattr(bot.scanner, "scan_universe", tracked_scan)
    monkeypatch.setattr(MarketScanner, "is_session_open", lambda sym, now: (True, "Open"))

    now_utc = datetime(2026, 10, 18, 10, 2, 0, tzinfo=timezone.utc)
    metrics = bot.run_cycle(now_utc)

    assert scan_calls == 1
    assert metrics["evaluated"] == 1
    assert metrics["signals"] == 1
    assert metrics["orders_sent"] == 1

    metrics2 = bot.run_cycle(now_utc)
    assert scan_calls == 2
    assert metrics2["orders_sent"] == 0


def test_symbol_with_open_order_does_not_produce_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_POSITION_PCT", "20")

    class FakeAccount:
        cash = "100000"
        equity = "100000"
        buying_power = "200000"

    class FakeOrder:
        symbol = "AAPL"

    class FakeTradingClient:
        def __init__(self) -> None:
            self.submitted_orders: list[Any] = []

        def get_account(self):
            return FakeAccount()

        def get_all_positions(self):
            return []

        def get_orders(self, req):
            return [FakeOrder()]

        def submit_order(self, order_req: Any) -> Any:
            self.submitted_orders.append(order_req)
            return object()

    class FakeBar:
        def __init__(self, ts, o, h, l, c, v):
            self.timestamp = ts
            self.open = o
            self.high = h
            self.low = l
            self.close = c
            self.volume = v

    class FakeBarsResponse:
        def __init__(self, data):
            self.data = data

    class FakeStockClient:
        def get_stock_bars(self, req):
            now = datetime.now(timezone.utc)
            bars = [FakeBar(now - timedelta(minutes=5 * i), 100, 101, 99, 100, 1000) for i in range(50, 0, -1)]
            return FakeBarsResponse({req.symbol_or_symbols: bars})

    class FakeStrategy:
        def analyze(self, snapshot):
            class Analysis:
                passed = True
                reason = "ok"
            return Analysis()

        def signal(self, snapshot):
            return TradeSignal(
                symbol="AAPL",
                side=Side.BUY,
                entry_price=100.0,
                stop_price=99.0,
                target_price=103.0,
                confidence=0.9,
                timestamp=snapshot.as_of,
                reason="signal",
            )

    bot = LiveMarketBot()
    bot.global_universe = ["AAPL"]
    bot.trading_client = FakeTradingClient()
    bot.portfolio_manager = LivePortfolioManager(bot.trading_client, bot.logger)
    bot.scanner = MarketScanner(bot.global_universe, FakeStockClient(), FakeStockClient(), bot.logger)
    bot.strategy = FakeStrategy()

    monkeypatch.setattr(MarketScanner, "is_session_open", lambda sym, now: (True, "Open"))
    now_utc = datetime(2026, 10, 18, 10, 2, 0, tzinfo=timezone.utc)
    metrics = bot.run_cycle(now_utc)

    assert metrics["orders_sent"] == 0
    assert len(bot.trading_client.submitted_orders) == 0


def test_crypto_slash_blocked_by_normalized_position() -> None:
    class FakeAccount:
        cash = "100000"
        equity = "100000"
        buying_power = "200000"

    class FakePosition:
        symbol = "BTCUSD"
        qty = "1"
        avg_entry_price = "50000"

    class FakeTradingClient:
        def get_account(self):
            return FakeAccount()

        def get_all_positions(self):
            return [FakePosition()]

        def get_orders(self, req):
            return []

    client = FakeTradingClient()
    manager = LivePortfolioManager(client, logging.getLogger("test"))
    _, _, _, _, _, locked = manager.fetch_account_and_positions()

    open_order_symbols = manager.fetch_open_order_symbols()
    locked_normalized = {normalize_symbol(s) for s in locked} | open_order_symbols

    assert normalize_symbol("BTC/USD") in locked_normalized
