from __future__ import annotations

import logging
import requests
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
import pytest

from money_mintter.core.live_bot import MarketScanner, ConnectivityError
from money_mintter.strategy import Candle, LiquiditySweepReversalStrategy, StrategyConfig, Timeframe
from money_mintter.strategy.timeframes import MultiTimeframeSeries


def test_stale_cached_candidate_cannot_displace_fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    now_utc = datetime(2026, 11, 3, 15, 0, 0, tzinfo=timezone.utc)
    bar_time = now_utc.replace(minute=55, second=0, microsecond=0) - timedelta(hours=1) # outdated
    fresh_time = now_utc.replace(minute=55, second=0, microsecond=0) - timedelta(minutes=5) # fresh

    stock_client = MagicMock()
    # Return empty on fresh fetch so cache fallback is used
    stock_client.get_stock_bars.return_value = MagicMock(data={}, next_page_token=None)

    scanner = MarketScanner(["STALE", "FRESH"], stock_client, logging.getLogger("test"))
    
    # Stale candidate has old cached bars
    scanner._candle_cache["STALE"] = [
        Candle("STALE", bar_time, 100, 105, 95, 100, 1000)
    ]
    # Fresh candidate has current cached bars
    scanner._candle_cache["FRESH"] = [
        Candle("FRESH", fresh_time, 100, 105, 95, 100, 1000)
    ]

    monkeypatch.setattr(MarketScanner, "is_session_open", lambda sym, now: (True, "Open"))
    strategy = LiquiditySweepReversalStrategy(StrategyConfig())

    analyzed, _ = scanner.scan_universe(now_utc, set(), strategy, daily_pool=["STALE", "FRESH"])
    
    analyzed_symbols = [item[0] for item in analyzed]
    assert "STALE" not in analyzed_symbols


def test_valid_current_cached_candles_remain_eligible_on_refresh_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    now_utc = datetime(2026, 11, 3, 15, 0, 0, tzinfo=timezone.utc)
    fresh_time = now_utc.replace(minute=55, second=0, microsecond=0) - timedelta(minutes=5)

    stock_client = MagicMock()
    stock_client.get_stock_bars.side_effect = requests.ConnectionError("Connection lost")

    scanner = MarketScanner(["AAPL"], stock_client, logging.getLogger("test"))
    scanner._candle_cache["AAPL"] = [
        Candle("AAPL", fresh_time, 150, 155, 145, 150, 5000)
    ]

    monkeypatch.setattr(MarketScanner, "is_session_open", lambda sym, now: (True, "Open"))
    strategy = LiquiditySweepReversalStrategy(StrategyConfig())

    # Should fall back to cache and remain eligible because candle is fresh
    analyzed, _ = scanner.scan_universe(now_utc, set(), strategy, daily_pool=["AAPL"])
    assert scanner._candle_cache.get("AAPL") is not None


def test_batch_connectivity_error_skips_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    now_utc = datetime(2026, 11, 3, 15, 0, 0, tzinfo=timezone.utc)
    stock_client = MagicMock()
    stock_client.get_stock_bars.side_effect = requests.ConnectionError("Batch connection lost")

    scanner = MarketScanner(["AAPL"], stock_client, logging.getLogger("test"))
    monkeypatch.setattr(MarketScanner, "is_session_open", lambda sym, now: (True, "Open"))
    strategy = LiquiditySweepReversalStrategy(StrategyConfig())

    scanner.scan_universe(now_utc, set(), strategy, daily_pool=["AAPL"])
    assert scanner.last_scan_connectivity_errors > 0


def test_batch_failure_triggers_symbol_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    now_utc = datetime(2026, 11, 3, 15, 0, 0, tzinfo=timezone.utc)
    bar_time = now_utc - timedelta(minutes=10)
    mock_bar = MagicMock(timestamp=bar_time, open=100, high=101, low=99, close=100, volume=1000)

    stock_client = MagicMock()
    call_count = 0

    def mock_get_bars(req):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise ValueError("Batch endpoint error triggering fallback")
        return MagicMock(data={req.symbol_or_symbols: [mock_bar]})

    stock_client.get_stock_bars.side_effect = mock_get_bars

    scanner = MarketScanner(["AAPL"], stock_client, logging.getLogger("test"))
    monkeypatch.setattr(MarketScanner, "is_session_open", lambda sym, now: (True, "Open"))
    strategy = LiquiditySweepReversalStrategy(StrategyConfig())

    scanner.scan_universe(now_utc, set(), strategy, daily_pool=["AAPL"])
    cached = scanner._candle_cache.get("AAPL", [])
    assert any(c.timestamp == bar_time for c in cached)


def test_symbol_fallback_failure_records_error(monkeypatch: pytest.MonkeyPatch) -> None:
    now_utc = datetime(2026, 11, 3, 15, 0, 0, tzinfo=timezone.utc)
    stock_client = MagicMock()

    def mock_get_bars(req):
        raise ValueError("Total failure")

    stock_client.get_stock_bars.side_effect = mock_get_bars

    scanner = MarketScanner(["AAPL"], stock_client, logging.getLogger("test"))
    monkeypatch.setattr(MarketScanner, "is_session_open", lambda sym, now: (True, "Open"))
    strategy = LiquiditySweepReversalStrategy(StrategyConfig())

    scanner.scan_universe(now_utc, set(), strategy, daily_pool=["AAPL"])
    assert scanner.last_scan_errors > 0
