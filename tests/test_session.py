from __future__ import annotations

import os
import time
import logging
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import pytest

from money_mintter.config import Settings
from money_mintter.core.session_lock import SessionLock
from money_mintter.core.live_bot import LiveMarketBot, LivePortfolioManager, main


class FakeClock:
    def __init__(self, is_open: bool, next_open: datetime, next_close: datetime) -> None:
        self.is_open = is_open
        self.next_open = next_open
        self.next_close = next_close


class FakeTradingClient:
    def __init__(self, clock: FakeClock, positions: list[dict] = None) -> None:
        self.clock = clock
        self._positions = positions or []
        self.cancel_orders_calls = 0
        self.close_all_positions_calls = 0
        self.cancel_orders_raises = False
        self.close_all_positions_raises = False
        self.call_order = []

    def get_clock(self):
        return self.clock

    def cancel_orders(self):
        self.call_order.append("cancel_orders")
        self.cancel_orders_calls += 1
        if self.cancel_orders_raises:
            raise RuntimeError("Cancel orders failed")
        return []

    def close_all_positions(self, cancel_orders: bool | None = None):
        self.call_order.append("close_all_positions")
        self.close_all_positions_calls += 1
        if self.close_all_positions_raises:
            raise RuntimeError("Close all positions failed")
        return []

    def get_all_positions(self):
        class Pos:
            def __init__(self, sym):
                self.symbol = sym
                self.qty = "10"
                self.avg_entry_price = "100.0"
                self.current_price = "100.0"
                self.unrealized_pl = "0.0"
        return [Pos(p["symbol"]) for p in self._positions]

    def get_account(self):
        class Acc:
            cash = "50000.0"
            equity = "100000.0"
            buying_power = "100000.0"
            last_equity = "100000.0"
        return Acc()

    def get_orders(self, req):
        return []


def test_settings_defaults_and_validation():
    # Break test if default values or validation checks are removed from Settings
    settings = Settings()
    assert settings.flatten_minutes_before_close == 5
    assert settings.no_new_entries_minutes_before_close == 30
    assert settings.max_wait_for_open_minutes == 45

    with pytest.raises(ValueError, match="flatten_minutes_before_close"):
        Settings(flatten_minutes_before_close=0)

    with pytest.raises(ValueError, match="no_new_entries_minutes_before_close"):
        Settings(flatten_minutes_before_close=10, no_new_entries_minutes_before_close=10)

    with pytest.raises(ValueError, match="max_wait_for_open_minutes"):
        Settings(max_wait_for_open_minutes=-1)


def test_session_lock(tmp_path):
    # Break test if SessionLock logic (exclusive creation, freshness check, stale removal) is removed
    lock_path = str(tmp_path / "test_session.lock")
    lock1 = SessionLock(lock_path, stale_hours=12)
    assert lock1.acquire() is True

    lock2 = SessionLock(lock_path, stale_hours=12)
    assert lock2.acquire() is False  # Fresh lock

    lock1.release()
    assert lock2.acquire() is True  # Released successfully
    lock2.release()

    # Test stale lock replacement
    lock3 = SessionLock(lock_path, stale_hours=12)
    assert lock3.acquire() is True
    old_time = time.time() - 13 * 3600
    os.utime(lock_path, (old_time, old_time))

    lock4 = SessionLock(lock_path, stale_hours=12)
    assert lock4.acquire() is True  # Stale lock replaced
    lock4.release()


def test_market_closed_skipped(monkeypatch):
    # Break test if market closed check and skip logic is removed
    bot = LiveMarketBot()
    t0 = datetime(2026, 10, 21, 10, 0, tzinfo=timezone.utc)
    clock = FakeClock(is_open=False, next_open=t0 + timedelta(hours=3), next_close=t0 + timedelta(hours=9))
    client = FakeTradingClient(clock)
    bot.trading_client = client
    bot.portfolio_manager = LivePortfolioManager(client, bot.logger)

    run_once_called = [False]
    monkeypatch.setattr(bot, "run_once", lambda now: run_once_called.__setitem__(0, True) or {"orders_sent": 0})

    res = bot.run_session(now_fn=lambda: t0, sleep_fn=lambda s: None)
    assert res["status"] == "skipped_market_closed"
    assert not run_once_called[0]
    assert client.cancel_orders_calls == 0
    assert client.close_all_positions_calls == 0


def test_market_closed_waits_then_runs(monkeypatch):
    # Break test if market wait logic for close-by next_open is removed
    bot = LiveMarketBot()
    t0 = datetime(2026, 10, 21, 9, 20, tzinfo=timezone.utc)
    t_open = datetime(2026, 10, 21, 9, 30, tzinfo=timezone.utc)
    t_close = datetime(2026, 10, 21, 16, 0, tzinfo=timezone.utc)

    clock_closed = FakeClock(is_open=False, next_open=t_open, next_close=t_close)
    clock_open = FakeClock(is_open=True, next_open=t_open, next_close=t_close)

    client = FakeTradingClient(clock_closed)
    bot.trading_client = client
    bot.portfolio_manager = LivePortfolioManager(client, bot.logger)

    current_time = t0
    sleep_calls = []
    def fake_sleep(secs):
        nonlocal current_time, client
        sleep_calls.append(secs)
        current_time += timedelta(seconds=secs)
        if current_time >= t_open:
            client.clock = clock_open

    monkeypatch.setattr(bot, "run_once", lambda now: {"orders_sent": 1})

    flatten_at = t_close - timedelta(minutes=bot.settings.flatten_minutes_before_close)
    res = bot.run_session(now_fn=lambda: flatten_at if current_time >= t_open else current_time, sleep_fn=fake_sleep)
    assert len(sleep_calls) > 0
    assert all(s <= 60.0 for s in sleep_calls)


