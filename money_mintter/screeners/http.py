from __future__ import annotations

import json
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from money_mintter.domain import Candidate
from money_mintter.data.interfaces import Screener


class HttpScreenerError(RuntimeError):
    """External screener integration failure."""


class HttpScreener(Screener):
    """Provider-neutral JSON screener adapter.

    Expected response shape is either a list of candidate objects or:
    {"candidates": [...]}.

    Each candidate object must contain:
    symbol, market, liquidity_score, volatility_score, activity_score,
    signal_quality_score, and timestamp (ISO-8601).
    """

    def __init__(
        self,
        endpoint: str,
        api_key: str | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        if not endpoint.startswith(("http://", "https://")):
            raise ValueError("screener endpoint must be HTTP(S)")
        if timeout_seconds <= 0:
            raise ValueError("timeout must be positive")
        self.endpoint = endpoint
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    def candidates(self, now_utc: datetime) -> list[Candidate]:
        headers = {"Accept": "application/json", "User-Agent": "Money-Mintter/0.1"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        request = Request(self.endpoint, headers=headers, method="GET")
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise HttpScreenerError(f"screener request failed: {exc}") from exc

        raw_candidates = payload.get("candidates", payload) if isinstance(payload, dict) else payload
        if not isinstance(raw_candidates, list):
            raise HttpScreenerError("screener response must contain a candidate list")

        parsed: list[Candidate] = []
        for raw in raw_candidates:
            try:
                timestamp = datetime.fromisoformat(str(raw["timestamp"]).replace("Z", "+00:00"))
                candidate = Candidate(
                    symbol=str(raw["symbol"]),
                    market=str(raw["market"]),
                    liquidity_score=float(raw["liquidity_score"]),
                    volatility_score=float(raw["volatility_score"]),
                    activity_score=float(raw["activity_score"]),
                    signal_quality_score=float(raw["signal_quality_score"]),
                    timestamp=timestamp,
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise HttpScreenerError(f"invalid candidate payload: {raw!r}") from exc
            if candidate.timestamp <= now_utc:
                parsed.append(candidate)

        return parsed
