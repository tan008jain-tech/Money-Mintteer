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
        leverage: float = 1.0,
    ) -> None:
        if not 0 < risk_per_trade_pct <= max_portfolio_risk_pct:
            raise ValueError("invalid risk-per-trade setting")
        if not 0 < max_position_pct <= 1:
            raise ValueError("max position percentage must be in (0, 1]")
        self.risk_per_trade_pct = risk_per_trade_pct
        self.max_position_pct = max_position_pct
        self.max_portfolio_risk_pct = max_portfolio_risk_pct
        self.leverage = leverage

    def _size(self, available_capital: float, entry: float, stop: float, open_risk: float, *, side: str, active_candidates_count: int = 5, market_open: bool = True) -> RiskDecision:
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

        effective_max_position_pct = min(
            1.0, self.max_position_pct * (5 / max(1, active_candidates_count))
        )

        leverage = self.leverage if market_open else min(self.leverage, 2.0)
        leveraged_capital = available_capital * leverage

        raw_qty = risk_budget / risk_per_share
        max_qty_by_capital = (leveraged_capital * effective_max_position_pct) / entry
        qty_float = min(raw_qty, max_qty_by_capital)

        if market_open:
            qty = round(qty_float, 4)
            if qty <= 1e-8:
                return RiskDecision(False, reason="size_below_minimum")
        else:
            int_qty = int(floor(qty_float))
            if int_qty <= 0:
                return RiskDecision(False, reason="size_below_minimum")
            qty = float(int_qty)

        return RiskDecision(True, float(qty), qty * risk_per_share, "approved")

    def size_long(self, available_capital: float, entry: float, stop: float, open_risk: float, active_candidates_count: int = 5, market_open: bool = True) -> RiskDecision:
        return self._size(available_capital, entry, stop, open_risk, side="long", active_candidates_count=active_candidates_count, market_open=market_open)

    def size_short(self, available_capital: float, entry: float, stop: float, open_risk: float, active_candidates_count: int = 5, market_open: bool = True) -> RiskDecision:
        return self._size(available_capital, entry, stop, open_risk, side="short", active_candidates_count=active_candidates_count, market_open=market_open)
