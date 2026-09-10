"""Durable, leased delivery of local audit events to the remote audit store.

The delivery itself is technical infrastructure and deliberately does not emit
another business audit event.  Remote idempotency is the immutable audit_id.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import sqlite3
import threading
import time
import uuid
from typing import Any, Callable

from internal_audit import sanitize_audit_text


PENDING = "PENDING"
LEASED = "LEASED"
RETRY = "RETRY"
DELIVERED = "DELIVERED"
OUTBOX_STATUSES = frozenset({PENDING, LEASED, RETRY, DELIVERED})

SYSTEM_AUDIT_DELIVERY_ACTOR_ID = "10000000-0000-4000-8000-000000000008"
DEFAULT_LEASE_SECONDS = 60.0
BASE_BACKOFF_SECONDS = 5.0
MAX_BACKOFF_SECONDS = 3600.0


class LeaseLost(RuntimeError):
    pass


_connection_factory: Callable[[], sqlite3.Connection] | None = None
_remote_sender: Callable[[dict[str, Any]], Any] | None = None
_configuration_lock = threading.Lock()
_worker_lock = threading.Lock()
_worker_thread: threading.Thread | None = None
_stop_event = threading.Event()
_worker_last_error: str | None = None
_worker_last_error_at: str | None = None


def configure(
    connection_factory: Callable[[], sqlite3.Connection],
    remote_sender: Callable[[dict[str, Any]], Any] | None = None,
) -> None:
    if not callable(connection_factory):
        raise TypeError("connection_factory musi być wywoływalne")
    global _connection_factory, _remote_sender
    with _configuration_lock:
        _connection_factory = connection_factory
        if remote_sender is not None:
            _remote_sender = remote_sender


def configure_remote_sender(remote_sender: Callable[[dict[str, Any]], Any]) -> None:
    if not callable(remote_sender):
        raise TypeError("remote_sender musi być wywoływalne")
    global _remote_sender
    with _configuration_lock:
        _remote_sender = remote_sender


def initialize_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS internal_audit_outbox(
            audit_id TEXT PRIMARY KEY
                REFERENCES internal_audit_log(audit_id) ON DELETE RESTRICT,
            status TEXT NOT NULL DEFAULT 'PENDING'
                CHECK(status IN ('PENDING','LEASED','RETRY','DELIVERED')),
            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
            next_attempt_at REAL NOT NULL DEFAULT 0,
            last_attempt_at REAL,
            last_error TEXT,
            created_at TEXT NOT NULL,
            delivered_at TEXT,
            lease_owner TEXT,
            lease_until REAL NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_internal_audit_outbox_due
            ON internal_audit_outbox(status, next_attempt_at, lease_until, created_at);
        CREATE INDEX IF NOT EXISTS idx_internal_audit_outbox_delivered
            ON internal_audit_outbox(delivered_at);
        """
    )
    db.commit()


def _factory() -> Callable[[], sqlite3.Connection]:
    if _connection_factory is None:
        raise RuntimeError("Audit outbox nie został skonfigurowany")
    return _connection_factory


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def retry_delay(attempt_count: int) -> float:
    exponent = max(0, min(int(attempt_count) - 1, 20))
    return min(BASE_BACKOFF_SECONDS * (2 ** exponent), MAX_BACKOFF_SECONDS)


def claim_one(
    worker_id: str,
    *,
    now: float | None = None,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
) -> dict[str, Any] | None:
    """Atomically lease one due row; an expired lease is reclaimable."""
    current = time.time() if now is None else float(now)
    db = _factory()()
    try:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            """SELECT audit_id FROM internal_audit_outbox
               WHERE status <> 'DELIVERED'
                 AND next_attempt_at <= ?
                 AND lease_until <= ?
               ORDER BY created_at, audit_id
               LIMIT 1""",
            (current, current),
        ).fetchone()
        if row is None:
            db.rollback()
            return None
        audit_id = row[0]
        cursor = db.execute(
            """UPDATE internal_audit_outbox
               SET status='LEASED',attempt_count=attempt_count+1,
                   last_attempt_at=?,lease_owner=?,lease_until=?
               WHERE audit_id=? AND status <> 'DELIVERED' AND lease_until <= ?""",
            (current, worker_id, current + float(lease_seconds), audit_id, current),
        )
        if cursor.rowcount != 1:
            db.rollback()
            return None
        joined = db.execute(
            """SELECT o.*,a.* FROM internal_audit_outbox o
               JOIN internal_audit_log a ON a.audit_id=o.audit_id
               WHERE o.audit_id=?""",
            (audit_id,),
        ).fetchone()
        db.commit()
        return dict(joined)
    finally:
        db.close()


