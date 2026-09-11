"""Central, backend-owned approval gate for internal operations.

This module exposes no HTTP routes.  Callers must pass an ActorContext that is
reloaded from the trusted RBAC store before every decision.  Request payloads,
headers and AI-provided identity or risk claims are never authority sources.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import sqlite3
import threading
import uuid
from typing import Any, Callable, Mapping

from internal_audit import (
    CONFLICT,
    DENIED,
    PENDING_APPROVAL,
    SUCCESS,
    current_correlation_id,
    record_audit_event,
    sanitize_audit_data,
    sanitize_audit_text,
)
from internal_rbac import (
    ACTOR_HUMAN,
    ALLOW,
    DENY as PERMISSION_DENY,
    ActorContext,
    load_actor_context,
)


GREEN = "GREEN"
YELLOW = "YELLOW"
RED = "RED"
RISK_LEVELS = frozenset({GREEN, YELLOW, RED})
RISK_ORDER = {GREEN: 1, YELLOW: 2, RED: 3}

PENDING = "PENDING"
APPROVED = "APPROVED"
REJECTED = "REJECTED"
EXPIRED = "EXPIRED"
CANCELLED = "CANCELLED"
CONSUMED = "CONSUMED"
STATUSES = frozenset({PENDING, APPROVED, REJECTED, EXPIRED, CANCELLED, CONSUMED})


class ApprovalDenied(PermissionError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class StaleApproval(ApprovalDenied):
    pass


@dataclass(frozen=True)
class OperationEvaluation:
    allowed: bool
    requires_approval: bool
    operation: str
    operation_version: int
    permission: str
    risk_level: str
    reason: str = ""


@dataclass(frozen=True)
class ApprovalPolicy:
    policy_id: str
    operation: str
    operation_version: int
    permission: str
    risk_level: str
    requires_approval: bool
    approver_permission: str
    expiry_seconds: int
    self_approval_allowed: bool
    threshold_amount: float | None
    enabled: bool


_connection_factory: Callable[[], sqlite3.Connection] | None = None
_configuration_lock = threading.Lock()


PILOT_POLICIES = (
    ("policy-inventory-product-search", "inventory.product.search", 1, "inventory.read", GREEN, 0, "approvals.decide", 3600, 1, None, 1),
    ("policy-inventory-product-get", "inventory.product.get", 1, "inventory.read", GREEN, 0, "approvals.decide", 3600, 1, None, 1),
    ("policy-inventory-summary", "inventory.summary", 1, "inventory.read", GREEN, 0, "approvals.decide", 3600, 1, None, 1),
    ("policy-orders-search", "orders.search", 1, "orders.read_full", GREEN, 0, "approvals.decide", 3600, 1, None, 1),
    ("policy-orders-get", "orders.get", 1, "orders.read_full", GREEN, 0, "approvals.decide", 3600, 1, None, 1),
    ("policy-orders-summary", "orders.summary", 1, "orders.read_full", GREEN, 0, "approvals.decide", 3600, 1, None, 1),
    ("policy-invoices-search", "invoices.search", 1, "invoices.read", GREEN, 0, "approvals.decide", 3600, 1, None, 1),
    ("policy-invoices-get", "invoices.get", 1, "invoices.read", GREEN, 0, "approvals.decide", 3600, 1, None, 1),
    ("policy-invoices-overdue", "invoices.overdue", 1, "payments.read", GREEN, 0, "approvals.decide", 3600, 1, None, 1),
    ("policy-customers-search", "customers.search", 1, "customers.read", GREEN, 0, "approvals.decide", 3600, 1, None, 1),
    ("policy-customers-get", "customers.get", 1, "customers.read", GREEN, 0, "approvals.decide", 3600, 1, None, 1),
    ("policy-china-orders-summary", "china.orders.summary", 1, "purchases.read", GREEN, 0, "approvals.decide", 3600, 1, None, 1),
    ("policy-business-sales-summary", "business.sales.summary", 1, "reports.read", GREEN, 0, "approvals.decide", 3600, 1, None, 1),
    ("policy-internal-test-read", "internal.test.read_status", 1, "system.audit_read", GREEN, 0, "approvals.decide", 3600, 1, None, 1),
    ("policy-internal-test-change", "internal.test.change_setting", 1, "internal.test.change_setting", YELLOW, 1, "approvals.decide", 3600, 0, None, 1),
    ("policy-internal-test-external", "internal.test.external.execute", 1, "internal.test.change_setting", YELLOW, 1, "approvals.decide", 3600, 0, None, 1),
    ("policy-internal-test-red", "internal.test.red_action", 1, "internal.test.change_setting", RED, 1, "approvals.decide", 1800, 0, None, 1),
)


def configure(connection_factory: Callable[[], sqlite3.Connection]) -> None:
    if not callable(connection_factory):
        raise TypeError("connection_factory musi być wywoływalne")
    global _connection_factory
    with _configuration_lock:
        _connection_factory = connection_factory


def _factory() -> Callable[[], sqlite3.Connection]:
    if _connection_factory is None:
        raise RuntimeError("Approval Engine nie został skonfigurowany")
    return _connection_factory


def _utc_now_dt() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def initialize_schema(db: sqlite3.Connection) -> None:
    """Create only isolated approval tables and seed deterministic pilot policy."""
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS internal_approval_policies(
            policy_id TEXT PRIMARY KEY,
            operation TEXT NOT NULL,
            operation_version INTEGER NOT NULL CHECK(operation_version > 0),
            permission TEXT NOT NULL REFERENCES internal_permissions(permission_key) ON DELETE RESTRICT,
            risk_level TEXT NOT NULL CHECK(risk_level IN ('GREEN','YELLOW','RED')),
            requires_approval INTEGER NOT NULL CHECK(requires_approval IN (0,1)),
            approver_permission TEXT NOT NULL REFERENCES internal_permissions(permission_key) ON DELETE RESTRICT,
            expiry_seconds INTEGER NOT NULL CHECK(expiry_seconds > 0),
            self_approval_allowed INTEGER NOT NULL DEFAULT 0 CHECK(self_approval_allowed IN (0,1)),
            threshold_amount REAL,
            enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(operation, operation_version)
        );
        CREATE TABLE IF NOT EXISTS internal_approval_requests(
            approval_id TEXT PRIMARY KEY,
            status TEXT NOT NULL CHECK(status IN ('PENDING','APPROVED','REJECTED','EXPIRED','CANCELLED','CONSUMED')),
            requested_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            resolved_at TEXT,
            requesting_actor_id TEXT NOT NULL REFERENCES internal_actors(actor_id) ON DELETE RESTRICT,
            requesting_actor_type TEXT NOT NULL CHECK(requesting_actor_type IN ('HUMAN','SYSTEM','AI_AGENT')),
            requesting_actor_roles TEXT NOT NULL,
            operation TEXT NOT NULL,
            operation_version INTEGER NOT NULL CHECK(operation_version > 0),
            permission TEXT NOT NULL REFERENCES internal_permissions(permission_key) ON DELETE RESTRICT,
            risk_level TEXT NOT NULL CHECK(risk_level IN ('GREEN','YELLOW','RED')),
            policy_id TEXT NOT NULL REFERENCES internal_approval_policies(policy_id) ON DELETE RESTRICT,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            request_id TEXT NOT NULL,
            correlation_id TEXT NOT NULL,
            reason TEXT,
            safe_payload TEXT NOT NULL,
            operation_fingerprint TEXT NOT NULL,
            expected_entity_version INTEGER,
            approved_by_actor_id TEXT REFERENCES internal_actors(actor_id) ON DELETE RESTRICT,
            resolution_reason TEXT,
            resolution_request_id TEXT,
            consumed_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_internal_approval_requests_status_expiry
            ON internal_approval_requests(status, expires_at, requested_at);
        CREATE INDEX IF NOT EXISTS idx_internal_approval_requests_correlation
            ON internal_approval_requests(correlation_id);
        CREATE INDEX IF NOT EXISTS idx_internal_approval_requests_entity
            ON internal_approval_requests(entity_type, entity_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_internal_approval_requests_fingerprint
            ON internal_approval_requests(operation_fingerprint);
        """
    )
    now = _iso(_utc_now_dt())
    db.executemany(
        """INSERT INTO internal_approval_policies(
               policy_id,operation,operation_version,permission,risk_level,
               requires_approval,approver_permission,expiry_seconds,
               self_approval_allowed,threshold_amount,enabled,created_at,updated_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(operation,operation_version) DO NOTHING""",
        [row + (now, now) for row in PILOT_POLICIES],
    )
    db.commit()


