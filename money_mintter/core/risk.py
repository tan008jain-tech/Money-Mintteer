from __future__ import annotations

from dataclasses import dataclass
from math import floor


@dataclass(frozen=True, slots=True)
class RiskDecision:
    approved: bool
    quantity: float = 0.0
    capital_at_risk: float = 0.0
    reason: str = ""


class RiskEngine:
    def __init__(
        self,
        risk_per_trade_pct: float = 0.005,
        max_position_pct: float = 0.20,
        max_portfolio_risk_pct: float = 0.02,
    ) -> None:
        if not 0 < risk_per_trade_pct <= max_portfolio_risk_pct:
            raise ValueError("invalid risk-per-trade setting")
        if not 0 < max_position_pct <= 1:
            raise ValueError("max position percentage must be in (0, 1]")
        self.risk_per_trade_pct = risk_per_trade_pct
        self.max_position_pct = max_position_pct
        self.max_portfolio_risk_pct = max_portfolio_risk_pct

    def _size(self, available_capital: float, entry: float, stop: float, open_risk: float, *, side: str) -> RiskDecision:
        if available_capital <= 0:
            return RiskDecision(False, reason="no available capital")
        if side == "long":
            if not 0 < stop < entry:
                return RiskDecision(False, reason="invalid long stop")
            risk_per_share = entry - stop
        else:
            if not entry < stop:
                return RiskDecision(False, reason="invalid short stop")
            risk_per_share = stop - entry

        risk_budget = available_capital * self.risk_per_trade_pct
        if open_risk + risk_budget > available_capital * self.max_portfolio_risk_pct + 1e-9:
            return RiskDecision(False, reason="portfolio risk limit exceeded")

        raw_qty = risk_budget / risk_per_share
        max_qty_by_capital = (available_capital * self.max_position_pct) / entry
        qty = floor(min(raw_qty, max_qty_by_capital) * 1_000_000) / 1_000_000
        if qty <= 0:
            return RiskDecision(False, reason="position size rounds to zero")

        return RiskDecision(True, qty, qty * risk_per_share, "approved")

    def size_long(self, available_capital: float, entry: float, stop: float, open_risk: float) -> RiskDecision:
        return self._size(available_capital, entry, stop, open_risk, side="long")

    def size_short(self, available_capital: float, entry: float, stop: float, open_risk: float) -> RiskDecision:
        return self._size(available_capital, entry, stop, open_risk, side="short")
