from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True, slots=True)
class Quote:
    symbol: str
    bid: float
    ask: float
    last: float
    timestamp: datetime

    def __post_init__(self) -> None:
        if min(self.bid, self.ask, self.last) <= 0:
            raise ValueError("quote prices must be positive")
        if self.bid > self.ask:
            raise ValueError("bid cannot exceed ask")


@dataclass(frozen=True, slots=True)
class Candidate:
    symbol: str
    market: str
    liquidity_score: float
    volatility_score: float
    activity_score: float
    signal_quality_score: float
    timestamp: datetime

    def __post_init__(self) -> None:
        scores = (
            self.liquidity_score,
            self.volatility_score,
            self.activity_score,
            self.signal_quality_score,
        )
        if any(not 0 <= score <= 1 for score in scores):
            raise ValueError("candidate scores must be in [0, 1]")


@dataclass(frozen=True, slots=True)
class TradeSignal:
    symbol: str
    side: Side
    entry_price: float
    stop_price: float
    target_price: float
    confidence: float
    timestamp: datetime
    reason: str
    strategy_version: str = "lsr.v1"

    def __post_init__(self) -> None:
        if min(self.entry_price, self.stop_price, self.target_price) <= 0:
            raise ValueError("signal prices must be positive")
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be in [0, 1]")
        if not self.strategy_version.strip():
            raise ValueError("strategy version cannot be empty")
        if self.side is Side.BUY:
            if not self.stop_price < self.entry_price < self.target_price:
                raise ValueError("long signal requires stop < entry < target")
        else:
            if not self.target_price < self.entry_price < self.stop_price:
                raise ValueError("short signal requires target < entry < stop")


@dataclass(slots=True)
class Position:
    symbol: str
    quantity: float
    avg_entry_price: float
    entry_fees_total: float = 0.0


@dataclass(frozen=True, slots=True)
class Fill:
    order_id: str
    symbol: str
    side: Side
    quantity: float
    price: float
    fee: float
    timestamp: datetime
    realized_pnl: float | None = None


@dataclass(slots=True)
class ManagedTrade:
    symbol: str
    side: Side
    quantity: float
    entry_price: float
    stop_price: float
    target_price: float
    opened_at: datetime
    entry_fill_id: str
    exit_fill_id: str | None = None
    closed_at: datetime | None = None
    realized_pnl: float | None = None
    close_reason: str | None = None


@dataclass(slots=True)
class WalletLedger:
    trading_wallet: float
    profit_reserve: float = 0.0
    profit_reserve_pct: float = 0.20
    realized_profit: float = 0.0
    realized_loss: float = 0.0

    def __post_init__(self) -> None:
        if self.trading_wallet < 0:
            raise ValueError("trading wallet cannot start negative")
        if not 0 <= self.profit_reserve_pct <= 1:
            raise ValueError("profit reserve percentage must be in [0, 1]")

    @property
    def total_tracked_capital(self) -> float:
        return self.trading_wallet + self.profit_reserve

    def apply_realized_pnl(self, pnl: float) -> tuple[float, float]:
        if pnl > 0:
            reserve = pnl * self.profit_reserve_pct
            reinvested = pnl - reserve
            self.trading_wallet += reinvested
            self.profit_reserve += reserve
            self.realized_profit += pnl
            return reinvested, reserve

        if pnl < 0:
            self.trading_wallet += pnl
            self.realized_loss += -pnl
        return pnl, 0.0
