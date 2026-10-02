from __future__ import annotations

import csv
import json
from datetime import date, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from money_mintter.strategy import Candle


class HfDataError(RuntimeError):
    """HF Data Library integration failure."""


class HfDataClient:
    """Client for the HF Data Library REST API."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.hfdatalibrary.com/v1",
        timeout_seconds: float = 30.0,
    ) -> None:
        if not api_key.strip():
            raise ValueError("HF Data Library API key cannot be empty")
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("HF Data Library base URL must be HTTP(S)")
        if timeout_seconds <= 0:
            raise ValueError("timeout must be positive")
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def _request(
        self,
        endpoint: str,
        *,
        params: dict[str, str] | None = None,
        authenticated: bool = True,
    ) -> bytes:
        path = endpoint.strip("/")
        url = f"{self.base_url}/{path}"
        if params:
            url = f"{url}?{urlencode(params)}"

        headers = {"Accept": "application/json", "User-Agent": "Money-Mintter/0.1"}
        if authenticated:
            headers["X-API-Key"] = self.api_key

        request = Request(url, headers=headers, method="GET")
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                return response.read()
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            detail = body[:300].strip()
            raise HfDataError(
                f"HF Data Library HTTP {exc.code} for {path}"
                + (f": {detail}" if detail else "")
            ) from exc
        except (URLError, TimeoutError) as exc:
            raise HfDataError(f"HF Data Library request failed for {path}: {exc}") from exc

    def symbols(self) -> list[dict[str, object]]:
        payload = json.loads(self._request("symbols", authenticated=False).decode("utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("symbols"), list):
            raise HfDataError("HF symbols response has an unexpected shape")
        return [row for row in payload["symbols"] if isinstance(row, dict)]

    def request_download_url(
        self,
        symbol: str,
        *,
        version: str = "clean",
        timeframe: str = "1min",
        format: str = "csv",
    ) -> str:
        symbol = symbol.strip().upper()
        if not symbol:
            raise ValueError("symbol cannot be empty")
        payload = json.loads(
            self._request(
                f"download-token/{symbol}",
                params={
                    "version": version,
                    "timeframe": timeframe,
                    "format": format,
                },
            ).decode("utf-8")
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("url"), str):
            raise HfDataError("HF download-token response did not contain a signed URL")
        return payload["url"]

    def download_1min_csv(
        self,
        symbol: str,
        output_path: str | Path,
        *,
        version: str = "clean",
    ) -> Path:
        url = self.request_download_url(
            symbol,
            version=version,
            timeframe="1min",
            format="csv",
        )
        destination = Path(output_path)
        destination.parent.mkdir(parents=True, exist_ok=True)

        request = Request(
            url,
            headers={"Accept": "text/csv", "User-Agent": "Money-Mintter/0.1"},
            method="GET",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response, destination.open(
                "wb"
            ) as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise HfDataError(f"failed to download {symbol} data: {exc}") from exc

        return destination


class HfCandleLoader:
    """Load HF 1-minute CSV data into validated Money Mintter candles."""

    EXPECTED = ("datetime", "open", "high", "low", "close", "volume")
    DEFAULT_TIMEZONE = "America/New_York"

    def load(
        self,
        path: str | Path,
        symbol: str,
        *,
        start: date | None = None,
        end: date | None = None,
        timezone_name: str = DEFAULT_TIMEZONE,
    ) -> list[Candle]:
        try:
            market_tz = ZoneInfo(timezone_name)
        except Exception as exc:
            raise ValueError(f"invalid market timezone: {timezone_name}") from exc

        candles: list[Candle] = []
        with Path(path).open("r", newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None or any(
                field not in reader.fieldnames for field in self.EXPECTED
            ):
                raise HfDataError(
                    f"HF CSV must contain {', '.join(self.EXPECTED)}"
                )

            for row_number, row in enumerate(reader, start=2):
                try:
                    timestamp = self._parse_timestamp(row["datetime"], market_tz)
                    local_date = timestamp.astimezone(market_tz).date()
                    if start is not None and local_date < start:
                        continue
                    if end is not None and local_date > end:
                        continue

                    candles.append(
                        Candle(
                            symbol=symbol.upper(),
                            timestamp=timestamp,
                            open=float(row["open"]),
                            high=float(row["high"]),
                            low=float(row["low"]),
                            close=float(row["close"]),
                            volume=float(row["volume"]),
                        )
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    raise HfDataError(
                        f"invalid HF candle at CSV row {row_number}"
                    ) from exc

        if not candles:
            raise HfDataError("no HF candles matched the requested range")

        candles.sort(key=lambda candle: candle.timestamp)
        timestamps = [candle.timestamp for candle in candles]
        if any(left >= right for left, right in zip(timestamps, timestamps[1:])):
            raise HfDataError("HF candles contain duplicate or out-of-order timestamps")

        return candles

    @staticmethod
    def _parse_timestamp(value: str, timezone: ZoneInfo) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone)
        return parsed
