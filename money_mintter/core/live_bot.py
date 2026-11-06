from __future__ import annotations

import logging
import os
import random
import sys
import threading
import time
from datetime import datetime, timedelta, timezone, time as dt_time
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
from money_mintter.strategy import Candle, ICTStrategy, LiquiditySweepReversalStrategy, MultiTimeframeSeries, StrategyConfig, Timeframe
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


BAR_CLOSE_DELAY_SECONDS = 20
HISTORY_DAYS = 30


def apply_discovery_timeout(client: Any, connect: float, read_timeout: float | list[float]) -> None:
    if not hasattr(client, "_session") or client._session is None:
        raise RuntimeError("Client cannot apply HTTP timeout: missing session")
    session = client._session
    if not hasattr(session, "request") or session.request is None:
        raise RuntimeError("Client cannot apply HTTP timeout: missing session request")

    orig_request = session.request
    if getattr(orig_request, "_mm_timeout_applied", False):
        if hasattr(orig_request, "_mm_read_timeout_ref"):
            ref = orig_request._mm_read_timeout_ref
            if isinstance(ref, list):
                if isinstance(read_timeout, list):
                    ref[0] = read_timeout[0]
                else:
                    ref[0] = float(read_timeout)
            else:
                if isinstance(read_timeout, list):
                    orig_request._mm_read_timeout_ref = read_timeout[0]
                else:
                    orig_request._mm_read_timeout_ref = float(read_timeout)
        if hasattr(orig_request, "_mm_connect_timeout_ref"):
            ref_c = orig_request._mm_connect_timeout_ref
            if isinstance(ref_c, list):
                ref_c[0] = float(connect)
            else:
                orig_request._mm_connect_timeout_ref = float(connect)
        return

    if isinstance(read_timeout, list):
        read_ref = read_timeout
    else:
        read_ref = [float(read_timeout)]

    connect_ref = [float(connect)]

    def wrapped_request(method, url, **kwargs):
        settings = Settings()
        if "timeout" not in kwargs:
            if isinstance(url, str) and "/assets" in url:
                r_val = read_ref[0] if isinstance(read_ref, list) else read_ref
                c_val = connect_ref[0] if isinstance(connect_ref, list) else connect_ref
                kwargs["timeout"] = (c_val, r_val)
            else:
                c_val = connect_ref[0] if isinstance(connect_ref, list) else connect_ref
                kwargs["timeout"] = (c_val, settings.http_read_timeout_seconds)
        return orig_request(method, url, **kwargs)

    wrapped_request._mm_timeout_applied = True
    wrapped_request._mm_discovery_timeout_applied = True
    wrapped_request._mm_read_timeout_ref = read_ref
    wrapped_request._mm_connect_timeout_ref = connect_ref
    session.request = wrapped_request


apply_default_timeout = apply_discovery_timeout


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


class UniverseDiscoveryWorker:
    def __init__(self, trading_client: TradingClient, logger: logging.Logger, api_key: str = "") -> None:
        self.trading_client = trading_client
        self.logger = logger
        self.api_key = api_key
        self.state = "IDLE"  # IDLE, DISCOVERING, SUCCESS, FAILED
        self.universe: list[str] = []
        self.last_error: Exception | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> bool:
        with self._lock:
            if self.state == "DISCOVERING":
                return False
            self.state = "DISCOVERING"
            self.last_error = None
        
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def start_if_needed(self, predicate: Any = None) -> bool:
        with self._lock:
            if self.state == "DISCOVERING":
                return False
            if predicate is not None and not predicate():
                return False
            self.state = "DISCOVERING"
            self.last_error = None
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
            return True

    def _run(self) -> None:
        try:
            discovered = discover_universe(self.trading_client, self.logger, self.api_key)
            with self._lock:
                self.universe = discovered
                self.state = "SUCCESS"
                self.last_error = None
            self.logger.info(f"[DiscoveryWorker] Successfully discovered {len(discovered)} symbols.")
        except Exception as exc:
            with self._lock:
                self.state = "FAILED"
                self.last_error = exc
            self.logger.error(f"[DiscoveryWorker] Discovery failed: {exc}")


