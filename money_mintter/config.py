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


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    val = raw.strip().lower()
    if val in ("true", "1", "yes", "on"):
        return True
    if val in ("false", "0", "no", "off"):
        return False
    raise ValueError(f"{name} must be boolean")





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
    max_single_position_pct: float = field(
        default_factory=lambda: _env_percent("MAX_SINGLE_POSITION_PCT", 0.90)
    )
    isolated_opportunity_max_pct: float = field(
        default_factory=lambda: _env_percent("ISOLATED_OPPORTUNITY_MAX_PCT", 0.65)
    )
    commission_per_order_usd: float = field(
        default_factory=lambda: _env_float("COMMISSION_PER_ORDER_USD", 0.0)
    )
    commission_per_share_usd: float = field(
        default_factory=lambda: _env_float("COMMISSION_PER_SHARE_USD", 0.0)
    )
    estimated_regulatory_fee_per_share: float = field(
        default_factory=lambda: _env_float("ESTIMATED_REGULATORY_FEE_PER_SHARE", 0.000195)
    )
    estimated_sec_fee_rate: float = field(
        default_factory=lambda: _env_float("ESTIMATED_SEC_FEE_RATE", 0.0000206)
    )
    estimated_taf_cap_usd: float = field(
        default_factory=lambda: _env_float("ESTIMATED_TAF_CAP_USD", 9.79)
    )
    estimated_cat_fee_per_share: float = field(
        default_factory=lambda: _env_float("ESTIMATED_CAT_FEE_PER_SHARE", 0.000003)
    )
    min_gross_to_cost_multiple: float = field(
        default_factory=lambda: _env_float("MIN_GROSS_TO_COST_MULTIPLE", 3.0)
    )
    min_reward_risk: float = field(
        default_factory=lambda: _env_float("MIN_REWARD_RISK", 0.0)
    )
    min_target_pct: float = field(
        default_factory=lambda: _env_float("MIN_TARGET_PCT", 0.0)
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
    enable_trend_continuation: bool = field(
        default_factory=lambda: _env_bool("ENABLE_TREND_CONTINUATION", False)
    )
    min_latest_close: float = field(
        default_factory=lambda: _env_float("MIN_LATEST_CLOSE", 5.0)
    )
    min_median_close_5: float = field(
        default_factory=lambda: _env_float("MIN_MEDIAN_CLOSE_5", 5.0)
    )
    min_valid_daily_bars: int = field(
        default_factory=lambda: _env_int("MIN_VALID_DAILY_BARS", 15)
    )
    lookback_sessions: int = field(
        default_factory=lambda: _env_int("LOOKBACK_SESSIONS", 20)
    )
    min_median_dollar_volume: float = field(
        default_factory=lambda: _env_float("MIN_MEDIAN_DOLLAR_VOLUME", 10_000_000.0)
    )
    min_atr_pct: float = field(
        default_factory=lambda: _env_float("MIN_ATR_PCT", 0.01)
    )
    max_atr_pct: float = field(
        default_factory=lambda: _env_float("MAX_ATR_PCT", 0.10)
    )
    max_daily_pool_size: int = field(
        default_factory=lambda: _env_int("MAX_DAILY_POOL_SIZE", 200)
    )
    max_daily_loss_pct: float = field(
        default_factory=lambda: _env_percent("MAX_DAILY_LOSS_PCT", 0.02)
    )
    max_consecutive_errors: int = field(
        default_factory=lambda: _env_int("MAX_CONSECUTIVE_ERRORS", 5)
    )
    flatten_minutes_before_close: int = field(
        default_factory=lambda: _env_int("FLATTEN_MINUTES_BEFORE_CLOSE", 5)
    )
    no_new_entries_minutes_before_close: int = field(
        default_factory=lambda: _env_int("NO_NEW_ENTRIES_MINUTES_BEFORE_CLOSE", 30)
    )
    max_wait_for_open_minutes: int = field(
        default_factory=lambda: _env_int("MAX_WAIT_FOR_OPEN_MINUTES", 45)
    )
    http_connect_timeout_seconds: float = field(
        default_factory=lambda: _env_float("ALPACA_HTTP_CONNECT_TIMEOUT", 5.0)
    )
    http_read_timeout_seconds: float = field(
        default_factory=lambda: _env_float("ALPACA_HTTP_READ_TIMEOUT", 20.0)
    )
    max_outage_minutes: int = field(
        default_factory=lambda: _env_int("MAX_OUTAGE_MINUTES", 15)
    )
    flatten_max_wait_seconds: int = field(
        default_factory=lambda: _env_int("FLATTEN_MAX_WAIT_SECONDS", 600)
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
        if self.min_latest_close < 0:
            raise ValueError("min_latest_close must be >= 0")
        if self.min_median_close_5 < 0:
            raise ValueError("min_median_close_5 must be >= 0")
        if self.min_valid_daily_bars <= 0:
            raise ValueError("min_valid_daily_bars must be positive")
        if self.lookback_sessions <= 0:
            raise ValueError("lookback_sessions must be positive")
        if self.min_median_dollar_volume < 0:
            raise ValueError("min_median_dollar_volume must be >= 0")
        if not 0 <= self.min_atr_pct <= self.max_atr_pct:
            raise ValueError("min_atr_pct must be <= max_atr_pct")
        if self.max_daily_pool_size <= 0:
            raise ValueError("max_daily_pool_size must be positive")
        if self.max_deep_candidates <= 0:
            raise ValueError("max deep candidates must be positive")
        if not 0 <= self.min_candidate_score <= 1:
            raise ValueError("minimum candidate score must be in [0, 1]")
        if not 0 < self.risk_per_trade_pct <= self.max_portfolio_risk_pct:
            raise ValueError("per-trade risk must be positive and <= portfolio risk")
        if not 0 < self.max_position_pct <= 1:
            raise ValueError("max position percentage must be in (0, 1]")
        if not 0 < self.max_single_position_pct <= 1:
            raise ValueError("MAX_SINGLE_POSITION_PCT must be in (0, 1]")
        if not 0 < self.isolated_opportunity_max_pct <= 1:
            raise ValueError("ISOLATED_OPPORTUNITY_MAX_PCT must be in (0, 1]")
        if self.commission_per_order_usd < 0:
            raise ValueError("COMMISSION_PER_ORDER_USD must be >= 0")
        if self.commission_per_share_usd < 0:
            raise ValueError("COMMISSION_PER_SHARE_USD must be >= 0")
        if self.estimated_regulatory_fee_per_share < 0:
            raise ValueError("ESTIMATED_REGULATORY_FEE_PER_SHARE must be >= 0")
        if self.estimated_sec_fee_rate < 0:
            raise ValueError("ESTIMATED_SEC_FEE_RATE must be >= 0")
        if self.estimated_taf_cap_usd < 0:
            raise ValueError("ESTIMATED_TAF_CAP_USD must be >= 0")
        if self.estimated_cat_fee_per_share < 0:
            raise ValueError("ESTIMATED_CAT_FEE_PER_SHARE must be >= 0")
        if self.min_gross_to_cost_multiple < 0:
            raise ValueError("MIN_GROSS_TO_COST_MULTIPLE must be >= 0")
        if self.min_reward_risk < 0:
            raise ValueError("min_reward_risk must be >= 0")
        if self.min_target_pct < 0:
            raise ValueError("min_target_pct must be >= 0")
        if self.flatten_minutes_before_close < 1:
            raise ValueError("flatten_minutes_before_close must be >= 1")
        if self.no_new_entries_minutes_before_close <= self.flatten_minutes_before_close:
            raise ValueError("no_new_entries_minutes_before_close must be > flatten_minutes_before_close")
        if self.max_wait_for_open_minutes < 0:
            raise ValueError("max_wait_for_open_minutes must be >= 0")
        if self.http_connect_timeout_seconds <= 0:
            raise ValueError("http_connect_timeout_seconds must be > 0")
        if self.http_read_timeout_seconds <= 0:
            raise ValueError("http_read_timeout_seconds must be > 0")
        if self.max_outage_minutes <= 0:
            raise ValueError("max_outage_minutes must be > 0")
        if self.flatten_max_wait_seconds < 0:
            raise ValueError("flatten_max_wait_seconds must be >= 0")
