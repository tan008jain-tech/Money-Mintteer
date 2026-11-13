from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Sequence

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import DataFeed

from money_mintter.strategy.candles import Candle
from money_mintter.strategy.ict.adapter import _validate_and_sort_candles, ICTAcquisitionResult, _create_verified_acquisition_result

logger = logging.getLogger(__name__)


def fetch_ict_market_data(
    stock_client: StockHistoricalDataClient,
    symbol: str,
    start: datetime,
    end: datetime,
    feed: str = "iex",
) -> ICTAcquisitionResult:
    """
    Retrieve direct 1-minute and 5-minute equity candles using one explicitly
    selected realtime feed for both streams. Feed provenance is retained.
    Handles pagination correctly and validates output streams.
    """
    if start.tzinfo is None or start.utcoffset() is None:
        raise ValueError(f"naive or invalid timezone start datetime encountered: {start}")
    start = start.astimezone(timezone.utc)

    if end.tzinfo is None or end.utcoffset() is None:
        raise ValueError(f"naive or invalid timezone end datetime encountered: {end}")
    end = end.astimezone(timezone.utc)

    feed_val = str(getattr(feed, "value", feed)).strip().lower()
    if feed_val not in ("iex", "sip"):
        raise ValueError("ICT acquisition feed must be either 'iex' or 'sip'")
    feed_enum = DataFeed.SIP if feed_val == "sip" else DataFeed.IEX

    b1_bars_raw = []
    b1_success = False
    b1_error = None

    try:
        page_token = None
        while True:
            req = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame.Minute,
                start=start,
                end=end,
                feed=feed_enum,
                page_token=page_token,
            )
            resp = stock_client.get_stock_bars(req)
            resp_data = getattr(resp, "data", {}) or {}
            bars = []
            for sym_key, b_list in resp_data.items():
                if str(sym_key) == symbol or (isinstance(sym_key, (list, tuple)) and symbol in sym_key):
                    bars.extend(b_list)
            b1_bars_raw.extend(bars)

            page_token = getattr(resp, "next_page_token", None)
            if not page_token:
                break
        b1_success = True
    except Exception as e:
        b1_error = str(e)
        b1_bars_raw = []  # Clear any partial bars on pagination/stream failure
        logger.error(f"[ICT Acquisition] Failed to fetch 1m bars for {symbol} with feed {feed_val}: {e}")

    b5_bars_raw = []
    b5_success = False
    b5_error = None

    try:
        page_token = None
        while True:
            req = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame(5, TimeFrameUnit.Minute),
                start=start,
                end=end,
                feed=feed_enum,
                page_token=page_token,
            )
            resp = stock_client.get_stock_bars(req)
            resp_data = getattr(resp, "data", {}) or {}
            bars = []
            for sym_key, b_list in resp_data.items():
                if str(sym_key) == symbol or (isinstance(sym_key, (list, tuple)) and symbol in sym_key):
                    bars.extend(b_list)
            b5_bars_raw.extend(bars)

            page_token = getattr(resp, "next_page_token", None)
            if not page_token:
                break
        b5_success = True
    except Exception as e:
        b5_error = str(e)
        b5_bars_raw = []  # Clear any partial bars on pagination/stream failure
        logger.error(f"[ICT Acquisition] Failed to fetch 5m bars for {symbol} with feed {feed_val}: {e}")

    # Convert raw bars to Candle dataclasses, rejecting naive timestamps
    b1_candles = []
    for b in b1_bars_raw:
        ts = b.timestamp
        if ts.tzinfo is None:
            raise ValueError(f"naive timestamp encountered in Alpaca bar for {symbol}: {ts}")
        ts = ts.astimezone(timezone.utc)
        b1_candles.append(
            Candle(
                symbol=symbol,
                timestamp=ts,
                open=float(b.open),
                high=float(b.high),
                low=float(b.low),
                close=float(b.close),
                volume=float(b.volume),
            )
        )

    b5_candles = []
    for b in b5_bars_raw:
        ts = b.timestamp
        if ts.tzinfo is None:
            raise ValueError(f"naive timestamp encountered in Alpaca bar for {symbol}: {ts}")
        ts = ts.astimezone(timezone.utc)
        b5_candles.append(
            Candle(
                symbol=symbol,
                timestamp=ts,
                open=float(b.open),
                high=float(b.high),
                low=float(b.low),
                close=float(b.close),
                volume=float(b.volume),
            )
        )

    # Validate and sort candles strictly (raises ValueError on defects like duplicates or out of order)
    validated_b1 = _validate_and_sort_candles(b1_candles, symbol) if b1_success else ()
    validated_b5 = _validate_and_sort_candles(b5_candles, symbol) if b5_success else ()

    error_parts = []
    if b1_error:
        error_parts.append(f"1m acquisition error: {b1_error}")
    if b5_error:
        error_parts.append(f"5m acquisition error: {b5_error}")
    error_message = "; ".join(error_parts) if error_parts else None

    effective_b1_success = b1_success and (b1_error is None)
    effective_b5_success = b5_success and (b5_error is None)

    return _create_verified_acquisition_result(
        symbol=symbol,
        b1_candles=validated_b1,
        b5_candles=validated_b5,
        feed=feed_val,
        b1_success=effective_b1_success,
        b5_success=effective_b5_success,
        error_message=error_message,
    )
