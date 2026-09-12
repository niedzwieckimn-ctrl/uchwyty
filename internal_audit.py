"""Durable business audit foundation for trusted internal actors.

The public API deliberately accepts an ActorContext, never loose actor identity
fields.  HTTP request data may provide correlation identifiers, but cannot set
identity, permissions, operation versions or risk levels.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
import sqlite3
import threading
import uuid
from typing import Any, Callable, Mapping

from flask import g, has_request_context, request


GREEN = "GREEN"
YELLOW = "YELLOW"
RED = "RED"
RISK_LEVELS = frozenset({GREEN, YELLOW, RED})

SUCCESS = "SUCCESS"
DENIED = "DENIED"
FAILED = "FAILED"
PENDING_APPROVAL = "PENDING_APPROVAL"
NOOP = "NOOP"
CONFLICT = "CONFLICT"
RESULTS = frozenset({SUCCESS, DENIED, FAILED, PENDING_APPROVAL, NOOP, CONFLICT})

READ_STANDARD = "READ_STANDARD"
READ_SENSITIVE = "READ_SENSITIVE"
WRITE = "WRITE"
EXTERNAL = "EXTERNAL"
SECURITY = "SECURITY"
AUDIT_POLICIES = frozenset({READ_STANDARD, READ_SENSITIVE, WRITE, EXTERNAL, SECURITY})

MAX_TEXT_LENGTH = 2_000
MAX_COLLECTION_ITEMS = 100
MAX_DEPTH = 6
MAX_STATE_BYTES = 32_000
REDACTED = "[REDACTED]"
MASKED = "[MASKED]"
TRUNCATED = "[TRUNCATED]"


@dataclass(frozen=True)
class OperationDefinition:
    name: str
    version: int
    permission: str | None
    risk_level: str
    audit_policy: str

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("operation version musi być dodatnia")
        if self.risk_level not in RISK_LEVELS:
            raise ValueError(f"Nieznany risk level: {self.risk_level}")
        if self.audit_policy not in AUDIT_POLICIES:
            raise ValueError(f"Nieznana polityka audytu: {self.audit_policy}")


# This backend catalogue is the only source of operation version and risk.
OPERATION_DEFINITIONS: dict[str, OperationDefinition] = {
    "security.login": OperationDefinition("security.login", 1, None, GREEN, SECURITY),
    "security.permission.denied": OperationDefinition(
        "security.permission.denied", 1, None, YELLOW, SECURITY
    ),
    "inventory.product.read": OperationDefinition(
        "inventory.product.read", 1, "inventory.read", GREEN, READ_STANDARD
    ),
    "internal.audit_outbox.status": OperationDefinition(
        "internal.audit_outbox.status", 1, "system.audit_read", GREEN, READ_SENSITIVE
    ),
    "internal.versioned_resource.create": OperationDefinition(
        "internal.versioned_resource.create", 1, "system.audit_read", GREEN, WRITE
    ),
    "internal.versioned_resource.update": OperationDefinition(
        "internal.versioned_resource.update", 1, "system.audit_read", YELLOW, WRITE
    ),
    "approval.requested": OperationDefinition("approval.requested", 1, None, YELLOW, SECURITY),
    "approval.approved": OperationDefinition("approval.approved", 1, "approvals.decide", YELLOW, SECURITY),
    "approval.rejected": OperationDefinition("approval.rejected", 1, "approvals.decide", YELLOW, SECURITY),
    "approval.expired": OperationDefinition("approval.expired", 1, None, YELLOW, SECURITY),
    "approval.cancelled": OperationDefinition("approval.cancelled", 1, None, YELLOW, SECURITY),
    "approval.consumed": OperationDefinition("approval.consumed", 1, None, YELLOW, SECURITY),
    "approval.execution_denied": OperationDefinition("approval.execution_denied", 1, None, YELLOW, SECURITY),
    "approval.stale": OperationDefinition("approval.stale", 1, None, YELLOW, SECURITY),
    "business_operation.requested": OperationDefinition("business_operation.requested", 1, None, GREEN, WRITE),
    "business_operation.denied": OperationDefinition("business_operation.denied", 1, None, YELLOW, SECURITY),
    "business_operation.pending_approval": OperationDefinition("business_operation.pending_approval", 1, None, YELLOW, WRITE),
    "business_operation.started": OperationDefinition("business_operation.started", 1, None, YELLOW, WRITE),
    "business_operation.success": OperationDefinition("business_operation.success", 1, None, GREEN, WRITE),
    "business_operation.failed": OperationDefinition("business_operation.failed", 1, None, YELLOW, SECURITY),
    "business_operation.conflict": OperationDefinition("business_operation.conflict", 1, None, YELLOW, SECURITY),
    "business_operation.requested": OperationDefinition("business_operation.requested", 1, None, GREEN, SECURITY),
    "business_operation.denied": OperationDefinition("business_operation.denied", 1, None, YELLOW, SECURITY),
    "business_operation.pending_approval": OperationDefinition("business_operation.pending_approval", 1, None, YELLOW, SECURITY),
    "business_operation.started": OperationDefinition("business_operation.started", 1, None, YELLOW, SECURITY),
    "business_operation.success": OperationDefinition("business_operation.success", 1, None, GREEN, SECURITY),
    "business_operation.failed": OperationDefinition("business_operation.failed", 1, None, YELLOW, SECURITY),
    "business_operation.conflict": OperationDefinition("business_operation.conflict", 1, None, YELLOW, SECURITY),
    "external_execution.queued": OperationDefinition("external_execution.queued", 1, None, YELLOW, WRITE),
    "external_execution.claimed": OperationDefinition("external_execution.claimed", 1, None, YELLOW, SECURITY),
    "external_execution.started": OperationDefinition("external_execution.started", 1, None, YELLOW, SECURITY),
    "external_execution.success": OperationDefinition("external_execution.success", 1, None, GREEN, WRITE),
    "external_execution.retryable_failure": OperationDefinition("external_execution.retryable_failure", 1, None, YELLOW, SECURITY),
    "external_execution.permanent_failure": OperationDefinition("external_execution.permanent_failure", 1, None, YELLOW, SECURITY),
    "external_execution.unknown": OperationDefinition("external_execution.unknown", 1, None, YELLOW, SECURITY),
    "external_execution.reconciliation_started": OperationDefinition("external_execution.reconciliation_started", 1, None, YELLOW, SECURITY),
    "external_execution.reconciled_success": OperationDefinition("external_execution.reconciled_success", 1, None, GREEN, WRITE),
    "external_execution.reconciled_failure": OperationDefinition("external_execution.reconciled_failure", 1, None, YELLOW, SECURITY),
    "external_execution.still_unknown": OperationDefinition("external_execution.still_unknown", 1, None, YELLOW, SECURITY),
    "agent.terminology.remembered": OperationDefinition("agent.terminology.remembered", 1, "agent.terminology.remember", GREEN, WRITE),
    "agent.requested": OperationDefinition("agent.requested", 1, None, GREEN, SECURITY),
    "agent.tool_selected": OperationDefinition("agent.tool_selected", 1, None, GREEN, SECURITY),
    "agent.tool_result": OperationDefinition("agent.tool_result", 1, None, GREEN, SECURITY),
    "agent.completed": OperationDefinition("agent.completed", 1, None, GREEN, SECURITY),
    "agent.failed": OperationDefinition("agent.failed", 1, None, YELLOW, SECURITY),
    "agent.conversation.created": OperationDefinition("agent.conversation.created", 1, None, GREEN, SECURITY),
    "agent.conversation.resumed": OperationDefinition("agent.conversation.resumed", 1, None, GREEN, SECURITY),
    "agent.conversation.expired": OperationDefinition("agent.conversation.expired", 1, None, GREEN, SECURITY),
    "agent.conversation.reset": OperationDefinition("agent.conversation.reset", 1, None, GREEN, SECURITY),
}


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
    """Create an independent append-only SQLite audit store."""
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS internal_audit_log(
            audit_id TEXT PRIMARY KEY,
            occurred_at TEXT NOT NULL,
            actor_id TEXT REFERENCES internal_actors(actor_id) ON DELETE RESTRICT,
            actor_type TEXT CHECK(actor_type IS NULL OR actor_type IN ('HUMAN','SYSTEM','AI_AGENT')),
            actor_display_name TEXT,
            roles_json TEXT NOT NULL DEFAULT '[]',
            permission TEXT,
            operation TEXT NOT NULL,
            operation_version INTEGER NOT NULL CHECK(operation_version > 0),
            audit_policy TEXT NOT NULL CHECK(audit_policy IN ('READ_STANDARD','READ_SENSITIVE','WRITE','EXTERNAL','SECURITY')),
            entity_type TEXT,
            entity_id TEXT,
            request_id TEXT,
            correlation_id TEXT,
            session_id TEXT,
            credential_id TEXT,
            risk_level TEXT NOT NULL CHECK(risk_level IN ('GREEN','YELLOW','RED')),
            reason TEXT,
            approval_required INTEGER NOT NULL DEFAULT 0 CHECK(approval_required IN (0,1)),
            approval_id TEXT,
            approved_by TEXT,
            result TEXT NOT NULL CHECK(result IN ('SUCCESS','DENIED','FAILED','PENDING_APPROVAL','NOOP','CONFLICT')),
            error_code TEXT,
            error_message TEXT,
            external_integration TEXT,
            external_request_id TEXT,
            external_result TEXT,
            idempotency_key TEXT,
            is_replay INTEGER NOT NULL DEFAULT 0 CHECK(is_replay IN (0,1)),
            before_state TEXT,
            after_state TEXT,
            entity_version_before INTEGER,
            entity_version_after INTEGER,
            expected_version INTEGER,
            current_version INTEGER,
            source TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_internal_audit_actor_time
            ON internal_audit_log(actor_id, occurred_at);
        CREATE INDEX IF NOT EXISTS idx_internal_audit_operation_time
            ON internal_audit_log(operation, occurred_at);
        CREATE INDEX IF NOT EXISTS idx_internal_audit_entity
            ON internal_audit_log(entity_type, entity_id, occurred_at);
        CREATE INDEX IF NOT EXISTS idx_internal_audit_correlation
            ON internal_audit_log(correlation_id, occurred_at);
        CREATE INDEX IF NOT EXISTS idx_internal_audit_idempotency
            ON internal_audit_log(idempotency_key) WHERE idempotency_key IS NOT NULL;
        CREATE TRIGGER IF NOT EXISTS internal_audit_log_no_update
        BEFORE UPDATE ON internal_audit_log
        BEGIN
            SELECT RAISE(ABORT, 'internal_audit_log is append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS internal_audit_log_no_delete
        BEFORE DELETE ON internal_audit_log
        BEGIN
            SELECT RAISE(ABORT, 'internal_audit_log is append-only');
        END;
        """
    )
    existing_columns = {
        row[1] for row in db.execute("PRAGMA table_info(internal_audit_log)").fetchall()
    }
    for column in ("expected_version", "current_version"):
        if column not in existing_columns:
            db.execute(f"ALTER TABLE internal_audit_log ADD COLUMN {column} INTEGER")
    db.commit()


