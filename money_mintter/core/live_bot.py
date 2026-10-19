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
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
from alpaca.data.historical import StockHistoricalDataClient, CryptoHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import DataFeed

from money_mintter.config import Settings
from money_mintter.core.risk import RiskEngine
from money_mintter.domain import Side
from money_mintter.strategy import Candle, LiquiditySweepReversalStrategy, MultiTimeframeSeries, StrategyConfig, Timeframe


BAR_CLOSE_DELAY_SECONDS = 20
HISTORY_DAYS = 30


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
    def __init__(self, global_universe: list[str], stock_client: StockHistoricalDataClient, crypto_client: CryptoHistoricalDataClient, logger: logging.Logger, data_feed: DataFeed = DataFeed.IEX) -> None:
        self.global_universe = global_universe
        self.stock_client = stock_client
        self.crypto_client = crypto_client
        self.logger = logger
        self.data_feed = data_feed
        self.latest_snapshots: dict[str, MultiTimeframeSnapshot] = {}

    @staticmethod
    def is_session_open(symbol: str, now_utc: datetime) -> tuple[bool, str]:
        sym_upper = symbol.upper()
        if "/" in sym_upper and (sym_upper.endswith("/USD") or sym_upper.endswith("/USDT")):
            return True, "Crypto 24/7"

        if sym_upper.endswith("=F"):
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
        analyzed_candidates: list[tuple[str, float, Side | None, str, float, float, float]] = []
        dashboard_stocks = []
        locked_normalized = {normalize_symbol(s) for s in locked_symbols}

        self.logger.info(f"Scanning global asset pool ({len(self.global_universe)} assets) with core trading hours filtration...")

        for symbol in self.global_universe:
            clean_symbol = symbol.upper().strip()
            if "-" in clean_symbol and "/" not in clean_symbol and ("USD" in clean_symbol or "USDT" in clean_symbol):
                clean_symbol = clean_symbol.replace("-", "/")

            open_status, session_msg = MarketScanner.is_session_open(clean_symbol, now_utc)
            if not open_status:
                self.logger.info(f"Skipping {clean_symbol}: {session_msg}")
                continue

            if normalize_symbol(clean_symbol) in locked_normalized:
                self.logger.info(f"Retaining locked active trade slot for {clean_symbol} (Module 3.2 Slot Freezing)")
                continue

            try:
                end_dt = now_utc
                start_dt = end_dt - timedelta(days=HISTORY_DAYS)

                self.logger.info(f"Analyzing {clean_symbol} | Venue Status: {session_msg}")

                is_crypto = "/" in clean_symbol
                if is_crypto:
                    bar_request = CryptoBarsRequest(
                        symbol_or_symbols=clean_symbol,
                        timeframe=TimeFrame(5, TimeFrameUnit.Minute),
                        start=start_dt,
                        end=end_dt
                    )
                    bars_response = self.crypto_client.get_crypto_bars(bar_request)
                else:
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
                    "market": "Global/US/Crypto",
                    "current_price": signal.entry_price,
                    "score": signal.confidence,
                })

            except Exception as ex:
                self.logger.error(f"CRITICAL: Data stream error for ticker {clean_symbol}: {ex}")

        return analyzed_candidates, dashboard_stocks


