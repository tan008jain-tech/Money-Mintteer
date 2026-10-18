from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from money_mintter.strategy import Candle


class FmpApiError(RuntimeError):
    """FMP request or response failure."""


class FmpClient:
    """Small dependency-free client for FMP's current Stable API."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://financialmodelingprep.com/stable",
        timeout_seconds: float = 10.0,
    ) -> None:
        if not api_key.strip():
            raise ValueError("FMP API key cannot be empty")
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("FMP base URL must be HTTP(S)")
        if timeout_seconds <= 0:
            raise ValueError("timeout must be positive")
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def get_json(
        self,
        endpoint: str,
        params: dict[str, str] | None = None,
    ) -> Any:
        path = endpoint.strip("/")
        query = dict(params or {})
        url = f"{self.base_url}/{path}"
        if query:
            url = f"{url}?{urlencode(query)}"

        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "Money-Mintter/0.1",
                "apikey": self.api_key,
            },
            method="GET",
        )

        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                status = getattr(response, "status", 200)
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            if exc.code == 402:
                raise FmpApiError(
                    f"FMP HTTP 402 Payment Required for {path}. Your current FMP API tier does not include 1-minute intraday historical chart data. "
                    "To test on real historical data, place real 1-minute CSV files into data/raw/hf/ (e.g. AAPL.csv), "
                    "which Money Mintter loads automatically with zero API limits."
                ) from exc
            raise FmpApiError(f"FMP HTTP {exc.code} for {path}") from exc
        except (URLError, TimeoutError) as exc:
            raise FmpApiError(f"FMP request failed for {path}: {exc}") from exc

        if status >= 400:
            raise FmpApiError(f"FMP HTTP {status} for {path}")

        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise FmpApiError(f"FMP returned invalid JSON for {path}") from exc

        if isinstance(payload, dict):
            message = payload.get("error") or payload.get("Error Message")
            if message:
                raise FmpApiError(f"FMP rejected {path}: {message}")

        return payload


class FmpMinuteData:
    """Temporary intraday candle adapter for strategy/research validation."""

    def __init__(
        self,
        client: FmpClient,
        default_timezone: str = "America/New_York",
    ) -> None:
        self.client = client
        self.default_timezone_name = default_timezone
        self._resolve_timezone(default_timezone)

    def candles(
        self,
        symbol: str,
        *,
        start: date | None = None,
        end: date | None = None,
        timezone_name: str | None = None,
    ) -> list[Candle]:
        if not symbol.strip():
            raise ValueError("symbol cannot be empty")
        if start and end and start > end:
            raise ValueError("start date cannot be after end date")

        params: dict[str, str] = {"symbol": symbol.strip()}
        if start is not None:
            params["from"] = start.isoformat()
        if end is not None:
            params["to"] = end.isoformat()

        payload = self.client.get_json("historical-chart/1min", params)
        if not isinstance(payload, list):
            raise FmpApiError("FMP 1-minute endpoint must return a list")

        market_timezone = self._resolve_timezone(timezone_name or self.default_timezone_name)
        parsed: list[Candle] = []
        for row in payload:
            if not isinstance(row, dict):
                raise FmpApiError(f"invalid 1-minute row: {row!r}")
            try:
                timestamp = self._parse_timestamp(str(row["date"]), market_timezone)
                parsed.append(
                    Candle(
                        symbol=str(row.get("symbol") or symbol),
                        timestamp=timestamp,
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=float(row.get("volume", 0.0)),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise FmpApiError(f"invalid 1-minute row: {row!r}") from exc

        parsed.sort(key=lambda candle: candle.timestamp)
        for previous, current in zip(parsed, parsed[1:]):
            if current.timestamp == previous.timestamp:
                raise FmpApiError(f"duplicate candle timestamp for {symbol}")
        return parsed

    @staticmethod
    def _parse_timestamp(value: str, timezone: ZoneInfo) -> datetime:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"invalid timestamp: {value}") from exc

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone)
        return parsed

    @staticmethod
    def _resolve_timezone(timezone_name: str) -> ZoneInfo:
        try:
            return ZoneInfo(timezone_name)
        except Exception as exc:
            raise ValueError(f"invalid market timezone: {timezone_name}") from exc