_SECRET_KEY_PARTS = (
    "password", "passwd", "secret", "token", "api_key", "apikey", "authorization",
    "cookie", "credential", "private_key", "client_secret", "session",
)
_CARD_KEY_PARTS = ("card_number", "pan", "cvv", "cvc", "security_code")


def _sensitive_kind(key: str) -> str | None:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(key).lower()).strip("_")
    if any(part in normalized for part in _CARD_KEY_PARTS):
        return "mask"
    if any(part in normalized for part in _SECRET_KEY_PARTS):
        return "redact"
    return None


def _sanitize(value: Any, *, depth: int = 0) -> Any:
    if depth >= MAX_DEPTH:
        return TRUNCATED
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, bytes):
        return f"[BINARY {len(value)} bytes]"
    if isinstance(value, str):
        # Free-form values can contain credentials even when their field name is harmless.
        text = _INLINE_SECRET_PATTERNS[0].sub("Bearer " + REDACTED, value)
        text = _INLINE_SECRET_PATTERNS[1].sub(lambda match: f"{match.group(1)}={REDACTED}", text)
        return text if len(text) <= MAX_TEXT_LENGTH else text[:MAX_TEXT_LENGTH] + TRUNCATED
    if isinstance(value, Mapping):
        cleaned: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= MAX_COLLECTION_ITEMS:
                cleaned[TRUNCATED] = f"{len(value) - MAX_COLLECTION_ITEMS} pól"
                break
            text_key = str(key)[:128]
            kind = _sensitive_kind(text_key)
            cleaned[text_key] = REDACTED if kind == "redact" else MASKED if kind == "mask" else _sanitize(item, depth=depth + 1)
        return cleaned
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        cleaned = [_sanitize(item, depth=depth + 1) for item in items[:MAX_COLLECTION_ITEMS]]
        if len(items) > MAX_COLLECTION_ITEMS:
            cleaned.append(f"{TRUNCATED} {len(items) - MAX_COLLECTION_ITEMS} elementów")
        return cleaned
    return _sanitize(str(value), depth=depth + 1)


