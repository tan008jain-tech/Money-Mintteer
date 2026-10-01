from __future__ import annotations

from datetime import datetime, timezone

from money_mintter.brokers.paper import PaperBroker
from money_mintter.config import Settings
from money_mintter.core.candidates import CandidateRanker
from money_mintter.core.engine import MoneyMintterEngine
from money_mintter.core.risk import RiskEngine
from money_mintter.core.sessions import SessionEngine
from money_mintter.core.trade_manager import TradeManager
from money_mintter.domain import Candidate, Quote, Side, TradeSignal, WalletLedger


def run_demo() -> None:
    settings = Settings()
    now = datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)

    broker = PaperBroker(
        settings.initial_capital,
        settings.paper_slippage_bps,
        settings.paper_commission_bps,
    )
    wallet = WalletLedger(settings.initial_capital, profit_reserve_pct=settings.profit_reserve_pct)
    risk = RiskEngine(
        settings.risk_per_trade_pct,
        settings.max_position_pct,
        settings.max_portfolio_risk_pct,
    )
    manager = TradeManager(broker, risk, wallet)
    engine = MoneyMintterEngine(
        broker,
        manager,
        SessionEngine.default(),
        CandidateRanker(settings.min_candidate_score, settings.max_deep_candidates),
    )

    candidates = [
        Candidate("DEMO", "NYSE", 0.95, 0.88, 0.92, 0.91, now),
        Candidate("NOISE", "NYSE", 0.72, 0.60, 0.58, 0.50, now),
    ]
    quotes = {
        "DEMO": Quote("DEMO", 99.95, 100.05, 100.00, now),
    }
    signal = TradeSignal(
        "DEMO", Side.BUY, 100.05, 98.05, 108.05, 0.91, now, "demo validated setup"
    )

    opened = engine.cycle(now, candidates, quotes, [signal])
    print("Open markets:", opened.open_markets)
    print("Deep candidates:", [(x.candidate.symbol, round(x.score, 3)) for x in opened.deep_candidates])
    print("Events:", [(x.kind, x.symbol, x.message) for x in opened.trade_events])

    exit_time = now.replace(second=30)
    exit_quote = Quote("DEMO", 108.10, 108.20, 108.15, exit_time)
    closed = engine.cycle(exit_time, candidates, {"DEMO": exit_quote}, [])
    print("Close events:", [(x.kind, x.symbol, x.message, round(x.fill.realized_pnl or 0, 2)) for x in closed.trade_events])
    print("Trading Wallet:", round(wallet.trading_wallet, 2))
    print("Profit Reserve:", round(wallet.profit_reserve, 2))
    print("Broker cash:", round(broker.available_cash, 2))


if __name__ == "__main__":
    run_demo()
