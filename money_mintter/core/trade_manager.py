from __future__ import annotations

from dataclasses import dataclass

from money_mintter.brokers.base import Broker, BrokerError
from money_mintter.core.risk import RiskEngine
from money_mintter.domain import Fill, ManagedTrade, Quote, Side, TradeSignal, WalletLedger
from money_mintter.strategy.candles import Candle


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
            else:
                total += max(0.0, trade.stop_price - trade.entry_price) * trade.quantity
        return total

    def open_trade(self, signal: TradeSignal, quote: Quote, active_candidates_count: int = 5, market_open: bool = True) -> TradeEvent:
        if signal.symbol in self.open_trades:
            return TradeEvent("REJECT", signal.symbol, "trade already open")

        available_capital = min(self.wallet.trading_wallet, self.broker.available_cash)
        if signal.side is Side.BUY:
            decision = self.risk.size_long(
                available_capital, quote.ask, signal.stop_price, self.open_risk, active_candidates_count=active_candidates_count, market_open=market_open
            )
        else:
            decision = self.risk.size_short(
                available_capital, quote.bid, signal.stop_price, self.open_risk, active_candidates_count=active_candidates_count, market_open=market_open
            )

        if not decision.approved:
            return TradeEvent("REJECT", signal.symbol, decision.reason)

        try:
            fill = self.broker.submit_market_order(
                signal.symbol, signal.side, decision.quantity, quote
            )
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

        if trade.side is Side.BUY:
            if quote.last <= trade.stop_price:
                reason, exit_side = "stop", Side.SELL
            elif quote.last >= trade.target_price:
                reason, exit_side = "target", Side.SELL
            else:
                return None
        else:
            if quote.last >= trade.stop_price:
                reason, exit_side = "stop", Side.BUY
            elif quote.last <= trade.target_price:
                reason, exit_side = "target", Side.BUY
            else:
                return None

        try:
            fill = self.broker.submit_market_order(
                trade.symbol, exit_side, trade.quantity, quote
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

    def close_trade(self, symbol: str, quote: Quote, reason: str = "analysis_invalidation") -> TradeEvent | None:
        trade = self.open_trades.get(symbol)
        if trade is None:
            return None

        exit_side = Side.SELL if trade.side is Side.BUY else Side.BUY
        try:
            fill = self.broker.submit_market_order(
                trade.symbol, exit_side, trade.quantity, quote
            )
        except BrokerError as exc:
            return TradeEvent("ERROR", symbol, str(exc))

        pnl = fill.realized_pnl or 0.0
        self.wallet.apply_realized_pnl(pnl)
        trade.exit_fill_id = fill.order_id
        trade.closed_at = fill.timestamp
        trade.realized_pnl = pnl
        trade.close_reason = reason
        del self.open_trades[symbol]
        return TradeEvent("CLOSE", symbol, reason, fill)

    def process_bar(self, candle: Candle, quote: Quote) -> TradeEvent | None:
        """Discretionary candle-aware trade management (human-like trader logic).

        Evaluates candle close, body fraction, and volume alongside quote stops/targets.
        If price retraces near entry and a candle closes decisively against the thesis,
        it cuts the trade early to prevent major drawdown. If it's noise/doji (confusion),
        it waits for the next candle.
        """
        trade = self.open_trades.get(candle.symbol)
        if trade is None:
            return None

        # First check standard stop and target via quote/last
        event = self.process_quote(quote)
        if event is not None:
            return event

        # Candle-aware discretionary evaluation
        if trade.side is Side.BUY:
            if candle.close < trade.entry_price and candle.bearish and candle.body_fraction > 0.55:
                reason, exit_side = "thesis_invalidation", Side.SELL
            else:
                return None
        else:
            if candle.close > trade.entry_price and candle.bullish and candle.body_fraction > 0.55:
                reason, exit_side = "thesis_invalidation", Side.BUY
            else:
                return None

        try:
            fill = self.broker.submit_market_order(
                trade.symbol, exit_side, trade.quantity, quote
            )
        except BrokerError as exc:
            return TradeEvent("ERROR", candle.symbol, str(exc))

        pnl = fill.realized_pnl or 0.0
        self.wallet.apply_realized_pnl(pnl)
        trade.exit_fill_id = fill.order_id
        trade.closed_at = fill.timestamp
        trade.realized_pnl = pnl
        trade.close_reason = reason
        del self.open_trades[candle.symbol]
        return TradeEvent("CLOSE", candle.symbol, reason, fill)
