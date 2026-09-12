"""Isolated optimistic-concurrency pilot for future business operations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import sqlite3
import threading
import uuid
from typing import Any, Callable, Mapping

from internal_audit import CONFLICT, SUCCESS, record_audit_event


@dataclass(frozen=True)
class VersionedResource:
    resource_id: str
    namespace: str
    payload: dict[str, Any]
    version: int
    created_at: str
    updated_at: str


class OptimisticConcurrencyConflict(RuntimeError):
    def __init__(self, resource_id: str, expected_version: int, current_version: int | None):
        self.resource_id = resource_id
        self.expected_version = expected_version
        self.current_version = current_version
        super().__init__(
            f"Konflikt wersji {resource_id}: oczekiwano {expected_version}, aktualna {current_version}"
        )


_connection_factory: Callable[[], sqlite3.Connection] | None = None
_configuration_lock = threading.Lock()


def configure(connection_factory: Callable[[], sqlite3.Connection]) -> None:
    if not callable(connection_factory):
        raise TypeError("connection_factory musi być wywoływalne")
    global _connection_factory
    with _configuration_lock:
        _connection_factory = connection_factory


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def initialize_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS internal_versioned_resources(
            resource_id TEXT PRIMARY KEY,
            namespace TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_internal_versioned_resources_namespace
            ON internal_versioned_resources(namespace, updated_at);
        """
    )
    db.commit()


def _factory() -> Callable[[], sqlite3.Connection]:
    if _connection_factory is None:
        raise RuntimeError("Concurrency service nie został skonfigurowany")
    return _connection_factory


def _decode(row) -> VersionedResource:
    return VersionedResource(
        resource_id=row["resource_id"],
        namespace=row["namespace"],
        payload=json.loads(row["payload_json"]),
        version=int(row["version"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def get_versioned_resource(resource_id: str, *, transaction_connection=None) -> VersionedResource | None:
    db = transaction_connection or _factory()()
    try:
        row = db.execute(
            "SELECT * FROM internal_versioned_resources WHERE resource_id=?", (resource_id,)
        ).fetchone()
        return _decode(row) if row else None
    finally:
        if transaction_connection is None:
            db.close()


def create_versioned_resource(
    namespace: str,
    payload: Mapping[str, Any],
    *,
    actor_context=None,
    resource_id: str = "",
    correlation_id: str = "",
) -> VersionedResource:
    resource_id = resource_id or str(uuid.uuid4())
    now = _utc_now()
    normalized_payload = dict(payload)
    db = _factory()()
    try:
        db.execute(
            """INSERT INTO internal_versioned_resources(
                resource_id,namespace,payload_json,version,created_at,updated_at
            ) VALUES(?,?,?,1,?,?)""",
            (resource_id, namespace, json.dumps(normalized_payload, ensure_ascii=False), now, now),
        )
        db.commit()
    finally:
        db.close()
    resource = get_versioned_resource(resource_id)
    record_audit_event(
        "internal.versioned_resource.create",
        result=SUCCESS,
        actor_context=actor_context,
        entity_type="internal_versioned_resource",
        entity_id=resource_id,
        correlation_id=correlation_id,
        after_state=normalized_payload,
        entity_version_after=1,
    )
    return resource


def update_versioned_resource(
    resource_id: str,
    expected_version: int,
    payload: Mapping[str, Any],
    *,
    actor_context=None,
    correlation_id: str = "",
    transaction_connection=None,
) -> VersionedResource:
    """Atomically update only when the caller still owns the current version."""
    db = transaction_connection or _factory()()
    previous = None
    current_version = None
    new_version = expected_version + 1
    normalized_payload = dict(payload)
    try:
        if transaction_connection is None:
            db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT * FROM internal_versioned_resources WHERE resource_id=?", (resource_id,)
        ).fetchone()
        if row is None:
            db.rollback()
            raise KeyError(resource_id)
        previous = _decode(row)
        current_version = previous.version
        if current_version != expected_version:
            db.rollback()
        else:
            updated_at = _utc_now()
            cursor = db.execute(
                """UPDATE internal_versioned_resources
                   SET payload_json=?,version=?,updated_at=?
                   WHERE resource_id=? AND version=?""",
                (
                    json.dumps(normalized_payload, ensure_ascii=False),
                    new_version,
                    updated_at,
                    resource_id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                db.rollback()
                current = db.execute(
                    "SELECT version FROM internal_versioned_resources WHERE resource_id=?",
                    (resource_id,),
                ).fetchone()
                current_version = int(current[0]) if current else None
            else:
                if transaction_connection is None:
                    db.commit()
    finally:
        if transaction_connection is None:
            db.close()

    if current_version != expected_version:
        record_audit_event(
            "internal.versioned_resource.update",
            result=CONFLICT,
            actor_context=actor_context,
            entity_type="internal_versioned_resource",
            entity_id=resource_id,
            correlation_id=correlation_id,
            error_code="VERSION_CONFLICT",
            error_message="Oczekiwana wersja nie jest już aktualna",
            before_state=previous.payload if previous else None,
            after_state=normalized_payload,
            entity_version_before=current_version,
            entity_version_after=None,
            expected_version=expected_version,
            current_version=current_version,
        )
        raise OptimisticConcurrencyConflict(resource_id, expected_version, current_version)

    resource = get_versioned_resource(resource_id, transaction_connection=transaction_connection)
    record_audit_event(
        "internal.versioned_resource.update",
        result=SUCCESS,
        actor_context=actor_context,
        entity_type="internal_versioned_resource",
        entity_id=resource_id,
        correlation_id=correlation_id,
        before_state=previous.payload,
        after_state=normalized_payload,
        entity_version_before=expected_version,
        entity_version_after=new_version,
        transaction_connection=transaction_connection,
    )
    return resource
