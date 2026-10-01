from __future__ import annotations

from dataclasses import dataclass

from money_mintter.brokers.base import Broker, BrokerError
from money_mintter.core.risk import RiskEngine
from money_mintter.domain import Fill, ManagedTrade, Quote, Side, TradeSignal, WalletLedger


@dataclass(frozen=True, slots=True)
class TradeEvent:
    kind: str
    symbol: str
    message: str
    fill: Fill | None = None


class TradeManager:
    def __init__(self, broker: Broker, risk: RiskEngine, wallet: WalletLedger) -> None:
        self.broker = broker
        self.risk = risk
        self.wallet = wallet
        self.open_trades: dict[str, ManagedTrade] = {}

    @property
    def open_risk(self) -> float:
        total = 0.0
        for trade in self.open_trades.values():
            if trade.side is Side.BUY:
                total += max(0.0, trade.entry_price - trade.stop_price) * trade.quantity
        return total

    def open_trade(self, signal: TradeSignal, quote: Quote) -> TradeEvent:
        if signal.side is not Side.BUY:
            return TradeEvent("REJECT", signal.symbol, "short signals are disabled in the first vertical slice")
        if signal.symbol in self.open_trades:
            return TradeEvent("REJECT", signal.symbol, "trade already open")

        decision = self.risk.size_long(
            available_capital=min(self.wallet.trading_wallet, self.broker.available_cash),
            entry=quote.ask,
            stop=signal.stop_price,
            open_risk=self.open_risk,
        )
        if not decision.approved:
            return TradeEvent("REJECT", signal.symbol, decision.reason)

        try:
            fill = self.broker.submit_market_order(signal.symbol, Side.BUY, decision.quantity, quote)
        except BrokerError as exc:
            return TradeEvent("REJECT", signal.symbol, str(exc))

        self.open_trades[signal.symbol] = ManagedTrade(
            symbol=signal.symbol,
            side=signal.side,
            quantity=fill.quantity,
            entry_price=fill.price,
            stop_price=signal.stop_price,
            target_price=signal.target_price,
            opened_at=fill.timestamp,
            entry_fill_id=fill.order_id,
        )
        return TradeEvent("OPEN", signal.symbol, "trade opened", fill)

    def process_quote(self, quote: Quote) -> TradeEvent | None:
        trade = self.open_trades.get(quote.symbol)
        if trade is None:
            return None

        reason: str | None = None
        if quote.last <= trade.stop_price:
            reason = "stop"
        elif quote.last >= trade.target_price:
            reason = "target"
        if reason is None:
            return None

        try:
            fill = self.broker.submit_market_order(
                trade.symbol, Side.SELL, trade.quantity, quote
            )
        except BrokerError as exc:
            return TradeEvent("ERROR", quote.symbol, str(exc))

        pnl = fill.realized_pnl or 0.0
        self.wallet.apply_realized_pnl(pnl)
        trade.exit_fill_id = fill.order_id
        trade.closed_at = fill.timestamp
        trade.realized_pnl = pnl
        trade.close_reason = reason
        del self.open_trades[quote.symbol]
        return TradeEvent("CLOSE", quote.symbol, reason, fill)
