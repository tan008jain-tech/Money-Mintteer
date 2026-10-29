from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from money_mintter.core.live_bot import MarketScanner
from money_mintter.strategy import Candle, LiquiditySweepReversalStrategy, StrategyConfig
from money_mintter.strategy.timeframes import _resample, Timeframe
from zoneinfo import ZoneInfo


def test_in_progress_candle_not_inserted_into_cache() -> None:
    stock_client = MagicMock()
    bar_time = datetime(2026, 10, 1, 14, 30, 0, tzinfo=timezone.utc)
    mock_bar = MagicMock()
    mock_bar.timestamp = bar_time
    mock_bar.open = 100.0
    mock_bar.high = 101.0
    mock_bar.low = 99.0
    mock_bar.close = 100.5
    mock_bar.volume = 1000.0

    mock_resp = MagicMock()
    mock_resp.data = {"AAPL": [mock_bar]}
    mock_resp.next_page_token = None
    stock_client.get_stock_bars.return_value = mock_resp

    scanner = MarketScanner(["AAPL"], stock_client, MagicMock())
    strategy = LiquiditySweepReversalStrategy(StrategyConfig())

    now_utc = datetime(2026, 10, 1, 14, 30, 30, tzinfo=timezone.utc)
    scanner.scan_universe(now_utc, set(), strategy, daily_pool=["AAPL"])

    cached = scanner._candle_cache.get("AAPL", [])
    assert not any(c.timestamp == bar_time for c in cached)


def test_failed_refresh_purges_uncompleted_candle() -> None:
    stock_client = MagicMock()
    bar_time = datetime(2026, 10, 1, 14, 30, 0, tzinfo=timezone.utc)
    
    scanner = MarketScanner(["AAPL"], stock_client, MagicMock())
    scanner._candle_cache["AAPL"] = [
        Candle("AAPL", bar_time, 100.0, 101.0, 99.0, 100.5, 1000.0)
    ]

    mock_resp = MagicMock()
    mock_resp.data = {}
    mock_resp.next_page_token = None
    stock_client.get_stock_bars.return_value = mock_resp

    strategy = LiquiditySweepReversalStrategy(StrategyConfig())
    now_utc = datetime(2026, 10, 1, 14, 30, 30, tzinfo=timezone.utc)
    scanner.scan_universe(now_utc, set(), strategy, daily_pool=["AAPL"])

    cached = scanner._candle_cache.get("AAPL", [])
    assert not any(c.timestamp == bar_time for c in cached)


def test_successful_finalized_replacement_after_completion() -> None:
    stock_client = MagicMock()
    bar_time = datetime(2026, 10, 1, 14, 30, 0, tzinfo=timezone.utc)
    
    # Pre-seed cache with an initial/placeholder candle at the same timestamp with different OHLCV values
    scanner = MarketScanner(["AAPL"], stock_client, MagicMock())
    scanner._candle_cache["AAPL"] = [
        Candle("AAPL", bar_time, 90.0, 95.0, 85.0, 92.0, 500.0)
    ]

    mock_bar = MagicMock()
    mock_bar.timestamp = bar_time
    mock_bar.open = 100.0
    mock_bar.high = 102.0
    mock_bar.low = 98.0
    mock_bar.close = 101.5
    mock_bar.volume = 2000.0

    mock_resp = MagicMock()
    mock_resp.data = {"AAPL": [mock_bar]}
    mock_resp.next_page_token = None
    stock_client.get_stock_bars.return_value = mock_resp

    strategy = LiquiditySweepReversalStrategy(StrategyConfig())

    # Current time is 14:31:00 UTC — the 14:30 bar is now fully completed
    now_utc = datetime(2026, 10, 1, 14, 31, 0, tzinfo=timezone.utc)
    scanner.scan_universe(now_utc, set(), strategy, daily_pool=["AAPL"])

    cached = scanner._candle_cache.get("AAPL", [])
    matching = [c for c in cached if c.timestamp == bar_time]
    assert len(matching) == 1
    assert matching[0].open == 100.0
    assert matching[0].high == 102.0
    assert matching[0].low == 98.0
    assert matching[0].close == 101.5
    assert matching[0].volume == 2000.0


