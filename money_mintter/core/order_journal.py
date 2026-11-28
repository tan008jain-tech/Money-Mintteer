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

# Alpaca TradeActivitySide is distinct from OrderSide. These values are returned
# by /account/activities/FILL and must be normalized to their signed share effect.
# Ambiguous activity sides (for example "undisclosed" and "cross") deliberately
# remain unsupported so inventory reconciliation fails closed rather than guessing.
TRADE_ACTIVITY_SIDE_SIGNS = {
    "buy": Decimal("1"),
    "buy_minus": Decimal("1"),
    "sell": Decimal("-1"),
    "sell_plus": Decimal("-1"),
    "sell_short": Decimal("-1"),
    "sell_short_exempt": Decimal("-1"),
    "cross_short": Decimal("-1"),
}
MONEY_MINTTER_CLIENT_ORDER_PREFIX = "mm-ict-"


def _value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _plain(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    # Alpaca's enums inherit from str, so extract Enum.value before the
    # primitive-string check. Otherwise str(enum_member) is "OrderType.LIMIT".
    if hasattr(value, "value"):
        return value.value
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _normalized_text(value: Any) -> str:
    plain = _plain(value)
    return str(plain).strip().lower() if plain is not None else ""


def _infer_bracket_child_role(parent: dict[str, Any], child: dict[str, Any]) -> str:
    """Recover a missing role only from a fully linked, structurally valid bracket leg."""
    parent_id = str(parent.get("order_id") or "").strip()
    child_parent_id = str(child.get("parent_order_id") or "").strip()
    if not parent_id or child_parent_id != parent_id:
        return "unknown"

    if (
        _normalized_text(parent.get("order_class")) != "bracket"
        or _normalized_text(child.get("order_class")) != "bracket"
    ):
        return "unknown"

    parent_symbol = _normalized_text(parent.get("symbol"))
    child_symbol = _normalized_text(child.get("symbol"))
    if not parent_symbol or child_symbol != parent_symbol:
        return "unknown"

    parent_side = _normalized_text(parent.get("side"))
    child_side = _normalized_text(child.get("side"))
    expected_child_side = {"buy": "sell", "sell": "buy"}.get(parent_side)
    if expected_child_side is None or child_side != expected_child_side:
        return "unknown"

    child_type = _normalized_text(child.get("order_type"))
    limit_price = child.get("limit_price")
    stop_price = child.get("stop_price")
    if child_type == "limit" and limit_price is not None and stop_price is None:
        return "take_profit"
    if child_type == "stop" and stop_price is not None and limit_price is None:
        return "stop_loss"
    return "unknown"


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
        "time_in_force": _value(order, "time_in_force"),
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


    def owned_order_records(
        self,
        client_order_prefix: str = MONEY_MINTTER_CLIENT_ORDER_PREFIX,
    ) -> dict[str, dict[str, Any]]:
        """Return DAY-only broker order IDs proven to originate from Money Mintter.

        Entry ownership is anchored to the persisted MM client-order-ID prefix.
        Bracket legs inherit ownership and time-in-force from their parent. Older
        MM ICT journal records without a TIF field are DAY: the submitter has
        always created these prefixed orders with TimeInForce.DAY.
        """
        records: dict[str, dict[str, Any]] = {}
        excluded_client_ids: set[str] = set()

        for event in self.read_events():
            event_type = event.get("event_type")
            if event_type in {"entry_order_submitted", "entry_order_recovered"}:
                parent = event.get("parent_order") or {}
                client_order_id = (
                    event.get("client_order_id")
                    or (parent.get("client_order_id") if isinstance(parent, dict) else None)
                )
                if (
                    not isinstance(client_order_id, str)
                    or not client_order_id.startswith(client_order_prefix)
                ):
                    continue

                # Old prefixed ICT entries came exclusively from a DAY submitter.
                parent_tif = str(parent.get("time_in_force") or "day").strip().lower()
                if parent_tif != "day":
                    excluded_client_ids.add(client_order_id)
                    continue

                children = event.get("child_orders") or []
                for index, snapshot in enumerate([parent, *children]):
                    if not isinstance(snapshot, dict) or not snapshot.get("order_id"):
                        continue
                    order_id = str(snapshot["order_id"])
                    snapshot_tif = str(snapshot.get("time_in_force") or parent_tif).strip().lower()
                    if snapshot_tif != "day":
                        excluded_client_ids.add(client_order_id)
                    role = "entry" if index == 0 else str(snapshot.get("order_role") or "unknown")
                    if index > 0 and role == "unknown":
                        # Older journals persisted raw order_type/parent linkage but
                        # recorded take-profit legs as unknown. Recover only when both
                        # snapshots prove a linked DAY bracket with opposing sides.
                        role = _infer_bracket_child_role(parent, snapshot)
                    records[order_id] = {
                        **records.get(order_id, {}),
                        **snapshot,
                        "order_id": order_id,
                        "order_role": role,
                        "time_in_force": snapshot_tif,
                        "client_order_id": client_order_id,
                    }

            elif event_type == "order_state":
                snapshot = event.get("order")
                if not isinstance(snapshot, dict) or not snapshot.get("order_id"):
                    continue
                order_id = str(snapshot["order_id"])
                if order_id not in records:
                    continue
                previous = records[order_id]
                previous_role = previous.get("order_role", "unknown")
                reported_role = snapshot.get("order_role")
                time_in_force = str(
                    snapshot.get("time_in_force")
                    or previous.get("time_in_force")
                    or "day"
                ).strip().lower()
                client_order_id = previous.get("client_order_id") or snapshot.get("client_order_id")
                if time_in_force != "day" and client_order_id:
                    excluded_client_ids.add(str(client_order_id))
                records[order_id] = {
                    **previous,
                    **snapshot,
                    "order_id": order_id,
                    "order_role": (
                        reported_role
                        if reported_role not in (None, "", "unknown")
                        else previous_role
                    ),
                    "time_in_force": time_in_force,
                    "client_order_id": client_order_id,
                }

            elif event_type in {"eod_close_order_submitted", "flatten_close_order_submitted"}:
                # Older releases journaled account-wide close orders without an
                # ownership marker. Never adopt those orders as MM inventory.
                if event.get("owner") != "money_mintter":
                    continue
                time_in_force = str(event.get("time_in_force") or "day").strip().lower()
                if time_in_force != "day":
                    continue
                raw_order_id = event.get("order_id")
                if not raw_order_id:
                    continue
                order_id = str(raw_order_id)
                records[order_id] = {
                    **records.get(order_id, {}),
                    "order_id": order_id,
                    "symbol": event.get("symbol") or records.get(order_id, {}).get("symbol"),
                    "order_role": str(
                        event.get("order_role")
                        or (
                            "eod_liquidation"
                            if event_type == "eod_close_order_submitted"
                            else "emergency_cleanup"
                        )
                    ),
                    "status": event.get("broker_status") or records.get(order_id, {}).get("status", "unknown"),
                    "time_in_force": time_in_force,
                    "filled_qty": records.get(order_id, {}).get("filled_qty", "0"),
                }

        return {
            order_id: record
            for order_id, record in records.items()
            if record.get("client_order_id") not in excluded_client_ids
            and str(record.get("time_in_force") or "day").strip().lower() == "day"
        }

    def unowned_legacy_close_order_ids(self) -> list[str]:
        """Find unresolved close orders from pre-ownership-aware cleanup versions."""
        terminal = {"filled", "canceled", "expired", "rejected", "replaced", "done_for_day"}
        status_by_id: dict[str, str] = {}
        legacy_ids: set[str] = set()
        for event in self.read_events():
            event_type = event.get("event_type")
            if event_type in {"eod_close_order_submitted", "flatten_close_order_submitted"}:
                order_id = str(event.get("order_id") or "")
                if order_id and event.get("owner") != "money_mintter":
                    legacy_ids.add(order_id)
                    status_by_id[order_id] = str(event.get("broker_status") or "unknown").strip().lower()
            elif event_type == "order_state":
                snapshot = event.get("order")
                if isinstance(snapshot, dict) and snapshot.get("order_id"):
                    order_id = str(snapshot["order_id"])
                    if order_id in legacy_ids:
                        status_by_id[order_id] = str(snapshot.get("status") or "unknown").strip().lower()
        return sorted(order_id for order_id in legacy_ids if status_by_id.get(order_id) not in terminal)

    def unresolved_entry_intent_ids(
        self,
        client_order_prefix: str = MONEY_MINTTER_CLIENT_ORDER_PREFIX,
    ) -> set[str]:
        """Return MM entry intents without a verified broker outcome.

        A persisted intent is resolved only by a definitive broker rejection or by
        a submitted/recovered parent with at least two identifiable bracket legs.
        Incomplete broker responses must remain pending across process restarts.
        """
        intents: set[str] = set()
        resolved: set[str] = set()
        for event in self.read_events():
            client_order_id = event.get("client_order_id")
            if (
                not isinstance(client_order_id, str)
                or not client_order_id.startswith(client_order_prefix)
            ):
                continue

            event_type = event.get("event_type")
            if event_type == "entry_intent":
                intents.add(client_order_id)
            elif event_type == "entry_submission_rejected":
                resolved.add(client_order_id)
            elif event_type in {"entry_order_submitted", "entry_order_recovered"}:
                parent = event.get("parent_order")
                children = event.get("child_orders")
                if (
                    isinstance(parent, dict)
                    and parent.get("order_id")
                    and isinstance(children, list)
                    and len(children) >= 2
                    and all(
                        isinstance(child, dict) and child.get("order_id")
                        for child in children
                    )
                ):
                    resolved.add(client_order_id)

        return intents - resolved

    def has_unresolved_entry_intents(
        self,
        client_order_prefix: str = MONEY_MINTTER_CLIENT_ORDER_PREFIX,
    ) -> bool:
        """Return True if an MM entry intent has no verified broker outcome."""
        return bool(self.unresolved_entry_intent_ids(client_order_prefix))

    def owned_position_quantities(
        self,
        client_order_prefix: str = MONEY_MINTTER_CLIENT_ORDER_PREFIX,
    ) -> dict[str, Decimal]:
        """Return signed net filled quantities for MM-owned orders only.

        Refuse to guess when a fill is malformed or broker-filled quantity exceeds
        the fill activity persisted in this journal.
        """
        records = self.owned_order_records(client_order_prefix)
        roles = {
            order_id: str(record.get("order_role", "unknown"))
            for order_id, record in records.items()
        }
        recognized_roles = {"entry", *EXIT_ROLES}
        inventory: dict[str, Decimal] = {}
        observed_by_order: dict[str, Decimal] = {}
        seen_fill_ids: set[str] = set()

        for event in self.read_events():
            if event.get("event_type") != "fill":
                continue
            order_id = str(event.get("order_id", ""))
            if order_id not in records:
                continue
            if roles.get(order_id, "unknown") not in recognized_roles:
                raise ValueError(f"Money Mintter fill has an unknown order role: {order_id}")

            fill_id = str(event.get("fill_id", "")).strip()
            if not fill_id:
                raise ValueError(f"Money Mintter fill has no fill_id: {order_id}")
            if fill_id in seen_fill_ids:
                continue
            seen_fill_ids.add(fill_id)

            symbol = str(event.get("symbol") or records[order_id].get("symbol") or "").strip().upper()
            side = str(event.get("side", "")).strip().lower()
            quantity = _decimal(event.get("qty"))
            side_sign = TRADE_ACTIVITY_SIDE_SIGNS.get(side)
            if (
                not symbol
                or side_sign is None
                or not quantity.is_finite()
                or quantity <= 0
            ):
                raise ValueError(
                    f"Money Mintter fill is incomplete, invalid, or has an unsupported "
                    f"Alpaca activity side {side!r}: {order_id}"
                )

            signed_quantity = quantity * side_sign
            inventory[symbol] = inventory.get(symbol, Decimal("0")) + signed_quantity
            observed_by_order[order_id] = observed_by_order.get(order_id, Decimal("0")) + quantity

        tolerance = Decimal("0.00000001")
        for order_id, record in records.items():
            expected_filled = _decimal(record.get("filled_qty") or "0")
            if not expected_filled.is_finite() or expected_filled < 0:
                raise ValueError(f"Invalid broker filled quantity for Money Mintter order {order_id}")
            observed_filled = observed_by_order.get(order_id, Decimal("0"))
            if expected_filled > observed_filled + tolerance:
                raise ValueError(
                    f"Money Mintter fills are not fully reconciled for order {order_id}: "
                    f"broker={expected_filled}, journal={observed_filled}"
                )

        return {
            symbol: quantity
            for symbol, quantity in inventory.items()
            if abs(quantity) > tolerance
        }

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
                # Without a trustworthy timestamp, the fill cannot be assigned to
                # the reporting window or safely excluded from it.
                unattributed_fill_count += 1
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
            side_sign = TRADE_ACTIVITY_SIDE_SIGNS.get(side)
            if (
                not symbol
                or side_sign is None
                or not quantity.is_finite()
                or quantity <= 0
                or not price.is_finite()
                or price <= 0
            ):
                # Do not silently omit a broker-reported fill with an unknown side.
                # Its direction is required to calculate matched P&L safely.
                if start <= fill_time <= end:
                    unattributed_fill_count += 1
                continue
            if start <= fill_time <= end:
                session_fill_count += 1

            signed_quantity = quantity * side_sign
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