def _trusted_actor(actor: ActorContext | None) -> ActorContext:
    """Resolve identity and permissions again from trusted storage."""
    if actor is None or not isinstance(actor, ActorContext):
        raise ApprovalDenied("UNTRUSTED_ACTOR", "Brak zaufanego ActorContext")
    trusted = load_actor_context(
        actor.actor_id,
        request_id=actor.request_id,
        session_id=actor.session_id,
        credential_id=actor.credential_id,
        delegated_by_actor_id=actor.delegated_by_actor_id,
        reason=actor.reason,
        source=actor.source,
    )
    if trusted is None or trusted.actor_type != actor.actor_type:
        raise ApprovalDenied("UNTRUSTED_ACTOR", "Tożsamość aktora nie zgadza się z zaufanym źródłem")
    return trusted


def _policy_from_row(row) -> ApprovalPolicy:
    return ApprovalPolicy(
        policy_id=row["policy_id"], operation=row["operation"],
        operation_version=int(row["operation_version"]), permission=row["permission"],
        risk_level=row["risk_level"], requires_approval=bool(row["requires_approval"]),
        approver_permission=row["approver_permission"], expiry_seconds=int(row["expiry_seconds"]),
        self_approval_allowed=bool(row["self_approval_allowed"]),
        threshold_amount=row["threshold_amount"], enabled=bool(row["enabled"]),
    )


