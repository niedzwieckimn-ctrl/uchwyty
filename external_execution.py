"""Durable external execution and reconciliation foundation.

Only explicitly registered adapters are callable.  The bundled test adapter is
fully local and performs no network traffic.  UNKNOWN executions are never
submitted again; only reconciliation may resolve them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import sqlite3
import threading
import time
from typing import Any, Callable, Mapping, Protocol

import internal_approval as approvals
from internal_audit import FAILED, SUCCESS, record_audit_event, sanitize_audit_data, sanitize_audit_text
from internal_rbac import DENY, ActorContext, load_actor_context


QUEUED = "QUEUED"
LEASED = "LEASED"
SENDING = "SENDING"
SUCCEEDED = "SUCCEEDED"
FAILED_RETRYABLE = "FAILED_RETRYABLE"
FAILED_PERMANENT = "FAILED_PERMANENT"
UNKNOWN = "UNKNOWN"
RECONCILING = "RECONCILING"
RECONCILED_SUCCESS = "RECONCILED_SUCCESS"
RECONCILED_FAILED = "RECONCILED_FAILED"
TERMINAL = frozenset({SUCCEEDED, FAILED_PERMANENT, RECONCILED_SUCCESS, RECONCILED_FAILED})


@dataclass(frozen=True)
class ExternalResult:
    resource_id: str
    status: str = "created"
    request_id: str = ""


@dataclass(frozen=True)
class ReconciliationResult:
    outcome: str  # SUCCESS, FAILURE, UNKNOWN
    resource_id: str = ""
    status: str = ""
    safe_message: str = ""


class ExternalAdapter(Protocol):
    def prepare_request(self, snapshot: Mapping[str, Any], external_idempotency_key: str) -> Mapping[str, Any]: ...
    def execute(self, prepared: Mapping[str, Any], external_idempotency_key: str) -> ExternalResult: ...
    def classify_error(self, error: Exception) -> str: ...
    def reconcile(self, record: Mapping[str, Any]) -> ReconciliationResult: ...


class AdapterError(RuntimeError):
    classification = FAILED_PERMANENT


class RetryableBeforeSend(AdapterError):
    classification = FAILED_RETRYABLE


class RetryableExternalError(AdapterError):
    classification = FAILED_RETRYABLE


class PermanentExternalError(AdapterError):
    classification = FAILED_PERMANENT


class UnknownExternalOutcome(AdapterError):
    classification = UNKNOWN


class SimulatedWorkerCrash(BaseException):
    """Test-only abrupt stop; intentionally bypasses normal exception handling."""


class IsolatedTestAdapter:
    """Deterministic, in-memory fake. It never opens sockets or imports integrations."""

    def __init__(self):
        self._lock = threading.Lock()
        self.resources: dict[str, str] = {}
        self.prepare_calls: dict[str, int] = {}
        self.execute_calls: dict[str, int] = {}
        self.create_count = 0

    def prepare_request(self, snapshot, external_idempotency_key):
        scenario = str(snapshot.get("scenario", "success"))
        with self._lock:
            prepare_attempt = self.prepare_calls.get(external_idempotency_key, 0) + 1
            self.prepare_calls[external_idempotency_key] = prepare_attempt
        if scenario == "timeout_before_send":
            raise RetryableBeforeSend("Timeout przed wysłaniem")
        if scenario == "crash_before_send" and prepare_attempt == 1:
            raise SimulatedWorkerCrash("Awaria przed wysłaniem")
        return {
            "scenario": scenario,
            "value": str(snapshot.get("value", "")),
            "idempotency_key": external_idempotency_key,
        }

    def execute(self, prepared, external_idempotency_key):
        scenario = str(prepared["scenario"])
        with self._lock:
            attempt = self.execute_calls.get(external_idempotency_key, 0) + 1
            self.execute_calls[external_idempotency_key] = attempt
            existing = self.resources.get(external_idempotency_key)
            if existing:
                return ExternalResult(existing, "duplicate")
            if scenario == "http_400":
                raise PermanentExternalError("HTTP 400: odrzucone dane")
            if scenario == "secret_error":
                raise PermanentExternalError("Upstream api_token=super-secret-adapter-token")
            if scenario in {"rate_limit", "http_500"} and attempt == 1:
                raise RetryableExternalError("HTTP 429" if scenario == "rate_limit" else "HTTP 500")
            if scenario in {"remote_absent", "reconciliation_failure"}:
                raise UnknownExternalOutcome("Brak pewnego wyniku po wysłaniu")
            if scenario == "still_unknown":
                raise UnknownExternalOutcome("Wynik nadal nieznany")
            resource_id = "test-" + hashlib.sha256(external_idempotency_key.encode()).hexdigest()[:16]
            self.resources[external_idempotency_key] = resource_id
            self.create_count += 1
            if scenario in {"lost_response", "timeout_after_side_effect"}:
                raise UnknownExternalOutcome("Odpowiedź zaginęła po utworzeniu zasobu")
            if scenario == "crash_after_remote_success":
                raise SimulatedWorkerCrash("Awaria po utworzeniu zasobu")
            return ExternalResult(resource_id, "created")

    def classify_error(self, error):
        return getattr(error, "classification", FAILED_PERMANENT)

    def reconcile(self, record):
        snapshot = json.loads(record["safe_input_snapshot"])
        scenario = snapshot.get("scenario")
        key = record["external_idempotency_key"]
        resource_id = self.resources.get(key, "")
        if resource_id:
            return ReconciliationResult("SUCCESS", resource_id, "created")
        if scenario in {"remote_absent", "reconciliation_failure"}:
            return ReconciliationResult("FAILURE", safe_message="System zewnętrzny potwierdził brak operacji")
        return ReconciliationResult("UNKNOWN", safe_message="System zewnętrzny nie potwierdził wyniku")


_connection_factory: Callable[[], sqlite3.Connection] | None = None
_adapters: dict[str, ExternalAdapter] = {}
_config_lock = threading.Lock()


def configure(connection_factory: Callable[[], sqlite3.Connection]) -> None:
    global _connection_factory
    if not callable(connection_factory):
        raise TypeError("connection_factory musi być wywoływalne")
    with _config_lock:
        _connection_factory = connection_factory


def register_adapter(name: str, adapter: ExternalAdapter) -> None:
    if not name or name not in {"test"}:
        raise ValueError("Adapter nie znajduje się na zamkniętej allowliście")
    _adapters[name] = adapter


def _factory():
    if _connection_factory is None:
        raise RuntimeError("External execution nie zostało skonfigurowane")
    return _connection_factory


def _dt(value: datetime | None = None) -> datetime:
    return (value or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0)


def _iso(value: datetime | None = None) -> str:
    return _dt(value).isoformat()


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _backoff(attempt: int) -> int:
    return min(3600, 5 * (2 ** max(0, min(attempt - 1, 9))))


def initialize_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS internal_external_execution_queue(
            execution_id TEXT PRIMARY KEY REFERENCES internal_operation_executions(execution_id) ON DELETE CASCADE,
            operation TEXT NOT NULL,
            operation_version INTEGER NOT NULL CHECK(operation_version > 0),
            actor_id TEXT NOT NULL REFERENCES internal_actors(actor_id) ON DELETE RESTRICT,
            approval_id TEXT REFERENCES internal_approval_requests(approval_id) ON DELETE RESTRICT,
            idempotency_key TEXT NOT NULL,
            input_fingerprint TEXT NOT NULL,
            safe_input_snapshot TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('QUEUED','LEASED','SENDING','SUCCEEDED','FAILED_RETRYABLE','FAILED_PERMANENT','UNKNOWN','RECONCILING','RECONCILED_SUCCESS','RECONCILED_FAILED')),
            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
            next_attempt_at TEXT,
            last_attempt_at TEXT,
            lease_owner TEXT,
            lease_until TEXT,
            external_system TEXT NOT NULL,
            external_request_id TEXT,
            external_idempotency_key TEXT NOT NULL UNIQUE,
            external_resource_id TEXT,
            external_status TEXT,
            external_request_fingerprint TEXT,
            safe_external_request_snapshot TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL,
            started_at TEXT,
            send_started_at TEXT,
            completed_at TEXT,
            reconciliation_required INTEGER NOT NULL DEFAULT 0 CHECK(reconciliation_required IN (0,1)),
            reconciliation_attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(reconciliation_attempt_count >= 0),
            next_reconciliation_at TEXT,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_external_execution_ready
            ON internal_external_execution_queue(status,next_attempt_at,lease_until);
        CREATE INDEX IF NOT EXISTS idx_external_reconciliation_ready
            ON internal_external_execution_queue(reconciliation_required,status,next_reconciliation_at,lease_until);
        CREATE INDEX IF NOT EXISTS idx_external_execution_approval
            ON internal_external_execution_queue(approval_id);
        """
    )
    db.commit()


