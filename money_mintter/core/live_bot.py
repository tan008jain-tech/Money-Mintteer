from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone, time as dt_time
from math import isfinite
from typing import Any
from zoneinfo import ZoneInfo
import socket
import requests

from dotenv import load_dotenv

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, TakeProfitRequest, StopLossRequest, GetOrdersRequest, GetAssetsRequest
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus, OrderClass, AssetClass, AssetStatus
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import DataFeed
from alpaca.common.exceptions import APIError

from money_mintter.config import Settings
from money_mintter.core.eligibility import StructuralEligibilityEngine
from money_mintter.core.suitability import DailySuitabilityEngine
from money_mintter.core.killswitch import KillSwitch
from money_mintter.core.risk import RiskEngine
from money_mintter.core.session_lock import SessionLock
from money_mintter.core.selector import select_layer4_candidates
from money_mintter.domain import Side
from money_mintter.strategy import Candle, LiquiditySweepReversalStrategy, MultiTimeframeSeries, StrategyConfig, Timeframe
from money_mintter.strategy.ict.acquisition import fetch_ict_market_data
from money_mintter.strategy.ict.integration import generate_production_ict_signals, ICTDeduplicationTracker, ICTExecutionInstruction
from money_mintter.strategy.timeframes import _resample


class ConnectivityError(RuntimeError):
    pass


def is_connectivity_error(exc: Any, depth: int = 0) -> bool:
    if depth > 8 or exc is None:
        return False
    if isinstance(exc, ConnectivityError):
        return True
    if isinstance(exc, (requests.ConnectionError, requests.Timeout, socket.gaierror, TimeoutError, ConnectionError)):
        return True
    try:
        from urllib3.exceptions import NameResolutionError, MaxRetryError
        if isinstance(exc, (NameResolutionError, MaxRetryError)):
            return True
    except ImportError:
        pass
    try:
        from alpaca.common.exceptions import APIError
        if isinstance(exc, APIError) and hasattr(exc, "status_code") and exc.status_code in (429, 502, 503, 504):
            return True
    except ImportError:
        pass

    if getattr(exc, "__cause__", None) is not None:
        if is_connectivity_error(exc.__cause__, depth + 1):
            return True
    if getattr(exc, "__context__", None) is not None:
        if is_connectivity_error(exc.__context__, depth + 1):
            return True
    return False


def is_broker_rejection(exc: Any, depth: int = 0) -> bool:
    if depth > 8 or exc is None:
        return False
    try:
        from alpaca.common.exceptions import APIError
        if isinstance(exc, APIError) and hasattr(exc, "status_code"):
            if exc.status_code in (400, 422) or (400 <= exc.status_code < 500 and exc.status_code not in (408, 429)):
                return True
    except ImportError:
        pass

    exc_str = str(exc).lower()
    if "rejected by broker" in exc_str or "order rejected" in exc_str:
        return True

    if getattr(exc, "__cause__", None) is not None:
        if is_broker_rejection(exc.__cause__, depth + 1):
            return True
    if getattr(exc, "__context__", None) is not None:
        if is_broker_rejection(exc.__context__, depth + 1):
            return True
    return False


BAR_CLOSE_DELAY_SECONDS = 20
HISTORY_DAYS = 30


def apply_default_timeout(client: Any, connect: float, read: float) -> None:
    if not hasattr(client, "_session") or client._session is None:
        raise RuntimeError("cannot apply HTTP timeout")
    session = client._session
    if not hasattr(session, "request"):
        raise RuntimeError("cannot apply HTTP timeout")
    orig_request = session.request
    if getattr(orig_request, "_mm_timeout_applied", False):
        return

    def wrapped_request(method, url, **kwargs):
        kwargs.setdefault("timeout", (connect, read))
        return orig_request(method, url, **kwargs)

    wrapped_request._mm_timeout_applied = True
    session.request = wrapped_request


def normalize_symbol(sym: str) -> str:
    return sym.upper().replace("/", "").replace("-", "")