def _json_or_none(value: Any) -> Any:
    if value in (None, ""):
        return None
    if isinstance(value, (dict, list)):
        return value
    return json.loads(value)


def remote_payload(claimed: dict[str, Any]) -> dict[str, Any]:
    """Map local SQLite types to the Supabase audit schema."""
    audit_columns = (
        "audit_id", "occurred_at", "actor_id", "actor_type", "actor_display_name",
        "permission", "operation", "operation_version", "audit_policy", "entity_type",
        "entity_id", "request_id", "correlation_id", "session_id", "credential_id",
        "risk_level", "reason", "approval_id", "approved_by", "result", "error_code",
        "error_message", "external_integration", "external_request_id", "external_result",
        "idempotency_key", "entity_version_before", "entity_version_after", "source",
        "expected_version", "current_version",
    )
    payload = {key: claimed.get(key) for key in audit_columns}
    payload["roles_json"] = _json_or_none(claimed.get("roles_json")) or []
    payload["before_state"] = _json_or_none(claimed.get("before_state"))
    payload["after_state"] = _json_or_none(claimed.get("after_state"))
    payload["approval_required"] = bool(claimed.get("approval_required"))
    payload["is_replay"] = bool(claimed.get("is_replay"))
    return payload


def mark_delivered(
    audit_id: str,
    worker_id: str,
    *,
    delivered_at: str | None = None,
) -> None:
    db = _factory()()
    try:
        cursor = db.execute(
            """UPDATE internal_audit_outbox
               SET status='DELIVERED',delivered_at=?,last_error=NULL,
                   lease_owner=NULL,lease_until=0,next_attempt_at=0
               WHERE audit_id=? AND status='LEASED' AND lease_owner=?""",
            (delivered_at or _utc_now(), audit_id, worker_id),
        )
        if cursor.rowcount != 1:
            db.rollback()
            raise LeaseLost(f"Utracono lease audytu {audit_id}")
        db.commit()
    finally:
        db.close()


def mark_retry(
    claimed: dict[str, Any],
    worker_id: str,
    error: Exception | str,
    *,
    now: float | None = None,
) -> None:
    current = time.time() if now is None else float(now)
    attempts = int(claimed.get("attempt_count") or 1)
    db = _factory()()
    try:
        cursor = db.execute(
            """UPDATE internal_audit_outbox
               SET status='RETRY',next_attempt_at=?,last_error=?,
                   lease_owner=NULL,lease_until=0
               WHERE audit_id=? AND status='LEASED' AND lease_owner=?""",
            (
                current + retry_delay(attempts),
                sanitize_audit_text(error),
                claimed["audit_id"],
                worker_id,
            ),
        )
        if cursor.rowcount != 1:
            db.rollback()
            raise LeaseLost(f"Utracono lease audytu {claimed['audit_id']}")
        db.commit()
    finally:
        db.close()


def deliver_claimed(
    claimed: dict[str, Any],
    worker_id: str,
    remote_sender: Callable[[dict[str, Any]], Any] | None = None,
    *,
    now: float | None = None,
) -> bool:
    sender = remote_sender or _remote_sender
    if sender is None:
        mark_retry(claimed, worker_id, "Supabase audit sender is not configured", now=now)
        return False
    try:
        sender(remote_payload(claimed))
        mark_delivered(claimed["audit_id"], worker_id)
        return True
    except Exception as exc:
        mark_retry(claimed, worker_id, exc, now=now)
        return False


