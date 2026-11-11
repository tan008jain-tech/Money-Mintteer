from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock
import pytest

from alpaca.data.enums import DataFeed
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from money_mintter.strategy.candles import Candle
from money_mintter.strategy.ict.acquisition import fetch_ict_market_data, ICTAcquisitionResult
from money_mintter.strategy.ict.integration import run_ict_integration


class MockBar:
    def __init__(self, timestamp: datetime, open: float, high: float, low: float, close: float, volume: float):
        self.timestamp = timestamp
        self.open = open
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume


def test_iex_enforced_internally_and_identical_requests(monkeypatch) -> None:
    from zoneinfo import ZoneInfo
    client = MagicMock()

    class CapturedRequest:
        def __init__(self, symbol_or_symbols, timeframe, start, end, feed, page_token=None):
            self.symbol_or_symbols = symbol_or_symbols
            self.timeframe = timeframe
            self.start = start
            self.end = end
            self.feed = feed
            self.page_token = page_token

    monkeypatch.setattr("money_mintter.strategy.ict.acquisition.StockBarsRequest", CapturedRequest)

    kolkata_tz = ZoneInfo("Asia/Kolkata")
    start_local = datetime(2026, 10, 1, 20, 0, tzinfo=kolkata_tz)
    end_local = datetime(2026, 10, 1, 21, 30, tzinfo=kolkata_tz)

    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc)

    b1_bar = MockBar(start_utc, 100.0, 101.0, 99.0, 100.5, 1000.0)
    b5_bar = MockBar(start_utc, 100.0, 102.0, 98.0, 101.0, 5000.0)

    resp_b1 = MagicMock(data={"AAPL": [b1_bar]}, next_page_token=None)
    resp_b5 = MagicMock(data={"AAPL": [b5_bar]}, next_page_token=None)
    client.get_stock_bars.side_effect = [resp_b1, resp_b5]

    result = fetch_ict_market_data(client, "AAPL", start_local, end_local)

    assert result.b1_success is True
    assert result.b5_success is True
    assert result.feed == "iex"

    calls = client.get_stock_bars.call_args_list
    assert len(calls) == 2

    req1 = calls[0][0][0]
    req2 = calls[1][0][0]

    assert req1.timeframe.amount == 1
    assert req1.timeframe.unit == TimeFrameUnit.Minute
    assert req2.timeframe.amount == 5
    assert req2.timeframe.unit == TimeFrameUnit.Minute
    assert req1.symbol_or_symbols == "AAPL"
    assert req2.symbol_or_symbols == "AAPL"

    assert req1.start == start_utc
    assert req2.start == start_utc
    assert req1.end == end_utc
    assert req2.end == end_utc

    assert req1.feed == DataFeed.IEX
    assert req2.feed == DataFeed.IEX


def test_naive_timestamp_rejected() -> None:
    client = MagicMock()
    start = datetime(2026, 10, 1, 14, 30, tzinfo=timezone.utc)
    end = datetime(2026, 10, 1, 16, 0, tzinfo=timezone.utc)

    naive_dt = datetime(2026, 10, 1, 14, 30)
    b1_bar = MockBar(naive_dt, 100.0, 101.0, 99.0, 100.5, 1000.0)
    resp_b1 = MagicMock(data={"AAPL": [b1_bar]}, next_page_token=None)
    resp_b5 = MagicMock(data={"AAPL": []}, next_page_token=None)
    client.get_stock_bars.side_effect = [resp_b1, resp_b5]

    with pytest.raises(ValueError, match="naive timestamp"):
        fetch_ict_market_data(client, "AAPL", start, end)


