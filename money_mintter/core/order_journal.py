from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4


TERMINAL_ORDER_STATUSES = {"filled", "canceled", "expired", "rejected", "replaced"}
EXIT_ROLES = {"take_profit", "stop_loss", "eod_liquidation", "emergency_cleanup"}


def _value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _plain(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "value"):
        return value.value
    return str(value)


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def snapshot_order(
    order: Any,
    *,
    role_hint: str = "unknown",
    client_order_id: str | None = None,
    parent_order_id: str | None = None,
) -> dict[str, Any]:
    """Extract only stable, auditable fields from an Alpaca order or order leg."""
    fields = {
        "order_id": _value(order, "id"),
        "client_order_id": _value(order, "client_order_id") or client_order_id,
        "symbol": _value(order, "symbol"),
        "side": _value(order, "side"),
        "order_type": _value(order, "type") or _value(order, "order_type"),
        "order_class": _value(order, "order_class"),
        "status": _value(order, "status"),
        "qty": _value(order, "qty"),
        "filled_qty": _value(order, "filled_qty"),
        "filled_avg_price": _value(order, "filled_avg_price"),
        "submitted_at": _value(order, "submitted_at"),
        "filled_at": _value(order, "filled_at"),
        "group_id": _value(order, "group_id"),
        "group_role": _value(order, "group_role"),
        "limit_price": _value(order, "limit_price"),
        "stop_price": _value(order, "stop_price"),
    }
    role = _plain(fields["group_role"])
    role = str(role).strip().lower() if role is not None else ""
    order_type = _plain(fields["order_type"])
    order_type = str(order_type).strip().lower() if order_type is not None else ""

    if role not in {"entry", "take_profit", "stop_loss"}:
        if role_hint in {"entry", *EXIT_ROLES}:
            role = role_hint
        elif "stop" in order_type:
            role = "stop_loss"
        elif order_type == "limit":
            role = "take_profit"
        else:
            role = "unknown"

    snapshot = {
        key: _plain(value)
        for key, value in fields.items()
        if value is not None
    }
    if snapshot.get("order_id") is not None:
        snapshot["order_id"] = str(snapshot["order_id"])
    if snapshot.get("client_order_id") is not None:
        snapshot["client_order_id"] = str(snapshot["client_order_id"])
    if snapshot.get("group_id") is not None:
        snapshot["group_id"] = str(snapshot["group_id"])
    if parent_order_id is not None:
        snapshot["parent_order_id"] = str(parent_order_id)
    snapshot["order_role"] = role
    return snapshot


