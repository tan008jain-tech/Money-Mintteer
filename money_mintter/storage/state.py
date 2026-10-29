from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from typing import Any


@dataclass(frozen=True, slots=True)
class PositionSnapshot:
    symbol: str
    quantity: float
    average_price: float


@dataclass(frozen=True, slots=True)
class TradeSnapshot:
    symbol: str
    side: str
    quantity: float
    entry_price: float
    stop_price: float
    target_price: float
    opened_at: str
    entry_fill_id: str


@dataclass(frozen=True, slots=True)
class RuntimeSnapshot:
    captured_at: str
    broker_cash: float
    wallet: float
    positions: tuple[PositionSnapshot, ...]
    open_trades: tuple[TradeSnapshot, ...]
    strategy_state: Any = ()
    schema_version: int = 1

    def __post_init__(self) -> None:
        if isinstance(self.strategy_state, (list, tuple)) and not isinstance(self.strategy_state, dict):
            object.__setattr__(self, "strategy_state", tuple(self.strategy_state))

    @classmethod
    def now(
        cls,
        *,
        broker_cash: float,
        wallet: float,
        positions: tuple[PositionSnapshot, ...],
        open_trades: tuple[TradeSnapshot, ...],
        strategy_state: tuple[Any, ...] = (),
    ) -> "RuntimeSnapshot":
        return cls(
            captured_at=datetime.now(timezone.utc).isoformat(),
            broker_cash=broker_cash,
            wallet=wallet,
            positions=positions,
            open_trades=open_trades,
            strategy_state=strategy_state,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RuntimeSnapshot":
        try:
            positions = tuple(PositionSnapshot(**item) for item in payload["positions"])
            open_trades = tuple(TradeSnapshot(**item) for item in payload["open_trades"])
            strategy_state = payload.get("strategy_state", ())
            return cls(
                captured_at=str(payload["captured_at"]),
                broker_cash=float(payload["broker_cash"]),
                wallet=float(payload.get("wallet", payload.get("trading_wallet", 0.0))),
                positions=positions,
                open_trades=open_trades,
                strategy_state=strategy_state,
                schema_version=int(payload.get("schema_version", 1)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid runtime snapshot") from exc

    @classmethod
    def from_json(cls, payload: str) -> "RuntimeSnapshot":
        try:
            value = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError("invalid runtime snapshot JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("runtime snapshot JSON must be an object")
        return cls.from_dict(value)
