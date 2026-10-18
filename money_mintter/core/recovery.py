from __future__ import annotations

from dataclasses import dataclass

from money_mintter.brokers.base import Broker
from money_mintter.domain import ManagedTrade, WalletLedger
from money_mintter.storage.state import (
    PositionSnapshot,
    RuntimeSnapshot,
    TradeSnapshot,
)


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    ok: bool
    reasons: tuple[str, ...]


class RecoveryManager:
    """Restart recovery that fails closed on any unexplained broker/state mismatch."""

    def capture(
        self,
        *,
        broker: Broker,
        wallet: WalletLedger,
        open_trades: dict[str, ManagedTrade],
    ) -> RuntimeSnapshot:
        positions = tuple(
            PositionSnapshot(
                symbol=position.symbol,
                quantity=position.quantity,
                average_price=position.avg_entry_price,
            )
            for position in sorted(broker.positions().values(), key=lambda item: item.symbol)
        )
        trades = tuple(
            TradeSnapshot(
                symbol=trade.symbol,
                side=trade.side.value,
                quantity=trade.quantity,
                entry_price=trade.entry_price,
                stop_price=trade.stop_price,
                target_price=trade.target_price,
                opened_at=trade.opened_at.isoformat(),
                entry_fill_id=trade.entry_fill_id,
            )
            for trade in sorted(open_trades.values(), key=lambda item: item.symbol)
        )
        return RuntimeSnapshot.now(
            broker_cash=broker.available_cash,
            wallet=wallet.wallet,
            positions=positions,
            open_trades=trades,
        )

    @staticmethod
    def reconcile(
        snapshot: RuntimeSnapshot,
        broker: Broker,
        *,
        cash_tolerance: float = 0.01,
        quantity_tolerance: float = 1e-8,
        price_tolerance: float = 1e-8,
    ) -> ReconciliationResult:
        reasons: list[str] = []

        if abs(snapshot.broker_cash - broker.available_cash) > cash_tolerance:
            reasons.append("broker cash mismatch")

        persisted = {
            position.symbol: position
            for position in snapshot.positions
        }
        actual = broker.positions()

        if set(persisted) != set(actual):
            reasons.append("position symbol mismatch")

        for symbol in sorted(set(persisted) & set(actual)):
            expected = persisted[symbol]
            current = actual[symbol]
            if abs(expected.quantity - current.quantity) > quantity_tolerance:
                reasons.append(f"{symbol} quantity mismatch")
            if abs(expected.average_price - current.avg_entry_price) > price_tolerance:
                reasons.append(f"{symbol} average price mismatch")

        expected_trades = {trade.symbol for trade in snapshot.open_trades}
        actual_symbols = set(actual)
        if expected_trades != actual_symbols:
            reasons.append("managed-trade/position mismatch")

        return ReconciliationResult(ok=not reasons, reasons=tuple(reasons))