def _row(db, execution_id: str):
    return db.execute("SELECT * FROM internal_external_execution_queue WHERE execution_id=?", (execution_id,)).fetchone()


def _stable_external_key(execution_id: str, idempotency_key: str) -> str:
    digest = hashlib.sha256(f"external:v1:{execution_id}:{idempotency_key}".encode()).hexdigest()
    return f"ext_v1_{digest}"


def _audit(event: str, row, actor: ActorContext | None, result: str, *, error: str = "", db=None) -> None:
    record_audit_event(
        event, result=result, actor_context=actor, permission="internal.test.change_setting",
        entity_type="external_execution", entity_id=row["execution_id"],
        correlation_id=row["correlation_id"], approval_required=bool(row["approval_id"]),
        approval_id=row["approval_id"] or "", risk_level="YELLOW",
        error_message=error, idempotency_key=row["idempotency_key"],
        external_integration=row["external_system"],
        external_request_id=row["external_request_id"] or "",
        external_result=row["external_status"] or row["queue_status"],
        after_state={
            "execution_id": row["execution_id"], "execution_status": row["queue_status"],
            "target_operation": row["operation"], "target_operation_version": row["operation_version"],
            "external_system": row["external_system"],
            "external_request_id": row["external_request_id"] or "",
            "external_resource_id": row["external_resource_id"] or "",
        }, source="external_execution", transaction_connection=db,
    )


