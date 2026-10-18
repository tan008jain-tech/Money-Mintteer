from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from dotenv import load_dotenv

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, TakeProfitRequest, StopLossRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient, CryptoHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import DataFeed

from money_mintter.config import Settings
from money_mintter.core.risk import RiskEngine
from money_mintter.domain import Side
from money_mintter.strategy import Candle, LiquiditySweepReversalStrategy, MultiTimeframeSeries, StrategyConfig, Timeframe


class MarketScanner:
    """Object-oriented Market Screener modeled after Freqtrade DataProvider / Lumibot Scanners.
    Responsible for global universe ingestion, session filtering, and candidate scoring.
    """
    def __init__(self, global_universe: list[str], stock_client: StockHistoricalDataClient, crypto_client: CryptoHistoricalDataClient, logger: logging.Logger) -> None:
        self.global_universe = global_universe
        self.stock_client = stock_client
        self.crypto_client = crypto_client
        self.logger = logger
        self.latest_snapshots: dict[str, MultiTimeframeSnapshot] = {}

    @staticmethod
    def is_session_open(symbol: str, now_utc: datetime) -> tuple[bool, str]:
        sym_upper = symbol.upper()
        if sym_upper.endswith(('-USD', '/USD', '-USDT', '/USDT')) or any(c in sym_upper for c in ['BTC', 'ETH', 'SOL', 'AVAX']):
            return True, "Crypto 🟢 (24/7)"

        if sym_upper.endswith('=F') or sym_upper in ['GLD', 'SLV', 'USO', 'UNG']:
            weekday = now_utc.weekday()
            hour_utc = now_utc.hour + now_utc.minute / 60.0
            if weekday == 5:
                return False, "Commodity 🔴 (Saturday Closed)"
            if weekday == 6 and hour_utc < 18.0:
                return False, "Commodity 🔴 (Sunday Pre-Open)"
            if weekday == 4 and hour_utc >= 17.0:
                return False, "Commodity 🔴 (Friday Closed)"
            if 17.0 <= hour_utc < 18.0 and weekday < 4:
                return False, "Commodity 🔴 (Daily Maintenance Break)"
            return True, "Commodities 🟢 (Active Futures/ETF)"

        hour_utc = now_utc.hour + now_utc.minute / 60.0
        if now_utc.weekday() >= 5:
            return False, "US Equities 🔴 (Weekend Closed)"
        
        if 14.5 <= hour_utc <= 21.0:
            return True, "US Equities 🟢 (Core Regular Hours 9:30-16:00 EST)"
        
        if 8.5 <= hour_utc < 14.5:
            return False, "US Equities ⏳ (Pre-Market - Ignored per Blueprint)"
        if 21.0 < hour_utc <= 25.0:
            return False, "US Equities ⏳ (After-Hours - Ignored per Blueprint)"

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

        self.logger.info(f"Scanning global asset pool ({len(self.global_universe)} assets) with core trading hours filtration...")

        for symbol in self.global_universe:
            clean_symbol = symbol.upper().strip()
            if "-" in clean_symbol and "/" not in clean_symbol and ("USD" in clean_symbol or "USDT" in clean_symbol):
                clean_symbol = clean_symbol.replace("-", "/")

            open_status, session_msg = self.is_session_open(clean_symbol, now_utc)
            if not open_status:
                self.logger.info(f"Skipping {clean_symbol}: {session_msg}")
                continue

            if clean_symbol in locked_symbols:
                self.logger.info(f"Retaining locked active trade slot for {clean_symbol} (Module 3.2 Slot Freezing)")
                continue

            try:
                end_dt = now_utc
                start_dt = end_dt - timedelta(days=5)

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
                        feed=DataFeed.IEX
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

                latest_bar = candles[-1]
                vwap = self.calculate_vwap(candles)
                vwap_dev = ((latest_bar.close - vwap) / vwap) * 100.0 if vwap > 0 else 0.0

                if clean_symbol in locked_symbols and abs(vwap_dev) >= 2.0:
                    self.logger.warning(f"EMERGENCY VWAP BREAKDOWN: {clean_symbol} price deviates {vwap_dev:+.2f}% from VWAP (${vwap:.2f})!")

                series = MultiTimeframeSeries.from_5m(
                    list(candles),
                    entry_timeframe=Timeframe.FIVE_MIN,
                    timezone_name="America/New_York",
                    spread_bps=2.0,
                )
                snapshot = series.snapshot_at(now_utc)
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
        try:
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
        except Exception as e:
            self.logger.error(f"Error querying open positions: {e}")

        return account_cash, account_equity, buying_power, open_positions, dashboard_positions, locked_symbols

    def submit_bracket_orders(self, top_new: list[tuple[str, float, Side | None, str, float, float, float]], locked_symbols: set[str], account_equity: float, settings: Settings) -> None:
        active_untraded_slots = max(1, 5 - len(locked_symbols))
        risk_engine = RiskEngine(
            risk_per_trade_pct=settings.risk_per_trade_pct,
            max_position_pct=settings.max_position_pct,
            max_portfolio_risk_pct=settings.max_portfolio_risk_pct,
        )
        open_risk = len(locked_symbols) * settings.risk_per_trade_pct * account_equity

        for sym, sc, side_val, reason, curr_price, sl_price, tp_price in top_new[:active_untraded_slots]:
            if sc >= settings.min_candidate_score and sym not in locked_symbols and side_val is not None:
                try:
                    is_crypto = "/" in sym
                    if side_val is Side.BUY:
                        decision = risk_engine.size_long(
                            available_capital=account_equity,
                            entry=curr_price,
                            stop=sl_price,
                            open_risk=open_risk,
                            active_candidates_count=max(1, len(top_new)),
                            market_open=True,
                        )
                    else:
                        decision = risk_engine.size_short(
                            available_capital=account_equity,
                            entry=curr_price,
                            stop=sl_price,
                            open_risk=open_risk,
                            active_candidates_count=max(1, len(top_new)),
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
                    self.logger.info(f">>> BRACKET ORDER SUBMITTED TO ALPACA: {alpaca_side.value} {qty} shares of {sym} @ ~${curr_price:.2f} (SL: ${sl_price:.2f}, TP: ${tp_price:.2f}, Score: {sc:.2f})")
                except Exception as ord_err:
                    self.logger.error(f"Failed to submit bracket order for {sym}: {ord_err}")


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
        self.scanner = MarketScanner(self.global_universe, self.stock_client, self.crypto_client, self.logger)

        self.latest_state = {
            "cash": 100000.0,
            "equity": 100000.0,
            "analyzed_stocks": [],
            "open_positions": [],
            "trade_history": [],
            "current_candidates": [],
        }

        self.last_scan_time: datetime | None = None
        self.rotation_interval_seconds = 1800  # 30-minute rotation clock

    def setup_logging(self) -> logging.Logger:
        log_dir = Path("logs") if 'Path' in globals() else __import__('pathlib').Path("logs")
        log_dir.mkdir(exist_ok=True)
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

    def run(self) -> None:
        self.logger.info("==================================================")
        self.logger.info(" Headless Trading Engine Active (Dashboard Removed) ")
        self.logger.info("==================================================")

        try:
            while True:
                now_utc = datetime.now(timezone.utc)

                # 1. Query Account & Positions
                try:
                    account_cash, account_equity, _, open_positions, dashboard_positions, locked_symbols = self.portfolio_manager.fetch_account_and_positions()
                except Exception as exc:
                    self.logger.error(f"Failed to fetch account and positions: {exc}")
                    time.sleep(2)
                    continue

                self.latest_state["cash"] = account_cash
                self.latest_state["equity"] = account_equity
                self.latest_state["open_positions"] = dashboard_positions

                if not hasattr(self, '_previously_locked_symbols'):
                    self._previously_locked_symbols = set()

                should_scan = False
                closed_externally = [sym for sym in self._previously_locked_symbols if sym not in locked_symbols]
                if closed_externally:
                    self.logger.info(f"[Reconciliation] Detected external closure/liquidation on Alpaca for symbols: {closed_externally}. Forcing immediate candidate refresh & lock release.")
                    should_scan = True

                self._previously_locked_symbols = set(locked_symbols)

                # 2. 30-Minute Rotation Clock Check
                if self.last_scan_time is None or (now_utc - self.last_scan_time).total_seconds() >= self.rotation_interval_seconds:
                    should_scan = True
                    self.last_scan_time = now_utc
                    self.logger.info("[Module 1.1] 30-Minute Rotation Clock Triggered: Executing Full Global Scan.")

                if should_scan or not self.latest_state["current_candidates"]:
                    analyzed_candidates, dashboard_stocks = self.scanner.scan_universe(now_utc, locked_symbols, self.strategy)

                    analyzed_candidates.sort(key=lambda x: x[1], reverse=True)
                    top_new = analyzed_candidates[:max(0, 5 - len(locked_symbols))]

                    final_candidates = list(locked_symbols) + [c[0] for c in top_new]
                    self.latest_state["current_candidates"] = final_candidates[:5]
                    if dashboard_stocks:
                        self.latest_state["analyzed_stocks"] = dashboard_stocks
                    self.logger.info(f"[Module 1.4] Rotation Complete. Active Candidates Set: {self.latest_state['current_candidates']}")

                    # Submit Bracket Orders
                    self.portfolio_manager.submit_bracket_orders(top_new, locked_symbols, account_equity, self.settings)

                # 3. Print Console Status
                active_untraded_slots = max(1, 5 - len(locked_symbols))
                slot_capital_allocation = account_equity / active_untraded_slots
                time_to_next_scan = max(0, self.rotation_interval_seconds - int((now_utc - (self.last_scan_time or now_utc)).total_seconds()))

                print("\n==========================================================================")
                print(f" MASTER BLUEPRINT SYSTEM (OO) | UTC: {now_utc.strftime('%Y-%m-%d %H:%M:%S')}")
                print("==========================================================================")
                print(f" [Global Scanner] Active Open Slots: {len(self.latest_state['current_candidates'])}/5 | Locked Trades: {len(locked_symbols)}")
                print(f" [Portfolio] Dynamic Capital Allocation per Untraded Slot: ${slot_capital_allocation:,.2f}")

                print(f"\n [Scanner Loop] Current Watchlist Candidates (Next 30M Rotation in {time_to_next_scan}s):")
                if not self.latest_state["current_candidates"]:
                    print("   (Awaiting open market core session hours / scanning active venues...)")
                else:
                    for sym in self.latest_state["current_candidates"]:
                        is_locked = " [LOCKED - ACTIVE TRADE]" if sym in locked_symbols else ""
                        print(f"   • {sym:<10}{is_locked}")

                print(f"\n [Position Tracker Loop] Independent 2s / Loop Monitoring & Alpaca Positions:")
                if not open_positions:
                    print("   (No active positions currently held on Alpaca)")
                else:
                    for pos in open_positions:
                        print(f"   • {pos['symbol']:<6} {pos['side']:<4} | Qty: {pos['quantity']} | Entry: ${pos['entry_price']:.2f} | Curr: ${pos['current_price']:.2f} | PnL: ${pos['unrealized_pnl']:+.2f}")

                print("\n--------------------------------------------------------------------------")
                print(f" Status: RUNNING (OO Bracket Orders Active) | Next Scan in {time_to_next_scan}s (Press Ctrl+C to exit)")
                print("==========================================================================\n")

                time.sleep(2)

        except KeyboardInterrupt:
            print("\nMaster Blueprint Trading Engine stopped gracefully.")