def process_one(
    *,
    worker_id: str = "",
    remote_sender: Callable[[dict[str, Any]], Any] | None = None,
    now: float | None = None,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
) -> bool | None:
    worker_id = worker_id or f"{SYSTEM_AUDIT_DELIVERY_ACTOR_ID}:{uuid.uuid4()}"
    claimed = claim_one(worker_id, now=now, lease_seconds=lease_seconds)
    if claimed is None:
        return None
    return deliver_claimed(claimed, worker_id, remote_sender, now=now)


def process_due(
    *,
    worker_id: str = "",
    remote_sender: Callable[[dict[str, Any]], Any] | None = None,
    max_items: int = 50,
) -> dict[str, int]:
    worker_id = worker_id or f"{SYSTEM_AUDIT_DELIVERY_ACTOR_ID}:{uuid.uuid4()}"
    result = {"delivered": 0, "failed": 0}
    for _ in range(max(1, min(int(max_items), 500))):
        outcome = process_one(worker_id=worker_id, remote_sender=remote_sender)
        if outcome is None:
            break
        result["delivered" if outcome else "failed"] += 1
    return result


def queue_health(*, now: float | None = None) -> dict[str, Any]:
    current = time.time() if now is None else float(now)
    db = _factory()()
    try:
        counts = {
            row["status"].lower(): int(row["n"])
            for row in db.execute(
                "SELECT status,COUNT(*) AS n FROM internal_audit_outbox GROUP BY status"
            ).fetchall()
        }
        oldest = db.execute(
            """SELECT created_at FROM internal_audit_outbox
               WHERE status <> 'DELIVERED' ORDER BY created_at LIMIT 1"""
        ).fetchone()
        latest_success = db.execute(
            "SELECT MAX(delivered_at) FROM internal_audit_outbox"
        ).fetchone()[0]
        latest_error = db.execute(
            """SELECT last_error,last_attempt_at FROM internal_audit_outbox
               WHERE last_error IS NOT NULL ORDER BY last_attempt_at DESC LIMIT 1"""
        ).fetchone()
    finally:
        db.close()
    oldest_age = None
    if oldest:
        try:
            created = datetime.fromisoformat(oldest[0]).timestamp()
            oldest_age = max(0.0, current - created)
        except (TypeError, ValueError):
            oldest_age = None
    pending_total = sum(counts.get(status, 0) for status in ("pending", "retry", "leased"))
    return {
        "pending": pending_total,
        "new": counts.get("pending", 0),
        "retry": counts.get("retry", 0),
        "leased": counts.get("leased", 0),
        "delivered": counts.get("delivered", 0),
        "oldest_pending_age_seconds": oldest_age,
        "last_success_at": latest_success,
        "last_error": latest_error[0] if latest_error else None,
        "last_error_at": latest_error[1] if latest_error else None,
        "worker_running": bool(_worker_thread and _worker_thread.is_alive()),
        "worker_last_error": _worker_last_error,
        "worker_last_error_at": _worker_last_error_at,
    }


def start_worker(*, interval_seconds: float | None = None) -> bool:
    """Start one local daemon; database leasing coordinates multiple processes."""
    global _worker_thread
    if os.environ.get("AUDIT_OUTBOX_WORKER", "1").strip().lower() in {"0", "false", "no", "off"}:
        return False
    if _remote_sender is None:
        return False
    interval = float(interval_seconds or os.environ.get("AUDIT_OUTBOX_INTERVAL_SEC", "10"))
    with _worker_lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            return False
        _stop_event.clear()
        worker_id = f"{SYSTEM_AUDIT_DELIVERY_ACTOR_ID}:{os.getpid()}:{uuid.uuid4()}"

        def run() -> None:
            global _worker_last_error, _worker_last_error_at
            while not _stop_event.is_set():
                try:
                    process_due(worker_id=worker_id)
                except Exception as exc:
                    # A database or process-local failure must not terminate the
                    # daemon. Claimed rows remain recoverable after lease expiry.
                    _worker_last_error = sanitize_audit_text(exc)
                    _worker_last_error_at = _utc_now()
                _stop_event.wait(max(1.0, interval))

        _worker_thread = threading.Thread(target=run, name="audit-outbox", daemon=True)
        _worker_thread.start()
        return True


def stop_worker_for_tests() -> None:
    _stop_event.set()
