from __future__ import annotations

import csv
from datetime import datetime

from money_mintter.strategy import Candle


class CsvDataError(ValueError):
    """Invalid historical candle CSV."""


class CandleCsvLoader:
    """Load normalized OHLCV CSV files for offline replay and research."""

    REQUIRED = ("timestamp", "open", "high", "low", "close", "volume")

    def load(self, path: str, symbol: str) -> list[Candle]:
        with open(path, "r", newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None or any(field not in reader.fieldnames for field in self.REQUIRED):
                raise CsvDataError(f"CSV must contain {', '.join(self.REQUIRED)}")

            candles: list[Candle] = []
            for row_number, row in enumerate(reader, start=2):
                try:
                    timestamp = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
                    candle = Candle(
                        symbol=symbol,
                        timestamp=timestamp,
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=float(row["volume"]),
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    raise CsvDataError(f"invalid candle at CSV row {row_number}") from exc
                candles.append(candle)

        if not candles:
            raise CsvDataError("CSV contains no candles")

        timestamps = [candle.timestamp for candle in candles]
        if timestamps != sorted(timestamps):
            raise CsvDataError("candles must be ordered by timestamp")
        return candles