class LivePortfolioManager:
    """Portfolio & Position Manager modeled after Freqtrade Trade persistence model.
    Handles order execution, bracket orders, state tracking, and lock protection.
    """
    def __init__(self, trading_client: TradingClient, logger: logging.Logger) -> None:
        self.trading_client = trading_client
        self.logger = logger

    def fetch_account_and_positions(self) -> tuple[float, float, float, list[dict[str, Any]], list[dict[str, Any]], set[str]]:
        account = self.trading_client.get_account()
        account_cash = float(account.cash)
        account_equity = float(account.equity)
        buying_power = float(account.buying_power)

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
    ) -> int:
        active_untraded_slots = max(1, 5 - len(locked_symbols))
        risk_engine = RiskEngine(
            risk_per_trade_pct=settings.risk_per_trade_pct,
            max_position_pct=settings.max_position_pct,
            max_portfolio_risk_pct=settings.max_portfolio_risk_pct,
            leverage=1.0,
        )
        open_risk = len(locked_symbols) * settings.risk_per_trade_pct * account_equity
        orders_sent = 0

        if locked_normalized is None:
            locked_normalized = {normalize_symbol(s) for s in locked_symbols}

        for sym, sc, side_val, reason, curr_price, sl_price, tp_price in top_new[:active_untraded_slots]:
            norm_sym = normalize_symbol(sym)
            if norm_sym in locked_normalized:
                continue
            if bar_time is not None and processed_signals is not None and (norm_sym, bar_time) in processed_signals:
                continue

            if sc >= settings.min_candidate_score and sym not in locked_symbols and side_val is not None:
                try:
                    is_crypto = "/" in sym
                    if is_crypto and side_val is Side.SELL:
                        self.logger.info(f"Skipping {sym}: crypto shorting is not supported")
                        continue

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
                        continue

                    qty_val = decision.quantity
                    if not is_crypto:
                        qty = int(qty_val)
                        if qty < 1:
                            self.logger.info(f"Skipping {sym}: whole share quantity ({qty}) < 1")
                            continue
                    else:
                        qty = round(qty_val, 4)
                        if qty <= 0:
                            self.logger.info(f"Skipping {sym}: fractional quantity ({qty}) <= 0")
                            continue

                    alpaca_side = OrderSide.BUY if side_val is Side.BUY else OrderSide.SELL
                    order_req = MarketOrderRequest(
                        symbol=sym,
                        qty=qty,
                        side=alpaca_side,
                        time_in_force=TimeInForce.DAY,
                        take_profit=TakeProfitRequest(limit_price=round(tp_price, 2)),
                        stop_loss=StopLossRequest(stop_price=round(sl_price, 2))
                    )
                    submitted_order = self.trading_client.submit_order(order_req)
                    open_risk += decision.capital_at_risk
                    orders_sent += 1
                    if bar_time is not None and processed_signals is not None:
                        processed_signals.add((norm_sym, bar_time))

                    self.logger.info(f">>> BRACKET ORDER SUBMITTED TO ALPACA: {alpaca_side.value} {qty} shares of {sym} @ ~${curr_price:.2f} (SL: ${sl_price:.2f}, TP: ${tp_price:.2f}, Score: {sc:.2f})")
                except Exception as ord_err:
                    self.logger.error(f"Failed to submit bracket order for {sym}: {ord_err}")

        return orders_sent


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
            self.crypto_client = CryptoHistoricalDataClient(api_key=api_key or "mock_key", secret_key=secret_key or "mock_secret")
        except Exception as exc:
            self.logger.error(f"Critical initialization error: {exc}")
            sys.exit(1)

        self.strategy = LiquiditySweepReversalStrategy(StrategyConfig())
        self.portfolio_manager = LivePortfolioManager(self.trading_client, self.logger)
        
        self.global_universe = [
            "AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "GOOGL", "META", "AMD", "NFLX", "INTC",
            "SPY", "QQQ", "IWM", "XLF", "XLE", "XLK", "ARKK", "JPM", "BAC", "DIS",
            "BTC/USD", "ETH/USD", "SOL/USD", "AVAX/USD",
            "GLD", "SLV", "USO", "UNG"
        ]
        if not self.settings.enable_crypto:
            self.global_universe = [sym for sym in self.global_universe if "/" not in sym]

        feed_val = self.settings.alpaca_data_feed.strip().lower()
        data_feed_enum = DataFeed.SIP if feed_val == "sip" else DataFeed.IEX
        if data_feed_enum == DataFeed.IEX:
            self.logger.warning("IEX feed carries only a fraction of market volume; relative volume confirmation is unreliable.")

        self.scanner = MarketScanner(self.global_universe, self.stock_client, self.crypto_client, self.logger, data_feed=data_feed_enum)

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
        account_cash, account_equity, _, open_positions, dashboard_positions, locked_symbols_raw = self.portfolio_manager.fetch_account_and_positions()
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
        signals = len(analyzed_candidates)

        analyzed_candidates.sort(key=lambda x: x[1], reverse=True)
        top_new = analyzed_candidates[:max(0, 5 - len(locked_symbols_raw))]

        final_candidates = list(locked_symbols_raw) + [c[0] for c in analyzed_candidates]
        self.latest_state["current_candidates"] = final_candidates[:5]
        if dashboard_stocks:
            self.latest_state["analyzed_stocks"] = dashboard_stocks

        minute = (now_utc.minute // 5) * 5
        bar_time = now_utc.replace(minute=minute, second=0, microsecond=0)

        orders_sent = self.portfolio_manager.submit_bracket_orders(
            top_new,
            locked_symbols_raw,
            account_equity,
            self.settings,
            bar_time=bar_time,
            processed_signals=self.processed_signals,
            locked_normalized=locked_normalized,
        )

        self.logger.info(f"[Cycle Summary] bar_time={bar_time.isoformat()}, evaluated={evaluated}, signals={signals}, orders_sent={orders_sent}")
        return {"evaluated": evaluated, "signals": signals, "orders_sent": orders_sent}

    def run(self) -> None:
        self.logger.info("==================================================")
        self.logger.info(" Headless Trading Engine Active (Event-Driven 5M) ")
        self.logger.info("==================================================")

        try:
            while True:
                now_utc = datetime.now(timezone.utc)
                try:
                    self.run_cycle(now_utc)
                except Exception as exc:
                    self.logger.error(f"Cycle execution error: {exc}")
                    time.sleep(10)
                    continue

                next_boundary = next_bar_boundary(now_utc)
                sleep_seconds = max(0.0, (next_boundary - datetime.now(timezone.utc)).total_seconds())
                self.logger.info(f"[Scheduler] Cycle completed. Sleeping for {sleep_seconds:.1f}s until next bar boundary {next_boundary.isoformat()}")
                time.sleep(sleep_seconds)

        except KeyboardInterrupt:
            print("\nMaster Blueprint Trading Engine stopped gracefully.")
