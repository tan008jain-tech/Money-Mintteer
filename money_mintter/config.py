from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

# Load local development secrets/configuration when present. .env is ignored
# by Git, so this never requires credentials to be committed.
load_dotenv()


def _env_text(name: str, default: str) -> str:
    return os.getenv(name, default)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be numeric") from exc


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _env_percent(name: str, default: float) -> float:
    return _env_float(name, default * 100.0) / 100.0





@dataclass(frozen=True, slots=True)
class Settings:
    initial_capital: float = field(default_factory=lambda: _env_float("INITIAL_CAPITAL", 10_000.0))
    alpaca_api_key: str = field(
        default_factory=lambda: os.getenv("APCA_API_KEY_ID") or os.getenv("APCA_API_KEY", "")
    )
    alpaca_secret_key: str = field(
        default_factory=lambda: os.getenv("APCA_API_SECRET_KEY") or os.getenv("APCA_API_SECRET", "")
    )
    alpaca_base_url: str = field(default_factory=lambda: _env_text("APCA_API_BASE_URL", "https://paper-api.alpaca.markets"))
    base_currency: str = field(default_factory=lambda: _env_text("BASE_CURRENCY", "USD"))
    max_deep_candidates: int = field(
        default_factory=lambda: _env_int("MAX_DEEP_CANDIDATES", 5)
    )
    min_candidate_score: float = field(
        default_factory=lambda: _env_float("MIN_CANDIDATE_SCORE", 0.60)
    )
    risk_per_trade_pct: float = field(
        default_factory=lambda: _env_percent("RISK_PER_TRADE_PCT", 0.005)
    )
    max_position_pct: float = field(
        default_factory=lambda: _env_percent("MAX_POSITION_PCT", 0.20)
    )
    max_portfolio_risk_pct: float = field(
        default_factory=lambda: _env_percent("MAX_PORTFOLIO_RISK_PCT", 0.02)
    )
    paper_slippage_bps: float = field(
        default_factory=lambda: _env_float("PAPER_SLIPPAGE_BPS", 5.0)
    )
    paper_commission_bps: float = field(
        default_factory=lambda: _env_float("PAPER_COMMISSION_BPS", 2.0)
    )

    alpaca_data_feed: str = field(
        default_factory=lambda: _env_text("ALPACA_DATA_FEED", "iex")
    )
    max_daily_loss_pct: float = field(
        default_factory=lambda: _env_percent("MAX_DAILY_LOSS_PCT", 0.02)
    )
    max_consecutive_errors: int = field(
        default_factory=lambda: _env_int("MAX_CONSECUTIVE_ERRORS", 5)
    )

    def __post_init__(self) -> None:
        feed_lower = self.alpaca_data_feed.strip().lower()
        if feed_lower not in ("iex", "sip"):
            raise ValueError("ALPACA_DATA_FEED must be either 'iex' or 'sip'")
        if not 0 < self.max_daily_loss_pct <= 1:
            raise ValueError("max daily loss percentage must be in (0, 1]")
        if self.max_consecutive_errors <= 0:
            raise ValueError("max consecutive errors must be positive")
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
