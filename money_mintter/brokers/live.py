from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from money_mintter.brokers.base import Broker, BrokerError
from money_mintter.domain import Fill, Position, Quote, Side
from money_mintter.execution.guard import ExecutionGuard, ExecutionBlocked


@dataclass(frozen=True, slots=True)
class GatewayPosition:
    symbol: str
    quantity: float
    average_price: float


@dataclass(frozen=True, slots=True)
class GatewayFill:
    order_id: str
    symbol: str
    side: Side
    quantity: float
    price: float
    fee: float
    timestamp: object
    realized_pnl: float | None = None


class BrokerGateway(Protocol):
    def available_cash(self) -> float:
        ...

    def positions(self) -> dict[str, GatewayPosition]:
        ...

    def market_order(
        self,
        *,
        symbol: str,
        side: Side,
        quantity: float,
        quote: Quote,
    ) -> GatewayFill:
        ...


class LiveBrokerAdapter(Broker):
    """Broker-agnostic live execution adapter.

    No provider credentials or network assumptions are embedded here. A broker
    implementation only needs to satisfy BrokerGateway. Real order placement is
    impossible until the gateway is explicitly wired to a user's regulated
    account and the ExecutionGuard is armed.
    """

    def __init__(self, gateway: BrokerGateway, execution_guard: ExecutionGuard) -> None:
        self.gateway = gateway
        self.execution_guard = execution_guard

    @property
    def available_cash(self) -> float:
        return float(self.gateway.available_cash())

    def positions(self) -> dict[str, Position]:
        return {
            symbol: Position(
                symbol,
                position.quantity,
                position.average_price,
            )
            for symbol, position in self.gateway.positions().items()
        }

    def submit_market_order(
        self,
        symbol: str,
        side: Side,
        quantity: float,
        quote: Quote,
    ) -> Fill:
        try:
            self.execution_guard.authorize(quote.timestamp, quantity)
            gateway_fill = self.gateway.market_order(
                symbol=symbol,
                side=side,
                quantity=quantity,
                quote=quote,
            )
        except ExecutionBlocked as exc:
            raise BrokerError(str(exc)) from exc
        except Exception as exc:
            raise BrokerError(f"live gateway order failed: {exc}") from exc

        return Fill(
            gateway_fill.order_id,
            gateway_fill.symbol,
            gateway_fill.side,
            gateway_fill.quantity,
            gateway_fill.price,
            gateway_fill.fee,
            gateway_fill.timestamp,
            gateway_fill.realized_pnl,
        )

    def equity(self, quotes: dict[str, Quote]) -> float:
        value = self.available_cash
        positions = self.positions()
        for symbol, position in positions.items():
            quote = quotes.get(symbol)
            if quote is None:
                raise BrokerError(f"missing quote for {symbol}")
            value += position.quantity * quote.bid
        return value
