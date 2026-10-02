from __future__ import annotations

from uuid import uuid4

from money_mintter.brokers.base import Broker, BrokerError
from money_mintter.domain import Fill, Position, Quote, Side


class PaperBroker(Broker):
    """Paper broker supporting long and short positions with signed quantity."""

    def __init__(
        self,
        initial_cash: float,
        slippage_bps: float = 5.0,
        commission_bps: float = 2.0,
    ) -> None:
        if initial_cash <= 0:
            raise ValueError("initial cash must be positive")
        if min(slippage_bps, commission_bps) < 0:
            raise ValueError("execution costs cannot be negative")
        self._cash = initial_cash
        self._slippage_bps = slippage_bps
        self._commission_bps = commission_bps
        self._positions: dict[str, Position] = {}

    @property
    def available_cash(self) -> float:
        return self._cash

    def positions(self) -> dict[str, Position]:
        return {
            symbol: Position(
                p.symbol,
                p.quantity,
                p.avg_entry_price,
                p.entry_fees_total,
            )
            for symbol, p in self._positions.items()
        }

    def _execution_price(self, quote: Quote, side: Side) -> float:
        slip = self._slippage_bps / 10_000
        if side is Side.BUY:
            return quote.ask * (1 + slip)
        return quote.bid * (1 - slip)

    def submit_market_order(
        self,
        symbol: str,
        side: Side,
        quantity: float,
        quote: Quote,
    ) -> Fill:
        if quantity <= 0:
            raise BrokerError("quantity must be positive")
        if quote.symbol != symbol:
            raise BrokerError("quote symbol does not match order symbol")

        price = self._execution_price(quote, side)
        fee = price * quantity * self._commission_bps / 10_000
        order_id = str(uuid4())
        current = self._positions.get(symbol)

        if side is Side.BUY:
            if current is None:
                total_cost = price * quantity + fee
                if total_cost > self._cash + 1e-9:
                    raise BrokerError("insufficient cash")
                self._cash -= total_cost
                self._positions[symbol] = Position(symbol, quantity, price, fee)
                return Fill(order_id, symbol, side, quantity, price, fee, quote.timestamp)

            if current.quantity >= 0:
                total_cost = price * quantity + fee
                if total_cost > self._cash + 1e-9:
                    raise BrokerError("insufficient cash")
                self._cash -= total_cost
                total_qty = current.quantity + quantity
                current.avg_entry_price = (
                    current.avg_entry_price * current.quantity + price * quantity
                ) / total_qty
                current.quantity = total_qty
                current.entry_fees_total += fee
                return Fill(order_id, symbol, side, quantity, price, fee, quote.timestamp)

            close_qty = min(quantity, -current.quantity)
            entry_fee_alloc = current.entry_fees_total * (close_qty / -current.quantity)
            close_fee = fee * (close_qty / quantity)
            self._cash -= price * close_qty + close_fee
            realized = (
                (current.avg_entry_price - price) * close_qty
                - close_fee
                - entry_fee_alloc
            )
            current.quantity += close_qty
            current.entry_fees_total -= entry_fee_alloc
            remaining = quantity - close_qty

            if abs(current.quantity) <= 1e-9:
                del self._positions[symbol]

            if remaining <= 1e-9:
                return Fill(order_id, symbol, side, quantity, price, fee, quote.timestamp, realized)

            remaining_fee = fee * (remaining / quantity)
            total_cost = price * remaining + remaining_fee
            if total_cost > self._cash + 1e-9:
                raise BrokerError("insufficient cash to flip short to long")
            self._cash -= total_cost
            self._positions[symbol] = Position(symbol, remaining, price, remaining_fee)
            return Fill(order_id, symbol, side, quantity, price, fee, quote.timestamp, realized)

        if current is None:
            self._cash += price * quantity - fee
            self._positions[symbol] = Position(symbol, -quantity, price, fee)
            return Fill(order_id, symbol, side, quantity, price, fee, quote.timestamp)

        if current.quantity <= 0:
            self._cash += price * quantity - fee
            total_qty = -current.quantity + quantity
            current.avg_entry_price = (
                current.avg_entry_price * (-current.quantity) + price * quantity
            ) / total_qty
            current.quantity = -total_qty
            current.entry_fees_total += fee
            return Fill(order_id, symbol, side, quantity, price, fee, quote.timestamp)

        close_qty = min(quantity, current.quantity)
        entry_fee_alloc = current.entry_fees_total * (close_qty / current.quantity)
        close_fee = fee * (close_qty / quantity)
        self._cash += price * close_qty - close_fee
        realized = (
            (price - current.avg_entry_price) * close_qty
            - close_fee
            - entry_fee_alloc
        )
        current.quantity -= close_qty
        current.entry_fees_total -= entry_fee_alloc
        remaining = quantity - close_qty

        if current.quantity <= 1e-9:
            del self._positions[symbol]

        if remaining <= 1e-9:
            return Fill(order_id, symbol, side, quantity, price, fee, quote.timestamp, realized)

        remaining_fee = fee * (remaining / quantity)
        self._cash += price * remaining - remaining_fee
        self._positions[symbol] = Position(symbol, -remaining, price, remaining_fee)
        return Fill(order_id, symbol, side, quantity, price, fee, quote.timestamp, realized)

    def equity(self, quotes: dict[str, Quote]) -> float:
        value = self._cash
        for symbol, position in self._positions.items():
            quote = quotes.get(symbol)
            if quote is None:
                raise BrokerError(f"missing quote for {symbol}")
            if position.quantity > 0:
                value += position.quantity * quote.bid
            else:
                value += position.quantity * quote.ask
        return value