class OrderLifecycleJournal:
    """Append-only JSONL journal for broker order and fill lifecycle events."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()

    def append(self, event_type: str, **payload: Any) -> dict[str, Any]:
        if not event_type or not isinstance(event_type, str):
            raise ValueError("event_type must be a non-empty string")
        event = {
            "schema_version": 1,
            "event_id": str(uuid4()),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            **payload,
        }
        encoded = json.dumps(event, ensure_ascii=False, default=_plain, separators=(",", ":"))
        with self._lock:
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(encoded + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        return event

    def read_events(self) -> list[dict[str, Any]]:
        with self._lock:
            if not self.path.exists():
                return []
            content = self.path.read_text(encoding="utf-8")
        if not content:
            return []
        lines = content.splitlines(keepends=True)
        events: list[dict[str, Any]] = []
        for index, line in enumerate(lines):
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                # A process may stop mid-write. Ignore only an incomplete final line;
                # malformed complete records are corruption and must not be hidden.
                if index == len(lines) - 1 and not line.endswith("\n"):
                    break
                raise ValueError(
                    f"Order journal contains invalid JSON at line {index + 1}"
                ) from exc
            if not isinstance(event, dict) or event.get("schema_version") != 1:
                raise ValueError(f"Order journal has an unsupported event at line {index + 1}")
            events.append(event)
        return events

    @staticmethod
    def _event_time(event: dict[str, Any]) -> datetime | None:
        raw = (
            event.get("transaction_time")
            or event.get("created_at")
            or event.get("date")
            or event.get("recorded_at")
        )
        if raw is None:
            return None
        if isinstance(raw, datetime):
            value = raw
        else:
            text = str(raw).strip()
            try:
                value = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                try:
                    value = datetime.fromisoformat(text[:10]).replace(tzinfo=timezone.utc)
                except ValueError:
                    return None
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def classify_exit(self, symbol: str) -> str:
        """Classify the most recent exit only when it follows the latest known entry."""
        normalized = str(symbol).strip().upper()
        order_roles: dict[str, str] = {}
        events = self.read_events()
        for event in events:
            event_type = event.get("event_type")
            if event_type in {"entry_order_submitted", "entry_order_recovered"}:
                for snapshot in [event.get("parent_order"), *(event.get("child_orders") or [])]:
                    if isinstance(snapshot, dict) and snapshot.get("order_id"):
                        order_roles[str(snapshot["order_id"])] = str(snapshot.get("order_role", "unknown"))
            elif event_type == "order_state":
                snapshot = event.get("order")
                if isinstance(snapshot, dict) and snapshot.get("order_id"):
                    order_roles[str(snapshot["order_id"])] = str(snapshot.get("order_role", "unknown"))
            elif event_type in {"eod_close_order_submitted", "flatten_close_order_submitted"}:
                if event.get("order_id"):
                    order_roles[str(event["order_id"])] = str(
                        event.get("order_role")
                        or ("eod_liquidation" if event_type == "eod_close_order_submitted" else "emergency_cleanup")
                    )

        latest_entry_time: datetime | None = None
        exits: list[tuple[datetime, str]] = []
        for event in events:
            if event.get("event_type") != "fill":
                continue
            if str(event.get("symbol", "")).strip().upper() != normalized:
                continue
            event_time = self._event_time(event)
            if event_time is None:
                continue
            role = order_roles.get(str(event.get("order_id", "")), "unknown")
            if role == "entry":
                if latest_entry_time is None or event_time > latest_entry_time:
                    latest_entry_time = event_time
            elif role in EXIT_ROLES:
                exits.append((event_time, role))

        if not exits:
            return "external_or_unattributed"
        exits.sort(key=lambda item: item[0])
        latest_exit_time, latest_exit_role = exits[-1]
        if latest_entry_time is not None and latest_exit_time <= latest_entry_time:
            return "external_or_unattributed"
        return latest_exit_role

    def summarize_session(
        self,
        start_utc: datetime,
        end_utc: datetime,
    ) -> dict[str, Any]:
        """Compute matched-fill gross P&L; report broker fee activities separately."""
        if start_utc.tzinfo is None or start_utc.utcoffset() is None:
            raise ValueError("start_utc must be timezone-aware")
        if end_utc.tzinfo is None or end_utc.utcoffset() is None:
            raise ValueError("end_utc must be timezone-aware")
        start = start_utc.astimezone(timezone.utc)
        end = end_utc.astimezone(timezone.utc)
        if end < start:
            raise ValueError("end_utc must not precede start_utc")

        events = self.read_events()
        order_roles: dict[str, str] = {}
        for event in events:
            event_type = event.get("event_type")
            if event_type in {"entry_order_submitted", "entry_order_recovered"}:
                for snapshot in [event.get("parent_order"), *(event.get("child_orders") or [])]:
                    if isinstance(snapshot, dict) and snapshot.get("order_id"):
                        order_roles[str(snapshot["order_id"])] = str(snapshot.get("order_role", "unknown"))
            elif event_type == "order_state":
                snapshot = event.get("order")
                if isinstance(snapshot, dict) and snapshot.get("order_id"):
                    order_roles[str(snapshot["order_id"])] = str(snapshot.get("order_role", "unknown"))
            elif event_type in {"eod_close_order_submitted", "flatten_close_order_submitted"} and event.get("order_id"):
                order_roles[str(event["order_id"])] = str(
                    event.get("order_role")
                    or ("eod_liquidation" if event_type == "eod_close_order_submitted" else "emergency_cleanup")
                )

        unique_fills: dict[str, dict[str, Any]] = {}
        for event in events:
            if event.get("event_type") != "fill":
                continue
            fill_id = str(event.get("fill_id", "")).strip()
            if fill_id:
                unique_fills[fill_id] = event

        fills = []
        unattributed_fill_count = 0
        for event in unique_fills.values():
            order_id = str(event.get("order_id", ""))
            role = order_roles.get(order_id, "unknown")
            fill_time = self._event_time(event)
            if fill_time is None:
                continue
            if role not in {"entry", *EXIT_ROLES}:
                if start <= fill_time <= end:
                    unattributed_fill_count += 1
                continue
            fills.append((fill_time, event, role))
        fills.sort(key=lambda item: item[0])

        inventory: dict[str, list[dict[str, Any]]] = {}
        realized = Decimal("0")
        matched_exit_quantity = Decimal("0")
        unmatched_exit_quantity = Decimal("0")
        closing_fill_count = 0
        session_fill_count = 0

        for fill_time, event, role in fills:
            symbol = str(event.get("symbol", "")).strip().upper()
            side = str(event.get("side", "")).strip().lower()
            quantity = abs(_decimal(event.get("qty")))
            price = _decimal(event.get("price"))
            if not symbol or side not in {"buy", "sell"} or quantity <= 0 or price <= 0:
                continue
            if start <= fill_time <= end:
                session_fill_count += 1

            signed_quantity = quantity if side == "buy" else -quantity
            lots = inventory.setdefault(symbol, [])
            if role == "entry":
                lots.append({
                    "signed_quantity": signed_quantity,
                    "entry_price": price,
                })
                continue
            if role not in EXIT_ROLES:
                continue

            if start <= fill_time <= end:
                closing_fill_count += 1
            remaining = quantity
            while remaining > 0:
                matching_index = next(
                    (
                        index for index, lot in enumerate(lots)
                        if lot["signed_quantity"] * signed_quantity < 0
                    ),
                    None,
                )
                if matching_index is None:
                    if start <= fill_time <= end:
                        unmatched_exit_quantity += remaining
                    break

                lot = lots[matching_index]
                open_quantity = abs(lot["signed_quantity"])
                matched = min(remaining, open_quantity)
                if lot["signed_quantity"] > 0:
                    trade_pnl = (price - lot["entry_price"]) * matched
                else:
                    trade_pnl = (lot["entry_price"] - price) * matched
                if start <= fill_time <= end:
                    realized += trade_pnl
                    matched_exit_quantity += matched

                lot["signed_quantity"] += matched if lot["signed_quantity"] < 0 else -matched
                remaining -= matched
                if lot["signed_quantity"] == 0:
                    lots.pop(matching_index)

        fee_events = []
        fee_net_amount = Decimal("0")
        for event in events:
            if event.get("event_type") != "fee_activity":
                continue
            event_time = self._event_time(event)
            if event_time is None or not (start <= event_time <= end):
                continue
            fee_events.append(event)
            fee_net_amount += _decimal(event.get("net_amount"))

        open_entry_quantity = sum(
            abs(lot["signed_quantity"])
            for symbol_lots in inventory.values()
            for lot in symbol_lots
        )
        pnl_coverage_status = (
            "complete_for_recorded_closed_trades"
            if (
                unattributed_fill_count == 0
                and unmatched_exit_quantity == 0
                and open_entry_quantity == 0
                and fills
            )
            else "open_or_unmatched_records"
        )

        return {
            "realized_gross_pnl_usd": round(float(realized), 2),
            "matched_exit_quantity": float(matched_exit_quantity),
            "unmatched_exit_quantity": float(unmatched_exit_quantity),
            "open_entry_quantity_after_session": float(open_entry_quantity),
            "unattributed_fill_count": unattributed_fill_count,
            "closing_fill_count": closing_fill_count,
            "fill_count_in_session": session_fill_count,
            "pnl_coverage_status": pnl_coverage_status,
            "fee_activity_net_amount_usd": (
                round(float(fee_net_amount), 2) if fee_events else None
            ),
            "fee_activity_count": len(fee_events),
            "net_pnl_usd": None,
            "net_pnl_status": "not_calculated",
        }