def test_duplicate_and_out_of_order_timestamps_rejected() -> None:
    client = MagicMock()
    start = datetime(2026, 10, 1, 14, 30, tzinfo=timezone.utc)
    end = datetime(2026, 10, 1, 16, 0, tzinfo=timezone.utc)

    t1 = start
    t2 = start + timedelta(minutes=1)

    # Duplicate timestamps
    b1_bars = [
        MockBar(t1, 100.0, 101.0, 99.0, 100.5, 1000.0),
        MockBar(t1, 100.5, 101.5, 100.0, 101.0, 1200.0),
    ]
    resp_b1 = MagicMock(data={"AAPL": b1_bars}, next_page_token=None)
    resp_b5 = MagicMock(data={"AAPL": []}, next_page_token=None)
    client.get_stock_bars.side_effect = [resp_b1, resp_b5]

    with pytest.raises(ValueError, match="duplicate candle timestamp"):
        fetch_ict_market_data(client, "AAPL", start, end)

    # Out-of-order timestamps
    client.reset_mock()
    b1_bars_unordered = [
        MockBar(t2, 100.5, 101.5, 100.0, 101.0, 1200.0),
        MockBar(t1, 100.0, 101.0, 99.0, 100.5, 1000.0),
    ]
    resp_b1_ord = MagicMock(data={"AAPL": b1_bars_unordered}, next_page_token=None)
    client.get_stock_bars.side_effect = [resp_b1_ord, resp_b5]

    with pytest.raises(ValueError, match="not strictly sorted"):
        fetch_ict_market_data(client, "AAPL", start, end)


def test_pagination_failure_halfway_clears_partial_stream() -> None:
    client = MagicMock()
    start = datetime(2026, 10, 1, 14, 30, tzinfo=timezone.utc)
    end = datetime(2026, 10, 1, 16, 0, tzinfo=timezone.utc)

    bar1 = MockBar(start, 100.0, 101.0, 99.0, 100.5, 1000.0)

    resp_b1_page1 = MagicMock(data={"AAPL": [bar1]}, next_page_token="token_page_2")
    client.get_stock_bars.side_effect = [
        resp_b1_page1,
        RuntimeError("Rate limit exceeded on page 2"),
        MagicMock(data={"AAPL": []}, next_page_token=None)
    ]

    result = fetch_ict_market_data(client, "AAPL", start, end)

    assert result.b1_success is False
    assert len(result.b1_candles) == 0
    assert result.error_message is not None
    assert "Rate limit" in result.error_message


def test_missing_or_empty_stream_not_fabricated() -> None:
    client = MagicMock()
    start = datetime(2026, 10, 1, 14, 30, tzinfo=timezone.utc)
    end = datetime(2026, 10, 1, 16, 0, tzinfo=timezone.utc)

    resp_b1 = MagicMock(data={"AAPL": []}, next_page_token=None)
    resp_b5 = MagicMock(data={"AAPL": []}, next_page_token=None)
    client.get_stock_bars.side_effect = [resp_b1, resp_b5]

    result = fetch_ict_market_data(client, "AAPL", start, end)

    assert result.b1_success is True
    assert result.b5_success is True
    assert result.b1_candles == ()
    assert result.b5_candles == ()


def test_integration_compatibility_with_iex_result(monkeypatch) -> None:
    client = MagicMock()
    start = datetime(2026, 10, 1, 14, 30, tzinfo=timezone.utc)
    end = datetime(2026, 10, 1, 16, 0, tzinfo=timezone.utc)

    b1_bars = [MockBar(start + timedelta(minutes=i), 100.0, 101.0, 99.0, 100.5, 1000.0) for i in range(10)]
    b5_bars = [MockBar(start + timedelta(minutes=i*5), 100.0, 102.0, 98.0, 101.0, 5000.0) for i in range(2)]

    resp_b1 = MagicMock(data={"AAPL": b1_bars}, next_page_token=None)
    resp_b5 = MagicMock(data={"AAPL": b5_bars}, next_page_token=None)
    client.get_stock_bars.side_effect = [resp_b1, resp_b5]

    result = fetch_ict_market_data(client, "AAPL", start, end)
    assert result.b1_success and result.b5_success
    assert result.feed == "iex"

    monkeypatch.setattr("money_mintter.strategy.ict.integration.generate_all", lambda eval_dates: ())

    adapter_res, signals = run_ict_integration(
        acquisition_result=result,
        as_of=datetime(2026, 10, 1, 16, 0, tzinfo=timezone.utc),
        evaluation_dates=[datetime(2026, 10, 1).date()],
    )

    assert adapter_res.symbol == "AAPL"
    assert adapter_res.feed == "iex"
    assert len(adapter_res.b1) == 10
    assert len(adapter_res.b5) == 2
    assert signals == ()