def test_valid_completed_historical_candles_preserved_on_refresh_failure() -> None:
    stock_client = MagicMock()
    bar_time_old = datetime(2026, 10, 1, 14, 29, 0, tzinfo=timezone.utc)
    
    scanner = MarketScanner(["AAPL"], stock_client, MagicMock())
    scanner._candle_cache["AAPL"] = [
        Candle("AAPL", bar_time_old, 100.0, 101.0, 99.0, 100.0, 1000.0)
    ]

    mock_resp = MagicMock()
    mock_resp.data = {}
    mock_resp.next_page_token = None
    stock_client.get_stock_bars.return_value = mock_resp

    strategy = LiquiditySweepReversalStrategy(StrategyConfig())
    now_utc = datetime(2026, 10, 1, 14, 32, 0, tzinfo=timezone.utc)
    scanner.scan_universe(now_utc, set(), strategy, daily_pool=["AAPL"])

    cached = scanner._candle_cache.get("AAPL", [])
    assert any(c.timestamp == bar_time_old for c in cached)


def test_sequential_in_progress_then_failed_refresh_resampling_isolation() -> None:
    """Verifies that an in-progress 1-minute candle is not cached, and that when a subsequent
    refresh fails at bucket close time, no unverified partial data survives to contaminate
    downstream 5-minute resampled data.
    """
    stock_client = MagicMock()
    bar_time = datetime(2026, 10, 1, 14, 30, 0, tzinfo=timezone.utc)
    
    mock_bar = MagicMock()
    mock_bar.timestamp = bar_time
    mock_bar.open = 100.0
    mock_bar.high = 105.0
    mock_bar.low = 95.0
    mock_bar.close = 104.0
    mock_bar.volume = 5000.0

    mock_resp = MagicMock()
    mock_resp.data = {"AAPL": [mock_bar]}
    mock_resp.next_page_token = None
    stock_client.get_stock_bars.return_value = mock_resp

    scanner = MarketScanner(["AAPL"], stock_client, MagicMock())
    strategy = LiquiditySweepReversalStrategy(StrategyConfig())

    # Phase 1: Scan at 14:30:30 while bar is in-progress
    t1 = datetime(2026, 10, 1, 14, 30, 30, tzinfo=timezone.utc)
    scanner.scan_universe(t1, set(), strategy, daily_pool=["AAPL"])
    assert not any(c.timestamp == bar_time for c in scanner._candle_cache.get("AAPL", []))

    # Phase 2: At bucket query time 14:35:30, refresh fails (returns empty data)
    fail_resp = MagicMock()
    fail_resp.data = {}
    fail_resp.next_page_token = None
    stock_client.get_stock_bars.return_value = fail_resp

    t2 = datetime(2026, 10, 1, 14, 35, 30, tzinfo=timezone.utc)
    scanner.scan_universe(t2, set(), strategy, daily_pool=["AAPL"])

    # Verify that resampling produces no 5-minute bucket containing the unverified partial bar
    cached_candles = scanner._candle_cache.get("AAPL", [])
    tz_ny = ZoneInfo("America/New_York")
    resampled = _resample(tuple(cached_candles), Timeframe.FIVE_MIN, tz_ny).bars
    assert not any(b.timestamp == bar_time for b in resampled)


