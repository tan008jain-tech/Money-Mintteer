from __future__ import annotations

import time
from money_mintter.dashboard.app import DashboardServer, DashboardSnapshot
from money_mintter.domain import WalletLedger

def main() -> None:
    wallet = WalletLedger(10000.0, profit_reserve_pct=0.20)
    
    def provider() -> DashboardSnapshot:
        return DashboardSnapshot(
            system_action="RUN",
            investing_wallet=wallet.trading_wallet,
            profit_wallet=wallet.profit_reserve,
            broker_cash=10000.0,
            analyzed_stocks=(
                {"symbol": "AAPL", "market": "NASDAQ", "liquidity_score": 0.95, "activity_score": 0.90, "signal_quality_score": 0.92, "score": 0.93},
                {"symbol": "MSFT", "market": "NASDAQ", "liquidity_score": 0.94, "activity_score": 0.88, "signal_quality_score": 0.90, "score": 0.91},
                {"symbol": "NVDA", "market": "NASDAQ", "liquidity_score": 0.96, "activity_score": 0.95, "signal_quality_score": 0.94, "score": 0.95},
                {"symbol": "TSLA", "market": "NASDAQ", "liquidity_score": 0.91, "activity_score": 0.92, "signal_quality_score": 0.88, "score": 0.90},
                {"symbol": "GOOGL", "market": "NASDAQ", "liquidity_score": 0.93, "activity_score": 0.89, "signal_quality_score": 0.91, "score": 0.91},
            ),
            open_positions=(),
            trade_history=(
                {"symbol": "AAPL", "side": "BUY", "quantity": 10, "entry_price": 180.0, "exit_price": 183.5, "realized_pnl": 35.0, "close_reason": "target"},
            ),
            health=(
                {"component": "market_data", "status": "HEALTHY", "message": "OK"},
                {"component": "broker", "status": "HEALTHY", "message": "OK"},
            ),
        )

    wallet_actions = {
        "deposit_investing": wallet.deposit_investing,
        "withdraw_investing": wallet.withdraw_investing,
        "profit_withdraw": wallet.withdraw_profit,
    }

    server = DashboardServer(provider, wallet_actions)
    server.start()
    print("========================================")
    print(" Money Mintter Dashboard is Running!   ")
    print(" Open your browser and go to:          ")
    print(" http://127.0.0.1:8765                 ")
    print("========================================")
    print("Press Ctrl+C in this terminal to stop.")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.stop()
        print("Dashboard stopped.")

if __name__ == "__main__":
    main()