def discover_universe(trading_client: TradingClient, logger: logging.Logger, api_key: str = "") -> list[str]:
    settings = Settings()
    min_size = settings.min_universe_size
    discovery_timeout = settings.discovery_read_timeout_seconds
    read_holder = [discovery_timeout]
    try:
        if hasattr(trading_client, "_session") and trading_client._session is not None:
            apply_default_timeout(trading_client, settings.http_connect_timeout_seconds, read_holder)
    except Exception as exc:
        logger.critical(f"Failed to apply discovery HTTP timeout: {exc}")
        raise RuntimeError(f"Discovery timeout application failed: {exc}") from exc

    engine = StructuralEligibilityEngine()
    start_time = datetime.now(timezone.utc)
    assets = None
    search_params = GetAssetsRequest(
        status=AssetStatus.ACTIVE,
        asset_class=AssetClass.US_EQUITY
    )
    
    logger.info("[Discovery State: DISCOVERY_PENDING] Starting live asset discovery request against Alpaca API (/v2/assets)...")
    last_exc = None
    
    max_recovery_seconds = 300.0
    recovery_deadline = time.monotonic() + max_recovery_seconds
    attempt = 0
    
    while True:
        now_mono = time.monotonic()
        remaining_budget = recovery_deadline - now_mono
        if remaining_budget <= 0:
            logger.error("[Discovery State: UNIVERSE_DISCOVERY_FAILED] Discovery recovery window of 300 seconds expired.")
            break
        
        read_holder[0] = min(discovery_timeout, max(1.0, remaining_budget))

        attempt += 1
        attempt_start = datetime.now(timezone.utc)
        try:
            logger.info(f"[Discovery State: DISCOVERY_RETRYING] Attempt {attempt} for GET /v2/assets (remaining budget: {remaining_budget:.1f}s)...")
            if hasattr(trading_client, "get_all_assets"):
                try:
                    from alpaca.trading.enums import AssetExchange
                    if "TXSE" not in AssetExchange._member_map_:
                        AssetExchange._add_member_("TXSE", "TXSE")
                except Exception:
                    pass
                try:
                    assets = trading_client.get_all_assets(search_params)
                except Exception as val_exc:
                    err_str = str(val_exc)
                    if "validation error" in err_str.lower() or "input_value" in err_str.lower() or "ValidationError" in type(val_exc).__name__:
                        logger.warning(f"[Discovery] Pydantic validation error in get_all_assets: {val_exc}. Falling back to raw assets endpoint query.")
                        params = search_params.to_request_fields() if search_params is not None else {}
                        raw_response = trading_client.get("/assets", params)
                        if isinstance(raw_response, list):
                            assets = raw_response
                        else:
                            raise val_exc
                    else:
                        raise val_exc
            break
        except Exception as exc:
            last_exc = exc
            duration = (datetime.now(timezone.utc) - attempt_start).total_seconds()
            err_name = type(exc).__name__
            logger.warning(f"[Discovery] Attempt {attempt} failed after {duration:.2f}s with {err_name}: {exc}")
            
            status_code = getattr(exc, "status_code", None)
            if status_code in (401, 403, 400, 422):
                logger.error(f"[Discovery] Non-retryable HTTP status code {status_code}. Aborting discovery immediately.")
                break
            
            is_retryable = is_connectivity_error(exc) or (status_code in (429, 500, 502, 503, 504))
            if not is_retryable:
                if not isinstance(exc, (requests.exceptions.RequestException, TimeoutError, ConnectionError, APIError)):
                    break

            backoff = min(15.0, (2 ** (min(attempt - 1, 4))) + random.uniform(0.1, 0.5))
            
            if status_code == 429:
                retry_after_header = getattr(exc, "response", None) and getattr(exc.response, "headers", {}).get("Retry-After")
                if retry_after_header:
                    try:
                        retry_seconds = float(retry_after_header)
                        backoff = max(backoff, retry_seconds)
                    except ValueError:
                        pass
            
            if time.monotonic() + backoff >= recovery_deadline:
                logger.warning("[Discovery] Backoff duration exceeds remaining recovery window. Stopping retries.")
                break
                
            logger.info(f"[Discovery State: DISCOVERY_RETRYING] Backing off for {backoff:.2f}s before retry...")
            time.sleep(backoff)

    if assets is None:
        logger.error(f"[Discovery State: UNIVERSE_DISCOVERY_FAILED] Asset discovery failed after 300-second recovery window. Last error: {last_exc}")
        raise ConnectivityError(f"UNIVERSE_DISCOVERY_FAILED: Asset discovery failed after 300 seconds: {last_exc}") from last_exc

    eligible_symbols, metadata_map, exclusion_counts = engine.filter_universe(assets, logger)
    duration = (datetime.now(timezone.utc) - start_time).total_seconds()

    if not eligible_symbols or len(eligible_symbols) < min_size:
        logger.error(f"[Discovery State: UNIVERSE_INVALID] Discovered universe size ({len(eligible_symbols)}) is below minimum required threshold ({min_size}).")
        raise ConnectivityError(f"UNIVERSE_INVALID: Discovered universe size {len(eligible_symbols)} below minimum threshold {min_size}.")

    engine.save_persisted_cache(eligible_symbols, metadata_map, start_time)

    # Layer 2 Daily Suitability Filter Integration (independent validation)
    suitability_engine = DailySuitabilityEngine(logger=logger)
    try:
        suitability_result = suitability_engine.evaluate_universe(eligible_symbols, start_time)
        daily_pool = suitability_result.pool
        quarantined_count = len(suitability_result.quarantined)
        actual_feed = suitability_result.actual_feed
        stale_flag = suitability_result.stale
    except Exception as exc:
        logger.error(f"[Universe Layer 2] Evaluation failed: {exc}. No fallback permitted. Raising UNIVERSE_DISCOVERY_FAILED.")
        raise ConnectivityError(f"UNIVERSE_DISCOVERY_FAILED: Layer 2 daily suitability evaluation failed: {exc}") from exc

    if not daily_pool:
        logger.error("[Discovery State: UNIVERSE_INVALID] Layer 2 daily pool is empty.")
        raise ConnectivityError("UNIVERSE_INVALID: Daily suitability pool is empty.")

    logger.info(
        f"[Discovery State: UNIVERSE_HEALTHY] [Universe Layer 1 & 2] L1 Eligible: {len(eligible_symbols)} | "
        f"L2 Daily Suitability Pool: {len(daily_pool)} (Quarantined: {quarantined_count}, "
        f"Feed: {actual_feed}, Stale: {stale_flag})"
    )
    return daily_pool


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

    def scan_universe(self, now_utc: datetime, locked_symbols: set[str], strategy: LiquiditySweepReversalStrategy, daily_pool: list[str] | None = None) -> tuple[list[tuple[str, float, Side | None, str, float, float, float]], list[dict[str, Any]]]:
        self.last_scan_attempts = 0
        self.last_scan_errors = 0
        self.last_scan_connectivity_errors = 0
        analyzed_candidates: list[tuple[str, float, Side | None, str, float, float, float]] = []
        dashboard_stocks = []
        locked_normalized = {normalize_symbol(s) for s in locked_symbols}

        pool_input = daily_pool if daily_pool is not None else self.global_universe
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

                bar_request = StockBarsRequest(
                    symbol_or_symbols=batch_symbols[0] if len(batch_symbols) == 1 else batch_symbols,
                    timeframe=TimeFrame(1, TimeFrameUnit.Minute),
                    start=batch_start_dt,
                    end=now_utc,
                    feed=self.data_feed,
                )
                bars_response = self.stock_client.get_stock_bars(bar_request)
                resp_data = getattr(bars_response, 'data', {}) or {}
                for sym_key, bars in resp_data.items():
                    if isinstance(sym_key, (list, tuple)):
                        for s in sym_key:
                            all_bars_data.setdefault(s, []).extend(bars)
                    else:
                        all_bars_data.setdefault(str(sym_key), []).extend(bars)

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

        layer4_selected = select_layer4_candidates(pool_input, bars_dict, max_candidates=8)
        self.logger.info(f"[Layer 4 Selector] Selected {len(layer4_selected)} candidates from pool of {len(pool_input)} symbols.")

        for item in layer4_selected:
            clean_symbol = item.symbol
            try:
                symbol_candles = self._candle_cache.get(clean_symbol, [])
                if not symbol_candles:
                    continue

                timezone_obj = ZoneInfo("America/New_York")
                series = MultiTimeframeSeries.from_1m(
                    list(symbol_candles),
                    entry_timeframe=Timeframe.FIVE_MIN,
                    timezone_name="America/New_York",
                    spread_bps=2.0,
                )
                snapshot = series.snapshot_at(now_utc, lookback_bars=len(series.entry.bars))

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
                if isinstance(strategy, ICTStrategy):
                    signal = strategy.signal(snapshot, analysis=analysis)
                else:
                    signal = strategy.signal(snapshot)
                if signal is None:
                    reason_msg = analysis.reason if analysis else "no signal"
                    self.logger.info(f"[{clean_symbol}] No signal generated | Reason: {reason_msg}")
                    continue

                self.logger.info(f"[{clean_symbol}] Signal | Score: {signal.confidence:.2f} | Side: {signal.side} | Entry: ${signal.entry_price:.2f}")

                analyzed_candidates.append((clean_symbol, signal.confidence, signal.side, signal.reason, signal.entry_price, signal.stop_price, signal.target_price, signal.timestamp))
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