def _joined(db, execution_id: str):
    return db.execute(
        """SELECT q.*,q.status AS queue_status,e.correlation_id,e.request_id,e.entity_type,e.entity_id,
                  e.permission,e.risk_level,e.actor_type
           FROM internal_external_execution_queue q
           JOIN internal_operation_executions e ON e.execution_id=q.execution_id
           WHERE q.execution_id=?""", (execution_id,),
    ).fetchone()


def queue_execution(execution_id: str, actor: ActorContext, input_data: Mapping[str, Any], *, external_system="test"):
    if external_system not in _adapters:
        raise RuntimeError("Adapter zewnętrzny nie jest zarejestrowany")
    now = _iso()
    db = _factory()()
    try:
        db.execute("BEGIN IMMEDIATE")
        base = db.execute("SELECT * FROM internal_operation_executions WHERE execution_id=?", (execution_id,)).fetchone()
        if base is None or base["operation"] != "internal.test.external.execute":
            raise ValueError("Execution nie jest zarejestrowaną operacją zewnętrzną")
        existing = _row(db, execution_id)
        if existing is None:
            safe_snapshot = sanitize_audit_data(dict(input_data))
            db.execute(
                """INSERT INTO internal_external_execution_queue(
                    execution_id,operation,operation_version,actor_id,approval_id,idempotency_key,
                    input_fingerprint,safe_input_snapshot,status,next_attempt_at,external_system,
                    external_idempotency_key,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,'QUEUED',?,?,?,?,?)""",
                (execution_id, base["operation"], base["operation_version"], base["actor_id"],
                 base["approval_id"], base["idempotency_key"], base["input_fingerprint"],
                 json.dumps(safe_snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                 now, external_system, _stable_external_key(execution_id, base["idempotency_key"]), now, now),
            )
            db.execute(
                """UPDATE internal_operation_executions SET status='AUTHORIZED',updated_at=?,result_summary=?
                   WHERE execution_id=? AND status='PENDING_APPROVAL'""",
                (now, json.dumps({"queue_status": QUEUED}), execution_id),
            )
            joined = _joined(db, execution_id)
            _audit("external_execution.queued", joined, actor, SUCCESS, db=db)
        db.commit()
        return get_execution(execution_id)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_execution(execution_id: str) -> dict[str, Any] | None:
    db = _factory()()
    try:
        row = _joined(db, execution_id)
        return dict(row) if row else None
    finally:
        db.close()


def _claim(statuses: tuple[str, ...], worker_id: str, lease_seconds: int, now: datetime, *, reconciliation=False):
    now_s, lease_s = _iso(now), _iso(now + timedelta(seconds=lease_seconds))
    db = _factory()()
    try:
        db.execute("BEGIN IMMEDIATE")
        due_field = "next_reconciliation_at" if reconciliation else "next_attempt_at"
        extra = "AND reconciliation_required=1" if reconciliation else ""
        marks = ",".join("?" for _ in statuses)
        row = db.execute(
            f"""SELECT execution_id FROM internal_external_execution_queue
                WHERE status IN ({marks}) {extra}
                  AND ({due_field} IS NULL OR {due_field}<=?)
                  AND (lease_until IS NULL OR lease_until<=?)
                ORDER BY created_at LIMIT 1""", (*statuses, now_s, now_s),
        ).fetchone()
        if row is None:
            db.rollback(); return None
        target = RECONCILING if reconciliation else LEASED
        count_sql = "reconciliation_attempt_count=reconciliation_attempt_count+1" if reconciliation else "attempt_count=attempt_count+1,last_attempt_at=?"
        params = [target, worker_id, lease_s]
        if not reconciliation:
            params.append(now_s)
        params.extend([now_s, row["execution_id"]])
        db.execute(
            f"UPDATE internal_external_execution_queue SET status=?,lease_owner=?,lease_until=?,{count_sql},updated_at=? WHERE execution_id=?",
            params,
        )
        joined = _joined(db, row["execution_id"])
        actor = load_actor_context(joined["actor_id"], source="external_worker")
        _audit("external_execution.reconciliation_started" if reconciliation else "external_execution.claimed", joined, actor, SUCCESS, db=db)
        db.commit()
        return dict(joined)
    except Exception:
        db.rollback(); raise
    finally:
        db.close()


def recover_expired_leases(*, now: datetime | None = None) -> dict[str, int]:
    now_s = _iso(now)
    db = _factory()()
    try:
        db.execute("BEGIN IMMEDIATE")
        safe_ids = [row[0] for row in db.execute(
            "SELECT execution_id FROM internal_external_execution_queue WHERE status='LEASED' AND lease_until<=?", (now_s,)
        ).fetchall()]
        uncertain_ids = [row[0] for row in db.execute(
            "SELECT execution_id FROM internal_external_execution_queue WHERE status IN ('SENDING','RECONCILING') AND lease_until<=?", (now_s,)
        ).fetchall()]
        safe = db.execute(
            """UPDATE internal_external_execution_queue
               SET status='QUEUED',lease_owner=NULL,lease_until=NULL,next_attempt_at=?,updated_at=?
               WHERE status='LEASED' AND lease_until<=?""", (now_s, now_s, now_s),
        ).rowcount
        uncertain = db.execute(
            """UPDATE internal_external_execution_queue
               SET status='UNKNOWN',reconciliation_required=1,lease_owner=NULL,lease_until=NULL,
                   next_reconciliation_at=?,last_error='Lease wygasł po rozpoczęciu wysyłania',updated_at=?
               WHERE status IN ('SENDING','RECONCILING') AND lease_until<=?""", (now_s, now_s, now_s),
        ).rowcount
        for execution_id in safe_ids:
            row = _joined(db, execution_id)
            actor = load_actor_context(row["actor_id"], source="external_lease_recovery")
            _audit("external_execution.retryable_failure", row, actor, FAILED,
                   error="Lease wygasł przed rozpoczęciem wysyłania", db=db)
        for execution_id in uncertain_ids:
            row = _joined(db, execution_id)
            actor = load_actor_context(row["actor_id"], source="external_lease_recovery")
            _audit("external_execution.unknown", row, actor, FAILED,
                   error="Lease wygasł po rozpoczęciu wysyłania", db=db)
        db.commit()
        return {"safe_requeued": safe, "moved_to_unknown": uncertain}
    except Exception:
        db.rollback(); raise
    finally:
        db.close()


def _prepared_fingerprint(prepared: Mapping[str, Any]) -> tuple[str, str]:
    safe = sanitize_audit_data(dict(prepared))
    serialized = json.dumps(safe, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).hexdigest(), serialized


def _mark(execution_id: str, status: str, event: str, result: str, *, error="", external_result=None,
          retry_at: datetime | None = None, reconciliation=False):
    now = _iso()
    db = _factory()()
    try:
        db.execute("BEGIN IMMEDIATE")
        fields = ["status=?", "updated_at=?", "lease_owner=NULL", "lease_until=NULL", "last_error=?"]
        values: list[Any] = [status, now, sanitize_audit_text(error) if error else None]
        if retry_at:
            fields.append("next_reconciliation_at=?" if reconciliation else "next_attempt_at=?")
            values.append(_iso(retry_at))
        if status == UNKNOWN:
            fields.append("reconciliation_required=1")
            fields.append("next_reconciliation_at=COALESCE(next_reconciliation_at,?)")
            values.append(now)
        if status in TERMINAL:
            fields.extend(["completed_at=?", "reconciliation_required=0"]); values.append(now)
        if external_result:
            fields.extend(["external_resource_id=?", "external_status=?", "external_request_id=?"])
            values.extend([external_result.resource_id, external_result.status, external_result.request_id or None])
        values.append(execution_id)
        db.execute("UPDATE internal_external_execution_queue SET " + ",".join(fields) + " WHERE execution_id=?", values)
        base_status = "SUCCESS" if status in {SUCCEEDED, RECONCILED_SUCCESS} else "FAILED" if status in {FAILED_PERMANENT, RECONCILED_FAILED} else "RUNNING"
        completed = now if base_status in {"SUCCESS", "FAILED"} else None
        db.execute(
            """UPDATE internal_operation_executions SET status=?,completed_at=COALESCE(?,completed_at),
                      error_code=?,safe_error_message=?,result_summary=?,updated_at=? WHERE execution_id=?""",
            (base_status, completed, status if error else None, sanitize_audit_text(error) if error else None,
             json.dumps({"queue_status": status, "external_resource_id": getattr(external_result, "resource_id", "")}),
             now, execution_id),
        )
        row = _joined(db, execution_id)
        actor = load_actor_context(row["actor_id"], source="external_worker")
        _audit(event, row, actor, result, error=error, db=db)
        db.commit()
        return dict(row)
    except Exception:
        db.rollback(); raise
    finally:
        db.close()


def process_next(worker_id: str, *, lease_seconds=30, now: datetime | None = None):
    current = _dt(now)
    recover_expired_leases(now=current)
    claimed = _claim((QUEUED, FAILED_RETRYABLE), worker_id, lease_seconds, current)
    if claimed is None:
        return None
    adapter = _adapters[claimed["external_system"]]
    snapshot = json.loads(claimed["safe_input_snapshot"])
    try:
        prepared = adapter.prepare_request(snapshot, claimed["external_idempotency_key"])
    except Exception as exc:
        classification = adapter.classify_error(exc)
        if classification == FAILED_RETRYABLE:
            return _mark(claimed["execution_id"], FAILED_RETRYABLE, "external_execution.retryable_failure", FAILED,
                         error=str(exc), retry_at=current + timedelta(seconds=_backoff(claimed["attempt_count"])))
        return _mark(claimed["execution_id"], FAILED_PERMANENT, "external_execution.permanent_failure", FAILED, error=str(exc))

    request_fingerprint, safe_request = _prepared_fingerprint(prepared)
    if claimed["external_request_fingerprint"] and claimed["external_request_fingerprint"] != request_fingerprint:
        return _mark(claimed["execution_id"], FAILED_PERMANENT, "external_execution.permanent_failure", FAILED,
                     error="Przygotowany request zmienił się między próbami")

    db = _factory()()
    try:
        db.execute("BEGIN IMMEDIATE")
        row = _joined(db, claimed["execution_id"])
        actor = load_actor_context(row["actor_id"], source="external_worker_recheck")
        if actor is None or actor.permission_decision(row["permission"]) == DENY:
            raise approvals.ApprovalDenied("PERMISSION_REVOKED", "Permission został odebrany przed wysłaniem")
        if not row["send_started_at"]:
            # Approval consumption and durable SENDING marker commit together.
            approvals.authorize_execution(
                row["approval_id"], actor, row["operation"], payload=snapshot,
                entity_type=row["entity_type"], entity_id=row["entity_id"],
                operation_version=row["operation_version"], transaction_connection=db,
            )
        now_s = _iso(current)
        db.execute(
            """UPDATE internal_external_execution_queue SET status='SENDING',started_at=COALESCE(started_at,?),
                      send_started_at=COALESCE(send_started_at,?),external_request_fingerprint=?,
                      safe_external_request_snapshot=?,updated_at=? WHERE execution_id=? AND status='LEASED'""",
            (now_s, now_s, request_fingerprint, safe_request, now_s, row["execution_id"]),
        )
        db.execute("UPDATE internal_operation_executions SET status='RUNNING',started_at=COALESCE(started_at,?),updated_at=? WHERE execution_id=?",
                   (now_s, now_s, row["execution_id"]))
        started = _joined(db, row["execution_id"])
        _audit("external_execution.started", started, actor, SUCCESS, db=db)
        db.commit()
    except (approvals.ApprovalDenied, approvals.StaleApproval) as exc:
        db.rollback()
        return _mark(claimed["execution_id"], FAILED_PERMANENT, "external_execution.permanent_failure", FAILED, error=str(exc))
    except Exception:
        db.rollback(); raise
    finally:
        db.close()

    try:
        outcome = adapter.execute(prepared, claimed["external_idempotency_key"])
        return _mark(claimed["execution_id"], SUCCEEDED, "external_execution.success", SUCCESS, external_result=outcome)
    except Exception as exc:
        classification = adapter.classify_error(exc)
        if classification == UNKNOWN:
            return _mark(claimed["execution_id"], UNKNOWN, "external_execution.unknown", FAILED, error=str(exc))
        if classification == FAILED_RETRYABLE:
            return _mark(claimed["execution_id"], FAILED_RETRYABLE, "external_execution.retryable_failure", FAILED,
                         error=str(exc), retry_at=current + timedelta(seconds=_backoff(claimed["attempt_count"])))
        return _mark(claimed["execution_id"], FAILED_PERMANENT, "external_execution.permanent_failure", FAILED, error=str(exc))


def reconcile_next(worker_id: str, *, lease_seconds=30, now: datetime | None = None):
    current = _dt(now)
    recover_expired_leases(now=current)
    claimed = _claim((UNKNOWN,), worker_id, lease_seconds, current, reconciliation=True)
    if claimed is None:
        return None
    adapter = _adapters[claimed["external_system"]]
    try:
        outcome = adapter.reconcile(claimed)
    except Exception as exc:
        return _mark(claimed["execution_id"], UNKNOWN, "external_execution.still_unknown", FAILED,
                     error=str(exc), retry_at=current + timedelta(seconds=_backoff(claimed["reconciliation_attempt_count"])), reconciliation=True)
    if outcome.outcome == "SUCCESS":
        result = ExternalResult(outcome.resource_id, outcome.status)
        return _mark(claimed["execution_id"], RECONCILED_SUCCESS, "external_execution.reconciled_success", SUCCESS, external_result=result)
    if outcome.outcome == "FAILURE":
        return _mark(claimed["execution_id"], RECONCILED_FAILED, "external_execution.reconciled_failure", FAILED, error=outcome.safe_message)
    return _mark(claimed["execution_id"], UNKNOWN, "external_execution.still_unknown", FAILED,
                 error=outcome.safe_message, retry_at=current + timedelta(seconds=_backoff(claimed["reconciliation_attempt_count"])), reconciliation=True)


def queue_health(actor_context: ActorContext) -> dict[str, Any]:
    actor = load_actor_context(actor_context.actor_id, source="external_health") if isinstance(actor_context, ActorContext) else None
    if actor is None or actor.permission_decision("system.audit_read") != "ALLOW":
        raise PermissionError("Brak permission system.audit_read")
    db = _factory()()
    try:
        now = _dt()
        counts = {row[0]: int(row[1]) for row in db.execute(
            "SELECT status,COUNT(*) FROM internal_external_execution_queue GROUP BY status"
        ).fetchall()}
        oldest = {}
        for status in (QUEUED, FAILED_RETRYABLE, UNKNOWN):
            value = db.execute("SELECT MIN(created_at) FROM internal_external_execution_queue WHERE status=?", (status,)).fetchone()[0]
            oldest[status.lower()] = max(0, int((now - _parse(value)).total_seconds())) if value else 0
        last_success = db.execute(
            "SELECT MAX(completed_at) FROM internal_external_execution_queue WHERE status IN ('SUCCEEDED','RECONCILED_SUCCESS')"
        ).fetchone()[0]
        last_error = db.execute(
            "SELECT last_error FROM internal_external_execution_queue WHERE last_error IS NOT NULL ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        return {
            "counts": counts,
            "queued": counts.get(QUEUED, 0),
            "leased": counts.get(LEASED, 0) + counts.get(SENDING, 0) + counts.get(RECONCILING, 0),
            "retryable_failures": counts.get(FAILED_RETRYABLE, 0),
            "unknown": counts.get(UNKNOWN, 0),
            "waiting_reconciliation": counts.get(UNKNOWN, 0),
            "oldest_age_seconds": oldest,
            "last_success_at": last_success,
            "last_safe_error": sanitize_audit_text(last_error[0]) if last_error else None,
        }
    finally:
        db.close()


def run_worker(worker_id: str, stop_event: threading.Event, *, poll_seconds=1.0) -> None:
    while not stop_event.is_set():
        handled = process_next(worker_id) or reconcile_next(worker_id)
        if handled is None:
            stop_event.wait(poll_seconds)