def get_policy(operation: str, operation_version: int = 1) -> ApprovalPolicy:
    db = _factory()()
    try:
        row = db.execute(
            "SELECT * FROM internal_approval_policies WHERE operation=? AND operation_version=?",
            (operation, int(operation_version)),
        ).fetchone()
    finally:
        db.close()
    if row is None:
        raise ApprovalDenied("POLICY_NOT_FOUND", "Brak aktywnej polityki operacji")
    policy = _policy_from_row(row)
    if not policy.enabled:
        raise ApprovalDenied("POLICY_DISABLED", "Polityka operacji jest wyłączona")
    return policy


def evaluate_operation(
    actor_context: ActorContext,
    operation: str,
    *,
    operation_version: int = 1,
    claimed_risk_level: str | None = None,
) -> OperationEvaluation:
    """Evaluate permission first and backend policy second; risk claim is ignored."""
    del claimed_risk_level
    actor = _trusted_actor(actor_context)
    policy = get_policy(operation, operation_version)
    decision = actor.permission_decision(policy.permission)
    if decision == PERMISSION_DENY:
        return OperationEvaluation(False, False, operation, operation_version, policy.permission, policy.risk_level, "PERMISSION_DENIED")
    requires = policy.risk_level == RED or policy.requires_approval or decision != ALLOW
    return OperationEvaluation(True, requires, operation, operation_version, policy.permission, policy.risk_level)


