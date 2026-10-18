from __future__ import annotations

import time
from datetime import datetime, timezone

from money_mintter.brokers.paper import PaperBroker
from money_mintter.core.candidates import CandidateRanker
from money_mintter.core.engine import MoneyMintterEngine
from money_mintter.core.risk import RiskEngine
from money_mintter.core.sessions import SessionEngine
from money_mintter.core.trade_manager import TradeManager
from money_mintter.dashboard.app import DashboardServer, DashboardSnapshot
from money_mintter.domain import Candidate, Quote, Side, TradeSignal, WalletLedger
from threading import Thread


def main() -> None:
    print("Initializing Money Mintter Paper Trading Demo...")
    
    initial_capital = 10000.0
    broker = PaperBroker(initial_capital, slippage_bps=5.0, commission_bps=2.0)
    wallet = WalletLedger(initial_capital)
    risk = RiskEngine(risk_per_trade_pct=0.005, max_position_pct=0.20, max_portfolio_risk_pct=0.02)
    manager = TradeManager(broker, risk, wallet)
    engine = MoneyMintterEngine(
        broker,
        manager,
        SessionEngine.default(),
        CandidateRanker(min_score=0.85, max_candidates=5),
    )

    now = datetime.now(timezone.utc)
    
    candidates = [
        Candidate("AAPL", "NASDAQ", 0.95, 0.70, 0.90, 0.92, now),
        Candidate("MSFT", "NASDAQ", 0.94, 0.68, 0.89, 0.90, now),
        Candidate("NVDA", "NASDAQ", 0.97, 0.75, 0.95, 0.96, now),
        Candidate("TSLA", "NASDAQ", 0.91, 0.80, 0.85, 0.88, now),
        Candidate("GOOGL", "NASDAQ", 0.93, 0.65, 0.88, 0.91, now),
    ]

    closed_trades_history: list[dict[str, object]] = []

    # Background simulation thread
    def simulation_loop():
        time.sleep(4)
        print("\n[Simulation] Injecting high-conviction trade signal for AAPL...")
        t_sig = datetime.now(timezone.utc)
        signal = TradeSignal(
            symbol="AAPL",
            side=Side.BUY,
            entry_price=180.05,
            stop_price=178.20,
            target_price=185.00,
            confidence=0.93,
            timestamp=t_sig,
            reason="elite liquidity sweep reversal (>85% standard)",
        )
        q = Quote("AAPL", 180.00, 180.10, 180.05, t_sig)
        engine.cycle(t_sig, candidates, {"AAPL": q}, [signal])
        print("[Simulation] AAPL trade opened successfully! Check dashboard open positions.")

        time.sleep(8)
        t_part = datetime.now(timezone.utc)
        print("[Simulation] Simulating price move to 1.0R (triggering 50% partial exit & breakeven stop)...")
        q_part = Quote("AAPL", 181.90, 182.00, 181.95, t_part)
        engine.cycle(t_part, candidates, {"AAPL": q_part}, [])

        time.sleep(8)
        t_target = datetime.now(timezone.utc)
        print("[Simulation] Simulating price reaching target (closing remaining position in profit)...")
        q_target = Quote("AAPL", 185.00, 185.10, 185.05, t_target)
        
        # Capture closed trades before engine clears them
        for sym, trade in list(manager.open_trades.items()):
            if sym == "AAPL":
                closed_trades_history.append({
                    "symbol": trade.symbol,
                    "side": trade.side,
                    "quantity": trade.quantity,
                    "entry_price": trade.entry_price,
                    "exit_price": 185.00,
                    "realized_pnl": 49.50, # estimated realized pnl
                    "close_reason": "target",
                })

        engine.cycle(t_target, candidates, {"AAPL": q_target}, [])
        print("[Simulation] Trade closed at target! Profit allocated to investing & profit wallets.\n")

    Thread(target=simulation_loop, daemon=True).start()

    def snapshot_provider() -> DashboardSnapshot:
        open_pos = [
            {
                "symbol": sym,
                "side": "BUY" if p.quantity > 0 else "SELL",
                "quantity": abs(p.quantity),
                "entry_price": p.avg_entry_price,
                "stop_price": manager.open_trades[sym].stop_price if sym in manager.open_trades else p.avg_entry_price * 0.98,
                "target_price": manager.open_trades[sym].target_price if sym in manager.open_trades else p.avg_entry_price * 1.05,
            }
            for sym, p in broker.positions().items()
        ]

        analyzed = tuple(
            {
                "symbol": c.symbol,
                "market": c.market,
                "current_price": 180.00,
                "score": 0.92,
            }
            for c in candidates
        )

        return DashboardSnapshot(
            system_action="PAPER TRADING ACTIVE (>85% ELITE)",
            wallet=wallet.wallet,
            broker_cash=broker.available_cash,
            analyzed_stocks=analyzed,
            open_positions=tuple(open_pos),
            trade_history=tuple(closed_trades_history),
            health=(
                {"component": "market_data", "status": "HEALTHY", "message": "Paper stream connected"},
                {"component": "paper_broker", "status": "HEALTHY", "message": "Paper execution active"},
                {"component": "strategy_engine", "status": "HEALTHY", "message": "Elite rules armed"},
            ),
        )

    server = DashboardServer(snapshot_provider, port=8765)
    server.start()

    print("==================================================")
    print(" Money Mintter Paper Trading Demo is Live!       ")
    print(" Open your browser and navigate to:              ")
    print(" http://127.0.0.1:8765                             ")
    print("==================================================")
    print("A test trade will automatically open in 4 seconds.")
    print("Press Ctrl+C in this terminal to stop.")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.stop()
        print("Paper demo stopped.")


if __name__ == "__main__":
    main()
