from __future__ import annotations

from datetime import datetime
from typing import Protocol

from money_mintter.domain import Candidate, Quote


class Screener(Protocol):
    """External coarse screener interface. The production implementation may be remote."""

    def candidates(self, now_utc: datetime) -> list[Candidate]:
        ...


class MarketDataFeed(Protocol):
    """Streaming/polling market-data boundary for future live adapters."""

    def latest_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        ...


class StaticScreener:
    """Deterministic screener used by tests and the initial demo."""

    def __init__(self, candidates: list[Candidate]) -> None:
        self._candidates = tuple(candidates)

    def candidates(self, now_utc: datetime) -> list[Candidate]:
        return [candidate for candidate in self._candidates if candidate.timestamp <= now_utc]


class StaticMarketData:
    """Deterministic market-data source used by tests and local prototyping."""

    def __init__(self, quotes: dict[str, Quote]) -> None:
        self._quotes = dict(quotes)

    def latest_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        return {symbol: self._quotes[symbol] for symbol in symbols if symbol in self._quotes}