def sanitize_audit_data(value: Any) -> Any:
    """Remove secrets and bound the serialized audit payload."""
    cleaned = _sanitize(value)
    encoded = json.dumps(cleaned, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(encoded.encode("utf-8")) <= MAX_STATE_BYTES:
        return cleaned
    return {
        "_truncated": True,
        "preview": encoded.encode("utf-8")[: MAX_STATE_BYTES - 200].decode("utf-8", errors="ignore"),
        "original_bytes": len(encoded.encode("utf-8")),
    }


_INLINE_SECRET_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+\-/=]+"),
    re.compile(
        r"(?i)\b(password|passwd|secret|token|api[_-]?(?:key|token)|access[_-]?token|authorization|cookie)"
        r"\s*[:=]\s*([^\s,;]+)"
    ),
)


def sanitize_audit_text(value: Any) -> str:
    """Bound free-form error/reason text and remove common inline credentials."""
    text = str(value or "")
    text = _INLINE_SECRET_PATTERNS[0].sub("Bearer " + REDACTED, text)
    text = _INLINE_SECRET_PATTERNS[1].sub(lambda match: f"{match.group(1)}={REDACTED}", text)
    return text if len(text) <= MAX_TEXT_LENGTH else text[:MAX_TEXT_LENGTH] + TRUNCATED