def operation_fingerprint(
    *, operation: str, operation_version: int, entity_type: str, entity_id: str,
    payload: Mapping[str, Any], requesting_actor_id: str,
    expected_entity_version: int | None,
) -> tuple[str, dict[str, Any]]:
    safe_payload = sanitize_audit_data(dict(payload))
    canonical = json.dumps(
        {
            "operation": operation,
            "operation_version": int(operation_version),
            "entity_type": str(entity_type),
            "entity_id": str(entity_id),
            "payload": safe_payload,
            "requesting_actor_id": requesting_actor_id,
            "expected_entity_version": expected_entity_version,
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest(), safe_payload


def request_approval(
    actor_context: ActorContext,
    operation: str,
    *, payload: Mapping[str, Any], entity_type: str, entity_id: str,
    operation_version: int = 1, expected_entity_version: int | None = None,
    reason: str = "", correlation_id: str = "",
    claimed_risk_level: str | None = None,
) -> str:
    actor = _trusted_actor(actor_context)
    evaluation = evaluate_operation(actor, operation, operation_version=operation_version, claimed_risk_level=claimed_risk_level)
    if not evaluation.allowed:
        raise ApprovalDenied("PERMISSION_DENIED", "Actor nie posiada wymaganego permission")
    if not evaluation.requires_approval:
        raise ApprovalDenied("APPROVAL_NOT_REQUIRED", "Operacja nie wymaga approval")
    policy = get_policy(operation, operation_version)
    fingerprint, safe_payload = operation_fingerprint(
        operation=operation, operation_version=operation_version,
        entity_type=entity_type, entity_id=entity_id, payload=payload,
        requesting_actor_id=actor.actor_id, expected_entity_version=expected_entity_version,
    )
    approval_id = str(uuid.uuid4())
    now_dt = _utc_now_dt()
    now = _iso(now_dt)
    expires_at = _iso(now_dt + timedelta(seconds=policy.expiry_seconds))
    correlation = current_correlation_id(correlation_id)
    db = _factory()()
    try:
        db.execute(
            """INSERT INTO internal_approval_requests(
                approval_id,status,requested_at,expires_at,requesting_actor_id,
                requesting_actor_type,requesting_actor_roles,operation,operation_version,
                permission,risk_level,policy_id,entity_type,entity_id,request_id,
                correlation_id,reason,safe_payload,operation_fingerprint,
                expected_entity_version,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (approval_id, PENDING, now, expires_at, actor.actor_id, actor.actor_type,
             json.dumps(list(actor.roles), ensure_ascii=False), operation, operation_version,
             policy.permission, policy.risk_level, policy.policy_id, str(entity_type), str(entity_id),
             actor.request_id or str(uuid.uuid4()), correlation, sanitize_audit_text(reason) or None,
             json.dumps(safe_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
             fingerprint, expected_entity_version, now, now),
        )
        record_audit_event(
            "approval.requested", result=PENDING_APPROVAL, actor_context=actor,
            permission=policy.permission, entity_type=entity_type, entity_id=entity_id,
            correlation_id=correlation, reason=reason, approval_required=True,
            approval_id=approval_id, risk_level=policy.risk_level,
            after_state={"target_operation": operation, "target_operation_version": operation_version, "safe_payload": safe_payload},
            expected_version=expected_entity_version, transaction_connection=db,
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return approval_id


def _request_row(approval_id: str):
    db = _factory()()
    try:
        return db.execute("SELECT * FROM internal_approval_requests WHERE approval_id=?", (approval_id,)).fetchone()
    finally:
        db.close()


def get_request_snapshot(approval_id: str) -> dict[str, Any] | None:
    """Return trusted internal execution metadata; safe_payload is already sanitized."""
    row = _request_row(approval_id)
    return dict(row) if row is not None else None


def get_request_snapshot(approval_id: str) -> dict[str, Any] | None:
    """Return a safe internal snapshot for orchestration; it grants no authority."""
    row = _request_row(approval_id)
    return dict(row) if row is not None else None


def _audit_transition(event: str, row, *, actor, result: str, reason: str = "", error_code: str = "", transaction_connection=None) -> None:
    record_audit_event(
        event, result=result, actor_context=actor, permission=row["permission"],
        entity_type=row["entity_type"], entity_id=row["entity_id"],
        correlation_id=row["correlation_id"], reason=reason,
        approval_required=True, approval_id=row["approval_id"],
        approved_by=row["approved_by_actor_id"] or "", risk_level=row["risk_level"],
        error_code=error_code,
        after_state={"target_operation": row["operation"], "target_operation_version": row["operation_version"], "approval_status": row["status"]},
        expected_version=row["expected_entity_version"],
        transaction_connection=transaction_connection,
    )


def _expire_if_needed(row, *, now: datetime | None = None):
    if row and row["status"] in (PENDING, APPROVED) and _parse_time(row["expires_at"]) <= (now or _utc_now_dt()):
        timestamp = _iso(now or _utc_now_dt())
        db = _factory()()
        try:
            cursor = db.execute(
                """UPDATE internal_approval_requests SET status='EXPIRED',resolved_at=?,
                   resolution_reason='Approval wygasł',updated_at=?
                   WHERE approval_id=? AND status IN ('PENDING','APPROVED')""",
                (timestamp, timestamp, row["approval_id"]),
            )
            if cursor.rowcount:
                expired = db.execute(
                    "SELECT * FROM internal_approval_requests WHERE approval_id=?",
                    (row["approval_id"],),
                ).fetchone()
                _audit_transition(
                    "approval.expired", expired, actor=None, result=DENIED,
                    reason="Approval wygasł", error_code="APPROVAL_EXPIRED",
                    transaction_connection=db,
                )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        if cursor.rowcount:
            return expired
    return row


def approve_request(approval_id: str, approver_context: ActorContext, *, reason: str = "") -> None:
    approver = _trusted_actor(approver_context)
    row = _expire_if_needed(_request_row(approval_id))
    if row is None:
        raise ApprovalDenied("APPROVAL_NOT_FOUND", "Nie znaleziono approval")
    policy = get_policy(row["operation"], row["operation_version"])
    if approver.actor_type != ACTOR_HUMAN:
        raise ApprovalDenied("HUMAN_APPROVER_REQUIRED", "Approval może zatwierdzić wyłącznie HUMAN")
    if approver.permission_decision(policy.approver_permission) != ALLOW:
        raise ApprovalDenied("APPROVER_PERMISSION_DENIED", "Approver nie posiada wymaganego permission")
    if approver.actor_id == row["requesting_actor_id"] and not policy.self_approval_allowed:
        raise ApprovalDenied("SELF_APPROVAL_DENIED", "Polityka zabrania self approval")
    now = _iso(_utc_now_dt())
    db = _factory()()
    try:
        cursor = db.execute(
            """UPDATE internal_approval_requests SET status='APPROVED',resolved_at=?,
               approved_by_actor_id=?,resolution_reason=?,resolution_request_id=?,updated_at=?
               WHERE approval_id=? AND status='PENDING'""",
            (now, approver.actor_id, sanitize_audit_text(reason) or None,
             approver.request_id or str(uuid.uuid4()), now, approval_id),
        )
        if cursor.rowcount == 1:
            changed = db.execute(
                "SELECT * FROM internal_approval_requests WHERE approval_id=?", (approval_id,)
            ).fetchone()
            _audit_transition(
                "approval.approved", changed, actor=approver, result=SUCCESS,
                reason=reason, transaction_connection=db,
            )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    if cursor.rowcount != 1:
        raise ApprovalDenied("INVALID_APPROVAL_STATUS", "Approval nie jest PENDING")


def reject_request(approval_id: str, approver_context: ActorContext, *, reason: str = "") -> None:
    approver = _trusted_actor(approver_context)
    row = _expire_if_needed(_request_row(approval_id))
    if row is None:
        raise ApprovalDenied("APPROVAL_NOT_FOUND", "Nie znaleziono approval")
    policy = get_policy(row["operation"], row["operation_version"])
    if approver.actor_type != ACTOR_HUMAN or approver.permission_decision(policy.approver_permission) != ALLOW:
        raise ApprovalDenied("APPROVER_PERMISSION_DENIED", "Brak prawa do odrzucenia approval")
    now = _iso(_utc_now_dt())
    db = _factory()()
    try:
        cursor = db.execute(
            """UPDATE internal_approval_requests SET status='REJECTED',resolved_at=?,
               approved_by_actor_id=?,resolution_reason=?,resolution_request_id=?,updated_at=?
               WHERE approval_id=? AND status='PENDING'""",
            (now, approver.actor_id, sanitize_audit_text(reason) or None,
             approver.request_id or str(uuid.uuid4()), now, approval_id),
        )
        if cursor.rowcount == 1:
            changed = db.execute(
                "SELECT * FROM internal_approval_requests WHERE approval_id=?", (approval_id,)
            ).fetchone()
            _audit_transition(
                "approval.rejected", changed, actor=approver, result=DENIED,
                reason=reason, transaction_connection=db,
            )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    if cursor.rowcount != 1:
        raise ApprovalDenied("INVALID_APPROVAL_STATUS", "Approval nie jest PENDING")


def cancel_request(approval_id: str, actor_context: ActorContext, *, reason: str = "") -> None:
    actor = _trusted_actor(actor_context)
    row = _request_row(approval_id)
    if row is None:
        raise ApprovalDenied("APPROVAL_NOT_FOUND", "Nie znaleziono approval")
    if actor.actor_id != row["requesting_actor_id"] and actor.permission_decision("approvals.decide") != ALLOW:
        raise ApprovalDenied("CANCEL_PERMISSION_DENIED", "Brak prawa do anulowania approval")
    now = _iso(_utc_now_dt())
    db = _factory()()
    try:
        cursor = db.execute(
            """UPDATE internal_approval_requests SET status='CANCELLED',resolved_at=?,
               resolution_reason=?,resolution_request_id=?,updated_at=?
               WHERE approval_id=? AND status='PENDING'""",
            (now, sanitize_audit_text(reason) or None, actor.request_id, now, approval_id),
        )
        if cursor.rowcount == 1:
            changed = db.execute(
                "SELECT * FROM internal_approval_requests WHERE approval_id=?", (approval_id,)
            ).fetchone()
            _audit_transition(
                "approval.cancelled", changed, actor=actor, result=DENIED,
                reason=reason, transaction_connection=db,
            )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    if cursor.rowcount != 1:
        raise ApprovalDenied("INVALID_APPROVAL_STATUS", "Approval nie jest PENDING")


def _deny(row, actor, code: str, message: str, *, stale: bool = False, transaction_connection=None):
    if transaction_connection is not None:
        transaction_connection.rollback()
    _audit_transition("approval.stale" if stale else "approval.execution_denied", row, actor=actor, result=CONFLICT if stale else DENIED, reason=message, error_code=code)
    exc = StaleApproval if stale else ApprovalDenied
    raise exc(code, message)


def authorize_execution(
    approval_id: str,
    requesting_actor_context: ActorContext,
    operation: str,
    *, payload: Mapping[str, Any], entity_type: str, entity_id: str,
    operation_version: int = 1, expected_entity_version: int | None = None,
    current_entity_version: int | None = None,
    claimed_risk_level: str | None = None,
    transaction_connection: sqlite3.Connection | None = None,
) -> None:
    """Revalidate current state and atomically claim one approval for execution."""
    del claimed_risk_level
    actor = _trusted_actor(requesting_actor_context)
    def deny(code: str, message: str, *, stale: bool = False):
        _deny(row, actor, code, message, stale=stale, transaction_connection=transaction_connection)
    row = _expire_if_needed(_request_row(approval_id))
    if row is None:
        raise ApprovalDenied("APPROVAL_NOT_FOUND", "Nie znaleziono approval")
    if row["status"] != APPROVED:
        deny("INVALID_APPROVAL_STATUS", f"Approval ma status {row['status']}")
    if actor.actor_id != row["requesting_actor_id"]:
        deny("REQUESTING_ACTOR_CHANGED", "Approval należy do innego aktora", stale=True)
    try:
        policy = get_policy(operation, operation_version)
    except ApprovalDenied as exc:
        deny(exc.code, str(exc), stale=True)
    if actor.permission_decision(policy.permission) == PERMISSION_DENY:
        deny("PERMISSION_REVOKED", "Permission został odebrany")
    if operation != row["operation"] or int(operation_version) != int(row["operation_version"]):
        deny("OPERATION_CHANGED", "Operacja lub jej wersja zmieniła się", stale=True)
    if policy.policy_id != row["policy_id"] or policy.permission != row["permission"] or RISK_ORDER[policy.risk_level] > RISK_ORDER[row["risk_level"]] or (policy.requires_approval and not bool(row["approved_by_actor_id"])):
        deny("POLICY_CHANGED", "Aktualna polityka jest bardziej restrykcyjna", stale=True)
    current_policy_expiry = _parse_time(row["requested_at"]) + timedelta(seconds=policy.expiry_seconds)
    if current_policy_expiry <= _utc_now_dt():
        deny("POLICY_CHANGED", "Aktualna polityka skróciła ważność approval", stale=True)
    fingerprint, _ = operation_fingerprint(
        operation=operation, operation_version=operation_version,
        entity_type=entity_type, entity_id=entity_id, payload=payload,
        requesting_actor_id=actor.actor_id, expected_entity_version=expected_entity_version,
    )
    if fingerprint != row["operation_fingerprint"]:
        deny("STALE_APPROVAL", "Fingerprint operacji nie jest już aktualny", stale=True)
    if row["expected_entity_version"] is not None and current_entity_version != row["expected_entity_version"]:
        deny("ENTITY_VERSION_CONFLICT", "Wersja obiektu zmieniła się", stale=True)
    approved_by = row["approved_by_actor_id"]
    approved_actor = load_actor_context(str(approved_by), source="approval_execution_recheck")
    if approved_actor is None or approved_actor.actor_type != ACTOR_HUMAN:
        deny("INVALID_APPROVER", "Approval nie pochodzi od aktywnego HUMAN")
    if approved_actor.permission_decision(policy.approver_permission) != ALLOW:
        deny("APPROVER_PERMISSION_REVOKED", "Approver utracił wymagane permission")
    if approved_actor.actor_id == actor.actor_id and not policy.self_approval_allowed:
        deny("SELF_APPROVAL_DENIED", "Aktualna polityka zabrania self approval", stale=True)
    now = _iso(_utc_now_dt())
    owns_transaction = transaction_connection is None
    db = transaction_connection or _factory()()
    try:
        if owns_transaction:
            db.execute("BEGIN IMMEDIATE")
        cursor = db.execute(
            """UPDATE internal_approval_requests SET status='CONSUMED',consumed_at=?,updated_at=?
               WHERE approval_id=? AND status='APPROVED' AND expires_at>?""",
            (now, now, approval_id, now),
        )
        if cursor.rowcount == 1:
            consumed = db.execute(
                "SELECT * FROM internal_approval_requests WHERE approval_id=?", (approval_id,)
            ).fetchone()
            _audit_transition(
                "approval.consumed", consumed, actor=actor, result=SUCCESS,
                reason="Approval atomowo skonsumowany", transaction_connection=db,
            )
            if owns_transaction:
                db.commit()
        elif owns_transaction:
            db.rollback()
    except Exception:
        if owns_transaction:
            db.rollback()
        raise
    finally:
        if owns_transaction:
            db.close()
    if cursor.rowcount != 1:
        latest = _request_row(approval_id)
        _deny(
            latest, actor, "ALREADY_CONSUMED", "Approval został już wykorzystany lub wygasł",
            transaction_connection=transaction_connection,
        )


consume_approval = authorize_execution


def execute_pilot_change(
    approval_id: str, actor_context: ActorContext, resource_id: str,
    *, payload: Mapping[str, Any], expected_entity_version: int,
) -> Any:
    """Execute the isolated YELLOW pilot after the central gate succeeds."""
    from internal_concurrency import get_versioned_resource, update_versioned_resource
    current = get_versioned_resource(resource_id)
    if current is None:
        raise KeyError(resource_id)
    authorize_execution(
        approval_id, actor_context, "internal.test.change_setting",
        payload=payload, entity_type="internal_versioned_resource", entity_id=resource_id,
        operation_version=1, expected_entity_version=expected_entity_version,
        current_entity_version=current.version,
    )
    row = _request_row(approval_id)
    return update_versioned_resource(
        resource_id, expected_entity_version, payload,
        actor_context=actor_context, correlation_id=row["correlation_id"],
    )