def test_omitted_timestamp_in_refresh_response() -> None:
    """Verifies that if an in-progress candle timestamped 14:30 is encountered at 14:30:30 (and not cached),
    and a subsequent refresh at 14:35:30 returns valid completed bars for 14:31, 14:32, etc. but omits 14:30,
    the omitted 14:30 timestamp is absent from the cache and resampling reflects only the returned completed candles.
    """
    stock_client = MagicMock()
    bar_1430 = datetime(2026, 10, 1, 14, 30, 0, tzinfo=timezone.utc)
    bar_1431 = datetime(2026, 10, 1, 14, 31, 0, tzinfo=timezone.utc)
    bar_1432 = datetime(2026, 10, 1, 14, 32, 0, tzinfo=timezone.utc)

    # 1. At 14:30:30 UTC, return an in-progress candle timestamped 14:30
    mock_bar_1430 = MagicMock()
    mock_bar_1430.timestamp = bar_1430
    mock_bar_1430.open = 100.0
    mock_bar_1430.high = 110.0
    mock_bar_1430.low = 90.0
    mock_bar_1430.close = 108.0
    mock_bar_1430.volume = 10000.0

    resp1 = MagicMock()
    resp1.data = {"AAPL": [mock_bar_1430]}
    resp1.next_page_token = None
    stock_client.get_stock_bars.return_value = resp1

    scanner = MarketScanner(["AAPL"], stock_client, MagicMock())
    strategy = LiquiditySweepReversalStrategy(StrategyConfig())

    t1 = datetime(2026, 10, 1, 14, 30, 30, tzinfo=timezone.utc)
    scanner.scan_universe(t1, set(), strategy, daily_pool=["AAPL"])
    assert not any(c.timestamp == bar_1430 for c in scanner._candle_cache.get("AAPL", []))

    # 2. At 14:35:30 UTC, return valid completed bars for 14:31 and 14:32, omitting 14:30
    mock_bar_1431 = MagicMock()
    mock_bar_1431.timestamp = bar_1431
    mock_bar_1431.open = 100.0
    mock_bar_1431.high = 102.0
    mock_bar_1431.low = 98.0
    mock_bar_1431.close = 101.0
    mock_bar_1431.volume = 1000.0

    mock_bar_1432 = MagicMock()
    mock_bar_1432.timestamp = bar_1432
    mock_bar_1432.open = 101.0
    mock_bar_1432.high = 103.0
    mock_bar_1432.low = 100.0
    mock_bar_1432.close = 102.0
    mock_bar_1432.volume = 1200.0

    resp2 = MagicMock()
    resp2.data = {"AAPL": [mock_bar_1431, mock_bar_1432]}
    resp2.next_page_token = None
    stock_client.get_stock_bars.return_value = resp2

    t2 = datetime(2026, 10, 1, 14, 35, 30, tzinfo=timezone.utc)
    scanner.scan_universe(t2, set(), strategy, daily_pool=["AAPL"])

    # 3. Assert omitted 14:30 candle is absent from cache
    cached = scanner._candle_cache.get("AAPL", [])
    assert not any(c.timestamp == bar_1430 for c in cached)
    assert any(c.timestamp == bar_1431 for c in cached)
    assert any(c.timestamp == bar_1432 for c in cached)

    # Assert resampled bucket reflects only returned completed candles (no 14:30 values)
    tz_ny = ZoneInfo("America/New_York")
    resampled = _resample(tuple(cached), Timeframe.FIVE_MIN, tz_ny).bars
    bucket_1430 = [b for b in resampled if b.timestamp == datetime(2026, 10, 1, 14, 30, 0, tzinfo=timezone.utc)]
    assert len(bucket_1430) == 1
    b = bucket_1430[0]
    assert b.open == 100.0
    assert b.high == 103.0
    assert b.low == 98.0
    assert b.close == 102.0
    assert b.volume == 2200.0


def test_api_request_exception_triggers_cache_protection() -> None:
    stock_client = MagicMock()
    completed_old = datetime(2026, 10, 1, 14, 28, 0, tzinfo=timezone.utc)
    uncompleted_bar = datetime(2026, 10, 1, 14, 30, 0, tzinfo=timezone.utc)

    scanner = MarketScanner(["AAPL"], stock_client, MagicMock())
    scanner._candle_cache["AAPL"] = [
        Candle("AAPL", completed_old, 100.0, 101.0, 99.0, 100.0, 1000.0),
        Candle("AAPL", uncompleted_bar, 102.0, 103.0, 101.0, 102.5, 1200.0),
    ]

    # Stock client raises a non-connectivity exception (or batch error)
    stock_client.get_stock_bars.side_effect = RuntimeError("API service error")

    strategy = LiquiditySweepReversalStrategy(StrategyConfig())
    now_utc = datetime(2026, 10, 1, 14, 30, 30, tzinfo=timezone.utc)
    scanner.scan_universe(now_utc, set(), strategy, daily_pool=["AAPL"])

    cached = scanner._candle_cache.get("AAPL", [])
    # Completed historical bar preserved, uncompleted bar purged
    assert any(c.timestamp == completed_old for c in cached)
    assert not any(c.timestamp == uncompleted_bar for c in cached)
