from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone, time as dt_time
from typing import Any
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, TakeProfitRequest, StopLossRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus, OrderClass
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import DataFeed

from money_mintter.config import Settings
from money_mintter.core.killswitch import KillSwitch
from money_mintter.core.risk import RiskEngine
from money_mintter.core.session_lock import SessionLock
from money_mintter.domain import Side
from money_mintter.strategy import Candle, LiquiditySweepReversalStrategy, MultiTimeframeSeries, StrategyConfig, Timeframe


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
        self.last_scan_attempts = 0
        self.last_scan_errors = 0

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

    def scan_universe(self, now_utc: datetime, locked_symbols: set[str], strategy: LiquiditySweepReversalStrategy) -> tuple[list[tuple[str, float, Side | None, str, float, float, float]], list[dict[str, Any]]]:
        self.last_scan_attempts = 0
        self.last_scan_errors = 0
        analyzed_candidates: list[tuple[str, float, Side | None, str, float, float, float]] = []
        dashboard_stocks = []
        locked_normalized = {normalize_symbol(s) for s in locked_symbols}

        self.logger.info(f"Scanning global asset pool ({len(self.global_universe)} assets) with core trading hours filtration...")

        for symbol in self.global_universe:
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

            self.last_scan_attempts += 1
            try:
                end_dt = now_utc
                start_dt = end_dt - timedelta(days=HISTORY_DAYS)

                self.logger.info(f"Analyzing {clean_symbol} | Venue Status: {session_msg}")

                bar_request = StockBarsRequest(
                    symbol_or_symbols=clean_symbol,
                    timeframe=TimeFrame(5, TimeFrameUnit.Minute),
                    start=start_dt,
                    end=end_dt,
                    feed=self.data_feed,
                )
                bars_response = self.stock_client.get_stock_bars(bar_request)

                symbol_bars = bars_response.data.get(clean_symbol, []) if hasattr(bars_response, 'data') else []

                if not symbol_bars:
                    self.logger.error(f"CRITICAL: Data stream returned empty for ticker {clean_symbol}")
                    continue

                candles = tuple(
                    Candle(
                        timestamp=b.timestamp,
                        open=float(b.open),
                        high=float(b.high),
                        low=float(b.low),
                        close=float(b.close),
                        volume=float(b.volume),
                        symbol=clean_symbol,
                    )
                    for b in symbol_bars
                )

                series = MultiTimeframeSeries.from_5m(
                    list(candles),
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
                self.logger.info(f"[{clean_symbol}] MTF Chart Analysis | {snapshot.candle_bias_summary} | 1H Levels: {snapshot.one_hour_levels} | 15M Levels: {snapshot.fifteen_minute_levels}")

                analysis = strategy.analyze(snapshot)
                signal = strategy.signal(snapshot)
                if signal is None:
                    reason_msg = analysis.reason if analysis else "no signal"
                    self.logger.info(f"[{clean_symbol}] No signal generated | Reason: {reason_msg}")
                    continue

                self.logger.info(f"[{clean_symbol}] Signal | Score: {signal.confidence:.2f} | Side: {signal.side} | Entry: ${signal.entry_price:.2f} | SL: ${signal.stop_price:.2f} | TP: ${signal.target_price:.2f} | Reason: {signal.reason}")

                analyzed_candidates.append((clean_symbol, signal.confidence, signal.side, signal.reason, signal.entry_price, signal.stop_price, signal.target_price))
                dashboard_stocks.append({
                    "symbol": clean_symbol,
                    "market": "Global/US",
                    "current_price": signal.entry_price,
                    "score": signal.confidence,
                })

            except Exception as ex:
                self.last_scan_errors += 1
                self.logger.error(f"CRITICAL: Data stream error for ticker {clean_symbol}: {ex}")

        return analyzed_candidates, dashboard_stocks


class LivePortfolioManager:
    """Portfolio & Position Manager modeled after Freqtrade Trade persistence model.
    Handles order execution, bracket orders, state tracking, and lock protection.
    """
    def __init__(self, trading_client: TradingClient, logger: logging.Logger) -> None:
        self.trading_client = trading_client
        self.logger = logger
        self.last_equity: float | None = None
        self.last_submit_failures = 0
        self._last_equity_date = None
        self.on_unprotected: Any | None = None

    def fetch_account_and_positions(self) -> tuple[float, float, float, list[dict[str, Any]], list[dict[str, Any]], set[str]]:
        account = self.trading_client.get_account()
        account_cash = float(account.cash)
        account_equity = float(account.equity)
        buying_power = float(account.buying_power)

        if not hasattr(self, 'last_equity') or self.last_equity is None:
            if hasattr(account, 'last_equity') and account.last_equity is not None:
                try:
                    self.last_equity = float(account.last_equity)
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
        for o in orders:
            if hasattr(o, 'symbol') and o.symbol:
                symbols.add(normalize_symbol(o.symbol))
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
        active_untraded_slots = max(1, 5 - len(locked_symbols))
        open_risk = len(locked_symbols) * settings.risk_per_trade_pct * account_equity
        orders_sent = 0
        self.last_submit_failures = 0

        if locked_normalized is None:
            locked_normalized = {normalize_symbol(s) for s in locked_symbols}

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

        for idx, (sym, sc, side_val, reason, curr_price, sl_price, tp_price) in enumerate(eligible):
            norm_sym = normalize_symbol(sym)
            remaining = len(eligible) - idx
            notional_cap = min(settings.max_single_position_pct * account_equity, free_capital / remaining if remaining > 0 else 0.0)

            if notional_cap < curr_price:
                self.logger.info(f"Skipping {sym}: allocation ${notional_cap:.2f} below one share (${curr_price:.2f})")
                self.logger.info(f"Candidate {sym}: notional_cap=${notional_cap:.2f}, qty=0, gross=$0.00, cost=$0.00, decision=skipped_allocation_below_one_share")
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
                    decision = risk_engine.size_long(
                        available_capital=account_equity,
                        entry=curr_price,
                        stop=sl_price,
                        open_risk=open_risk,
                        active_candidates_count=5,
                        market_open=True,
                    )
                else:
                    decision = risk_engine.size_short(
                        available_capital=account_equity,
                        entry=curr_price,
                        stop=sl_price,
                        open_risk=open_risk,
                        active_candidates_count=5,
                        market_open=True,
                    )

                if not decision.approved:
                    self.logger.info(f"Skipping {sym}: RiskEngine rejected trade ({decision.reason})")
                    self.logger.info(f"Candidate {sym}: notional_cap=${notional_cap:.2f}, qty=0, gross=$0.00, cost=$0.00, decision=rejected_{decision.reason}")
                    continue

                qty = int(decision.quantity)
                if qty < 1:
                    self.logger.info(f"Skipping {sym}: whole share quantity ({qty}) < 1")
                    self.logger.info(f"Candidate {sym}: notional_cap=${notional_cap:.2f}, qty={qty}, gross=$0.00, cost=$0.00, decision=qty_below_one")
                    continue

                gross = qty * abs(tp_price - curr_price)
                cost = 2 * max(settings.commission_per_order_usd, settings.commission_per_share_usd * qty)
                min_multiple = settings.min_gross_to_cost_multiple

                if gross < min_multiple * cost:
                    self.logger.info(f"Skipping {sym}: expected gross ${gross:.2f} below {min_multiple} x round-trip commission ${cost:.2f}")
                    self.logger.info(f"Candidate {sym}: notional_cap=${notional_cap:.2f}, qty={qty}, gross=${gross:.2f}, cost=${cost:.2f}, decision=skipped_cost_gate")
                    continue

                self.logger.info(f"Candidate {sym}: notional_cap=${notional_cap:.2f}, qty={qty}, gross=${gross:.2f}, cost=${cost:.2f}, decision=submitted")

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
                    open_risk += decision.capital_at_risk
                    orders_sent += 1
                elif oc_lower == "bracket" and isinstance(legs, list) and len(legs) >= 2:
                    self.logger.info(f"ORDER CONFIRMED: {sym} id={getattr(submitted_order, 'id', 'unknown')} class=bracket legs={len(legs)}")
                    open_risk += decision.capital_at_risk
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

    def flatten_all(self, sleep_fn=time.sleep, retries: int = 3, retry_delay: int = 5) -> list[str]:
        try:
            self.trading_client.cancel_orders()
        except Exception as exc:
            self.logger.error(f"Error cancelling orders during flatten_all: {exc}")

        try:
            self.trading_client.close_all_positions(cancel_orders=True)
        except Exception as exc:
            self.logger.error(f"Error closing all positions during flatten_all: {exc}")

        remaining = []
        for attempt in range(retries):
            try:
                _, _, _, open_pos, _, _ = self.fetch_account_and_positions()
                remaining = [p["symbol"] for p in open_pos]
                if not remaining:
                    break
            except Exception as exc:
                self.logger.error(f"Error fetching open positions during flatten_all verification attempt {attempt+1}: {exc}")
            if attempt < retries - 1:
                sleep_fn(retry_delay)
        return remaining


class LiveMarketBot:
    """Master Orchestrator class managing the trading lifecycle, dashboard server, and execution loops."""
    def __init__(self) -> None:
        load_dotenv()
        self.logger = self.setup_logging()
        self.settings = Settings()

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

        self.strategy = LiquiditySweepReversalStrategy(StrategyConfig())
        self.portfolio_manager = LivePortfolioManager(self.trading_client, self.logger)
        self.portfolio_manager.on_unprotected = lambda reason: self.kill_switch.trip(reason)
        
        self.global_universe = [
            "AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "GOOGL", "META", "AMD", "NFLX", "INTC",
            "SPY", "QQQ", "IWM", "XLF", "XLE", "XLK", "ARKK", "JPM", "BAC", "DIS",
            "GLD", "SLV", "USO", "UNG"
        ]

        feed_val = self.settings.alpaca_data_feed.strip().lower()
        data_feed_enum = DataFeed.SIP if feed_val == "sip" else DataFeed.IEX
        if data_feed_enum == DataFeed.IEX:
            self.logger.warning("IEX feed carries only a fraction of market volume; relative volume confirmation is unreliable.")

        self.scanner = MarketScanner(self.global_universe, self.stock_client, self.logger, data_feed=data_feed_enum)
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

        account_cash, account_equity, _, open_positions, dashboard_positions, locked_symbols_raw = self.portfolio_manager.fetch_account_and_positions()
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
            return res
        except Exception as exc:
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
                if res.get("error"):
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
                return self.portfolio_manager.flatten_all(sleep_fn)
            except Exception as exc:
                self.logger.critical(f"flatten_all raised exception: {exc}")
                return ["UNKNOWN"]

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
                    if res.get("error"):
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
        if cycles == 0:
            self.logger.info(f"[Session Summary] status={status}, cycles={cycles}, orders_sent={orders_sent}, errors={errors}, equity=n/a")
        else:
            equity = self.latest_state.get("equity", 100000.0)
            last_eq = self.portfolio_manager.last_equity
            change_str = ""
            if last_eq is not None and last_eq > 0:
                diff = equity - last_eq
                pct = (diff / last_eq) * 100.0
                change_str = f", equity=${equity:.2f}, change=${diff:+.2f} ({pct:+.2f}%) vs last_equity=${last_eq:.2f}"
            else:
                change_str = f", equity=${equity:.2f}"
            self.logger.info(f"[Session Summary] status={status}, cycles={cycles}, orders_sent={orders_sent}, errors={errors}{change_str}")


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