def next_bar_boundary(now_utc: datetime, minutes: int = 5, delay_seconds: int = BAR_CLOSE_DELAY_SECONDS) -> datetime:
    base = now_utc.replace(minute=0, second=0, microsecond=0)
    total_seconds = (now_utc - base).total_seconds()
    interval_sec = minutes * 60
    k = int(total_seconds // interval_sec)
    candidate = base + timedelta(seconds=(k * interval_sec) + delay_seconds)
    if candidate <= now_utc:
        k += 1
        candidate = base + timedelta(seconds=(k * interval_sec) + delay_seconds)
    return candidate


def discover_universe(trading_client: TradingClient, logger: logging.Logger, api_key: str = "") -> list[str]:
    """Discover Layer 1 assets, enforce its health contract, then evaluate Layer 2.

    Returns only a validated Layer 2 pool. Raw Layer 1 symbols are never returned,
    including during API outages or cache recovery.
    """
    engine = StructuralEligibilityEngine()
    settings = Settings()
    start_time = datetime.now(timezone.utc)
    search_params = GetAssetsRequest(status=AssetStatus.ACTIVE, asset_class=AssetClass.US_EQUITY)
    data_source = "Alpaca Live API"
    raw_asset_count = 0
    cached_used = False
    cache_age_hours = 0.0
    min_health = settings.min_universe_health

    def load_healthy_l1_cache(reason: str):
        cached_symbols, cached_metadata, cached_fetched = engine.load_validated_cache(
            now_utc=start_time,
            max_age_hours=settings.max_universe_cache_age_hours,
            min_symbols=min_health,
        )
        if cached_symbols and cached_fetched:
            age = (start_time - cached_fetched).total_seconds() / 3600.0
            logger.warning(
                f"[Discovery] Using validated Layer 1 cache because {reason}; "
                f"symbols={len(cached_symbols)}, age={age:.2f}h."
            )
            return cached_symbols, cached_metadata, cached_fetched
        logger.error(f"[Discovery] No compatible Layer 1 cache available after {reason}; new entries disabled.")
        return [], {}, None

    assets = None
    try:
        if hasattr(trading_client, "get_all_assets"):
            try:
                assets = trading_client.get_all_assets(search_params)
            except Exception as sdk_exc:
                err_str = str(sdk_exc)
                if (
                    "validation errors" in err_str
                    or "ValidationError" in type(sdk_exc).__name__
                    or "exchange" in err_str
                ) and hasattr(trading_client, "get"):
                    assets = trading_client.get("/assets", search_params.to_request_fields())
                    data_source = "Alpaca REST Fallback"
                else:
                    raise
        elif hasattr(trading_client, "get"):
            assets = trading_client.get("/assets", search_params.to_request_fields())
            data_source = "Alpaca REST Get"
        else:
            raise RuntimeError("trading_client does not support asset retrieval")
    except Exception as exc:
        logger.error(f"[Discovery] Failed to fetch assets from Alpaca API: {exc}")
        cached_symbols, cached_metadata, cached_fetched = load_healthy_l1_cache("Alpaca API failure")
        if not cached_symbols:
            raise ConnectivityError(f"Asset discovery failed and no healthy Layer 1 cache exists: {exc}") from exc
        eligible_symbols, metadata_map = cached_symbols, cached_metadata
        cached_used = True
        raw_asset_count = len(cached_symbols)
        cache_age_hours = (start_time - cached_fetched).total_seconds() / 3600.0

    if assets is None and not cached_used:
        cached_symbols, cached_metadata, cached_fetched = load_healthy_l1_cache("empty API response")
        if not cached_symbols:
            raise RuntimeError("Alpaca asset discovery returned None and no healthy Layer 1 cache exists")
        eligible_symbols, metadata_map = cached_symbols, cached_metadata
        cached_used = True
        raw_asset_count = len(cached_symbols)
        cache_age_hours = (start_time - cached_fetched).total_seconds() / 3600.0

    if not cached_used:
        raw_asset_count = len(assets) if assets is not None else 0
        eligible_symbols, metadata_map, exclusion_counts = engine.filter_universe(assets or [], logger)
    else:
        exclusion_counts = {}

    if len(eligible_symbols) < min_health:
        logger.warning(
            f"[Discovery] Live Layer 1 universe unhealthy: eligible={len(eligible_symbols)}, "
            f"required={min_health}. Checking recent validated cache."
        )
        cached_symbols, cached_metadata, cached_fetched = load_healthy_l1_cache(
            f"live Layer 1 health floor failure ({len(eligible_symbols)}<{min_health})"
        )
        if cached_symbols:
            eligible_symbols, metadata_map = cached_symbols, cached_metadata
            cached_used = True
            raw_asset_count = len(cached_symbols)
            cache_age_hours = (start_time - cached_fetched).total_seconds() / 3600.0
        else:
            logger.critical(
                "[Universe Layer 1] No healthy live or cached universe. "
                "Position-management-only mode active; Layer 2 and new entries disabled."
            )
            return []

    logger.info(
        f"[Discovery Observability] L1 Source={data_source} raw={raw_asset_count} "
        f"eligible={len(eligible_symbols)} cached={cached_used} cache_age_hours={cache_age_hours:.2f} "
        f"health_floor={min_health} cache_max_age_hours={settings.max_universe_cache_age_hours:.1f} "
        f"exclusions={exclusion_counts}"
    )
    if not cached_used:
        engine.save_persisted_cache(eligible_symbols, metadata_map, start_time)

    suitability_engine = DailySuitabilityEngine(
        logger=logger, settings=settings, historical_feed=settings.alpaca_historical_feed
    )
    requested_feed = settings.alpaca_historical_feed.upper()
    try:
        suitability_result = suitability_engine.evaluate_universe(eligible_symbols, start_time)
        daily_pool = suitability_result.pool or []
        quarantined_count = len(suitability_result.quarantined)
        excluded_count = max(0, len(eligible_symbols) - len(daily_pool) - quarantined_count)
        actual_feed = suitability_result.actual_feed
        requested_feed = suitability_result.requested_feed
        stale_flag = suitability_result.stale
        new_entries_disabled = not bool(daily_pool)
    except Exception as exc:
        logger.error(f"[Universe Layer 2] Evaluation failed: {exc}. Checking compatible Layer 2 stale fallback.")
        cached_pool_res = suitability_engine.load_stale_fallback_pool(eligible_symbols=eligible_symbols)
        if cached_pool_res and cached_pool_res.pool:
            daily_pool = cached_pool_res.pool
            quarantined_count = len(cached_pool_res.quarantined)
            excluded_count = 0
            actual_feed = cached_pool_res.actual_feed
            requested_feed = cached_pool_res.requested_feed
            stale_flag = True
            new_entries_disabled = False
            logger.warning(
                f"[Universe Layer 2] Using compatible stale Layer 2 pool: "
                f"pool={len(daily_pool)}, actual_feed={actual_feed}."
            )
        else:
            daily_pool = []
            quarantined_count = 0
            excluded_count = len(eligible_symbols)
            actual_feed = requested_feed
            stale_flag = True
            new_entries_disabled = True
            logger.critical(
                "[Universe Layer 2] No valid compatible Layer 2 pool exists. "
                "Position-management-only mode active; new entries disabled."
            )

    logger.info(
        f"[Universe Layer 1 & 2 Observability] L1 raw={raw_asset_count} eligible={len(eligible_symbols)} "
        f"L2 evaluated={len(eligible_symbols)} pool={len(daily_pool)} max_pool={settings.max_daily_pool_size} "
        f"quarantined={quarantined_count} excluded={excluded_count} "
        f"historical_feed_requested={requested_feed} historical_feed_actual={actual_feed} "
        f"stale={stale_flag} new_entries_disabled={new_entries_disabled}"
    )
    return daily_pool



def count_b15_gaps(b15: tuple[dict[str, Any], ...]) -> int:
    gaps = 0
    for i in range(1, len(b15)):
        prev = b15[i-1]
        curr = b15[i]
        if prev.get("date") == curr.get("date"):
            diff_ns = curr["time_ns"] - prev["time_ns"]
            if diff_ns > 15 * 60 * 1_000_000_000:
                gaps += 1
    return gaps


class MarketScanner:
    """Object-oriented Market Screener modeled after Freqtrade DataProvider / Lumibot Scanners.
    Responsible for global universe ingestion, session filtering, and candidate scoring.
    """
    def __init__(self, global_universe: list[str], stock_client: StockHistoricalDataClient, logger: logging.Logger, data_feed: DataFeed = DataFeed.IEX) -> None:
        self.global_universe = global_universe
        self.stock_client = stock_client
        self.logger = logger
        self.data_feed = data_feed
        self.latest_snapshots: dict[str, MultiTimeframeSnapshot] = {}
        self._candle_cache: dict[str, list[Candle]] = {}
        self.last_scan_attempts = 0
        self.last_scan_errors = 0
        self.last_scan_connectivity_errors = 0
        self.ict_attempts = 0
        self.ict_acquisition_successes = 0
        self.ict_evaluation_successes = 0
        self.ict_errors = 0
        self.ict_connectivity_errors = 0
        self.ict_raw_candidates = 0
        self.ict_entry_mode_passes = 0
        self.ict_instructions = 0
        self.last_l2_eligible = 0
        self.last_l4_selected = 0

    @staticmethod
    def is_session_open(symbol: str, now_utc: datetime) -> tuple[bool, str]:
        sym_upper = symbol.upper()
        if "/" in sym_upper or sym_upper.endswith("=F"):
            return False, "Unsupported symbol"

        try:
            ny_time = now_utc.astimezone(ZoneInfo("America/New_York"))
        except Exception:
            ny_time = now_utc

        if ny_time.weekday() >= 5:
            return False, "US Equities 🔴 (Weekend Closed)"

        market_open = dt_time(9, 30)
        market_close = dt_time(16, 0)

        if market_open <= ny_time.time() < market_close:
            return True, "US Equities 🟢 (Core Regular Hours 9:30-16:00 EST/EDT)"
        else:
            return False, "US Equities 🔴 (Market Closed)"

    @staticmethod
    def calculate_vwap(candles: tuple[Candle, ...]) -> float:
        if not candles:
            return 0.0
        cum_pv = sum(((c.high + c.low + c.close) / 3.0) * c.volume for c in candles)
        cum_v = sum(c.volume for c in candles)
        return cum_pv / cum_v if cum_v > 0 else candles[-1].close

    def scan_universe(self, now_utc: datetime, locked_symbols: set[str], strategy: Any = None, daily_pool: list[str] | None = None, dedup_tracker: ICTDeduplicationTracker | None = None, **kwargs: Any) -> tuple[Any, list[dict[str, Any]]]:
        self.last_scan_attempts = 0
        self.last_scan_errors = 0
        self.last_scan_connectivity_errors = 0
        self.ict_attempts = 0
        self.ict_acquisition_successes = 0
        self.ict_evaluation_successes = 0
        self.ict_errors = 0
        self.ict_connectivity_errors = 0
        self.ict_raw_candidates = 0
        self.ict_entry_mode_passes = 0
        self.ict_instructions = 0

        analyzed_candidates: list[tuple[str, float, Side | None, str, float, float, float]] = []
        ict_instructions: list[ICTExecutionInstruction] = []
        dashboard_stocks = []
        locked_normalized = {normalize_symbol(s) for s in locked_symbols}

        pool_input = daily_pool if daily_pool is not None else self.global_universe
        self.last_l2_eligible = len(pool_input)
        self.logger.info(f"Scanning Layer 2 daily asset pool ({len(pool_input)} assets) with core trading hours filtration...")

        eligible_symbols = []
        for symbol in pool_input:
            clean_symbol = symbol.upper().strip()
            if "/" in clean_symbol:
                self.logger.info(f"Skipping {clean_symbol}: Unsupported symbol")
                continue

            open_status, session_msg = MarketScanner.is_session_open(clean_symbol, now_utc)
            if not open_status:
                self.logger.info(f"Skipping {clean_symbol}: {session_msg}")
                continue

            if normalize_symbol(clean_symbol) in locked_normalized:
                self.logger.info(f"Retaining locked active trade slot for {clean_symbol} (Module 3.2 Slot Freezing)")
                continue
            eligible_symbols.append((clean_symbol, session_msg))

        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=timezone.utc)

        all_bars_data = {}
        BATCH_SIZE = 50
        for i in range(0, len(eligible_symbols), BATCH_SIZE):
            batch = eligible_symbols[i:i + BATCH_SIZE]
            batch_symbols = [item[0] for item in batch]

            self.last_scan_attempts += len(batch_symbols)
            try:
                self.logger.info(f"Analyzing batch of {len(batch_symbols)} symbols...")

                # Determine incremental start per symbol in batch
                symbol_start_dts = []
                for sym, _ in batch:
                    cached = self._candle_cache.get(sym, [])
                    if cached:
                        last_ts = max(c.timestamp for c in cached)
                        if last_ts.tzinfo is None:
                            last_ts = last_ts.replace(tzinfo=timezone.utc)
                        symbol_start_dts.append(last_ts - timedelta(minutes=15))
                    else:
                        symbol_start_dts.append(now_utc - timedelta(days=HISTORY_DAYS))

                batch_start_dt = min(symbol_start_dts) if symbol_start_dts else (now_utc - timedelta(days=HISTORY_DAYS))
                if batch_start_dt.tzinfo is None:
                    batch_start_dt = batch_start_dt.replace(tzinfo=timezone.utc)

                page_token = None
                while True:
                    bar_request = StockBarsRequest(
                        symbol_or_symbols=batch_symbols[0] if len(batch_symbols) == 1 else batch_symbols,
                        timeframe=TimeFrame(1, TimeFrameUnit.Minute),
                        start=batch_start_dt,
                        end=now_utc,
                        feed=self.data_feed,
                        page_token=page_token,
                    )
                    bars_response = self.stock_client.get_stock_bars(bar_request)
                    resp_data = getattr(bars_response, 'data', {}) or {}
                    for sym_key, bars in resp_data.items():
                        if isinstance(sym_key, (list, tuple)):
                            for s in sym_key:
                                all_bars_data.setdefault(s, []).extend(bars)
                        else:
                            all_bars_data.setdefault(str(sym_key), []).extend(bars)

                    page_token = getattr(bars_response, 'next_page_token', None)
                    if not page_token:
                        break

            except Exception as batch_ex:
                if is_connectivity_error(batch_ex):
                    self.last_scan_errors += len(batch_symbols)
                    self.last_scan_connectivity_errors += len(batch_symbols)
                    self.logger.error(f"CRITICAL: Connectivity/Auth error for batch {batch_symbols}: {batch_ex}")
                    continue
                else:
                    self.logger.warning(f"Batch request failed for {batch_symbols} ({batch_ex}), falling back to symbol-level isolation...")
                    for sym, _ in batch:
                        try:
                            single_req = StockBarsRequest(
                                symbol_or_symbols=sym,
                                timeframe=TimeFrame(1, TimeFrameUnit.Minute),
                                start=batch_start_dt,
                                end=now_utc,
                                feed=self.data_feed,
                            )
                            single_resp = self.stock_client.get_stock_bars(single_req)
                            single_data = getattr(single_resp, 'data', {}) or {}
                            for sym_key, bars in single_data.items():
                                all_bars_data.setdefault(str(sym_key), []).extend(bars)
                        except Exception as single_ex:
                            self.last_scan_errors += 1
                            if is_connectivity_error(single_ex):
                                self.last_scan_connectivity_errors += 1
                            self.logger.error(f"Failed to fetch bars for symbol {sym}: {single_ex}")

        bars_dict: dict[str, list[Candle]] = {}
        for clean_symbol, _ in eligible_symbols:
            symbol_bars = all_bars_data.get(clean_symbol, [])
            cached = self._candle_cache.get(clean_symbol, [])
            if not symbol_bars:
                completed_cached = [
                    c for c in cached
                    if c.timestamp + timedelta(minutes=1) <= now_utc
                ]
                self._candle_cache[clean_symbol] = completed_cached
                symbol_candles = completed_cached
            else:
                try:
                    new_candles = [
                        Candle(
                            timestamp=b.timestamp.replace(tzinfo=timezone.utc) if b.timestamp.tzinfo is None else b.timestamp,
                            open=float(b.open),
                            high=float(b.high),
                            low=float(b.low),
                            close=float(b.close),
                            volume=float(b.volume),
                            symbol=clean_symbol,
                        )
                        for b in symbol_bars
                    ]
                except (AttributeError, TypeError, ValueError) as bar_error:
                    self.last_scan_errors += 1
                    self.logger.error(
                        f"Excluding {clean_symbol}: malformed one-minute OHLCV response: {bar_error}"
                    )
                    continue
                completed_new_candles = [
                    c for c in new_candles
                    if c.timestamp + timedelta(minutes=1) <= now_utc
                ]
                combined = {c.timestamp: c for c in cached}
                for c in completed_new_candles:
                    combined[c.timestamp] = c
                symbol_candles = sorted(combined.values(), key=lambda x: x.timestamp)
                cutoff = now_utc - timedelta(days=HISTORY_DAYS + 2)
                symbol_candles = [
                    c for c in symbol_candles
                    if c.timestamp >= cutoff and c.timestamp + timedelta(minutes=1) <= now_utc
                ]
                self._candle_cache[clean_symbol] = symbol_candles

            if symbol_candles:
                completed_candles = [
                    c for c in symbol_candles
                    if c.timestamp + timedelta(minutes=1) <= now_utc
                ]
                if completed_candles:
                    bars_dict[clean_symbol] = completed_candles

        # Layer 4 is shared by both production and legacy routes. Reject stale
        # completed 1m data before ranking so the active ICT route cannot select
        # a candidate from an old movement burst.
        minute = (now_utc.minute // 5) * 5
        bar_time = now_utc.replace(minute=minute, second=0, microsecond=0)
        expected_last_bar_start = bar_time - timedelta(minutes=5)
        expected_last_1m_bar_start = (
            now_utc.replace(second=0, microsecond=0) - timedelta(minutes=1)
        )
        fresh_bars_dict: dict[str, list[Candle]] = {}
        for symbol, candles in bars_dict.items():
            if not candles:
                continue
            latest_ts = max(
                (
                    candle.timestamp.replace(tzinfo=timezone.utc)
                    if candle.timestamp.tzinfo is None
                    else candle.timestamp.astimezone(timezone.utc)
                )
                for candle in candles
            )
            if latest_ts < expected_last_1m_bar_start:
                self.logger.warning(
                    f"stale bars (Layer 4) for {symbol}: latest_1m={latest_ts}, "
                    f"expected_1m>={expected_last_1m_bar_start}; excluding before selection"
                )
                continue
            fresh_bars_dict[symbol] = list(candles)
        bars_dict = fresh_bars_dict

        layer4_selected = select_layer4_candidates(pool_input, bars_dict, max_candidates=8)
        self.last_l4_selected = len(layer4_selected)
        self.logger.info(f"[Layer 4 Selector] Selected {len(layer4_selected)} candidates from pool of {len(pool_input)} symbols.")

        if strategy is not None:
            for item in layer4_selected:
                clean_symbol = item.symbol
                try:
                    symbol_candles = self._candle_cache.get(clean_symbol, [])
                    if not symbol_candles:
                        continue

                    timezone_obj = ZoneInfo("America/New_York")
                    resampled_5m = _resample(tuple(symbol_candles), Timeframe.FIVE_MIN, timezone_obj).bars
                    if not resampled_5m:
                        continue

                    series = MultiTimeframeSeries.from_5m(
                        list(resampled_5m),
                        entry_timeframe=Timeframe.FIVE_MIN,
                        timezone_name="America/New_York",
                        spread_bps=2.0,
                    )
                    snapshot = series.snapshot_at(now_utc)

                    minute = (now_utc.minute // 5) * 5
                    bar_time = now_utc.replace(minute=minute, second=0, microsecond=0)
                    expected_last_bar_start = bar_time - timedelta(minutes=5)
                    if expected_last_bar_start.tzinfo is None:
                        expected_last_bar_start = expected_last_bar_start.replace(tzinfo=timezone.utc)

                    if not snapshot.entry:
                        self.logger.warning(f"stale bars for {clean_symbol}: no entry-timeframe candles in snapshot")
                        continue

                    last_entry = snapshot.entry[-1]
                    last_ts = last_entry.timestamp
                    if last_ts.tzinfo is None:
                        last_ts = last_ts.replace(tzinfo=timezone.utc)

                    if last_ts < expected_last_bar_start:
                        self.logger.warning(f"stale bars for {clean_symbol}: latest={last_ts}, expected>={expected_last_bar_start}")
                        continue
                    self.latest_snapshots[clean_symbol] = snapshot
                    self.logger.info(f"[{clean_symbol}] MTF Chart Analysis | {snapshot.candle_bias_summary}")

                    analysis = strategy.analyze(snapshot)
                    signal = strategy.signal(snapshot)
                    if signal is None:
                        reason_msg = analysis.reason if analysis else "no signal"
                        self.logger.info(f"[{clean_symbol}] No signal generated | Reason: {reason_msg}")
                        continue

                    self.logger.info(f"[{clean_symbol}] Signal | Score: {signal.confidence:.2f} | Side: {signal.side} | Entry: ${signal.entry_price:.2f}")

                    analyzed_candidates.append((clean_symbol, signal.confidence, signal.side, signal.reason, signal.entry_price, signal.stop_price, signal.target_price))
                    dashboard_stocks.append({
                        "symbol": clean_symbol,
                        "market": "Global/US",
                        "current_price": signal.entry_price,
                        "score": signal.confidence,
                    })
                except Exception as sym_ex:
                    self.last_scan_errors += 1
                    if is_connectivity_error(sym_ex):
                        self.last_scan_connectivity_errors += 1
                    self.logger.error(f"CRITICAL: Symbol analysis error for {clean_symbol}: {sym_ex}")
            return analyzed_candidates, dashboard_stocks
        else:
            dedup = dedup_tracker if dedup_tracker is not None else ICTDeduplicationTracker()
            for item in layer4_selected:
                clean_symbol = item.symbol
                self.ict_attempts += 1
                try:
                    start_dt = now_utc - timedelta(days=HISTORY_DAYS)
                    if start_dt.tzinfo is None:
                        start_dt = start_dt.replace(tzinfo=timezone.utc)

                    acq_result = fetch_ict_market_data(
                        self.stock_client, clean_symbol, start_dt, now_utc,
                        feed=getattr(self.data_feed, "value", str(self.data_feed)).lower(),
                    )
                    if not (acq_result.b1_success and acq_result.b5_success):
                        self.ict_errors += 1
                        self.logger.error(f"[ICT Pipeline] symbol={clean_symbol} acquisition=failure error={acq_result.error_message}")
                        continue
                    self.ict_acquisition_successes += 1

                    diag = {}
                    adapter_res, instructions = generate_production_ict_signals(
                        acq_result, now_utc, dedup_tracker=dedup, _allow_historical_evaluation=False, diagnostics=diag
                    )

                    if not adapter_res.b1 or not adapter_res.b5:
                        self.ict_errors += 1
                        self.logger.error(
                            f"[ICT Pipeline] symbol={clean_symbol} stale/incomplete adapter data: "
                            "one-minute or five-minute stream is empty; executable instructions discarded"
                        )
                        continue

                    latest_b1_ns = max(bar["time_ns"] for bar in adapter_res.b1)
                    latest_b5_ns = max(bar["time_ns"] for bar in adapter_res.b5)
                    latest_b1_ts = datetime.fromtimestamp(
                        latest_b1_ns / 1_000_000_000, tz=timezone.utc
                    )
                    latest_b5_ts = datetime.fromtimestamp(
                        latest_b5_ns / 1_000_000_000, tz=timezone.utc
                    )
                    if (
                        latest_b1_ts < expected_last_1m_bar_start
                        or latest_b5_ts < expected_last_bar_start
                    ):
                        self.logger.warning(
                            f"[ICT Pipeline] symbol={clean_symbol} stale dedicated acquisition: "
                            f"latest_1m={latest_b1_ts} expected_1m>={expected_last_1m_bar_start}; "
                            f"latest_5m={latest_b5_ts} expected_5m>={expected_last_bar_start}; "
                            "executable instructions discarded"
                        )
                        continue
                    self.ict_evaluation_successes += 1

                    b1_len = len(adapter_res.b1)
                    b5_len = len(adapter_res.b5)
                    b15_len = len(adapter_res.b15)
                    b15_complete = sum(1 for e in adapter_res.b15_coverage if e["status"] == "complete")
                    b15_partial = sum(1 for e in adapter_res.b15_coverage if e["status"] == "partial")
                    b15_gaps = count_b15_gaps(adapter_res.b15)
                    raw_entries = diag.get("raw_entries_count", 0)
                    entry_mode_passes = diag.get("entry_mode_passes_count", 0)
                    inst_count = len(instructions)

                    self.ict_raw_candidates += raw_entries
                    self.ict_entry_mode_passes += entry_mode_passes
                    self.ict_instructions += inst_count

                    self.logger.info(
                        f"[ICT Pipeline] symbol={clean_symbol} acquisition=success b1={b1_len} b5={b5_len} adapter=success "
                        f"b15={b15_len} b15_complete={b15_complete} b15_partial={b15_partial} b15_gaps={b15_gaps} "
                        f"raw_entries={raw_entries} entry_mode_passes={entry_mode_passes} instructions={inst_count}"
                    )

                    for inst in instructions:
                        ict_instructions.append(inst)
                        dashboard_stocks.append({
                            "symbol": clean_symbol,
                            "market": "Global/US",
                            "current_price": inst.entry_price,
                            "score": 1.0,
                        })
                except Exception as sym_ex:
                    self.ict_errors += 1
                    if is_connectivity_error(sym_ex):
                        self.ict_connectivity_errors += 1
                    self.logger.error(f"CRITICAL: ICT acquisition/signal error for {clean_symbol}: {sym_ex}")
            return tuple(ict_instructions), dashboard_stocks


class LivePortfolioManager:
    """Portfolio & Position Manager modeled after Freqtrade Trade persistence model.
    Handles order execution, bracket orders, state tracking, and lock protection.
    """
    def __init__(
        self,
        trading_client: TradingClient,
        logger: logging.Logger,
        stock_client: StockHistoricalDataClient | None = None,
    ) -> None:
        self.trading_client = trading_client
        self.logger = logger
        self.stock_client = stock_client
        self.last_equity: float | None = None
        self.last_submit_failures = 0
        self.last_flatten_errors = 0
        self._last_equity_date = None
        self.on_unprotected: Any | None = None

    def _get_executable_price(
        self,
        symbol: str,
        side: Side,
        signal_entry_price: float,
        settings: Settings,
    ) -> float | None:
        """Return a fresh side-aware quote price, or fail closed with a logged reason."""
        if self.stock_client is None or not hasattr(self.stock_client, "get_stock_latest_quote"):
            self.logger.info(
                f"Skipping {symbol}: latest-quote client unavailable [REJECTED_QUOTE_CLIENT_UNAVAILABLE]"
            )
            return None
        if not isfinite(signal_entry_price) or signal_entry_price <= 0:
            self.logger.info(
                f"Skipping {symbol}: invalid signal entry price [REJECTED_INVALID_SIGNAL_ENTRY]"
            )
            return None

        feed_name = settings.alpaca_realtime_feed.strip().lower()
        if feed_name not in ("iex", "sip"):
            self.logger.info(
                f"Skipping {symbol}: unsupported realtime quote feed {feed_name!r} "
                "[REJECTED_INVALID_QUOTE_FEED]"
            )
            return None
        feed_enum = DataFeed.SIP if feed_name == "sip" else DataFeed.IEX
        feed_label = "SIP NBBO" if feed_enum == DataFeed.SIP else "IEX reference quote"

        try:
            request = StockLatestQuoteRequest(symbol_or_symbols=[symbol], feed=feed_enum)
            response = self.stock_client.get_stock_latest_quote(request)
            if isinstance(response, dict):
                quote = response.get(symbol) or response.get(symbol.upper())
            else:
                quote = getattr(response, symbol, None) or getattr(response, symbol.upper(), None)
            if quote is None:
                self.logger.info(
                    f"Skipping {symbol}: no latest quote returned from {feed_label} "
                    "[REJECTED_QUOTE_MISSING]"
                )
                return None

            quote_time = getattr(quote, "timestamp", None)
            if quote_time is None or not isinstance(quote_time, datetime):
                self.logger.info(
                    f"Skipping {symbol}: missing or invalid quote timestamp from {feed_label} "
                    "[REJECTED_QUOTE_TIMESTAMP_MISSING]"
                )
                return None
            if quote_time.tzinfo is None:
                quote_time = quote_time.replace(tzinfo=timezone.utc)

            quote_age = (datetime.now(timezone.utc) - quote_time.astimezone(timezone.utc)).total_seconds()
            if quote_age < -settings.quote_max_future_skew_seconds:
                self.logger.info(
                    f"Skipping {symbol}: future quote timestamp from {feed_label} "
                    f"(age={quote_age:.2f}s) [REJECTED_FUTURE_QUOTE]"
                )
                return None
            if quote_age > settings.quote_max_age_seconds:
                self.logger.info(
                    f"Skipping {symbol}: stale quote from {feed_label} "
                    f"(age={quote_age:.2f}s) [REJECTED_STALE_QUOTE]"
                )
                return None

            bid = float(getattr(quote, "bid_price", 0.0) or 0.0)
            ask = float(getattr(quote, "ask_price", 0.0) or 0.0)
            if not isfinite(bid) or not isfinite(ask) or bid <= 0 or ask <= 0:
                self.logger.info(
                    f"Skipping {symbol}: invalid quote prices from {feed_label} "
                    f"(bid={bid}, ask={ask}) [REJECTED_INVALID_QUOTE_PRICES]"
                )
                return None
            if bid >= ask:
                self.logger.info(
                    f"Skipping {symbol}: crossed or locked quote from {feed_label} "
                    f"(bid={bid:.4f}, ask={ask:.4f}) [REJECTED_CROSSED_QUOTE]"
                )
                return None

            midpoint = (bid + ask) / 2.0
            spread_pct = (ask - bid) / midpoint
            if spread_pct > settings.max_quote_spread_pct:
                self.logger.info(
                    f"Skipping {symbol}: quote spread={spread_pct:.4%} exceeds "
                    f"{settings.max_quote_spread_pct:.4%} from {feed_label} "
                    f"(bid={bid:.4f}, ask={ask:.4f}, midpoint={midpoint:.4f}) "
                    "[REJECTED_QUOTE_SPREAD]"
                )
                return None

            executable_price = ask if side is Side.BUY else bid
            deviation = abs(executable_price - signal_entry_price) / signal_entry_price
            if deviation > settings.max_execution_price_deviation_pct:
                self.logger.info(
                    f"Skipping {symbol}: quote execution price deviation={deviation:.4%} "
                    f"exceeds {settings.max_execution_price_deviation_pct:.4%} "
                    f"(signal={signal_entry_price:.4f}, executable={executable_price:.4f}) "
                    "[REJECTED_EXECUTION_PRICE_DEVIATION]"
                )
                return None

            self.logger.info(
                f"[Execution Quote] {symbol} feed={feed_label} bid={bid:.4f} ask={ask:.4f} "
                f"executable={executable_price:.4f} age={quote_age:.2f}s "
                f"spread={spread_pct:.4%} deviation={deviation:.4%}"
            )
            return executable_price
        except Exception as exc:
            self.logger.info(
                f"Skipping {symbol}: latest quote retrieval/validation failed ({exc}) "
                "[REJECTED_QUOTE_RETRIEVAL_ERROR]"
            )
            return None

    def fetch_account_and_positions(self) -> tuple[float, float, float, list[dict[str, Any]], list[dict[str, Any]], set[str]]:
        account = self.trading_client.get_account()
        account_cash = float(account.cash)
        account_equity = float(account.equity)
        buying_power = float(account.buying_power)
        if not all(isfinite(value) for value in (account_cash, account_equity, buying_power)):
            raise RuntimeError(
                "Alpaca account snapshot contains non-finite cash, equity, or buying power"
            )
        if account_equity <= 0:
            raise RuntimeError("Alpaca account equity is not positive; new entries disabled")

        if not hasattr(self, 'last_equity') or self.last_equity is None:
            if hasattr(account, 'last_equity') and account.last_equity is not None:
                try:
                    candidate_last_equity = float(account.last_equity)
                    self.last_equity = (
                        candidate_last_equity
                        if isfinite(candidate_last_equity) and candidate_last_equity > 0
                        else account_equity
                    )
                except (ValueError, TypeError):
                    self.last_equity = account_equity
            else:
                self.last_equity = account_equity

        open_positions = []
        dashboard_positions = []
        locked_symbols = set()
        positions_resp = self.trading_client.get_all_positions()
        for p in positions_resp:
            sym = p.symbol.upper().strip()
            locked_symbols.add(sym)
            qty = float(p.qty)
            avg_entry = float(p.avg_entry_price)
            curr_price = float(p.current_price) if hasattr(p, 'current_price') else avg_entry
            unrealized_pnl = float(p.unrealized_pl) if hasattr(p, 'unrealized_pl') else 0.0
            if not all(isfinite(value) for value in (qty, avg_entry, curr_price, unrealized_pnl)):
                raise RuntimeError(
                    f"Alpaca position snapshot contains non-finite values for {sym}"
                )
            if qty == 0 or avg_entry <= 0 or curr_price <= 0:
                raise RuntimeError(
                    f"Alpaca position snapshot contains invalid quantity or prices for {sym}"
                )
            side_str = "BUY" if qty > 0 else "SELL"
            sl_price = avg_entry * 0.98 if qty > 0 else avg_entry * 1.02
            tp_price = avg_entry * 1.05 if qty > 0 else avg_entry * 0.95

            open_positions.append({
                "symbol": sym,
                "side": side_str,
                "quantity": abs(qty),
                "entry_price": avg_entry,
                "current_price": curr_price,
                "unrealized_pnl": unrealized_pnl,
            })
            dashboard_positions.append({
                "symbol": sym,
                "side": side_str,
                "quantity": abs(qty),
                "entry_price": avg_entry,
                "stop_price": sl_price,
                "target_price": tp_price,
            })

        return account_cash, account_equity, buying_power, open_positions, dashboard_positions, locked_symbols

    def fetch_open_order_symbols(self) -> set[str]:
        req = GetOrdersRequest(status=QueryOrderStatus.OPEN)
        orders = self.trading_client.get_orders(req)
        symbols = set()
        for order in orders:
            symbol = order.get("symbol") if isinstance(order, dict) else getattr(order, "symbol", None)
            if (
                not isinstance(symbol, str)
                or not symbol
                or symbol != symbol.strip()
                or "/" in symbol
                or any(character.isspace() for character in symbol)
            ):
                raise RuntimeError(
                    "Alpaca returned an open order with a missing or invalid symbol; "
                    "new entries disabled until broker order state is verified"
                )
            normalized = normalize_symbol(symbol)
            if not normalized:
                raise RuntimeError(
                    "Alpaca returned an open order with an empty normalized symbol; "
                    "new entries disabled until broker order state is verified"
                )
            symbols.add(normalized)
        return symbols

    def submit_bracket_orders(
        self,
        top_new: list[tuple[str, float, Side | None, str, float, float, float]],
        locked_symbols: set[str],
        account_equity: float,
        settings: Settings,
        bar_time: datetime | None = None,
        processed_signals: set[tuple[str, datetime]] | None = None,
        locked_normalized: set[str] | None = None,
        open_exposure: float = 0.0,
    ) -> int:
        if locked_normalized is None:
            locked_normalized = {normalize_symbol(s) for s in locked_symbols}
        occupied_symbols = set(locked_normalized) | {normalize_symbol(s) for s in locked_symbols}
        active_untraded_slots = max(0, settings.max_open_positions - len(occupied_symbols))
        open_risk = len(occupied_symbols) * settings.risk_per_trade_pct * account_equity
        orders_sent = 0
        self.last_submit_failures = 0

        eligible = []
        for sym, sc, side_val, reason, curr_price, sl_price, tp_price in top_new[:active_untraded_slots]:
            norm_sym = normalize_symbol(sym)
            if norm_sym in locked_normalized:
                continue
            if bar_time is not None and processed_signals is not None and (norm_sym, bar_time) in processed_signals:
                continue
            if "/" in sym:
                self.logger.info(f"Skipping {sym}: unsupported slash symbol")
                continue
            if sc >= settings.min_candidate_score and sym not in locked_symbols and side_val is not None:
                eligible.append((sym, sc, side_val, reason, curr_price, sl_price, tp_price))

        free_capital = max(0.0, account_equity - open_exposure)

        valid_eligible = []
        for sym, sc, side_val, reason, curr_price, sl_price, tp_price in eligible:
            risk_per_share = abs(curr_price - sl_price)
            reward = abs(tp_price - curr_price)
            stop_pct = (risk_per_share / curr_price * 100) if curr_price > 0 else 0.0
            target_pct = (reward / curr_price * 100) if curr_price > 0 else 0.0
            rr = (reward / risk_per_share) if risk_per_share > 0 else 0.0

            if risk_per_share <= 0:
                self.logger.info(f"Skipping {sym}: risk per share <= 0")
                decision = "skipped_risk_per_share_non_positive"
                self.logger.info(f"Candidate {sym}: score={sc:.2f}, side={side_val}, entry=${curr_price:.2f}, stop=${sl_price:.2f}, target=${tp_price:.2f}, stop_pct={stop_pct:.2f}%, target_pct={target_pct:.2f}%, rr={rr:.2f}, notional_cap=$0.00, qty=0, gross=$0.00, cost=$0.00, decision={decision}")
                continue

            if settings.min_reward_risk > 0 and rr < settings.min_reward_risk:
                self.logger.info(f"Skipping {sym}: reward/risk {rr:.2f} below minimum {settings.min_reward_risk}")
                decision = "skipped_reward_risk"
                self.logger.info(f"Candidate {sym}: score={sc:.2f}, side={side_val}, entry=${curr_price:.2f}, stop=${sl_price:.2f}, target=${tp_price:.2f}, stop_pct={stop_pct:.2f}%, target_pct={target_pct:.2f}%, rr={rr:.2f}, notional_cap=$0.00, qty=0, gross=$0.00, cost=$0.00, decision={decision}")
                continue

            if settings.min_target_pct > 0 and target_pct < settings.min_target_pct:
                self.logger.info(f"Skipping {sym}: target {target_pct:.2f}% below minimum {settings.min_target_pct}%")
                decision = "skipped_target_pct"
                self.logger.info(f"Candidate {sym}: score={sc:.2f}, side={side_val}, entry=${curr_price:.2f}, stop=${sl_price:.2f}, target=${tp_price:.2f}, stop_pct={stop_pct:.2f}%, target_pct={target_pct:.2f}%, rr={rr:.2f}, notional_cap=$0.00, qty=0, gross=$0.00, cost=$0.00, decision={decision}")
                continue

            valid_eligible.append((sym, sc, side_val, reason, curr_price, sl_price, tp_price, risk_per_share, reward, stop_pct, target_pct, rr))

        for idx, (sym, sc, side_val, reason, curr_price, sl_price, tp_price, risk_per_share, reward, stop_pct, target_pct, rr) in enumerate(valid_eligible):
            norm_sym = normalize_symbol(sym)
            remaining = len(valid_eligible) - idx
            notional_cap = min(settings.max_single_position_pct * account_equity, free_capital / remaining if remaining > 0 else 0.0)

            if notional_cap < curr_price:
                self.logger.info(f"Skipping {sym}: allocation ${notional_cap:.2f} below one share (${curr_price:.2f})")
                decision = "skipped_allocation_below_one_share"
                self.logger.info(f"Candidate {sym}: score={sc:.2f}, side={side_val}, entry=${curr_price:.2f}, stop=${sl_price:.2f}, target=${tp_price:.2f}, stop_pct={stop_pct:.2f}%, target_pct={target_pct:.2f}%, rr={rr:.2f}, notional_cap=${notional_cap:.2f}, qty=0, gross=$0.00, cost=$0.00, decision={decision}")
                continue

            pos_pct = min(1.0, max(1e-9, notional_cap / account_equity))
            risk_engine = RiskEngine(
                risk_per_trade_pct=settings.risk_per_trade_pct,
                max_position_pct=pos_pct,
                max_portfolio_risk_pct=settings.max_portfolio_risk_pct,
                leverage=1.0,
            )

            try:
                if side_val is Side.BUY:
                    decision_obj = risk_engine.size_long(
                        available_capital=account_equity,
                        entry=curr_price,
                        stop=sl_price,
                        open_risk=open_risk,
                        active_candidates_count=5,
                        market_open=True,
                    )
                else:
                    decision_obj = risk_engine.size_short(
                        available_capital=account_equity,
                        entry=curr_price,
                        stop=sl_price,
                        open_risk=open_risk,
                        active_candidates_count=5,
                        market_open=True,
                    )

                if not decision_obj.approved:
                    self.logger.info(f"Skipping {sym}: RiskEngine rejected trade ({decision_obj.reason})")
                    decision = f"rejected_{decision_obj.reason}"
                    self.logger.info(f"Candidate {sym}: score={sc:.2f}, side={side_val}, entry=${curr_price:.2f}, stop=${sl_price:.2f}, target=${tp_price:.2f}, stop_pct={stop_pct:.2f}%, target_pct={target_pct:.2f}%, rr={rr:.2f}, notional_cap=${notional_cap:.2f}, qty=0, gross=$0.00, cost=$0.00, decision={decision}")
                    continue

                qty = int(decision_obj.quantity)
                if qty < 1:
                    self.logger.info(f"Skipping {sym}: whole share quantity ({qty}) < 1")
                    decision = "qty_below_one"
                    self.logger.info(f"Candidate {sym}: score={sc:.2f}, side={side_val}, entry=${curr_price:.2f}, stop=${sl_price:.2f}, target=${tp_price:.2f}, stop_pct={stop_pct:.2f}%, target_pct={target_pct:.2f}%, rr={rr:.2f}, notional_cap=${notional_cap:.2f}, qty={qty}, gross=$0.00, cost=$0.00, decision={decision}")
                    continue

                gross = qty * abs(tp_price - curr_price)
                cost = 2 * max(settings.commission_per_order_usd, settings.commission_per_share_usd * qty)
                min_multiple = settings.min_gross_to_cost_multiple

                if gross < min_multiple * cost:
                    self.logger.info(f"Skipping {sym}: expected gross ${gross:.2f} below {min_multiple} x round-trip commission ${cost:.2f}")
                    decision = "skipped_cost_gate"
                    self.logger.info(f"Candidate {sym}: score={sc:.2f}, side={side_val}, entry=${curr_price:.2f}, stop=${sl_price:.2f}, target=${tp_price:.2f}, stop_pct={stop_pct:.2f}%, target_pct={target_pct:.2f}%, rr={rr:.2f}, notional_cap=${notional_cap:.2f}, qty={qty}, gross=${gross:.2f}, cost=${cost:.2f}, decision={decision}")
                    continue

                decision = "submitted"
                self.logger.info(f"Candidate {sym}: score={sc:.2f}, side={side_val}, entry=${curr_price:.2f}, stop=${sl_price:.2f}, target=${tp_price:.2f}, stop_pct={stop_pct:.2f}%, target_pct={target_pct:.2f}%, rr={rr:.2f}, notional_cap=${notional_cap:.2f}, qty={qty}, gross=${gross:.2f}, cost=${cost:.2f}, decision={decision}")

                alpaca_side = OrderSide.BUY if side_val is Side.BUY else OrderSide.SELL
                order_req = MarketOrderRequest(
                    symbol=sym,
                    qty=qty,
                    side=alpaca_side,
                    time_in_force=TimeInForce.DAY,
                    order_class=OrderClass.BRACKET,
                    take_profit=TakeProfitRequest(limit_price=round(tp_price, 2)),
                    stop_loss=StopLossRequest(stop_price=round(sl_price, 2))
                )
                submitted_order = self.trading_client.submit_order(order_req)

                oc = getattr(submitted_order, 'order_class', None)
                oc_val = oc.value if hasattr(oc, 'value') else str(oc) if oc is not None else None
                oc_lower = oc_val.lower() if oc_val is not None else None
                legs = getattr(submitted_order, 'legs', None)

                self.logger.info(f"ORDER SUBMITTED (class={oc_val if oc is not None else 'unknown'}): {alpaca_side.value} {qty} shares of {sym} @ ~${curr_price:.2f} (SL: ${sl_price:.2f}, TP: ${tp_price:.2f}, Score: {sc:.2f})")

                if oc_lower is None or legs is None:
                    self.logger.warning("cannot verify order class")
                    open_risk += decision_obj.capital_at_risk
                    orders_sent += 1
                elif oc_lower == "bracket" and isinstance(legs, list) and len(legs) >= 2:
                    self.logger.info(f"ORDER CONFIRMED: {sym} id={getattr(submitted_order, 'id', 'unknown')} class=bracket legs={len(legs)}")
                    open_risk += decision_obj.capital_at_risk
                    orders_sent += 1
                else:
                    reason_msg = f"unprotected order: class={oc_val}, legs={len(legs) if isinstance(legs, list) else legs}"
                    self.logger.critical(f"UNPROTECTED ORDER {sym} id={getattr(submitted_order, 'id', 'unknown')} class={oc_val} legs={len(legs) if isinstance(legs, list) else 0}")
                    try:
                        if hasattr(submitted_order, 'id') and submitted_order.id:
                            self.trading_client.cancel_order_by_id(submitted_order.id)
                    except Exception as cancel_err:
                        self.logger.error(f"Failed to cancel unprotected order {sym}: {cancel_err}")
                    try:
                        self.trading_client.close_position(sym)
                    except Exception as close_err:
                        self.logger.error(f"Failed to close position for unprotected order {sym}: {close_err}")
                    self.last_submit_failures += 1
                    if self.on_unprotected is not None:
                        try:
                            self.on_unprotected(reason_msg)
                        except Exception as cb_err:
                            self.logger.error(f"Error in on_unprotected callback: {cb_err}")

                if bar_time is not None and processed_signals is not None:
                    processed_signals.add((norm_sym, bar_time))

                free_capital = max(0.0, free_capital - qty * curr_price)

            except Exception as ord_err:
                self.last_submit_failures += 1
                self.logger.error(f"Failed to submit bracket order for {sym}: {ord_err}")

        return orders_sent

    def submit_ict_instructions(
        self,
        instructions: Sequence[ICTExecutionInstruction],
        locked_symbols: set[str],
        account_equity: float,
        settings: Settings,
        dedup_tracker: ICTDeduplicationTracker | None = None,
        bar_time: datetime | None = None,
        processed_signals: set[tuple[str, datetime]] | None = None,
        locked_normalized: set[str] | None = None,
        open_exposure: float = 0.0,
        buying_power: float | None = None,
    ) -> int:
        """Submit at most one instruction per symbol within combined position/order/pending capacity.

        Occupied symbols reserve one portfolio slot and one conservative per-trade risk
        budget. Candidates retain incoming priority; a candidate rejected before broker
        submission does not consume a slot.
        """
        orders_sent = 0
        self.last_submit_failures = 0
        if account_equity <= 0:
            self.logger.warning("[Execution] New entries disabled: account equity is not positive.")
            return 0

        occupied_symbols = {normalize_symbol(symbol) for symbol in locked_symbols}
        if locked_normalized is not None:
            occupied_symbols.update(normalize_symbol(symbol) for symbol in locked_normalized)
        if dedup_tracker is not None:
            occupied_symbols.update(normalize_symbol(symbol) for symbol in dedup_tracker.pending_symbols)

        max_positions = settings.max_open_positions
        slots_remaining = max(0, max_positions - len(occupied_symbols))
        if slots_remaining <= 0:
            self.logger.info(
                f"[Execution Capacity] occupied={len(occupied_symbols)}/{max_positions}; "
                "no new position slots available."
            )
            return 0

        # Existing positions, broker-open orders, and uncertain pending submissions each
        # reserve the full per-trade risk budget. This is intentionally conservative because
        # the current broker snapshot does not provide a verified stop-risk value for every
        # existing position/order.
        open_risk = len(occupied_symbols) * settings.risk_per_trade_pct * account_equity
        available_cash = max(0.0, account_equity - max(0.0, open_exposure))
        if buying_power is not None:
            available_cash = min(available_cash, max(0.0, buying_power))
        free_capital = available_cash

        self.logger.info(
            f"[Execution Capacity] positions_orders_pending={len(occupied_symbols)}/"
            f"{max_positions} free_slots={slots_remaining} "
            f"conservative_open_risk=${open_risk:.2f} available_capital=${free_capital:.2f}"
        )

        eligible: list[ICTExecutionInstruction] = []
        seen_batch_symbols: set[str] = set()
        for inst in instructions:
            sym = str(inst.symbol).strip()
            norm_sym = normalize_symbol(sym)
            if not norm_sym or "/" in sym:
                self.logger.info(f"Skipping {sym}: unsupported or empty symbol")
                continue
            if norm_sym in occupied_symbols:
                continue
            if bar_time is not None and processed_signals is not None and (norm_sym, bar_time) in processed_signals:
                continue
            if getattr(inst, "is_diagnostic", False):
                self.logger.info(f"Skipping {sym}: diagnostic-only ICT instruction")
                continue

            side_str = "bull" if inst.side is Side.BUY else "bear"
            if dedup_tracker is not None and (
                dedup_tracker.is_executed(sym, side_str, inst.zone_type, inst.zone_top, inst.zone_bot)
                or dedup_tracker.is_pending(sym, side_str, inst.zone_type, inst.zone_top, inst.zone_bot)
            ):
                continue

            # Preserve current scanner/integration ordering; do not invent an ICT ranking rule.
            if norm_sym in seen_batch_symbols:
                self.logger.info(
                    f"Skipping additional instruction for {sym}: one instruction per symbol per batch."
                )
                continue

            risk_per_share = abs(inst.entry_price - inst.stop_price)
            if risk_per_share <= 0:
                self.logger.info(f"Skipping {sym}: risk per share <= 0")
                continue

            seen_batch_symbols.add(norm_sym)
            eligible.append(inst)

        for inst in eligible:
            if slots_remaining <= 0:
                break

            sym = inst.symbol
            norm_sym = normalize_symbol(sym)
            signal_entry_price = float(inst.entry_price)
            side_val = inst.side
            executable_price = self._get_executable_price(
                sym,
                side_val,
                signal_entry_price,
                settings,
            )
            if executable_price is None:
                continue

            # Recalculate geometry and R/R from the side-aware executable quote,
            # using the same two-decimal prices that will be sent to the broker.
            curr_price = round(executable_price, 2)
            sl_price = round(float(inst.stop_price), 2)
            tp_price = round(float(inst.target_price), 2)
            if side_val is Side.BUY:
                valid_geometry = sl_price < curr_price < tp_price
            else:
                valid_geometry = tp_price < curr_price < sl_price
            if not valid_geometry:
                self.logger.info(
                    f"Skipping {sym}: invalid directional bracket geometry after quote "
                    f"(side={side_val}, entry={curr_price:.2f}, stop={sl_price:.2f}, "
                    f"target={tp_price:.2f}) [REJECTED_EXECUTION_BRACKET_GEOMETRY]"
                )
                continue

            risk_per_share = abs(curr_price - sl_price)
            reward = abs(tp_price - curr_price)
            rr = reward / risk_per_share if risk_per_share > 0 else 0.0
            if rr < 1.10:
                self.logger.info(
                    f"Skipping {sym}: executable reward/risk={rr:.3f} below 1.10 "
                    "[REJECTED_EXECUTION_MIN_RR]"
                )
                continue

            notional_cap = min(
                settings.max_single_position_pct * account_equity,
                free_capital / slots_remaining if slots_remaining > 0 else 0.0,
            )
            if curr_price <= 0 or notional_cap < curr_price:
                self.logger.info(
                    f"Skipping {sym}: allocation ${notional_cap:.2f} below one share "
                    f"or invalid entry price (${curr_price:.2f})"
                )
                continue

            pos_pct = min(1.0, max(1e-9, notional_cap / account_equity))
            risk_engine = RiskEngine(
                risk_per_trade_pct=settings.risk_per_trade_pct,
                max_position_pct=pos_pct,
                max_portfolio_risk_pct=settings.max_portfolio_risk_pct,
                leverage=1.0,
            )

            decision_obj = None
            submission_attempted = False
            try:
                if side_val is Side.BUY:
                    decision_obj = risk_engine.size_long(
                        available_capital=account_equity,
                        entry=curr_price,
                        stop=sl_price,
                        open_risk=open_risk,
                        active_candidates_count=5,
                        market_open=True,
                    )
                else:
                    decision_obj = risk_engine.size_short(
                        available_capital=account_equity,
                        entry=curr_price,
                        stop=sl_price,
                        open_risk=open_risk,
                        active_candidates_count=5,
                        market_open=True,
                    )

                if not decision_obj.approved:
                    self.logger.info(
                        f"Skipping {sym}: RiskEngine rejected trade ({decision_obj.reason}); "
                        "candidate slot remains available for the next symbol."
                    )
                    continue

                qty = int(decision_obj.quantity)
                if qty < 1:
                    self.logger.info(
                        f"Skipping {sym}: whole-share quantity ({qty}) < 1; "
                        "candidate slot remains available."
                    )
                    continue

                gross_target_profit = qty * reward
                round_trip_commission = 2 * max(
                    settings.commission_per_order_usd,
                    settings.commission_per_share_usd * qty,
                )
                minimum_gross_profit = (
                    settings.min_gross_to_cost_multiple * round_trip_commission
                )
                if gross_target_profit < minimum_gross_profit:
                    self.logger.info(
                        f"Skipping {sym}: expected gross target profit "
                        f"${gross_target_profit:.2f} below "
                        f"{settings.min_gross_to_cost_multiple:.2f}x round-trip "
                        f"commission ${round_trip_commission:.2f}; "
                        "candidate slot remains available for the next symbol. "
                        "[REJECTED_EXECUTION_COST_GATE]"
                    )
                    continue

                self.logger.info(
                    f"[Execution Cost Gate] {sym} qty={qty} "
                    f"gross_target_profit=${gross_target_profit:.2f} "
                    f"round_trip_commission=${round_trip_commission:.2f} "
                    f"required_multiple={settings.min_gross_to_cost_multiple:.2f}"
                )

                alpaca_side = OrderSide.BUY if side_val is Side.BUY else OrderSide.SELL
                order_req = MarketOrderRequest(
                    symbol=sym,
                    qty=qty,
                    side=alpaca_side,
                    time_in_force=TimeInForce.DAY,
                    order_class=OrderClass.BRACKET,
                    take_profit=TakeProfitRequest(limit_price=round(tp_price, 2)),
                    stop_loss=StopLossRequest(stop_price=round(sl_price, 2)),
                )
                side_str = "bull" if side_val is Side.BUY else "bear"
                if dedup_tracker is not None:
                    dedup_tracker.mark_pending(sym, side_str, inst.zone_type, inst.zone_top, inst.zone_bot)

                submission_attempted = True
                submitted_order = self.trading_client.submit_order(order_req)
                oc = getattr(submitted_order, "order_class", None)
                oc_val = oc.value if hasattr(oc, "value") else str(oc) if oc is not None else None
                oc_lower = oc_val.lower() if oc_val is not None else None
                legs = getattr(submitted_order, "legs", None)

                self.logger.info(
                    f"ORDER SUBMITTED (class={oc_val if oc is not None else 'unknown'}): "
                    f"{alpaca_side.value} {qty} shares of {sym} @ ~${curr_price:.2f} "
                    f"(SL: ${sl_price:.2f}, TP: ${tp_price:.2f})"
                )

                reserve_slot = False
                if oc_lower is None or legs is None:
                    # Broker returned a response, but bracket protection is not verified.
                    # Keep pending and reserve capacity until next-cycle reconciliation.
                    self.logger.warning(
                        f"ORDER RESPONSE AMBIGUOUS for {sym} id={getattr(submitted_order, 'id', 'unknown')}; "
                        "retaining pending state and reserving slot/risk until broker reconciliation."
                    )
                    self.last_submit_failures += 1
                    reserve_slot = True
                elif oc_lower == "bracket" and isinstance(legs, list) and len(legs) >= 2:
                    self.logger.info(
                        f"ORDER CONFIRMED: {sym} id={getattr(submitted_order, 'id', 'unknown')} "
                        f"class=bracket legs={len(legs)}"
                    )
                    orders_sent += 1
                    reserve_slot = True
                    if dedup_tracker is not None:
                        dedup_tracker.mark_executed(
                            sym, side_str, inst.zone_type, inst.zone_top, inst.zone_bot
                        )
                else:
                    reason_msg = (
                        f"unprotected order: class={oc_val}, "
                        f"legs={len(legs) if isinstance(legs, list) else legs}"
                    )
                    self.logger.critical(
                        f"UNPROTECTED ORDER {sym} id={getattr(submitted_order, 'id', 'unknown')} "
                        f"class={oc_val} legs={len(legs) if isinstance(legs, list) else 0}"
                    )
                    try:
                        if getattr(submitted_order, "id", None):
                            self.trading_client.cancel_order_by_id(submitted_order.id)
                    except Exception as cancel_err:
                        self.logger.error(f"Failed to cancel unprotected order {sym}: {cancel_err}")
                    try:
                        self.trading_client.close_position(sym)
                    except Exception as close_err:
                        self.logger.error(f"Failed to close position for unprotected order {sym}: {close_err}")
                    self.last_submit_failures += 1
                    reserve_slot = True
                    if self.on_unprotected is not None:
                        try:
                            self.on_unprotected(reason_msg)
                        except Exception as cb_err:
                            self.logger.error(f"Error in on_unprotected callback: {cb_err}")

                if reserve_slot:
                    occupied_symbols.add(norm_sym)
                    slots_remaining -= 1
                    open_risk += decision_obj.capital_at_risk
                    free_capital = max(0.0, free_capital - qty * curr_price)
                    if bar_time is not None and processed_signals is not None:
                        processed_signals.add((norm_sym, bar_time))

            except Exception as ord_err:
                self.last_submit_failures += 1
                self.logger.error(f"Failed to submit bracket order for {sym}: {ord_err}")
                if is_broker_rejection(ord_err):
                    if dedup_tracker is not None:
                        dedup_tracker.clear_pending(sym, side_str, inst.zone_type, inst.zone_top, inst.zone_bot)
                    self.logger.info(f"Broker definitively rejected {sym}; no slot/risk reservation retained.")
                elif submission_attempted:
                    # A timeout or generic failure after the API call may have reached the broker.
                    self.logger.info(
                        f"Retaining pending status and reserving slot for {sym} after uncertain "
                        f"submission outcome: {ord_err}"
                    )
                    occupied_symbols.add(norm_sym)
                    slots_remaining -= 1
                    reserved_risk = (
                        decision_obj.capital_at_risk
                        if decision_obj is not None
                        else settings.risk_per_trade_pct * account_equity
                    )
                    open_risk += reserved_risk
                    free_capital = max(0.0, free_capital - qty * curr_price)
                    if bar_time is not None and processed_signals is not None:
                        processed_signals.add((norm_sym, bar_time))
                elif dedup_tracker is not None:
                    dedup_tracker.clear_pending(sym, side_str, inst.zone_type, inst.zone_top, inst.zone_bot)

        return orders_sent

    def flatten_all(self, sleep_fn=time.sleep, retries: int = 3, retry_delay: int = 5, max_wait_seconds: int = 0) -> list[str]:
        """Cancel outstanding orders and liquidate positions with broker-side reconciliation.

        Alpaca's bulk endpoint returns ClosePositionResponse objects (HTTP status plus
        order_id), while close_position() returns an Order object (id/status). Both
        response shapes are tracked explicitly so an active liquidation is never
        mistaken for an unrelated order and cancelled by the retry loop.
        """
        if max_wait_seconds <= 0:
            try:
                max_wait_seconds = Settings().flatten_max_wait_seconds
            except Exception:
                max_wait_seconds = 600

        self.last_flatten_errors = 0
        start_time = time.monotonic()
        total_slept = 0.0
        effective_delay = min(retry_delay, 15)
        last_successful_remaining: list[str] | None = None
        close_order_symbols: dict[str, str] = {}
        cancellation_order_symbols: dict[str, str] = {}
        unidentified_order_ids: set[str] = set()
        cancel_requested_order_ids: set[str] = set()
        first_close_attempt = True
        retry_symbols: set[str] = set()
        cycle_number = 0
        # A filled close/cancel order may be visible before the positions endpoint
        # catches up. Require two later position snapshots before reissuing a close.
        filled_order_confirmation_checks: dict[str, tuple[int, int]] = {}
        untrackable_close_symbols: set[str] = set()
        unknown_close_response = False
        unidentified_order_without_id = False

        active_order_statuses = {
            "new", "accepted", "pending_new", "partially_filled",
            "pending_cancel", "pending_replace", "accepted_for_bidding", "stopped",
        }
        pending_cancel_statuses = {"pending_cancel", "pending_replace"}
        terminal_order_statuses = {
            "filled", "canceled", "expired", "rejected", "replaced",
        }

        def _field(item: Any, name: str, default: Any = None) -> Any:
            if isinstance(item, dict):
                return item.get(name, default)
            return getattr(item, name, default)

        def _status_text(item: Any) -> str:
            status = _field(item, "status")
            value = getattr(status, "value", status)
            return str(value).strip().lower() if value is not None else ""

        def _body_details(body: Any) -> str:
            names = ("code", "message", "available", "existing_qty", "held_for_orders")
            parts: list[str] = []
            for name in names:
                value = _field(body, name)
                if value is not None:
                    parts.append(f"{name}={value}")
            return ", ".join(parts) if parts else repr(body)

        def _response_items(responses: Any) -> list[Any]:
            if responses is None:
                return []
            if isinstance(responses, (list, tuple)):
                return list(responses)
            return [responses]

        def _remember_close_responses(responses: Any, phase: str) -> None:
            nonlocal unknown_close_response
            for response in _response_items(responses):
                raw_status = _field(response, "status")
                body = _field(response, "body")
                raw_symbol = _field(response, "symbol") or _field(body, "symbol")
                symbol = normalize_symbol(str(raw_symbol)) if raw_symbol else ""
                response_order_id = _field(response, "order_id")
                has_http_status = isinstance(raw_status, int) and not isinstance(raw_status, bool)

                # close_all_positions() returns ClosePositionResponse: HTTP status,
                # order_id, symbol and either an Order body or failure-details body.
                if response_order_id is not None or has_http_status:
                    try:
                        status_code = int(raw_status) if raw_status is not None else 0
                    except (TypeError, ValueError):
                        status_code = 0
                    order_id = response_order_id or _field(body, "id")
                    if status_code == 200 and order_id and symbol:
                        order_id_text = str(order_id)
                        close_order_symbols[order_id_text] = symbol
                        self.logger.info(
                            f"[Flatten] {phase} bulk-close accepted: symbol={symbol} "
                            f"order_id={order_id_text}"
                        )
                    else:
                        self.last_flatten_errors += 1
                        details = _body_details(body)
                        self.logger.error(
                            f"{phase} liquidation request for {symbol or 'UNKNOWN'} was not accepted "
                            f"(http_status={status_code}, details={details})"
                        )
                        if status_code == 200 and (not order_id or not symbol):
                            if symbol:
                                untrackable_close_symbols.add(symbol)
                            else:
                                unknown_close_response = True
                    continue

                # close_position() returns Order, not ClosePositionResponse.
                order_id = _field(response, "id")
                order_status = _status_text(response)
                if order_id and symbol:
                    order_id_text = str(order_id)
                    close_order_symbols[order_id_text] = symbol
                    self.logger.info(
                        f"[Flatten] {phase} individual close order tracked: "
                        f"symbol={symbol} order_id={order_id_text} "
                        f"status={order_status or 'unknown'}"
                    )
                else:
                    self.last_flatten_errors += 1
                    self.logger.error(
                        f"{phase} close_position returned an untrackable response "
                        f"(symbol={symbol or 'missing'}, order_id={order_id or 'missing'}, "
                        f"status={order_status or 'missing'})"
                    )
                    if symbol:
                        untrackable_close_symbols.add(symbol)
                    else:
                        unknown_close_response = True

        def _request_cancel(order_id: str, symbol: str, order_status: str, phase: str) -> None:
            if symbol:
                cancellation_order_symbols[order_id] = symbol
                unidentified_order_ids.discard(order_id)
            else:
                unidentified_order_ids.add(order_id)
            if order_status in pending_cancel_statuses:
                return
            if order_id in cancel_requested_order_ids:
                return

            # Record before the request. If the request races with a fill or another
            # cancellation, the next cycle reconciles by ID instead of repeating DELETE.
            cancel_requested_order_ids.add(order_id)
            try:
                self.trading_client.cancel_order_by_id(order_id)
            except Exception as exc:
                message = str(exc)
                lowered = message.lower()
                if "pending cancel" in lowered or (
                    "already" in lowered and "filled" in lowered
                ):
                    self.logger.info(
                        f"[Flatten] Cancellation for {order_id} ({symbol or 'UNKNOWN'}) is already "
                        f"in progress or terminal: {message}"
                    )
                else:
                    self.last_flatten_errors += 1
                    self.logger.error(
                        f"{phase} cancellation request failed for order {order_id} "
                        f"({symbol or 'UNKNOWN'}): {message}"
                    )

        while True:
            cycle_number += 1
            newly_filled_symbols: set[str] = set()
            real_elapsed = time.monotonic() - start_time
            elapsed = max(real_elapsed, total_slept)
            if elapsed >= max_wait_seconds:
                break

            # The bulk request runs once. All later liquidation retries are
            # symbol-scoped so another working close order cannot be cancelled.
            if first_close_attempt:
                phase = "Initial"
                try:
                    responses = self.trading_client.close_all_positions(cancel_orders=True)
                    _remember_close_responses(responses, phase)
                except Exception as exc:
                    self.last_flatten_errors += 1
                    self.logger.error(f"Error during initial close_all_positions: {exc}")
                first_close_attempt = False
            elif retry_symbols:
                phase = "Retry"
                symbols_to_retry = sorted(retry_symbols)
                retry_symbols.clear()
                for symbol in symbols_to_retry:
                    try:
                        response = self.trading_client.close_position(symbol)
                        _remember_close_responses(response, phase)
                    except Exception as exc:
                        self.last_flatten_errors += 1
                        self.logger.error(
                            f"Error retrying liquidation for {symbol}: {exc}"
                        )

            try:
                _, _, _, open_positions, _, _ = self.fetch_account_and_positions()
                position_symbols = {
                    normalize_symbol(str(position["symbol"]))
                    for position in open_positions
                    if str(position.get("symbol", "")).strip()
                }
                if len(position_symbols) != len(open_positions):
                    raise RuntimeError("position snapshot contains an empty or invalid symbol")

                open_orders = self.trading_client.get_orders(
                    GetOrdersRequest(status=QueryOrderStatus.OPEN)
                )
                open_order_symbols: set[str] = set()
                open_order_ids: set[str] = set()
                active_close_symbols: set[str] = set()
                had_untracked_orders = False
                unknown_order_state = False

                for order in open_orders:
                    order_id = _field(order, "id")
                    order_symbol = _field(order, "symbol")
                    if not order_id or not order_symbol or not str(order_symbol).strip():
                        unknown_order_state = True
                        if order_id:
                            _request_cancel(
                                str(order_id), "", _status_text(order),
                                "Unidentified open-order",
                            )
                        else:
                            unidentified_order_without_id = True
                        continue

                    normalized_symbol = normalize_symbol(str(order_symbol))
                    if not normalized_symbol:
                        unknown_order_state = True
                        _request_cancel(
                            str(order_id), "", _status_text(order),
                            "Invalid-symbol open-order",
                        )
                        continue

                    order_id_text = str(order_id)
                    order_status = _status_text(order)
                    tracked_symbol = close_order_symbols.get(order_id_text)
                    cancelling_symbol = cancellation_order_symbols.get(order_id_text)
                    if (
                        (tracked_symbol and tracked_symbol != normalized_symbol)
                        or (cancelling_symbol and cancelling_symbol != normalized_symbol)
                    ):
                        unknown_order_state = True
                        self.logger.error(
                            f"Broker order identity mismatch for {order_id_text}: "
                            f"reported_symbol={normalized_symbol}, tracked_close={tracked_symbol}, "
                            f"tracked_cancel={cancelling_symbol}"
                        )
                        continue

                    if order_status not in active_order_statuses:
                        unknown_order_state = True
                        open_order_ids.add(order_id_text)
                        open_order_symbols.add(normalized_symbol)
                        self.logger.error(
                            f"Unrecognized status {order_status!r} for open order "
                            f"{order_id_text} ({normalized_symbol}); treating it as active "
                            "because it was returned by the OPEN-order query."
                        )
                        continue

                    open_order_ids.add(order_id_text)
                    open_order_symbols.add(normalized_symbol)
                    tracked_close = tracked_symbol == normalized_symbol

                    if tracked_close and normalized_symbol in position_symbols:
                        active_close_symbols.add(normalized_symbol)
                        continue

                    # This is either an existing order being cancelled by the bulk
                    # request, or a tracked close order whose position has already
                    # disappeared. Never issue a duplicate cancel while cancellation
                    # is pending, and never submit another close while it remains open.
                    had_untracked_orders = True
                    _request_cancel(
                        order_id_text,
                        normalized_symbol,
                        order_status,
                        "Tracked-close" if tracked_close else "Existing-open-order",
                    )

                # Orders that disappear from OPEN are reconciled by ID. This is
                # essential for pending-cancel bracket legs and individual close
                # orders: absence from OPEN alone is not proof of cancellation/fill.
                reconciled_ids: dict[str, str] = dict(cancellation_order_symbols)
                for order_id, symbol in close_order_symbols.items():
                    prior = reconciled_ids.get(order_id)
                    if prior is not None and prior != symbol:
                        unknown_order_state = True
                        self.logger.error(
                            f"Conflicting symbol mappings for broker order {order_id}: "
                            f"close={symbol}, cancellation={prior}"
                        )
                    reconciled_ids[order_id] = symbol

                for order_id, normalized_symbol in list(reconciled_ids.items()):
                    if order_id in open_order_ids:
                        continue
                    try:
                        reconciled_order = self.trading_client.get_order_by_id(order_id)
                        status_text = _status_text(reconciled_order)
                        reported_symbol = _field(reconciled_order, "symbol")
                        if reported_symbol and normalize_symbol(str(reported_symbol)) != normalized_symbol:
                            raise RuntimeError(
                                f"order symbol mismatch: expected {normalized_symbol}, "
                                f"received {reported_symbol}"
                            )
                    except Exception as exc:
                        self.last_flatten_errors += 1
                        self.logger.error(
                            f"Unable to reconcile broker order {order_id} "
                            f"({normalized_symbol}): {exc}"
                        )
                        unknown_order_state = True
                        continue

                    if status_text in active_order_statuses:
                        open_order_symbols.add(normalized_symbol)
                        if (
                            order_id in close_order_symbols
                            and normalized_symbol in position_symbols
                        ):
                            active_close_symbols.add(normalized_symbol)
                        else:
                            had_untracked_orders = True
                            _request_cancel(
                                order_id, normalized_symbol, status_text,
                                "Reconciled-open-order",
                            )
                    elif status_text in terminal_order_statuses:
                        close_order_symbols.pop(order_id, None)
                        cancellation_order_symbols.pop(order_id, None)
                        cancel_requested_order_ids.discard(order_id)
                        if status_text == "filled":
                            filled_order_confirmation_checks[normalized_symbol] = (
                                cycle_number, 0
                            )
                            newly_filled_symbols.add(normalized_symbol)
                    else:
                        self.logger.error(
                            f"Unrecognized status {status_text!r} for broker order "
                            f"{order_id}; flatten state remains unverified."
                        )
                        unknown_order_state = True

                # An OPEN order with a missing/invalid symbol is still tracked by
                # broker ID. Do not let it disappear from the OPEN list and silently
                # become forgotten: reconcile it by ID until it is terminal and
                # identifiable. A filled order with no symbol remains UNKNOWN because
                # it cannot safely be associated with a position snapshot.
                for order_id in list(unidentified_order_ids):
                    if order_id in open_order_ids:
                        unknown_order_state = True
                        continue
                    try:
                        unidentified_order = self.trading_client.get_order_by_id(order_id)
                        status_text = _status_text(unidentified_order)
                        reported_symbol = _field(unidentified_order, "symbol")
                        normalized_symbol = (
                            normalize_symbol(str(reported_symbol))
                            if reported_symbol and str(reported_symbol).strip()
                            else ""
                        )
                    except Exception as exc:
                        self.last_flatten_errors += 1
                        self.logger.error(
                            f"Unable to reconcile unidentified broker order {order_id}: {exc}"
                        )
                        unknown_order_state = True
                        continue

                    if status_text in terminal_order_statuses:
                        if status_text == "filled":
                            if normalized_symbol:
                                unidentified_order_ids.discard(order_id)
                                filled_order_confirmation_checks[normalized_symbol] = (
                                    cycle_number, 0
                                )
                                newly_filled_symbols.add(normalized_symbol)
                            else:
                                unknown_order_state = True
                                self.logger.error(
                                    f"Unidentified broker order {order_id} filled without a "
                                    "verifiable symbol; flatten state remains UNKNOWN."
                                )
                        else:
                            unidentified_order_ids.discard(order_id)
                            cancel_requested_order_ids.discard(order_id)
                            if normalized_symbol:
                                cancellation_order_symbols.pop(order_id, None)
                    elif status_text in active_order_statuses:
                        unknown_order_state = True
                        if normalized_symbol:
                            unidentified_order_ids.discard(order_id)
                            had_untracked_orders = True
                            open_order_symbols.add(normalized_symbol)
                            _request_cancel(
                                order_id, normalized_symbol, status_text,
                                "Reconciled-unidentified-order",
                            )
                        else:
                            _request_cancel(
                                order_id, "", status_text,
                                "Reconciled-unidentified-order",
                            )
                    else:
                        unknown_order_state = True
                        self.logger.error(
                            f"Unrecognized status {status_text!r} for unidentified "
                            f"broker order {order_id}; flatten state remains UNKNOWN."
                        )

                if unidentified_order_ids:
                    unknown_order_state = True

                if unknown_close_response:
                    # A nominally successful close with no symbol/order ID cannot
                    # be reconciled. Do not declare success even if snapshots look flat.
                    unknown_order_state = True
                if untrackable_close_symbols.intersection(position_symbols):
                    unknown_order_state = True

                if not position_symbols and not open_order_symbols and not unknown_order_state:
                    return []

                if unknown_order_state:
                    self.logger.error(
                        "Unable to fully identify or reconcile broker positions/orders "
                        "during flatten verification."
                    )
                    if unidentified_order_ids or unidentified_order_without_id:
                        last_successful_remaining = ["UNKNOWN"]
                    else:
                        last_successful_remaining = sorted(position_symbols | open_order_symbols) or ["UNKNOWN"]
                    retry_symbols.clear()
                else:
                    remaining = position_symbols | open_order_symbols
                    last_successful_remaining = sorted(remaining)

                    ready_after_fill: set[str] = set()
                    for symbol, (last_checked_cycle, checks) in list(
                        filled_order_confirmation_checks.items()
                    ):
                        if symbol in newly_filled_symbols:
                            continue
                        if symbol not in position_symbols:
                            filled_order_confirmation_checks.pop(symbol, None)
                            continue
                        if cycle_number > last_checked_cycle:
                            checks += 1
                            if checks >= 2:
                                ready_after_fill.add(symbol)
                                filled_order_confirmation_checks.pop(symbol, None)
                            else:
                                filled_order_confirmation_checks[symbol] = (
                                    cycle_number, checks
                                )

                    retry_symbols = {
                        symbol for symbol in position_symbols
                        if symbol not in active_close_symbols
                        and symbol not in open_order_symbols
                        and symbol not in filled_order_confirmation_checks
                        and symbol not in untrackable_close_symbols
                    } | ready_after_fill
                    if had_untracked_orders:
                        retry_symbols.clear()

            except Exception as exc:
                self.last_flatten_errors += 1
                self.logger.error(
                    f"Error verifying positions/open orders during flatten_all: {exc}"
                )
                last_successful_remaining = ["UNKNOWN"]
                retry_symbols.clear()

            real_elapsed = time.monotonic() - start_time
            elapsed = max(real_elapsed, total_slept)
            if elapsed >= max_wait_seconds:
                break

            remaining_time = max_wait_seconds - elapsed
            sleep_duration = min(effective_delay, remaining_time)
            if sleep_duration > 0:
                sleep_fn(sleep_duration)
                total_slept += sleep_duration

        if last_successful_remaining is not None:
            return last_successful_remaining
        return ["UNKNOWN"]


class LiveMarketBot:
    """Master Orchestrator class managing the trading lifecycle, dashboard server, and execution loops."""
    def __init__(self) -> None:
        load_dotenv()
        self.logger = self.setup_logging()
        self.settings = Settings()
        self.logger.info(
            f"[Config] gates: min_reward_risk={self.settings.min_reward_risk}, "
            f"min_target_pct={self.settings.min_target_pct}, "
            f"min_gross_to_cost_multiple={self.settings.min_gross_to_cost_multiple}, "
            f"commission_per_order_usd={self.settings.commission_per_order_usd}, "
            f"commission_per_share_usd={self.settings.commission_per_share_usd}, "
            f"max_single_position_pct={self.settings.max_single_position_pct}, "
            f"risk_per_trade_pct={self.settings.risk_per_trade_pct}, "
            f"max_quote_spread_pct={self.settings.max_quote_spread_pct}"
        )

        api_key = self.settings.alpaca_api_key or os.getenv("APCA_API_KEY_ID", "") or os.getenv("APCA_API_KEY", "")
        secret_key = self.settings.alpaca_secret_key or os.getenv("APCA_SECRET_KEY", "") or os.getenv("APCA_API_SECRET", "")
        base_url = self.settings.alpaca_base_url or os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")

        self.logger.info("Initializing Master Blueprint Compliant Trading Engine (OO Refactored)...")
        self.logger.info(f"Alpaca Base URL: {base_url}")

        try:
            self.trading_client = TradingClient(api_key=api_key or "mock_key", secret_key=secret_key or "mock_secret", paper=True)
            self.stock_client = StockHistoricalDataClient(api_key=api_key or "mock_key", secret_key=secret_key or "mock_secret")
            apply_default_timeout(self.trading_client, self.settings.http_connect_timeout_seconds, self.settings.http_read_timeout_seconds)
            apply_default_timeout(self.stock_client, self.settings.http_connect_timeout_seconds, self.settings.http_read_timeout_seconds)
        except Exception as exc:
            self.logger.error(f"Critical initialization error: {exc}")
            sys.exit(1)

        self.portfolio_manager = LivePortfolioManager(
            self.trading_client,
            self.logger,
            stock_client=self.stock_client,
        )
        self.portfolio_manager.on_unprotected = lambda reason: self.kill_switch.trip(reason)

        self._cached_universe: list[str] = []
        self._universe_last_fetched: datetime | None = None
        self._universe_last_failed: datetime | None = None
        self._universe_retry_cooldown_minutes = 60
        self._api_key = api_key or "mock_key"

        # Startup and refresh share one validated Layer 1 -> Layer 2 path.
        now_utc = datetime.now(timezone.utc)
        try:
            self._cached_universe = self._get_or_refresh_universe(now_utc)
        except Exception as exc:
            self.logger.critical(
                f"[Discovery] Startup candidate discovery failed; position-management-only mode: {exc}"
            )
            self._cached_universe = []
        self.global_universe = self._cached_universe

        realtime_feed = self.settings.alpaca_realtime_feed.strip().lower()
        data_feed_enum = DataFeed.SIP if realtime_feed == "sip" else DataFeed.IEX
        self.logger.info(
            f"[Data Feed Policy] historical_suitability={self.settings.alpaca_historical_feed.upper()} "
            f"realtime_scanning={realtime_feed.upper()} "
            f"historical_cross_feed_fallback={self.settings.alpaca_allow_historical_feed_fallback}"
        )
        if data_feed_enum == DataFeed.IEX:
            self.logger.warning(
                "IEX feed carries only a fraction of market volume; relative volume confirmation is unreliable. "
                "Layer 4 and ICT use IEX for timely intraday data; Layer 2 historical suitability "
                "uses the separately configured historical feed."
            )
        self.scanner = MarketScanner(self.global_universe, self.stock_client, self.logger, data_feed=data_feed_enum)
        self.ict_dedup_tracker = ICTDeduplicationTracker()
        self.kill_switch = KillSwitch(
            max_consecutive_errors=self.settings.max_consecutive_errors,
            max_daily_loss_pct=self.settings.max_daily_loss_pct,
        )

        self.latest_state = {
            "cash": 100000.0,
            "equity": 100000.0,
            "analyzed_stocks": [],
            "open_positions": [],
            "trade_history": [],
            "current_candidates": [],
        }

        self.processed_signals: set[tuple[str, datetime]] = set()
        self._outage_start: datetime | None = None
        self._last_outage_log_time: datetime | None = None

    def _get_or_refresh_universe(self, now_utc: datetime) -> list[str]:
        max_cache_age = timedelta(hours=self.settings.max_universe_cache_age_hours)
        cooldown_delta = timedelta(minutes=self._universe_retry_cooldown_minutes)
        retry_due = (
            self._universe_last_failed is not None
            and (now_utc - self._universe_last_failed) >= cooldown_delta
        )
        refresh_interval_expired = (
            not self._cached_universe
            or self._universe_last_fetched is None
            or (now_utc - self._universe_last_fetched) > max_cache_age
            or retry_due
        )
        in_cooldown = (
            self._universe_last_failed is not None
            and (now_utc - self._universe_last_failed) < cooldown_delta
        )

        if refresh_interval_expired and not in_cooldown:
            try:
                discovered = discover_universe(self.trading_client, self.logger, self._api_key)
                self._cached_universe = list(discovered or [])
                self._universe_last_fetched = now_utc
                self._universe_last_failed = None
                try:
                    l2_engine = DailySuitabilityEngine(
                        logger=self.logger,
                        settings=self.settings,
                        historical_feed=self.settings.alpaca_historical_feed,
                    )
                    cached_l2 = l2_engine.load_persisted_pool()
                    if cached_l2 and cached_l2.pool == self._cached_universe and cached_l2.generated_at:
                        generated_at = datetime.fromisoformat(cached_l2.generated_at)
                        if generated_at.tzinfo is None:
                            generated_at = generated_at.replace(tzinfo=timezone.utc)
                        self._universe_last_fetched = generated_at.astimezone(timezone.utc)
                        if cached_l2.stale:
                            # A validated stale pool is safe to retain, but retry on the
                            # configured cooldown rather than hitting historical APIs every scan.
                            self._universe_last_failed = now_utc
                            self.logger.warning(
                                "[Discovery] Layer 2 returned a compatible stale pool; "
                                f"retry cooldown started for {self._universe_retry_cooldown_minutes}m."
                            )
                except Exception as cache_exc:
                    self.logger.warning(f"[Discovery] Could not read Layer 2 generation timestamp: {cache_exc}")
                if not self._cached_universe:
                    self._universe_last_failed = now_utc
                    self.logger.warning(
                        "[Discovery] No eligible Layer 2 candidates. New entries disabled; "
                        f"retry cooldown started for {self._universe_retry_cooldown_minutes}m. "
                        "Account and position management remain active."
                    )
            except Exception as exc:
                self._universe_last_failed = now_utc
                age_hours = None
                if self._universe_last_fetched is not None:
                    age_hours = (now_utc - self._universe_last_fetched).total_seconds() / 3600.0
                if (
                    self._cached_universe
                    and age_hours is not None
                    and 0 <= age_hours <= self.settings.max_universe_cache_age_hours
                ):
                    self.logger.warning(
                        f"[Discovery] Refresh failed ({exc}). Retaining last-known validated Layer 2 pool "
                        f"only (size={len(self._cached_universe)}, age={age_hours:.2f}h); "
                        "raw persisted Layer 1 symbols are never used as fallback."
                    )
                else:
                    self.logger.critical(
                        f"[Discovery] Refresh failed ({exc}) and no sufficiently recent validated "
                        "Layer 2 pool is available. Position-management-only mode active."
                    )
                    self._cached_universe = []
                    self._universe_last_fetched = None
        elif in_cooldown:
            age_hours = None
            if self._universe_last_fetched is not None:
                age_hours = (now_utc - self._universe_last_fetched).total_seconds() / 3600.0
            if (
                not self._cached_universe or age_hours is None or age_hours < 0
                or age_hours > self.settings.max_universe_cache_age_hours
            ):
                self._cached_universe = []
                self._universe_last_fetched = None
                self.logger.warning(
                    "[Discovery] Retry cooldown active without a sufficiently recent Layer 2 pool; "
                    "new entries remain disabled."
                )
            else:
                self.logger.info(
                    f"[Discovery] Retry cooldown active; retaining recent validated Layer 2 pool "
                    f"(size={len(self._cached_universe)}, age={age_hours:.2f}h)."
                )

        self.global_universe = self._cached_universe
        if hasattr(self, "scanner"):
            self.scanner.global_universe = self._cached_universe
        return self._cached_universe



    def _note_outage(self, now_utc: datetime) -> None:
        if self._outage_start is None:
            self._outage_start = now_utc
        seconds = int((now_utc - self._outage_start).total_seconds())
        limit_mins = self.settings.max_outage_minutes

        should_log = False
        if self._last_outage_log_time is None:
            should_log = True
        else:
            elapsed_log = (now_utc - self._last_outage_log_time).total_seconds()
            if elapsed_log >= 60:
                should_log = True

        if should_log:
            self.logger.warning(f"[Network] outage for {seconds} s (limit {limit_mins} min)")
            self._last_outage_log_time = now_utc

        if seconds >= limit_mins * 60:
            reason = f"network outage exceeded {limit_mins} minutes"
            self.logger.critical(f"Critical: {reason}")
            self.kill_switch.trip(reason)

    def setup_logging(self) -> logging.Logger:
        log_dir_env = os.getenv("MM_LOG_DIR", "logs")
        log_dir = Path(log_dir_env) if 'Path' in globals() else __import__('pathlib').Path(log_dir_env)
        log_dir.mkdir(exist_ok=True, parents=True)
        today_str = datetime.now(timezone.utc).strftime('%Y%m%d')
        daily_log = log_dir / f"live_market_{today_str}.log"
        master_log = log_dir / "live_market.log"

        logger = logging.getLogger("MoneyMintterMasterBlueprint")
        logger.setLevel(logging.INFO)
        logger.handlers.clear()

        formatter = logging.Formatter("%(asctime)s [%(levelname)s] [%(name)s] %(message)s")

        fh_daily = logging.FileHandler(daily_log, encoding="utf-8")
        fh_daily.setFormatter(formatter)
        fh_master = logging.FileHandler(master_log, encoding="utf-8")
        fh_master.setFormatter(formatter)
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(formatter)

        logger.addHandler(fh_daily)
        logger.addHandler(fh_master)
        logger.addHandler(sh)
        return logger

    def run_cycle(self, now_utc: datetime) -> dict:
        if self.kill_switch.tripped:
            self.logger.critical(f"Kill switch tripped: {self.kill_switch.reason}")
            return {"evaluated": 0, "signals": 0, "orders_sent": 0, "halted": True}

        account_cash, account_equity, buying_power, open_positions, dashboard_positions, locked_symbols_raw = self.portfolio_manager.fetch_account_and_positions()
        self.kill_switch.check_daily_loss(self.portfolio_manager.last_equity, account_equity)
        if self.kill_switch.tripped:
            self.logger.critical(f"Kill switch tripped: {self.kill_switch.reason}")
            return {"evaluated": 0, "signals": 0, "orders_sent": 0, "halted": True}

        # Recheck the universe at the start of each cycle, but only contact
        # discovery when the validated Layer 2 cache has expired or a prior
        # failure's retry cooldown has elapsed. This makes startup outages
        # recoverable without repeatedly hitting the APIs during a healthy session.
        # Lightweight unit fixtures built with __new__ do not carry the
        # production discovery scheduler state. LiveMarketBot.__init__ always
        # initializes these fields, so skip the scheduler only for such
        # intentionally partial test instances.
        if hasattr(self, "_universe_last_fetched") and hasattr(self, "_universe_last_failed"):
            max_cache_age = timedelta(hours=self.settings.max_universe_cache_age_hours)
            universe_last_fetched = self._universe_last_fetched
            universe_last_failed = self._universe_last_failed
            cache_expired = (
                universe_last_fetched is not None
                and (now_utc - universe_last_fetched) > max_cache_age
            )
            retry_due = (
                universe_last_failed is not None
                and now_utc >= universe_last_failed
                and (now_utc - universe_last_failed)
                >= timedelta(minutes=self._universe_retry_cooldown_minutes)
            )
            no_initial_cache = (
                universe_last_failed is None
                and universe_last_fetched is None
            )
            if retry_due or cache_expired or no_initial_cache:
                self._get_or_refresh_universe(now_utc)

        self.latest_state["cash"] = account_cash
        self.latest_state["equity"] = account_equity
        self.latest_state["open_positions"] = dashboard_positions

        if not hasattr(self, '_previously_locked_symbols'):
            self._previously_locked_symbols = set()

        closed_externally = [sym for sym in self._previously_locked_symbols if sym not in locked_symbols_raw]
        if closed_externally:
            self.logger.info(f"[Reconciliation] Detected external closure/liquidation on Alpaca for symbols: {closed_externally}. Forcing immediate candidate refresh & lock release.")
        self._previously_locked_symbols = set(locked_symbols_raw)

        open_order_symbols = self.portfolio_manager.fetch_open_order_symbols()
        locked_normalized = {normalize_symbol(s) for s in locked_symbols_raw} | open_order_symbols

        dedup = getattr(self, 'ict_dedup_tracker', None)
        if dedup is not None:
            for p_sym in list(dedup.pending_symbols):
                if p_sym in locked_normalized:
                    dedup.resolve_pending_to_executed_for_symbol(p_sym)
            locked_normalized = locked_normalized | {normalize_symbol(s) for s in dedup.pending_symbols}

        evaluated = 0
        for sym in self.global_universe:
            clean_sym = sym.upper().strip()
            if normalize_symbol(clean_sym) in locked_normalized:
                continue
            open_status, _ = MarketScanner.is_session_open(clean_sym, now_utc)
            if open_status:
                evaluated += 1

        use_legacy = getattr(self, 'use_legacy', False) and hasattr(self, 'strategy') and self.strategy is not None
        if use_legacy:
            analyzed_candidates, dashboard_stocks = self.scanner.scan_universe(now_utc, locked_symbols_raw, self.strategy)
            if self.scanner.last_scan_attempts > 0 and self.scanner.last_scan_errors == self.scanner.last_scan_attempts:
                if self.scanner.last_scan_connectivity_errors == self.scanner.last_scan_errors:
                    raise ConnectivityError("all symbol analyses failed due to connectivity issues")
                else:
                    raise RuntimeError("all symbol analyses failed")
            signals = len(analyzed_candidates)

            analyzed_candidates.sort(key=lambda x: x[1], reverse=True)
            top_new = analyzed_candidates[:max(0, 5 - len(locked_symbols_raw))]

            final_candidates = list(locked_symbols_raw) + [c[0] for c in analyzed_candidates]
            self.latest_state["current_candidates"] = final_candidates[:5]
            if dashboard_stocks:
                self.latest_state["analyzed_stocks"] = dashboard_stocks

            minute = (now_utc.minute // 5) * 5
            bar_time = now_utc.replace(minute=minute, second=0, microsecond=0)

            open_exposure = sum(p.get("quantity", 0.0) * p.get("current_price", p.get("entry_price", 0.0)) for p in open_positions)

            orders_sent = self.portfolio_manager.submit_bracket_orders(
                top_new,
                locked_symbols_raw,
                account_equity,
                self.settings,
                bar_time=bar_time,
                processed_signals=self.processed_signals,
                locked_normalized=locked_normalized,
                open_exposure=open_exposure,
            )
        else:
            dedup = getattr(self, 'ict_dedup_tracker', None)
            ict_instructions, dashboard_stocks = self.scanner.scan_universe(now_utc, locked_symbols_raw, dedup_tracker=dedup)

            scan_attempts = getattr(self.scanner, 'last_scan_attempts', 0)
            scan_errors = getattr(self.scanner, 'last_scan_errors', 0)
            scan_conn_errors = getattr(self.scanner, 'last_scan_connectivity_errors', 0)
            if scan_attempts > 0 and scan_errors == scan_attempts:
                if scan_conn_errors == scan_errors:
                    raise ConnectivityError("all symbol analyses failed due to connectivity issues")
                else:
                    raise RuntimeError("all symbol analyses failed")

            ict_attempts = getattr(self.scanner, 'ict_attempts', 0)
            ict_errors = getattr(self.scanner, 'ict_errors', 0)
            ict_conn_errors = getattr(self.scanner, 'ict_connectivity_errors', 0)
            if ict_attempts > 0 and ict_errors == ict_attempts:
                if ict_conn_errors == ict_errors:
                    raise ConnectivityError("all ICT symbol analyses failed due to connectivity issues")
                else:
                    raise RuntimeError("all ICT symbol analyses failed")
            signals = len(ict_instructions)

            final_candidates = list(locked_symbols_raw) + [inst.symbol for inst in ict_instructions]
            self.latest_state["current_candidates"] = final_candidates[:5]
            if dashboard_stocks:
                self.latest_state["analyzed_stocks"] = dashboard_stocks

            minute = (now_utc.minute // 5) * 5
            bar_time = now_utc.replace(minute=minute, second=0, microsecond=0)

            open_exposure = sum(p.get("quantity", 0.0) * p.get("current_price", p.get("entry_price", 0.0)) for p in open_positions)

            orders_sent = self.portfolio_manager.submit_ict_instructions(
                ict_instructions,
                locked_symbols_raw,
                account_equity,
                self.settings,
                dedup_tracker=dedup,
                bar_time=bar_time,
                processed_signals=self.processed_signals,
                locked_normalized=locked_normalized,
                open_exposure=open_exposure,
                buying_power=buying_power,
            )

            self.logger.info(
                f"[ICT Cycle Summary] l2_eligible={getattr(self.scanner, 'last_l2_eligible', 0)} "
                f"l4_selected={getattr(self.scanner, 'last_l4_selected', 0)} ict_attempts={ict_attempts} "
                f"acquisition_successes={getattr(self.scanner, 'ict_acquisition_successes', 0)} evaluation_successes={getattr(self.scanner, 'ict_evaluation_successes', 0)} "
                f"ict_errors={ict_errors} raw_entries={getattr(self.scanner, 'ict_raw_candidates', 0)} "
                f"entry_mode_passes={getattr(self.scanner, 'ict_entry_mode_passes', 0)} instructions={getattr(self.scanner, 'ict_instructions', 0)} "
                f"orders_sent={orders_sent} order_failures={self.portfolio_manager.last_submit_failures}"
            )

        if self.portfolio_manager.last_submit_failures > 0 and orders_sent == 0:
            raise RuntimeError("all order submissions failed")

        self.logger.info(f"[Cycle Summary] bar_time={bar_time.isoformat()}, evaluated={evaluated}, signals={signals}, orders_sent={orders_sent}")
        return {"evaluated": evaluated, "signals": signals, "orders_sent": orders_sent}

    def run_once(self, now_utc: datetime) -> dict:
        try:
            res = self.run_cycle(now_utc)
            if not res.get("halted", False):
                self.kill_switch.record_success()
                if self._outage_start is not None:
                    seconds = int((now_utc - self._outage_start).total_seconds())
                    self.logger.info(f"[Network] recovered after {seconds} s")
                    self._outage_start = None
                    self._last_outage_log_time = None
            return res
        except Exception as exc:
            if is_connectivity_error(exc):
                self._note_outage(now_utc)
                self.logger.error(f"Cycle connectivity error: {exc}")
                return {"error": True, "outage": True, "orders_sent": 0}
            else:
                self.kill_switch.record_error()
                self.logger.error(f"Cycle execution error: {exc}")
                return {"error": True, "orders_sent": 0}

    def run(self) -> None:
        self.logger.info("==================================================")
        self.logger.info(" Headless Trading Engine Active (Event-Driven 5M) ")
        self.logger.info("==================================================")

        try:
            while True:
                now_utc = datetime.now(timezone.utc)
                res = self.run_once(now_utc)
                if res.get("error") or res.get("outage"):
                    time.sleep(10)
                    continue

                next_boundary = next_bar_boundary(now_utc)
                sleep_seconds = max(0.0, (next_boundary - datetime.now(timezone.utc)).total_seconds())
                self.logger.info(f"[Scheduler] Cycle completed. Sleeping for {sleep_seconds:.1f}s until next bar boundary {next_boundary.isoformat()}")
                time.sleep(sleep_seconds)

        except KeyboardInterrupt:
            print("\nMaster Blueprint Trading Engine stopped gracefully.")

    def run_session(self, now_fn=None, sleep_fn=None) -> dict:
        if now_fn is None:
            now_fn = lambda: datetime.now(timezone.utc)
        if sleep_fn is None:
            sleep_fn = time.sleep

        self.logger.info("==================================================")
        self.logger.info(" PAPER TRADING SESSION ORCHESTRATION ACTIVE       ")
        self.logger.info("==================================================")

        cycles = 0
        orders_sent_total = 0
        errors = 0
        trading_started = False
        self.latest_state["final_account_snapshot_verified"] = False

        def _safe_flatten() -> list[str]:
            def _record_flatten_error() -> None:
                try:
                    self.portfolio_manager.last_flatten_errors = (
                        getattr(self.portfolio_manager, "last_flatten_errors", 0) + 1
                    )
                except Exception:
                    self.logger.error("Unable to increment flatten error counter.")

            try:
                res_pos = self.portfolio_manager.flatten_all(
                    sleep_fn,
                    max_wait_seconds=self.settings.flatten_max_wait_seconds,
                )
            except Exception as exc:
                _record_flatten_error()
                self.logger.critical(f"flatten_all raised exception: {exc}")
                res_pos = ["UNKNOWN"]

            if res_pos:
                if res_pos == ["UNKNOWN"]:
                    self.logger.critical(
                        f"POSITIONS MAY STILL BE OPEN: {res_pos} "
                        "(position state could not be verified; broker order state may also be unresolved)"
                    )
                else:
                    self.logger.critical(f"POSITIONS MAY STILL BE OPEN: {res_pos}")
                return res_pos

            # Take a fresh account/positions/orders snapshot after liquidation.
            # The last normal cycle's equity is not the final session equity.
            try:
                (
                    final_cash,
                    final_equity,
                    final_buying_power,
                    final_positions,
                    final_dashboard_positions,
                    final_locked_symbols,
                ) = self.portfolio_manager.fetch_account_and_positions()
                final_open_order_symbols = self.portfolio_manager.fetch_open_order_symbols()

                self.latest_state["cash"] = final_cash
                self.latest_state["equity"] = final_equity
                self.latest_state["open_positions"] = final_dashboard_positions
                self.latest_state["final_account_snapshot_verified"] = True

                remaining_symbols = {
                    normalize_symbol(str(position["symbol"]))
                    for position in final_positions
                    if str(position.get("symbol", "")).strip()
                }
                remaining_symbols.update(
                    normalize_symbol(str(symbol))
                    for symbol in final_locked_symbols
                    if str(symbol).strip()
                )
                remaining_symbols.update(
                    normalize_symbol(str(symbol))
                    for symbol in final_open_order_symbols
                    if str(symbol).strip()
                )
                remaining_symbols.discard("")

                self.logger.info(
                    f"[Final Broker Snapshot] equity=${final_equity:.2f} "
                    f"cash=${final_cash:.2f} buying_power=${final_buying_power:.2f} "
                    f"positions={len(final_positions)} "
                    f"open_order_symbols={len(final_open_order_symbols)}"
                )
                if remaining_symbols:
                    remaining = sorted(remaining_symbols)
                    _record_flatten_error()
                    self.logger.critical(
                        f"Final broker snapshot is not flat; positions/open orders remain: {remaining}"
                    )
                    return remaining
            except Exception as exc:
                _record_flatten_error()
                self.logger.critical(
                    f"Unable to verify final broker account/positions/orders snapshot: {exc}"
                )
                return ["UNKNOWN"]

            return []

        try:
            try:
                clock = self.trading_client.get_clock()
            except Exception as exc:
                self.logger.error(f"Failed to get market clock: {exc}")
                return {"status": "error", "cycles": 0, "orders_sent": 0, "errors": 1, "remaining": []}

            is_open = getattr(clock, 'is_open', False)
            next_open = getattr(clock, 'next_open', None)
            next_close = getattr(clock, 'next_close', None)

            now = now_fn()
            if not is_open:
                if next_open is not None:
                    if next_open.tzinfo is None:
                        next_open = next_open.replace(tzinfo=timezone.utc)
                    if now.tzinfo is None:
                        now = now.replace(tzinfo=timezone.utc)
                    wait_mins = (next_open - now).total_seconds() / 60.0
                    if wait_mins > self.settings.max_wait_for_open_minutes:
                        self.logger.info(f"Market closed and next_open ({next_open.isoformat()}) is {wait_mins:.1f} mins away (> max_wait={self.settings.max_wait_for_open_minutes} mins). Skipping session.")
                        return {"status": "skipped_market_closed", "cycles": 0, "orders_sent": 0, "errors": 0, "remaining": []}
                    else:
                        self.logger.info(f"Market closed. Waiting until next open at {next_open.isoformat()} ({wait_mins:.1f} mins)...")
                        _sleep_until(next_open, now_fn, sleep_fn)
                        try:
                            clock = self.trading_client.get_clock()
                            is_open = getattr(clock, 'is_open', False)
                            next_open = getattr(clock, 'next_open', next_open)
                            next_close = getattr(clock, 'next_close', next_close)
                        except Exception as exc:
                            self.logger.error(f"Failed to get clock after waiting for open: {exc}")

                        if not getattr(clock, 'is_open', False):
                            self.logger.info("Market still not open after waiting. Skipping session.")
                            return {"status": "skipped_market_closed", "cycles": 0, "orders_sent": 0, "errors": 0, "remaining": []}
                else:
                    return {"status": "skipped_market_closed", "cycles": 0, "orders_sent": 0, "errors": 0, "remaining": []}

            if next_close is not None and next_close.tzinfo is None:
                next_close = next_close.replace(tzinfo=timezone.utc)

            close_time = next_close
            flatten_mins = self.settings.flatten_minutes_before_close
            no_new_entries_mins = self.settings.no_new_entries_minutes_before_close

            last_known_close = close_time

            while True:
                now = now_fn()

                try:
                    clk = self.trading_client.get_clock()
                    if hasattr(clk, 'next_close') and clk.next_close is not None:
                        nc = clk.next_close
                        if nc.tzinfo is None:
                            nc = nc.replace(tzinfo=timezone.utc)
                        last_known_close = nc
                except Exception as exc:
                    self.logger.warning(f"Failed to re-fetch clock, using last known close {last_known_close}: {exc}")

                close_time = last_known_close
                flatten_at = close_time - timedelta(minutes=flatten_mins)
                cutoff = close_time - timedelta(minutes=no_new_entries_mins)

                if self.kill_switch.tripped:
                    self.logger.critical(f"Kill switch tripped mid-session: {self.kill_switch.reason}")
                    remaining = _safe_flatten()
                    status = "halted" if remaining != ["UNKNOWN"] else "flatten_incomplete"
                    self._log_session_summary(status, cycles, orders_sent_total, errors)
                    return {"status": status, "cycles": cycles, "orders_sent": orders_sent_total, "errors": errors, "remaining": remaining}

                if now >= flatten_at:
                    break

                if now < cutoff:
                    trading_started = True
                    res = self.run_once(now)
                    cycles += 1
                    orders_sent_total += res.get("orders_sent", 0)
                    if self.kill_switch.tripped or res.get("halted", False):
                        self.logger.critical(f"Kill switch tripped: {self.kill_switch.reason if hasattr(self.kill_switch, 'reason') else 'halted'}")
                        remaining = _safe_flatten()
                        status = "halted" if remaining != ["UNKNOWN"] else "flatten_incomplete"
                        self._log_session_summary(status, cycles, orders_sent_total, errors)
                        return {"status": status, "cycles": cycles, "orders_sent": orders_sent_total, "errors": errors, "remaining": remaining}
                    if res.get("error") or res.get("outage"):
                        errors += 1
                        sleep_fn(10)
                        continue

                nb = next_bar_boundary(now)
                target = min(nb, flatten_at)
                if target > now:
                    _sleep_until(target, now_fn, sleep_fn)

            remaining = _safe_flatten()
            status = "completed" if not remaining and remaining != ["UNKNOWN"] else "flatten_incomplete"
            if remaining == ["UNKNOWN"]:
                status = "flatten_incomplete"
            self._log_session_summary(status, cycles, orders_sent_total, errors)
            return {"status": status, "cycles": cycles, "orders_sent": orders_sent_total, "errors": errors, "remaining": remaining}

        except KeyboardInterrupt:
            self.logger.warning("Session interrupted by user (KeyboardInterrupt).")
            remaining = []
            if trading_started:
                remaining = _safe_flatten()
            status = "interrupted" if remaining != ["UNKNOWN"] else "flatten_incomplete"
            self._log_session_summary(status, cycles, orders_sent_total, errors)
            return {"status": status, "cycles": cycles, "orders_sent": orders_sent_total, "errors": errors, "remaining": remaining}

        except Exception as exc:
            self.logger.exception(f"Session error: {exc}")
            errors += 1
            remaining = []
            if trading_started:
                remaining = _safe_flatten()
            status = "error" if remaining != ["UNKNOWN"] else "flatten_incomplete"
            self._log_session_summary(status, cycles, orders_sent_total, errors)
            return {"status": status, "cycles": cycles, "orders_sent": orders_sent_total, "errors": errors, "remaining": remaining}

    def _log_session_summary(self, status: str, cycles: int, orders_sent: int, errors: int) -> None:
        flatten_errors = getattr(self.portfolio_manager, "last_flatten_errors", 0)
        snapshot_verified = bool(
            self.latest_state.get("final_account_snapshot_verified", False)
        )
        snapshot_label = "verified" if snapshot_verified else "unverified"

        if cycles == 0 and not snapshot_verified:
            self.logger.info(
                f"[Session Summary] status={status}, cycles={cycles}, orders_sent={orders_sent}, "
                f"cycle_errors={errors}, flatten_errors={flatten_errors}, "
                f"final_account_snapshot={snapshot_label}, equity=n/a"
            )
            return

        equity = self.latest_state.get("equity")
        last_eq = self.portfolio_manager.last_equity
        change_str = ""
        if equity is not None:
            equity_value = float(equity)
            if last_eq is not None and last_eq > 0:
                diff = equity_value - last_eq
                pct = (diff / last_eq) * 100.0
                change_str = (
                    f", equity=${equity_value:.2f}, change=${diff:+.2f} ({pct:+.2f}%) "
                    f"vs last_equity=${last_eq:.2f}"
                )
            else:
                change_str = f", equity=${equity_value:.2f}"
        else:
            change_str = ", equity=n/a"

        self.logger.info(
            f"[Session Summary] status={status}, cycles={cycles}, orders_sent={orders_sent}, "
            f"cycle_errors={errors}, flatten_errors={flatten_errors}, "
            f"final_account_snapshot={snapshot_label}{change_str}"
        )


def _sleep_until(target_dt: datetime, now_fn, sleep_fn) -> None:
    while True:
        now = now_fn()
        if now.tzinfo is None and target_dt.tzinfo is not None:
            now = now.replace(tzinfo=timezone.utc)
        elif now.tzinfo is not None and target_dt.tzinfo is None:
            target_dt = target_dt.replace(tzinfo=timezone.utc)
        diff = (target_dt - now).total_seconds()
        if diff <= 0:
            break
        chunk = min(60.0, diff)
        sleep_fn(chunk)
        if now_fn() == now:
            break


def main() -> int:
    print("==================================================")
    print(" PAPER TRADING SESSION MODE ACTIVE                ")
    print("==================================================")
    lock_path = os.path.join(os.getenv("MM_LOG_DIR", "logs"), "mm_session.lock")
    lock = SessionLock(lock_path)
    if not lock.acquire():
        print(f"Session lock acquisition failed (lock file exists and is fresh): {lock_path}")
        return 4

    try:
        bot = LiveMarketBot()
        res = bot.run_session()
        status = res.get("status")
        remaining = res.get("remaining", [])
        if remaining:
            print("==================================================")
            print(f" POSITIONS MAY STILL BE OPEN: {remaining}")
            print(" CHECK ALPACA AND CLOSE THEM MANUALLY")
            print("==================================================")

        if status in ("completed", "skipped_market_closed"):
            return 0
        elif status == "halted":
            return 2
        elif status == "flatten_incomplete":
            return 3
        elif status == "error":
            return 3
        elif status == "interrupted":
            return 130
        else:
            return 3
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
