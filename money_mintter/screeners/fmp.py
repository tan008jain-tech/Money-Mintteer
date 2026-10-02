from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from money_mintter.data.fmp import FmpClient, FmpApiError
from money_mintter.domain import Candidate
from money_mintter.data.interfaces import Screener


class FmpUniverseScreener(Screener):
    """Build a coarse trading universe from FMP market-mover snapshots.

    The temporary adapter intentionally uses provider data only for universe
    discovery. The deep strategy remains fully internal to Money Mintter.
    """

    def __init__(
        self,
        client: FmpClient,
        max_rows: int = 100,
        include_movers: bool = True,
    ) -> None:
        if max_rows <= 0:
            raise ValueError("max_rows must be positive")
        self.client = client
        self.max_rows = max_rows
        self.include_movers = include_movers

    def candidates(self, now_utc: datetime) -> list[Candidate]:
        active_rows = self._rows("most-actives")
        rows_by_symbol: dict[str, dict[str, Any]] = {}

        for index, row in enumerate(active_rows[: self.max_rows]):
            symbol = self._symbol(row)
            if symbol:
                normalized = dict(row)
                normalized["_activity_rank"] = index
                normalized["_activity_count"] = max(len(active_rows[: self.max_rows]), 1)
                rows_by_symbol[symbol] = normalized

        if self.include_movers:
            for endpoint in ("biggest-gainers", "biggest-losers"):
                try:
                    for row in self._rows(endpoint):
                        symbol = self._symbol(row)
                        if symbol and symbol not in rows_by_symbol:
                            rows_by_symbol[symbol] = dict(row)
                except FmpApiError:
                    # A limited plan may not expose every mover endpoint.
                    # Most-actives remains the required discovery source.
                    continue

        candidates: list[Candidate] = []
        for row in rows_by_symbol.values():
            symbol = self._symbol(row)
            if not symbol:
                continue

            price = self._float(row.get("price"))
            if price is not None and price <= 0:
                continue

            change_pct = abs(self._float(row.get("changesPercentage")) or 0.0)
            movement_score = min(change_pct / 5.0, 1.0)

            activity_rank = row.get("_activity_rank")
            activity_count = row.get("_activity_count")
            if activity_rank is not None and activity_count is not None:
                rank_denominator = max(int(activity_count) - 1, 1)
                rank_score = 1.0 - 0.40 * (int(activity_rank) / rank_denominator)
                liquidity_score = rank_score
                activity_score = 0.60 * rank_score + 0.40 * movement_score
            else:
                # Mover-only symbols are intentionally below the hard liquidity
                # gate until a richer market-data feed confirms tradability.
                liquidity_score = 0.60
                activity_score = max(0.60, movement_score)

            volatility_score = min(max(change_pct / 5.0, 0.0), 0.95)
            signal_quality_proxy = min(0.65 + 0.25 * movement_score, 0.90)

            candidates.append(
                Candidate(
                    symbol=symbol,
                    market=str(row.get("exchange") or "UNKNOWN"),
                    liquidity_score=round(liquidity_score, 6),
                    volatility_score=round(volatility_score, 6),
                    activity_score=round(activity_score, 6),
                    signal_quality_score=round(signal_quality_proxy, 6),
                    timestamp=now_utc,
                )
            )

        candidates.sort(
            key=lambda candidate: (
                -candidate.volatility_score,
                -candidate.activity_score,
                -candidate.liquidity_score,
                candidate.symbol,
            )
        )
        return candidates

    def _rows(self, endpoint: str) -> list[dict[str, Any]]:
        payload = self.client.get_json(endpoint)
        if not isinstance(payload, list):
            raise FmpApiError(f"FMP {endpoint} endpoint must return a list")
        return [row for row in payload if isinstance(row, dict)]

    @staticmethod
    def _symbol(row: dict[str, Any]) -> str:
        value = row.get("symbol")
        return str(value).strip().upper() if value else ""

    @staticmethod
    def _float(value: Any) -> float | None:
        if value is None or value == "":
            return None
        try:
            result = float(value)
        except (TypeError, ValueError):
            return None
        return result if math.isfinite(result) else None
