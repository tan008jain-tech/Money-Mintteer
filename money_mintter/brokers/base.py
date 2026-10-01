from __future__ import annotations

from abc import ABC, abstractmethod

from money_mintter.domain import Fill, Position, Quote, Side


class BrokerError(RuntimeError):
    """Expected broker/execution failure."""


class Broker(ABC):
    @property
    @abstractmethod
    def available_cash(self) -> float:
        raise NotImplementedError

    @abstractmethod
    def positions(self) -> dict[str, Position]:
        raise NotImplementedError

    @abstractmethod
    def submit_market_order(self, symbol: str, side: Side, quantity: float, quote: Quote) -> Fill:
        raise NotImplementedError

    @abstractmethod
    def equity(self, quotes: dict[str, Quote]) -> float:
        raise NotImplementedError
