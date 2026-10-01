from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Iterator

from money_mintter.strategy import Candle


class DataFeedError(RuntimeError):
    """Expected market-data feed failure."""


@dataclass(frozen=True, slots=True)
class CandleEnvelope:
    candle: Candle
    source: str


class ReplayCandleFeed:
    """Deterministic local replay feed used before a live provider is connected."""

    def __init__(self, candles: dict[str, Iterable[Candle]]) -> None:
        self._streams = {
            symbol: tuple(stream)
            for symbol, stream in candles.items()
        }
        self._indices = {symbol: 0 for symbol in self._streams}

    def symbols(self) -> tuple[str, ...]:
        return tuple(sorted(self._streams))

    def next(self, symbol: str) -> CandleEnvelope | None:
        stream = self._streams.get(symbol)
        if not stream:
            raise DataFeedError(f"unknown replay symbol: {symbol}")

        index = self._indices[symbol]
        if index >= len(stream):
            return None
        candle = stream[index]
        self._indices[symbol] += 1
        return CandleEnvelope(candle, "replay")

    def iter_symbol(self, symbol: str) -> Iterator[CandleEnvelope]:
        while True:
            envelope = self.next(symbol)
            if envelope is None:
                return
            yield envelope