def _changed_states(before: Any, after: Any) -> tuple[Any, Any]:
    """Keep only changed top-level fields when both states are mappings."""
    if not isinstance(before, Mapping) or not isinstance(after, Mapping):
        return before, after
    keys = set(before) | set(after)
    changed = [key for key in keys if before.get(key) != after.get(key)]
    return ({key: before.get(key) for key in changed}, {key: after.get(key) for key in changed})


def _json_state(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(sanitize_audit_data(value), ensure_ascii=False, separators=(",", ":"), default=str)


def current_correlation_id(explicit: str = "") -> str:
    if explicit and re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", explicit):
        return explicit
    if has_request_context():
        existing = getattr(g, "audit_correlation_id", "")
        if existing:
            return existing
        supplied = request.headers.get("X-Correlation-ID", "")
        value = supplied if re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", supplied or "") else str(uuid.uuid4())
        g.audit_correlation_id = value
        return value
    return str(uuid.uuid4())


def _operation(name: str) -> OperationDefinition:
    try:
        return OPERATION_DEFINITIONS[name]
    except KeyError as exc:
        raise ValueError(f"Niezarejestrowana operacja audytowa: {name}") from exc


def record_audit_event(
    operation: str,
    *,
    result: str,
    actor_context=None,
    permission: str | None = None,
    entity_type: str = "",
    entity_id: Any = None,
    correlation_id: str = "",
    reason: str = "",
    approval_required: bool = False,
    approval_id: str = "",
    approved_by: str = "",
    error_code: str = "",
    error_message: str = "",
    external_integration: str = "",
    external_request_id: str = "",
    external_result: str = "",
    idempotency_key: str = "",
    is_replay: bool = False,
    before_state: Any = None,
    after_state: Any = None,
    entity_version_before: int | None = None,
    entity_version_after: int | None = None,
    expected_version: int | None = None,
    current_version: int | None = None,
    changes_only: bool = True,
    source: str = "",
    risk_level: str | None = None,
    transaction_connection: sqlite3.Connection | None = None,
) -> str:
    """Append one durable event. Identity, risk and version come from backend state."""
    if _connection_factory is None:
        raise RuntimeError("Audit service nie został skonfigurowany")
    definition = _operation(operation)
    effective_risk_level = risk_level or definition.risk_level
    if effective_risk_level not in RISK_LEVELS:
        raise ValueError(f"Nieznany risk level: {effective_risk_level}")
    if result not in RESULTS:
        raise ValueError(f"Nieznany wynik audytu: {result}")
    if actor_context is None:
        from internal_rbac import current_actor_context
        actor_context = current_actor_context()
    if changes_only and before_state is not None and after_state is not None:
        before_state, after_state = _changed_states(before_state, after_state)

    audit_id = str(uuid.uuid4())
    request_id = getattr(actor_context, "request_id", "") or str(uuid.uuid4())
    values = (
        audit_id,
        _utc_now(),
        getattr(actor_context, "actor_id", None),
        getattr(actor_context, "actor_type", None),
        getattr(actor_context, "display_name", None),
        json.dumps(list(getattr(actor_context, "roles", ()) or ()), ensure_ascii=False),
        permission if permission is not None else definition.permission,
        definition.name,
        definition.version,
        definition.audit_policy,
        str(entity_type or "") or None,
        None if entity_id is None else str(entity_id)[:256],
        request_id,
        current_correlation_id(correlation_id),
        getattr(actor_context, "session_id", "") or None,
        getattr(actor_context, "credential_id", "") or None,
        effective_risk_level,
        sanitize_audit_text(reason or getattr(actor_context, "reason", "")) or None,
        int(bool(approval_required)),
        approval_id[:128] or getattr(actor_context, "approval_id", "") or None,
        approved_by[:128] or None,
        result,
        error_code[:128] or None,
        sanitize_audit_text(error_message) or None,
        external_integration[:64].upper() or None,
        external_request_id[:256] or None,
        external_result[:128] or None,
        idempotency_key[:256] or None,
        int(bool(is_replay)),
        _json_state(before_state),
        _json_state(after_state),
        entity_version_before,
        entity_version_after,
        expected_version,
        current_version,
        (source or getattr(actor_context, "source", "") or "internal")[:128],
    )
    owns_connection = transaction_connection is None
    db = transaction_connection or _connection_factory()
    try:
        db.execute(
            """INSERT INTO internal_audit_log(
                audit_id,occurred_at,actor_id,actor_type,actor_display_name,roles_json,
                permission,operation,operation_version,audit_policy,entity_type,entity_id,
                request_id,correlation_id,session_id,credential_id,risk_level,reason,
                approval_required,approval_id,approved_by,result,error_code,error_message,
                external_integration,external_request_id,external_result,idempotency_key,
                is_replay,before_state,after_state,entity_version_before,
                entity_version_after,expected_version,current_version,source
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            values,
        )
        db.execute(
            """INSERT INTO internal_audit_outbox(
                audit_id,status,attempt_count,next_attempt_at,created_at,lease_until
            ) VALUES(?,'PENDING',0,0,?,0)""",
            (audit_id, _utc_now()),
        )
        if owns_connection:
            db.commit()
    except Exception:
        if owns_connection:
            db.rollback()
        raise
    finally:
        if owns_connection:
            db.close()
    return audit_id


def try_record_audit_event(operation: str, **kwargs) -> str | None:
    """Non-disruptive adapter for legacy routes during gradual migration."""
    try:
        return record_audit_event(operation, **kwargs)
    except Exception:
        return None


def fetch_audit_events(actor_context, *, limit: int = 100) -> list[dict[str, Any]]:
    """Future read interface; no HTTP/UI endpoint is exposed in this stage."""
    if actor_context is None or actor_context.permission_decision("system.audit_read") != "ALLOW":
        raise PermissionError("Brak permission system.audit_read")
    if _connection_factory is None:
        raise RuntimeError("Audit service nie został skonfigurowany")
    safe_limit = max(1, min(int(limit), 500))
    db = _connection_factory()
    try:
        return [dict(row) for row in db.execute(
            "SELECT * FROM internal_audit_log ORDER BY occurred_at DESC, audit_id DESC LIMIT ?",
            (safe_limit,),
        ).fetchall()]
    finally:
        db.close()