class LivePortfolioManager:
    """Portfolio & Position Manager modeled after Freqtrade Trade persistence model.
    Handles order execution, bracket orders, state tracking, and lock protection.
    """
    def __init__(
        self,
        trading_client: TradingClient,
        logger: logging.Logger,
        stock_client: StockHistoricalDataClient | None = None,
        data_feed: DataFeed | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.trading_client = trading_client
        self.logger = logger
        self.stock_client = stock_client
        self.data_feed = data_feed
        self.settings = settings
        self.last_equity: float | None = None
        self.last_cash: float | None = None
        self.last_equity_snapshot: float | None = None
        self.last_snapshot_time: str | None = None
        self.last_submit_failures = 0
        self._last_equity_date = None
        self.on_unprotected: Any | None = None
        self.safe_uncertainty_state = False

    def fetch_account_position_lists(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]], set[str]]:
        try:
            positions_resp = self.trading_client.get_all_positions()
            orders_req = GetOrdersRequest(status=QueryOrderStatus.OPEN, nested=True, limit=500)
            orders_resp = self.trading_client.get_orders(orders_req)
        except Exception as exc:
            self.logger.error(f"Failed to fetch positions and open orders from Alpaca API: {exc}")
            raise

        position_symbols = {p.symbol.upper().strip() for p in positions_resp}
        order_symbols = {o.symbol.upper().strip() for o in orders_resp if hasattr(o, 'symbol') and o.symbol}
        locked_symbols = position_symbols | order_symbols

        open_positions = []
        dashboard_positions = []
        for p in positions_resp:
            sym = p.symbol.upper().strip()
            qty = float(p.qty)
            avg_entry = float(p.avg_entry_price)
            curr_price = float(p.current_price) if hasattr(p, 'current_price') else avg_entry
            unrealized_pnl = float(p.unrealized_pl) if hasattr(p, 'unrealized_pl') else 0.0
            side_str = "BUY" if qty > 0 else "SELL"

            sl_price = None
            tp_price = None
            for o in orders_resp:
                if hasattr(o, 'symbol') and o.symbol and o.symbol.upper().strip() == sym:
                    if hasattr(o, 'stop_loss') and o.stop_loss and hasattr(o.stop_loss, 'stop_price'):
                        sl_price = float(o.stop_loss.stop_price)
                    elif hasattr(o, 'legs') and o.legs:
                        for leg in o.legs:
                            if hasattr(leg, 'stop_price') and leg.stop_price:
                                sl_price = float(leg.stop_price)
                            if hasattr(leg, 'limit_price') and leg.limit_price:
                                tp_price = float(leg.limit_price)

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
        return open_positions, dashboard_positions, locked_symbols

    def fetch_account_and_positions(self) -> tuple[float, float, float, list[dict[str, Any]], list[dict[str, Any]], set[str]]:
        try:
            account = self.trading_client.get_account()
            account_cash = float(account.cash)
            account_equity = float(account.equity)
            buying_power = float(account.buying_power)
            self.last_equity = float(account.last_equity) if hasattr(account, 'last_equity') and account.last_equity is not None else account_equity
        except Exception as exc:
            self.logger.error(f"Failed to fetch account from Alpaca API: {exc}")
            raise

        try:
            open_positions, dashboard_positions, locked_symbols = self.fetch_account_position_lists()
        except Exception as exc:
            self.logger.error(f"Failed to fetch positions and orders from Alpaca API: {exc}")
            raise

        self.last_cash = account_cash
        self.last_equity_snapshot = account_equity
        self.last_snapshot_time = datetime.now(timezone.utc).isoformat()
        return account_cash, account_equity, buying_power, open_positions, dashboard_positions, locked_symbols

    def fetch_position_state(self) -> tuple[str, list[dict[str, Any]], set[str]]:
        try:
            open_positions, _, locked_symbols = self.fetch_account_position_lists()
            if open_positions or locked_symbols:
                return "POSITIONS_OPEN", open_positions, locked_symbols
            else:
                return "POSITIONS_CONFIRMED_FLAT", [], set()
        except Exception as exc:
            self.logger.error(f"Failed to fetch position state (POSITIONS_UNKNOWN): {exc}")
            return "POSITIONS_UNKNOWN", [], set()

    def fetch_open_order_symbols(self) -> set[str]:
        req = GetOrdersRequest(status=QueryOrderStatus.OPEN, nested=True, limit=500)
        orders = self.trading_client.get_orders(req)
        return {normalize_symbol(o.symbol) for o in orders if hasattr(o, 'symbol') and o.symbol}

    def submit_bracket_orders(
        self,
        top_new: list[tuple[Any, ...]],
        locked_symbols: set[str],
        account_equity: float,
        settings: Settings,
        bar_time: datetime | None = None,
        processed_signals: set[tuple[str, datetime]] | None = None,
        locked_normalized: set[str] | None = None,
        open_exposure: float = 0.0,
        upcoming_opportunities: list[Any] | None = None,
        win_probabilities: dict[str, float] | None = None,
    ) -> int:
        active_untraded_slots = max(1, 5 - len(locked_symbols))
        open_risk = len(locked_symbols) * settings.risk_per_trade_pct * account_equity
        orders_sent = 0
        self.last_submit_failures = 0

        if locked_normalized is None:
            locked_normalized = {normalize_symbol(s) for s in locked_symbols}

        eligible = []
        for item in top_new[:active_untraded_slots]:
            if len(item) >= 8:
                sym, sc, side_val, reason, curr_price, sl_price, tp_price, signal_ts = item[:8]
            else:
                sym, sc, side_val, reason, curr_price, sl_price, tp_price = item[:7]
                signal_ts = None

            if signal_ts is None:
                self.logger.info(f"Skipping {sym}: missing signal timestamp (fail-closed freshness requirement)")
                continue

            norm_sym = normalize_symbol(sym)
            if norm_sym in locked_normalized:
                continue
            if bar_time is not None and processed_signals is not None and (norm_sym, bar_time) in processed_signals:
                continue
            if "/" in sym:
                self.logger.info(f"Skipping {sym}: unsupported slash symbol")
                continue
            if curr_price > 0.70 * account_equity:
                self.logger.info(f"Skipping {sym}: candidate price ${curr_price:.2f} exceeds affordability threshold")
                continue

            # 1. Freshness check using 5-minute bar boundary and scanner latency
            if signal_ts is not None:
                if signal_ts.tzinfo is None:
                    signal_ts = signal_ts.replace(tzinfo=timezone.utc)
                eval_time = bar_time if bar_time is not None else datetime.now(timezone.utc)
                if eval_time.tzinfo is None:
                    eval_time = eval_time.replace(tzinfo=timezone.utc)
                age = (eval_time - signal_ts).total_seconds()
                if age > 390:  # 5m bar (300s) + 90s latency tolerance
                    self.logger.info(f"Skipping {sym}: stale signal (age={age:.1f}s exceeds 390s threshold)")
                    continue

            # If safe uncertainty state is active, block new entries
            if getattr(self, 'safe_uncertainty_state', False):
                self.logger.warning(f"Skipping {sym}: engine in safe uncertainty state due to prior ambiguous order submission.")
                continue

            # 2. Pre-flight executable price check & max deviation check
            exec_price = curr_price
            if self.stock_client is None:
                self.logger.info(f"Skipping {sym}: stock_client unavailable (fail-closed requirement)")
                continue

            if hasattr(self.stock_client, 'get_stock_latest_quote'):
                feed_enum = self.data_feed if self.data_feed is not None else (DataFeed.SIP if self.settings and self.settings.alpaca_data_feed.lower() == "sip" else DataFeed.IEX)
                feed_label = "SIP NBBO" if feed_enum == DataFeed.SIP else "IEX reference quote"
                try:
                    req = StockLatestQuoteRequest(symbol_or_symbols=[sym], feed=feed_enum)
                    quotes_resp = self.stock_client.get_stock_latest_quote(req)
                    quote_obj = quotes_resp.get(sym) if isinstance(quotes_resp, dict) else getattr(quotes_resp, sym, None)
                    if quote_obj is None:
                        self.logger.info(f"Skipping {sym}: quote retrieval failed (no quote returned from {feed_label})")
                        continue

                    q_time = getattr(quote_obj, 'timestamp', None)
                    if q_time is None:
                        self.logger.info(f"Skipping {sym}: missing quote timestamp from {feed_label}")
                        continue

                    if q_time.tzinfo is None:
                        q_time = q_time.replace(tzinfo=timezone.utc)
                    age = (eval_time - q_time).total_seconds()
                    max_future_skew = getattr(self.settings, 'quote_max_future_skew_seconds', 2.0) if self.settings else 2.0
                    if age < -max_future_skew:
                        self.logger.info(f"Skipping {sym}: future quote timestamp from {feed_label} (age={age:.1f}s violates max future skew -{max_future_skew}s)")
                        continue

                    max_age = getattr(self.settings, 'quote_max_age_seconds', 10.0) if self.settings else 10.0
                    if age > max_age:
                        self.logger.info(f"Skipping {sym}: stale quote from {feed_label} (age={age:.1f}s exceeds {max_age}s threshold)")
                        continue

                    bid = float(getattr(quote_obj, 'bid_price', 0.0) or 0.0)
                    ask = float(getattr(quote_obj, 'ask_price', 0.0) or 0.0)
                    if bid <= 0 or ask <= 0:
                        self.logger.info(f"Skipping {sym}: invalid quote prices from {feed_label} (bid=${bid:.2f}, ask=${ask:.2f})")
                        continue
                    if bid >= ask:
                        self.logger.info(f"Skipping {sym}: crossed or locked spread from {feed_label} (bid=${bid:.2f} >= ask=${ask:.2f})")
                        continue

                    exec_price = ask if side_val is Side.BUY else bid
                    spread = ask - bid
                    self.logger.info(f"Quote [{feed_label}] {sym}: bid=${bid:.2f}, ask=${ask:.2f}, spread=${spread:.2f}, ref_price=${exec_price:.2f}, age={age:.1f}s")
                except Exception as q_exc:
                    self.logger.info(f"Skipping {sym}: quote retrieval/validation failed: {q_exc}")
                    continue
            else:
                self.logger.info(f"Skipping {sym}: stock_client does not implement get_stock_latest_quote (fail-closed requirement)")
                continue

            deviation = abs(exec_price - curr_price) / curr_price if curr_price > 0 else 0.0
            if deviation > 0.01:
                self.logger.info(f"Skipping {sym}: price deviation {deviation*100:.2f}% exceeds 1.0% limit (exec=${exec_price:.2f}, curr=${curr_price:.2f})")
                continue

            # 3. Directional bracket geometry validation (after rounding to 2 decimals)
            exec_r = round(exec_price, 2)
            sl_r = round(sl_price, 2)
            tp_r = round(tp_price, 2)
            if side_val is Side.BUY:
                valid_geometry = (sl_r < exec_r < tp_r)
            else:
                valid_geometry = (tp_r < exec_r < sl_r)

            if not valid_geometry:
                self.logger.info(f"Skipping {sym}: invalid bracket geometry for {side_val}: stop=${sl_r}, entry=${exec_r}, target=${tp_r}")
                continue

            # 4. Explicit execution minimum RR of 1.10 (signed directional calculation)
            risk_exec = abs(exec_r - sl_r)
            reward_exec = abs(tp_r - exec_r)
            rr_exec = (reward_exec / risk_exec) if risk_exec > 0 else 0.0
            if rr_exec < 1.10:
                self.logger.info(f"Skipping {sym}: execution reward/risk {rr_exec:.2f} below minimum 1.10")
                continue

            if sc >= settings.min_candidate_score and sym not in locked_symbols and side_val is not None:
                eligible.append((sym, sc, side_val, reason, curr_price, sl_price, tp_price, exec_r, sl_r, tp_r, risk_exec, reward_exec, rr_exec))

        free_capital = max(0.0, account_equity - open_exposure)
        upcoming_ops = upcoming_opportunities or []
        win_probs = win_probabilities or {}

        # Opportunity-aware allocation model: rolling 30-min horizon opportunity count N
        total_ops_count = max(1, len(eligible) + len(upcoming_ops))
        baseline_allocation = free_capital / total_ops_count if total_ops_count > 0 else free_capital
        is_isolated = (len(eligible) == 1 and len(upcoming_ops) == 0)
        isolated_cap = settings.isolated_opportunity_max_pct * account_equity

        valid_eligible = []
        for sym, sc, side_val, reason, curr_price, sl_price, tp_price, exec_r, sl_r, tp_r, risk_exec, reward_exec, rr_exec in eligible:
            stop_pct = (risk_exec / exec_r * 100) if exec_r > 0 else 0.0
            target_pct = (reward_exec / exec_r * 100) if exec_r > 0 else 0.0

            if risk_exec <= 0:
                self.logger.info(f"Skipping {sym}: risk per share <= 0")
                decision = "skipped_risk_per_share_non_positive"
                self.logger.info(f"Candidate {sym}: score={sc:.2f}, side={side_val}, entry=${exec_r:.2f}, stop=${sl_r:.2f}, target=${tp_r:.2f}, stop_pct={stop_pct:.2f}%, target_pct={target_pct:.2f}%, rr={rr_exec:.2f}, notional_cap=$0.00, qty=0, gross=$0.00, cost=$0.00, decision={decision}")
                continue

            if settings.min_target_pct > 0 and target_pct < settings.min_target_pct:
                self.logger.info(f"Skipping {sym}: target {target_pct:.2f}% below minimum {settings.min_target_pct}%")
                decision = "skipped_target_pct"
                self.logger.info(f"Candidate {sym}: score={sc:.2f}, side={side_val}, entry=${exec_r:.2f}, stop=${sl_r:.2f}, target=${tp_r:.2f}, stop_pct={stop_pct:.2f}%, target_pct={target_pct:.2f}%, rr={rr_exec:.2f}, notional_cap=$0.00, qty=0, gross=$0.00, cost=$0.00, decision={decision}")
                continue

            valid_eligible.append((sym, sc, side_val, reason, curr_price, sl_price, tp_price, exec_r, sl_r, tp_r, risk_exec, reward_exec, stop_pct, target_pct, rr_exec))

        for idx, (sym, sc, side_val, reason, curr_price, sl_price, tp_price, exec_r, sl_r, tp_r, risk_exec, reward_exec, stop_pct, target_pct, rr_exec) in enumerate(valid_eligible):
            if getattr(self, 'safe_uncertainty_state', False):
                self.logger.warning("Aborting order submissions in this cycle: engine is in safe_uncertainty_state.")
                break

            norm_sym = normalize_symbol(sym)
            win_prob = win_probs.get(sym, None)  # None = unknown / uncalibrated

            if is_isolated:
                notional_cap = min(isolated_cap, free_capital, settings.max_single_position_pct * account_equity)
                alloc_mode = "isolated_opportunity_cap"
            else:
                required_one_share = exec_r
                base_notional = max(baseline_allocation, required_one_share)

                # Above-baseline exception rule: win probability > 60% (0.60)
                if win_prob is not None and win_prob > 0.60:
                    upcoming_demand = sum(
                        op.get("required_capital", 100.0)
                        for op in upcoming_ops
                        if op.get("actionable_mins", 30) <= 15
                    )
                    available_for_current = max(0.0, free_capital - upcoming_demand)
                    notional_cap = min(
                        settings.max_single_position_pct * account_equity,
                        free_capital,
                        max(base_notional, available_for_current)
                    )
                    alloc_mode = "above_baseline_approved_win_prob_gt_60"
                else:
                    notional_cap = min(base_notional, free_capital)
                    alloc_mode = "baseline_allocation_strict"

            self.logger.info(
                f"[Allocation Model] {sym}: N={total_ops_count}, baseline=${baseline_allocation:.2f}, "
                f"win_prob={win_prob if win_prob is not None else 'unknown/uncalibrated'}, "
                f"mode={alloc_mode}, notional_cap=${notional_cap:.2f}"
            )

            if notional_cap < exec_r:
                self.logger.info(f"Skipping {sym}: allocation ${notional_cap:.2f} below one share (${exec_r:.2f})")
                decision = "skipped_allocation_below_one_share"
                self.logger.info(f"Candidate {sym}: score={sc:.2f}, side={side_val}, entry=${exec_r:.2f}, stop=${sl_r:.2f}, target=${tp_r:.2f}, stop_pct={stop_pct:.2f}%, target_pct={target_pct:.2f}%, rr={rr_exec:.2f}, notional_cap=${notional_cap:.2f}, qty=0, gross=$0.00, cost=$0.00, decision={decision}")
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
                        entry=exec_r,
                        stop=sl_r,
                        open_risk=open_risk,
                        active_candidates_count=5,
                        market_open=True,
                    )
                else:
                    decision_obj = risk_engine.size_short(
                        available_capital=account_equity,
                        entry=exec_r,
                        stop=sl_r,
                        open_risk=open_risk,
                        active_candidates_count=5,
                        market_open=True,
                    )

                if not decision_obj.approved:
                    self.logger.info(f"Skipping {sym}: RiskEngine rejected trade ({decision_obj.reason})")
                    decision = f"rejected_{decision_obj.reason}"
                    self.logger.info(f"Candidate {sym}: score={sc:.2f}, side={side_val}, entry=${exec_r:.2f}, stop=${sl_r:.2f}, target=${tp_r:.2f}, stop_pct={stop_pct:.2f}%, target_pct={target_pct:.2f}%, rr={rr_exec:.2f}, notional_cap=${notional_cap:.2f}, qty=0, gross=$0.00, cost=$0.00, decision={decision}")
                    continue

                qty = int(decision_obj.quantity)
                if qty < 1:
                    self.logger.info(f"Skipping {sym}: whole share quantity ({qty}) < 1")
                    decision = "qty_below_one"
                    self.logger.info(f"Candidate {sym}: score={sc:.2f}, side={side_val}, entry=${exec_r:.2f}, stop=${sl_r:.2f}, target=${tp_r:.2f}, stop_pct={stop_pct:.2f}%, target_pct={target_pct:.2f}%, rr={rr_exec:.2f}, notional_cap=${notional_cap:.2f}, qty={qty}, gross=$0.00, cost=$0.00, decision={decision}")
                    continue

                gross = qty * reward_exec
                sale_notional = (qty * tp_r) if side_val is Side.BUY else (qty * exec_r)
                sec_fee = sale_notional * settings.estimated_sec_fee_rate
                finra_taf = min(settings.estimated_taf_cap_usd, settings.estimated_regulatory_fee_per_share * qty)
                cat_fee = settings.estimated_cat_fee_per_share * qty * 2
                regulatory_fee = sec_fee + finra_taf + cat_fee
                explicit_commission = max(settings.commission_per_order_usd, settings.commission_per_share_usd * qty)
                cost = explicit_commission + regulatory_fee
                min_multiple = settings.min_gross_to_cost_multiple

                if gross < min_multiple * cost:
                    self.logger.info(f"Skipping {sym}: expected gross ${gross:.2f} below {min_multiple} x round-trip fee estimate ${cost:.2f}")
                    decision = "skipped_cost_gate"
                    self.logger.info(f"Candidate {sym}: score={sc:.2f}, side={side_val}, entry=${exec_r:.2f}, stop=${sl_r:.2f}, target=${tp_r:.2f}, stop_pct={stop_pct:.2f}%, target_pct={target_pct:.2f}%, rr={rr_exec:.2f}, notional_cap=${notional_cap:.2f}, qty={qty}, gross=${gross:.2f}, cost=${cost:.2f}, decision={decision}")
                    continue

                decision = "submitted"
                self.logger.info(f"Candidate {sym}: score={sc:.2f}, side={side_val}, entry=${exec_r:.2f}, stop=${sl_r:.2f}, target=${tp_r:.2f}, stop_pct={stop_pct:.2f}%, target_pct={target_pct:.2f}%, rr={rr_exec:.2f}, notional_cap=${notional_cap:.2f}, qty={qty}, gross=${gross:.2f}, cost=${cost:.2f}, decision={decision}")

                client_order_id = f"mm-{norm_sym}-{int(time.time()*1000)}-{random.randint(100,999)}"
                alpaca_side = OrderSide.BUY if side_val is Side.BUY else OrderSide.SELL
                order_req = MarketOrderRequest(
                    symbol=sym,
                    qty=qty,
                    side=alpaca_side,
                    time_in_force=TimeInForce.DAY,
                    order_class=OrderClass.BRACKET,
                    take_profit=TakeProfitRequest(limit_price=tp_r),
                    stop_loss=StopLossRequest(stop_price=sl_r),
                    client_order_id=client_order_id
                )
                submitted_order = self.trading_client.submit_order(order_req)

                oc = getattr(submitted_order, 'order_class', None)
                oc_val = oc.value if hasattr(oc, 'value') else str(oc) if oc is not None else None
                oc_lower = oc_val.lower() if oc_val is not None else None
                legs = getattr(submitted_order, 'legs', None)

                self.logger.info(f"ORDER SUBMITTED (client_order_id={client_order_id}, class={oc_val if oc is not None else 'unknown'}): {alpaca_side.value} {qty} shares of {sym} @ ~${exec_r:.2f} (SL: ${sl_r:.2f}, TP: ${tp_r:.2f}, Score: {sc:.2f})")

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

                free_capital = max(0.0, free_capital - qty * exec_r)

            except Exception as ord_err:
                self.last_submit_failures += 1
                self.logger.error(f"Failed to submit bracket order for {sym} (client_order_id={client_order_id}): {ord_err}")
                try:
                    reconciled_order = None
                    if hasattr(self.trading_client, 'get_order_by_client_id'):
                        try:
                            reconciled_order = self.trading_client.get_order_by_client_id(client_order_id)
                        except Exception:
                            reconciled_order = None

                    if reconciled_order is not None:
                        status = getattr(reconciled_order, 'status', None)
                        status_val = status.value if hasattr(status, 'value') else (str(status).lower() if status is not None else None)
                        if status_val is not None:
                            status_val = status_val.lower()

                        active_states = {"filled", "partially_filled", "new", "accepted", "pending_new", "queued", "pending"}
                        terminal_states = {"canceled", "cancelled", "rejected", "expired", "stopped", "suspended", "done_for_day", "replaced"}

                        if status_val in active_states:
                            self.logger.warning(f"Reconciliation check: order {client_order_id} found active with status '{status_val}'.")
                            open_risk += decision_obj.capital_at_risk
                            orders_sent += 1
                        elif status_val in terminal_states:
                            self.logger.info(f"Order {client_order_id} status is terminal ({status_val}); not counting as open risk.")
                        else:
                            self.logger.error(f"Ambiguous or unrecognized order status '{status_val}' for client_order_id={client_order_id}. Entering safe uncertainty state.")
                            self.safe_uncertainty_state = True
                    else:
                        self.logger.error(f"Ambiguous submission for {sym} (client_order_id={client_order_id}): order not found via client_order_id lookup. Entering safe uncertainty state.")
                        self.safe_uncertainty_state = True
                except Exception as rec_err:
                    self.logger.error(f"Failed to reconcile order {client_order_id} via client_order_id lookup: {rec_err}")
                    self.safe_uncertainty_state = True

        return orders_sent

    def flatten_all(self, sleep_fn=time.sleep, retries: int = 3, retry_delay: int = 5, max_wait_seconds: int = 0) -> list[str]:
        if max_wait_seconds <= 0:
            try:
                max_wait_seconds = Settings().flatten_max_wait_seconds
            except Exception:
                max_wait_seconds = 600

        start_time = time.monotonic()
        total_slept = 0.0
        effective_delay = min(retry_delay, 15)
        last_successful_remaining: list[str] | None = None
        while True:
            real_elapsed = time.monotonic() - start_time
            elapsed = max(real_elapsed, total_slept)
            if elapsed >= max_wait_seconds:
                break

            try:
                self.trading_client.cancel_orders()
            except Exception as exc:
                self.logger.error(f"Error cancelling orders during flatten_all: {exc}")

            real_elapsed = time.monotonic() - start_time
            elapsed = max(real_elapsed, total_slept)
            if elapsed >= max_wait_seconds:
                break

            try:
                self.trading_client.close_all_positions(cancel_orders=True)
            except Exception as exc:
                self.logger.error(f"Error closing all positions during flatten_all: {exc}")

            real_elapsed = time.monotonic() - start_time
            elapsed = max(real_elapsed, total_slept)
            if elapsed >= max_wait_seconds:
                break

            try:
                _, _, _, open_pos, _, _ = self.fetch_account_and_positions()
                remaining = [p["symbol"] for p in open_pos]
                last_successful_remaining = remaining
                if not remaining:
                    return []
            except Exception as exc:
                self.logger.error(f"Error fetching open positions during flatten_all verification: {exc}")

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
            f"risk_per_trade_pct={self.settings.risk_per_trade_pct}"
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

        feed_val = self.settings.alpaca_data_feed.strip().lower()
        data_feed_enum = DataFeed.SIP if feed_val == "sip" else DataFeed.IEX
        if data_feed_enum == DataFeed.IEX:
            self.logger.warning("IEX feed carries only a fraction of market volume; relative volume confirmation is unreliable.")

        self.strategy = ICTStrategy()
        self.portfolio_manager = LivePortfolioManager(
            self.trading_client,
            self.logger,
            stock_client=self.stock_client,
            data_feed=data_feed_enum,
            settings=self.settings,
        )
        self.portfolio_manager.on_unprotected = lambda reason: self.kill_switch.trip(reason) if hasattr(self, 'kill_switch') else None
        
        self._cached_universe: list[str] = []
        self._universe_last_fetched: datetime | None = None
        self._universe_last_failed: datetime | None = None
        self._universe_retry_cooldown_minutes = 60
        self._api_key = api_key or "mock_key"
        self.safe_drain_mode = False
        self._last_safe_drain_retry: datetime | None = None
        self.discovery_worker = UniverseDiscoveryWorker(self.trading_client, self.logger, self._api_key)

        now_utc = datetime.now(timezone.utc)
        try:
            initial_universe = discover_universe(self.trading_client, self.logger, self._api_key)
            self._cached_universe = initial_universe
            self._universe_last_fetched = now_utc
        except Exception as exc:
            pos_status, open_pos, _ = self.portfolio_manager.fetch_position_state()
            if pos_status in ("POSITIONS_OPEN", "POSITIONS_UNKNOWN"):
                self.logger.warning(f"[Discovery State: SAFE_DRAIN] Startup discovery failed ({exc}) and position status is {pos_status}. Entering SAFE_DRAIN mode.")
                self.safe_drain_mode = True
                self._cached_universe = []
                self.discovery_worker.start()
            elif pos_status == "POSITIONS_CONFIRMED_FLAT":
                self.logger.critical(f"[Discovery State: UNIVERSE_DISCOVERY_FAILED] Startup discovery failed and account is flat ({pos_status}). Aborting startup.")
                raise ConnectivityError("UNIVERSE_DISCOVERY_FAILED: Startup discovery failed and account is flat.")
            else:
                self.logger.critical(f"[Discovery State: UNIVERSE_DISCOVERY_FAILED] Critical startup failure during dynamic universe discovery: {exc}")
                raise ConnectivityError(f"UNIVERSE_DISCOVERY_FAILED: Startup discovery failed: {exc}") from exc
        self.global_universe = self._cached_universe

        self.scanner = MarketScanner(self.global_universe, self.stock_client, self.logger, data_feed=data_feed_enum)
        self.kill_switch = KillSwitch(
            max_consecutive_errors=self.settings.max_consecutive_errors,
            max_daily_loss_pct=self.settings.max_daily_loss_pct,
        )
        self.portfolio_manager.on_unprotected = lambda reason: self.kill_switch.trip(reason)

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
        with self.discovery_worker._lock:
            if self.discovery_worker.state == "SUCCESS" and self.discovery_worker.universe:
                self._cached_universe = self.discovery_worker.universe
                self.global_universe = self._cached_universe
                self.safe_drain_mode = False
                self.discovery_worker.state = "IDLE"
                return self._cached_universe

        if getattr(self, 'safe_drain_mode', False):
            universe_last_failed = getattr(self, '_universe_last_failed', None)
            self.discovery_worker.start_if_needed(
                lambda: universe_last_failed is None or (now_utc - universe_last_failed) > timedelta(seconds=60)
            )
            return getattr(self, '_cached_universe', [])

        cached_universe = getattr(self, '_cached_universe', [])
        universe_last_fetched = getattr(self, '_universe_last_fetched', None)
        universe_last_failed = getattr(self, '_universe_last_failed', None)

        refresh_interval_expired = (
            not cached_universe or
            universe_last_fetched is None or
            (now_utc - universe_last_fetched) > timedelta(hours=24)
        )

        refresh_needed = refresh_interval_expired or universe_last_failed is not None

        if refresh_needed or not cached_universe:
            self.discovery_worker.start_if_needed()
            pos_status, _, _ = self.portfolio_manager.fetch_position_state()
            if not cached_universe and pos_status == "POSITIONS_CONFIRMED_FLAT":
                raise ConnectivityError("UNIVERSE_DISCOVERY_FAILED: Initial discovery pending and account is flat.")
        return getattr(self, '_cached_universe', [])

    def _fetch_position_and_account_safely(self) -> tuple[str, list[dict[str, Any]], set[str], list[dict[str, Any]], float, float]:
        try:
            cash, equity, _, open_pos, dash_pos, locked = self.portfolio_manager.fetch_account_and_positions()
            if open_pos:
                return "POSITIONS_OPEN", open_pos, locked, dash_pos, cash, equity
            else:
                return "POSITIONS_CONFIRMED_FLAT", [], locked, [], cash, equity
        except Exception as exc:
            self.logger.error(f"[Reconciliation] Failed to fetch account and positions: {exc}")
            raise

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

        # Non-blocking check of discovery worker result
        if hasattr(self, 'discovery_worker') and self.discovery_worker is not None:
            with self.discovery_worker._lock:
                if self.discovery_worker.state == "SUCCESS" and self.discovery_worker.universe:
                    self._cached_universe = self.discovery_worker.universe
                    self.global_universe = self._cached_universe
                    self.safe_drain_mode = False
                    self.discovery_worker.state = "IDLE"
                    self.logger.info("[Discovery State: UNIVERSE_HEALTHY] Background discovery succeeded, normal trading resumed.")
                elif self.discovery_worker.state == "FAILED":
                    pos_status, _, _, _, _, _ = self._fetch_position_and_account_safely()
                    if pos_status == "POSITIONS_CONFIRMED_FLAT":
                        self.logger.info("[Discovery State: UNIVERSE_DISCOVERY_FAILED] Discovery failed, account is confirmed flat. Shutting down gracefully.")
                        return {"evaluated": 0, "signals": 0, "orders_sent": 0, "halted": True, "safe_drain": True, "flat": True}
                    else:
                        self.safe_drain_mode = True
                        self.discovery_worker.state = "IDLE"

        # Always fetch position and account state safely for position management (Critical 2 & 3)
        position_status, open_positions, locked_symbols_raw, dashboard_positions, account_cash, account_equity = self._fetch_position_and_account_safely()

        if position_status == "POSITIONS_UNKNOWN":
            self.logger.warning("[SAFE_DRAIN] Position retrieval resulted in POSITIONS_UNKNOWN. Maintaining SAFE_DRAIN and retrying.")
            self.safe_drain_mode = True

        if not getattr(self, '_cached_universe', []):
            if getattr(self, 'global_universe', []):
                self._cached_universe = self.global_universe
            else:
                self.safe_drain_mode = True

        if getattr(self, 'safe_drain_mode', False):
            if position_status == "POSITIONS_CONFIRMED_FLAT":
                self.logger.info("[Discovery State: UNIVERSE_DISCOVERY_FAILED] SAFE_DRAIN active and broker confirmed zero open positions (POSITIONS_CONFIRMED_FLAT). Shutting down gracefully.")
                return {"evaluated": 0, "signals": 0, "orders_sent": 0, "halted": True, "safe_drain": True, "flat": True}

            self.logger.warning(f"[Discovery State: SAFE_DRAIN] Bot in SAFE_DRAIN mode with {len(open_positions)} open positions. Managing existing positions; new entries blocked.")
            
            # Maintain risk checks and position monitoring state
            self.latest_state["cash"] = account_cash
            self.latest_state["equity"] = account_equity
            self.latest_state["open_positions"] = dashboard_positions

            # Trigger background discovery worker periodically without blocking
            universe_last_failed = getattr(self, '_universe_last_failed', None)
            if hasattr(self, 'discovery_worker') and self.discovery_worker is not None:
                self.discovery_worker.start_if_needed(
                    lambda: universe_last_failed is None or (now_utc - universe_last_failed) > timedelta(seconds=60)
                )

            return {"evaluated": 0, "signals": 0, "orders_sent": 0, "safe_drain": True}

        self.kill_switch.check_daily_loss(self.portfolio_manager.last_equity, account_equity)
        if self.kill_switch.tripped:
            self.logger.critical(f"Kill switch tripped: {self.kill_switch.reason}")
            return {"evaluated": 0, "signals": 0, "orders_sent": 0, "halted": True}

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

        evaluated = 0
        for sym in self.global_universe:
            clean_sym = sym.upper().strip()
            if normalize_symbol(clean_sym) in locked_normalized:
                continue
            open_status, _ = MarketScanner.is_session_open(clean_sym, now_utc)
            if open_status:
                evaluated += 1

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

        def _safe_flatten() -> list[str]:
            try:
                res_pos = self.portfolio_manager.flatten_all(sleep_fn, max_wait_seconds=self.settings.flatten_max_wait_seconds)
            except Exception as exc:
                self.logger.critical(f"flatten_all raised exception: {exc}")
                res_pos = ["UNKNOWN"]

            if res_pos:
                if res_pos == ["UNKNOWN"]:
                    self.logger.critical(f"POSITIONS MAY STILL BE OPEN: {res_pos} (position state could not be verified)")
                else:
                    self.logger.critical(f"POSITIONS MAY STILL BE OPEN: {res_pos}")
            return res_pos

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
                            self.logger.info("Market open reached but clock reports closed (possible few-second lag). Polling continuously for up to 2 minutes...")
                            poll_start_time = now_fn()
                            poll_timeout_seconds = 120.0
                            poll_interval = 2.0
                            opened_detected = False
                            while True:
                                sleep_fn(poll_interval)
                                now = now_fn()
                                if now.tzinfo is None and poll_start_time.tzinfo is not None:
                                    now = now.replace(tzinfo=timezone.utc)
                                elif now.tzinfo is not None and poll_start_time.tzinfo is None:
                                    poll_start_time = poll_start_time.replace(tzinfo=timezone.utc)
                                elapsed_seconds = (now - poll_start_time).total_seconds()
                                if elapsed_seconds >= poll_timeout_seconds:
                                    break
                                try:
                                    clock = self.trading_client.get_clock()
                                    if getattr(clock, 'is_open', False):
                                        opened_detected = True
                                        next_open = getattr(clock, 'next_open', next_open)
                                        next_close = getattr(clock, 'next_close', next_close)
                                        self.logger.info("Market open detected during continuous polling grace period! Starting trading phase.")
                                        break
                                except Exception as poll_exc:
                                    self.logger.warning(f"Error fetching clock during open polling: {poll_exc}")

                            if not opened_detected:
                                self.logger.info("Market still not open after 2-minute polling grace period. Skipping session.")
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
        snapshot_time = getattr(self.portfolio_manager, 'last_snapshot_time', None)
        cash = getattr(self.portfolio_manager, 'last_cash', None)
        equity = getattr(self.portfolio_manager, 'last_equity_snapshot', None)

        if snapshot_time is None or cash is None or equity is None:
            self.logger.info(f"[Session Summary] source=Alpaca Account API, timestamp=unavailable, status={status}, cycles={cycles}, orders_sent={orders_sent}, errors={errors}, cash=unavailable, equity=unavailable")
        else:
            last_eq = self.portfolio_manager.last_equity
            change_str = ""
            if last_eq is not None and last_eq > 0:
                diff = equity - last_eq
                pct = (diff / last_eq) * 100.0
                change_str = f", cash=${cash:.2f}, equity=${equity:.2f}, change=${diff:+.2f} ({pct:+.2f}%) vs baseline_equity=${last_eq:.2f}"
            else:
                change_str = f", cash=${cash:.2f}, equity=${equity:.2f}"
            self.logger.info(f"[Session Summary] source=Alpaca Account API, timestamp={snapshot_time}, status={status}, cycles={cycles}, orders_sent={orders_sent}, errors={errors}{change_str}")


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
