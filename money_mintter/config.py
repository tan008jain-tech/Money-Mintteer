from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Settings:
    initial_capital: float = 10_000.0
    base_currency: str = "USD"
    profit_reserve_pct: float = 0.20
    max_deep_candidates: int = 5
    min_candidate_score: float = 0.60
    risk_per_trade_pct: float = 0.005
    max_position_pct: float = 0.20
    max_portfolio_risk_pct: float = 0.02
    paper_slippage_bps: float = 5.0
    paper_commission_bps: float = 2.0

    def __post_init__(self) -> None:
        if self.initial_capital <= 0:
            raise ValueError("initial capital must be positive")
        if self.max_deep_candidates <= 0:
            raise ValueError("max deep candidates must be positive")
        if not 0 <= self.min_candidate_score <= 1:
            raise ValueError("minimum candidate score must be in [0, 1]")
        if not 0 < self.risk_per_trade_pct <= self.max_portfolio_risk_pct:
            raise ValueError("per-trade risk must be positive and <= portfolio risk")
        if not 0 < self.max_position_pct <= 1:
            raise ValueError("max position percentage must be in (0, 1]")