def test_session_trading_cycles_completed(monkeypatch):
    # Break test if session execution loop, cutoff, and flatten_all at close are removed
    bot = LiveMarketBot()
    t_open = datetime(2026, 10, 21, 9, 30, tzinfo=timezone.utc)
    t_close = datetime(2026, 10, 21, 10, 30, tzinfo=timezone.utc)

    clock = FakeClock(is_open=True, next_open=t_open, next_close=t_close)
    client = FakeTradingClient(clock, positions=[])
    bot.trading_client = client
    bot.portfolio_manager = LivePortfolioManager(client, bot.logger)

    current_time = t_open
    def fake_sleep(secs):
        nonlocal current_time
        current_time += timedelta(seconds=secs)

    cycles_run = 0
    def fake_run_once(now):
        nonlocal cycles_run
        cycles_run += 1
        return {"orders_sent": 1}

    monkeypatch.setattr(bot, "run_once", fake_run_once)

    res = bot.run_session(now_fn=lambda: current_time, sleep_fn=fake_sleep)
    assert res["status"] == "completed"
    assert client.cancel_orders_calls == 1
    assert client.close_all_positions_calls == 1


def test_early_close_flatten(monkeypatch):
    # Break test if next_close calculation / early close handling is removed
    bot = LiveMarketBot()
    t_open = datetime(2026, 11, 27, 14, 30, tzinfo=timezone.utc)
    t_close = datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc)
    flatten_at = t_close - timedelta(minutes=5)

    clock = FakeClock(is_open=True, next_open=t_open, next_close=t_close)
    client = FakeTradingClient(clock, positions=[])
    bot.trading_client = client
    bot.portfolio_manager = LivePortfolioManager(client, bot.logger)

    current_time = flatten_at
    res = bot.run_session(now_fn=lambda: current_time, sleep_fn=lambda s: None)
    assert res["status"] == "completed"
    assert client.cancel_orders_calls == 1
    assert client.close_all_positions_calls == 1


def test_flatten_incomplete_and_exit_mapping(monkeypatch):
    # Break test if flatten_incomplete status and exit code mapping (status 3) are removed
    bot = LiveMarketBot()
    t_close = datetime(2026, 10, 21, 16, 0, tzinfo=timezone.utc)
    flatten_at = t_close - timedelta(minutes=5)

    clock = FakeClock(is_open=True, next_open=t_close - timedelta(hours=6.5), next_close=t_close)
    client = FakeTradingClient(clock, positions=[{"symbol": "AAPL"}])
    bot.trading_client = client
    bot.portfolio_manager = LivePortfolioManager(client, bot.logger)

    res = bot.run_session(now_fn=lambda: flatten_at, sleep_fn=lambda s: None)
    assert res["status"] == "flatten_incomplete"
    assert "AAPL" in res["remaining"]

    monkeypatch.setattr(LiveMarketBot, "run_session", lambda self, *a, **kw: res)
    monkeypatch.setattr(SessionLock, "acquire", lambda self: True)
    monkeypatch.setattr(SessionLock, "release", lambda self: None)
    assert main() == 3


def test_kill_switch_tripped_mid_session(monkeypatch):
    # Break test if mid-session kill switch check and halted status are removed
    bot = LiveMarketBot()
    t_open = datetime(2026, 10, 21, 9, 30, tzinfo=timezone.utc)
    t_close = datetime(2026, 10, 21, 16, 0, tzinfo=timezone.utc)

    clock = FakeClock(is_open=True, next_open=t_open, next_close=t_close)
    client = FakeTradingClient(clock, positions=[])
    bot.trading_client = client
    bot.portfolio_manager = LivePortfolioManager(client, bot.logger)

    for _ in range(bot.settings.max_consecutive_errors):
        bot.kill_switch.record_error()

    res = bot.run_session(now_fn=lambda: t_open, sleep_fn=lambda s: None)
    assert res["status"] == "halted"
    assert client.cancel_orders_calls == 1
    assert client.close_all_positions_calls == 1


def test_run_once_error_recovery(monkeypatch):
    # Break test if run_once error catching and 10s sleep / continuation logic is removed
    bot = LiveMarketBot()
    t_open = datetime(2026, 10, 21, 9, 30, tzinfo=timezone.utc)
    t_close = datetime(2026, 10, 21, 11, 0, tzinfo=timezone.utc)
    flatten_at = t_close - timedelta(minutes=5)

    clock = FakeClock(is_open=True, next_open=t_open, next_close=t_close)
    client = FakeTradingClient(clock, positions=[])
    bot.trading_client = client
    bot.portfolio_manager = LivePortfolioManager(client, bot.logger)

    def fake_run_once(now):
        return {"error": True}

    monkeypatch.setattr(bot, "run_once", fake_run_once)

    current_time = t_open
    def fake_now():
        return current_time

    def fake_sleep(secs):
        nonlocal current_time
        current_time += timedelta(hours=3)

    res = bot.run_session(now_fn=fake_now, sleep_fn=fake_sleep)
    assert res["errors"] == 1


def test_flatten_all_order_and_robustness(monkeypatch):
    # Break test if cancel_orders order before close_all_positions or exception robustness is removed
    clock = FakeClock(is_open=True, next_open=datetime.now(), next_close=datetime.now())
    client = FakeTradingClient(clock, positions=[])
    client.cancel_orders_raises = True
    manager = LivePortfolioManager(client, logging.getLogger("test"))

    remaining = manager.flatten_all(sleep_fn=lambda s: None)
    assert remaining == []
    assert "cancel_orders" in client.call_order
    assert "close_all_positions" in client.call_order
    assert client.call_order.index("cancel_orders") < client.call_order.index("close_all_positions")
    assert client.close_all_positions_calls == 1
