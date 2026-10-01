from __future__ import annotations

from dataclasses import asdict
from typing import Any, Callable, Protocol

from money_mintter.storage.state import RuntimeSnapshot


class Connection(Protocol):
    def cursor(self): ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...


class PostgresSnapshotStore:
    """PostgreSQL persistence boundary using a supplied DB-API connection.

    psycopg/asyncpg are deliberately kept out of the core package. Production
    deployment can inject either a synchronous DB-API connection wrapper or a
    small adapter around an async pool.
    """

    def __init__(self, connection_factory: Callable[[], Connection]) -> None:
        self._connection_factory = connection_factory

    def initialize(self) -> None:
        connection = self._connection_factory()
        cursor = connection.cursor()
        try:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_snapshots (
                    id BIGSERIAL PRIMARY KEY,
                    captured_at TIMESTAMPTZ NOT NULL,
                    schema_version INTEGER NOT NULL,
                    snapshot JSONB NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_events (
                    id BIGSERIAL PRIMARY KEY,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    event_type TEXT NOT NULL,
                    payload JSONB NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_runtime_snapshots_captured_at
                ON runtime_snapshots (captured_at DESC)
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_runtime_events_created_at
                ON runtime_events (created_at DESC)
                """
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()

    def save_snapshot(self, snapshot: RuntimeSnapshot) -> None:
        connection = self._connection_factory()
        cursor = connection.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO runtime_snapshots (captured_at, schema_version, snapshot)
                VALUES (%s, %s, %s::jsonb)
                """,
                (snapshot.captured_at, snapshot.schema_version, snapshot.to_json()),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()

    def load_latest(self) -> RuntimeSnapshot | None:
        connection = self._connection_factory()
        cursor = connection.cursor()
        try:
            cursor.execute(
                """
                SELECT snapshot
                FROM runtime_snapshots
                ORDER BY captured_at DESC, id DESC
                LIMIT 1
                """
            )
            row = cursor.fetchone()
            if row is None:
                return None
            payload: Any = row[0]
            if isinstance(payload, str):
                return RuntimeSnapshot.from_json(payload)
            if isinstance(payload, dict):
                return RuntimeSnapshot.from_dict(payload)
            raise ValueError("unsupported JSONB payload returned by PostgreSQL")
        finally:
            cursor.close()

    def append_event(self, event_type: str, payload: dict[str, Any]) -> None:
        if not event_type or not event_type.strip():
            raise ValueError("event type cannot be empty")
        connection = self._connection_factory()
        cursor = connection.cursor()
        try:
            import json

            cursor.execute(
                """
                INSERT INTO runtime_events (event_type, payload)
                VALUES (%s, %s::jsonb)
                """,
                (event_type, json.dumps(payload, sort_keys=True)),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
