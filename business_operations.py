"""Closed registry and execution gate for trusted business operations.

The module deliberately exposes operations, not SQL, tables, endpoints or
connections.  Identity, permissions and risk are reloaded from backend-owned
state for every execution.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

import internal_approval as approvals
from internal_audit import (
    CONFLICT, DENIED, FAILED, NOOP, PENDING_APPROVAL, SUCCESS,
    current_correlation_id, record_audit_event, sanitize_audit_data,
    sanitize_audit_text,
)
from internal_rbac import ACTOR_TYPES, DENY as PERMISSION_DENY, ActorContext, load_actor_context


IDEMPOTENCY_NONE = "NONE"
IDEMPOTENCY_OPTIONAL = "OPTIONAL"
IDEMPOTENCY_REQUIRED = "REQUIRED"
IDEMPOTENCY_MODES = frozenset({IDEMPOTENCY_NONE, IDEMPOTENCY_OPTIONAL, IDEMPOTENCY_REQUIRED})

EXECUTION_STATUSES = frozenset({
    "CREATED", "PENDING_APPROVAL", "AUTHORIZED", "RUNNING",
    "SUCCESS", "FAILED", "CONFLICT", "DENIED",
})
TERMINAL_STATUSES = frozenset({"SUCCESS", "FAILED", "CONFLICT", "DENIED"})
SAFE_IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9._:-]{1,200}")


@dataclass(frozen=True)
class BusinessOperationDefinition:
    operation_name: str
    operation_version: int
    description: str
    required_permission: str
    risk_level: str
    approval_requirement: str
    actor_types_allowed: frozenset[str]
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    idempotency_requirement: str
    audit_policy: str
    read_only: bool
    enabled: bool = True

    def __post_init__(self):
        if not re.fullmatch(r"[a-z][a-z0-9_.]{2,127}", self.operation_name):
            raise ValueError("Nieprawidłowa stabilna nazwa Business Operation")
        if self.operation_version < 1:
            raise ValueError("operation_version musi być dodatnie")
        if self.risk_level not in approvals.RISK_LEVELS:
            raise ValueError("Nieprawidłowy risk level")
        if self.idempotency_requirement not in IDEMPOTENCY_MODES:
            raise ValueError("Nieprawidłowy tryb idempotency")
        if not self.actor_types_allowed or not self.actor_types_allowed <= ACTOR_TYPES:
            raise ValueError("Nieprawidłowe actor_types_allowed")


@dataclass(frozen=True)
class OperationResult:
    status: str
    data: Any
    operation: str
    operation_version: int
    execution_id: str
    request_id: str
    correlation_id: str
    approval_id: str = ""
    error_code: str = ""
    safe_error_message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ControlledOperationError(RuntimeError):
    def __init__(self, error_code: str, safe_message: str, *, status: str = FAILED):
        self.error_code = error_code
        self.safe_message = sanitize_audit_text(safe_message)
        self.status = status
        super().__init__(self.safe_message)


PRODUCT_GET_INPUT = {
    "type": "object",
    "additionalProperties": False,
    "required": ["product_id"],
    "properties": {"product_id": {"type": "integer", "minimum": 1, "maximum": 9_223_372_036_854_775_807}},
}
PRODUCT_GET_OUTPUT = {
    "type": "object",
    "required": ["ok", "id", "sku", "model", "ean", "name", "stock"],
    "properties": {
        "ok": {"type": "boolean"}, "id": {"type": "integer"},
        "sku": {"type": "string"}, "model": {"type": ["string", "null"]},
        "ean": {"type": ["string", "null"]}, "name": {"type": ["string", "null"]},
        "stock": {"type": "integer"},
    },
}
PRODUCT_SEARCH_INPUT = {
    "type": "object", "additionalProperties": False, "required": ["query"],
    "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 120}},
}
_PRODUCT_FIELDS = {
    "type": "object", "additionalProperties": False,
    "required": ["id", "sku", "model", "name", "stock"],
    "properties": {
        "id": {"type": "integer"}, "sku": {"type": "string"},
        "model": {"type": ["string", "null"]}, "name": {"type": ["string", "null"]},
        "stock": {"type": "integer"},
    },
}
PRODUCT_SEARCH_OUTPUT = {
    "type": "object", "additionalProperties": False,
    "required": ["ok", "query", "candidates", "count", "truncated"],
    "properties": {
        "ok": {"type": "boolean"}, "query": {"type": "string"},
        "candidates": {"type": "array", "maxItems": 10, "items": _PRODUCT_FIELDS},
        "count": {"type": "integer"}, "truncated": {"type": "boolean"},
    },
}
PILOT_INPUT = {
    "type": "object",
    "additionalProperties": False,
    "required": ["resource_id", "expected_version", "amount"],
    "properties": {
        "resource_id": {"type": "string", "format": "uuid", "maxLength": 64},
        "expected_version": {"type": "integer", "minimum": 1, "maximum": 2_147_483_647},
        "amount": {"type": "integer", "minimum": -1_000_000_000, "maximum": 1_000_000_000},
    },
}
PILOT_OUTPUT = {
    "type": "object",
    "required": ["resource_id", "version", "amount"],
    "properties": {
        "resource_id": {"type": "string"}, "version": {"type": "integer"},
        "amount": {"type": "integer"},
    },
}
EXTERNAL_TEST_INPUT = {
    "type": "object",
    "additionalProperties": False,
    "required": ["scenario", "value"],
    "properties": {
        "scenario": {"type": "string", "maxLength": 64, "enum": [
            "success", "timeout_before_send", "timeout_after_side_effect", "lost_response",
            "http_500", "http_400", "rate_limit", "remote_absent",
            "reconciliation_failure", "still_unknown", "crash_before_send",
            "crash_after_remote_success", "duplicate", "secret_error",
        ]},
        "value": {"type": "string", "maxLength": 200},
    },
}
EXTERNAL_TEST_OUTPUT = {
    "type": "object",
    "required": ["queue_status"],
    "properties": {"queue_status": {"type": "string"}},
}


OPERATION_REGISTRY: dict[str, BusinessOperationDefinition] = {
    "inventory.product.search": BusinessOperationDefinition(
        "inventory.product.search", 1, "Wyszukuje produkty po SKU, modelu lub nazwie i zwraca ograniczony stan.",
        "inventory.read", approvals.GREEN, "NONE", frozenset({"HUMAN", "SYSTEM", "AI_AGENT"}),
        PRODUCT_SEARCH_INPUT, PRODUCT_SEARCH_OUTPUT, IDEMPOTENCY_NONE, "READ_STANDARD", True,
    ),
    "inventory.product.get": BusinessOperationDefinition(
        "inventory.product.get", 1, "Pobiera minimalny wewnętrzny widok produktu i stanu.",
        "inventory.read", approvals.GREEN, "NONE",
        frozenset({"HUMAN", "SYSTEM", "AI_AGENT"}),
        PRODUCT_GET_INPUT, PRODUCT_GET_OUTPUT, IDEMPOTENCY_NONE, "READ_STANDARD", True,
    ),
    "internal.test.change_setting": BusinessOperationDefinition(
        "internal.test.change_setting", 1, "Zmienia wyłącznie izolowany zasób testowy.",
        "internal.test.change_setting", approvals.YELLOW, "REQUIRED",
        frozenset({"HUMAN", "AI_AGENT"}),
        PILOT_INPUT, PILOT_OUTPUT, IDEMPOTENCY_REQUIRED, "WRITE", False,
    ),
    "internal.test.external.execute": BusinessOperationDefinition(
        "internal.test.external.execute", 1, "Uruchamia wyłącznie izolowany adapter zewnętrzny bez sieci.",
        "internal.test.change_setting", approvals.YELLOW, "REQUIRED",
        frozenset({"HUMAN", "AI_AGENT"}),
        EXTERNAL_TEST_INPUT, EXTERNAL_TEST_OUTPUT, IDEMPOTENCY_REQUIRED, "WRITE", False,
    ),
}


_connection_factory: Callable[[], sqlite3.Connection] | None = None
_configuration_lock = threading.Lock()


def configure(connection_factory: Callable[[], sqlite3.Connection]) -> None:
    if not callable(connection_factory):
        raise TypeError("connection_factory musi być wywoływalne")
    global _connection_factory
    with _configuration_lock:
        _connection_factory = connection_factory


def _factory() -> Callable[[], sqlite3.Connection]:
    if _connection_factory is None:
        raise RuntimeError("Business Operations nie zostały skonfigurowane")
    return _connection_factory


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def initialize_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS internal_operation_executions(
            execution_id TEXT PRIMARY KEY,
            operation TEXT NOT NULL,
            operation_version INTEGER NOT NULL CHECK(operation_version > 0),
            actor_id TEXT NOT NULL REFERENCES internal_actors(actor_id) ON DELETE RESTRICT,
            actor_type TEXT NOT NULL CHECK(actor_type IN ('HUMAN','SYSTEM','AI_AGENT')),
            permission TEXT NOT NULL REFERENCES internal_permissions(permission_key) ON DELETE RESTRICT,
            risk_level TEXT NOT NULL CHECK(risk_level IN ('GREEN','YELLOW','RED')),
            approval_id TEXT REFERENCES internal_approval_requests(approval_id) ON DELETE RESTRICT,
            entity_type TEXT,
            entity_id TEXT,
            idempotency_key TEXT,
            input_fingerprint TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('CREATED','PENDING_APPROVAL','AUTHORIZED','RUNNING','SUCCESS','FAILED','CONFLICT','DENIED')),
            created_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            request_id TEXT NOT NULL,
            correlation_id TEXT NOT NULL,
            error_code TEXT,
            safe_error_message TEXT,
            result_summary TEXT,
            updated_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uq_internal_operation_idempotency
            ON internal_operation_executions(operation,operation_version,actor_id,idempotency_key)
            WHERE idempotency_key IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_internal_operation_execution_status
            ON internal_operation_executions(status,created_at);
        CREATE INDEX IF NOT EXISTS idx_internal_operation_execution_correlation
            ON internal_operation_executions(correlation_id);
        CREATE INDEX IF NOT EXISTS idx_internal_operation_execution_approval
            ON internal_operation_executions(approval_id);
        """
    )
    db.commit()


def _trusted_actor(actor: ActorContext | None) -> ActorContext:
    if actor is None or not isinstance(actor, ActorContext):
        raise ControlledOperationError("UNTRUSTED_ACTOR", "Brak zaufanego ActorContext", status=DENIED)
    trusted = load_actor_context(
        actor.actor_id, request_id=actor.request_id, session_id=actor.session_id,
        credential_id=actor.credential_id, delegated_by_actor_id=actor.delegated_by_actor_id,
        reason=actor.reason, source=actor.source,
    )
    if trusted is None or trusted.actor_type != actor.actor_type:
        raise ControlledOperationError("UNTRUSTED_ACTOR", "Tożsamość aktora jest niezaufana", status=DENIED)
    return trusted


def _validate_value(name: str, value: Any, rule: Mapping[str, Any]) -> Any:
    expected = rule.get("type")
    if expected == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ControlledOperationError("INVALID_INPUT", f"Pole {name} musi być liczbą całkowitą", status=DENIED)
        if value < rule.get("minimum", value) or value > rule.get("maximum", value):
            raise ControlledOperationError("INVALID_INPUT", f"Pole {name} przekracza dozwolony zakres", status=DENIED)
    elif expected == "string":
        if not isinstance(value, str) or len(value) < rule.get("minLength", 1) or len(value) > rule.get("maxLength", 2_000):
            raise ControlledOperationError("INVALID_INPUT", f"Pole {name} ma nieprawidłową wartość", status=DENIED)
        if rule.get("format") == "uuid":
            try:
                value = str(uuid.UUID(value))
            except (ValueError, AttributeError, TypeError):
                raise ControlledOperationError("INVALID_INPUT", f"Pole {name} musi być UUID", status=DENIED)
    if "enum" in rule and value not in rule["enum"]:
        raise ControlledOperationError("INVALID_INPUT", f"Pole {name} ma wartość spoza enum", status=DENIED)
    return value


def validate_input(definition: BusinessOperationDefinition, supplied: Any) -> dict[str, Any]:
    if not isinstance(supplied, Mapping):
        raise ControlledOperationError("INVALID_INPUT", "Input musi być obiektem", status=DENIED)
    properties = definition.input_schema["properties"]
    unknown = set(supplied) - set(properties)
    if unknown:
        raise ControlledOperationError("UNKNOWN_INPUT_FIELD", "Input zawiera niedozwolone pola", status=DENIED)
    missing = set(definition.input_schema.get("required", ())) - set(supplied)
    if missing:
        raise ControlledOperationError("MISSING_INPUT_FIELD", "Brakuje wymaganego pola", status=DENIED)
    return {name: _validate_value(name, supplied[name], properties[name]) for name in supplied}


def validate_output(definition: BusinessOperationDefinition, supplied: Any) -> dict[str, Any]:
    if not isinstance(supplied, Mapping):
        raise ControlledOperationError("INVALID_HANDLER_OUTPUT", "Handler zwrócił nieprawidłowy wynik")
    schema = definition.output_schema
    if set(schema.get("required", ())) - set(supplied):
        raise ControlledOperationError("INVALID_HANDLER_OUTPUT", "Handler nie zwrócił wymaganych pól")
    if set(supplied) - set(schema.get("properties", {})):
        raise ControlledOperationError("INVALID_HANDLER_OUTPUT", "Handler zwrócił niedozwolone pola")
    for name, value in supplied.items():
        expected = schema["properties"][name].get("type")
        allowed = set(expected if isinstance(expected, list) else [expected])
        actual = "null" if value is None else "boolean" if isinstance(value, bool) else "integer" if isinstance(value, int) else "number" if isinstance(value, float) else "string" if isinstance(value, str) else "array" if isinstance(value, list) else "object" if isinstance(value, Mapping) else "other"
        if actual not in allowed:
            raise ControlledOperationError("INVALID_HANDLER_OUTPUT", f"Handler zwrócił zły typ pola {name}")
        if actual == "array":
            if len(value) > schema["properties"][name].get("maxItems", 100):
                raise ControlledOperationError("INVALID_HANDLER_OUTPUT", f"Handler zwrócił za dużo elementów pola {name}")
            item_schema = schema["properties"][name].get("items", {})
            for item in value:
                if not isinstance(item, Mapping):
                    raise ControlledOperationError("INVALID_HANDLER_OUTPUT", f"Handler zwrócił zły element pola {name}")
                if set(item_schema.get("required", ())) - set(item) or set(item) - set(item_schema.get("properties", {})):
                    raise ControlledOperationError("INVALID_HANDLER_OUTPUT", f"Handler zwrócił nieprawidłowe pola {name}")
    return dict(supplied)


def _fingerprint(definition: BusinessOperationDefinition, actor: ActorContext, data: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        {"operation": definition.operation_name, "version": definition.operation_version,
         "actor_id": actor.actor_id, "input": sanitize_audit_data(dict(data))},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _entity(definition: BusinessOperationDefinition, data: Mapping[str, Any]) -> tuple[str, str, int | None]:
    if definition.operation_name == "inventory.product.search":
        return "product_search", sanitize_audit_text(data["query"])[:120], None
    if definition.operation_name == "inventory.product.get":
        return "product", str(data["product_id"]), None
    if definition.operation_name == "internal.test.change_setting":
        return "internal_versioned_resource", data["resource_id"], data["expected_version"]
    if definition.operation_name == "internal.test.external.execute":
        return "external_test_operation", str(data["value"]), None
    raise ControlledOperationError("REGISTRY_INCONSISTENT", "Brak mapowania obiektu", status=DENIED)


def _result_from_row(row, *, status: str | None = None) -> OperationResult:
    data = json.loads(row["result_summary"]) if row["result_summary"] else None
    return OperationResult(
        status=status or row["status"], data=data, operation=row["operation"],
        operation_version=int(row["operation_version"]), execution_id=row["execution_id"],
        request_id=row["request_id"], correlation_id=row["correlation_id"],
        approval_id=row["approval_id"] or "", error_code=row["error_code"] or "",
        safe_error_message=row["safe_error_message"] or "",
    )


def _execution(execution_id: str):
    db = _factory()()
    try:
        return db.execute("SELECT * FROM internal_operation_executions WHERE execution_id=?", (execution_id,)).fetchone()
    finally:
        db.close()


def _idempotent_execution(definition, actor, key):
    if not key:
        return None
    db = _factory()()
    try:
        return db.execute(
            """SELECT * FROM internal_operation_executions
               WHERE operation=? AND operation_version=? AND actor_id=? AND idempotency_key=?""",
            (definition.operation_name, definition.operation_version, actor.actor_id, key),
        ).fetchone()
    finally:
        db.close()


def _audit(event: str, definition, actor, row, result: str, *, error_code="", message="", transaction_connection=None):
    record_audit_event(
        event, result=result, actor_context=actor,
        permission=definition.required_permission,
        entity_type=row["entity_type"] or "business_operation",
        entity_id=row["entity_id"] or row["execution_id"],
        correlation_id=row["correlation_id"], approval_required=bool(row["approval_id"]),
        approval_id=row["approval_id"] or "", risk_level=definition.risk_level,
        error_code=error_code, error_message=message,
        idempotency_key=row["idempotency_key"] or "",
        after_state={"execution_id": row["execution_id"], "execution_status": row["status"],
                     "target_operation": definition.operation_name,
                     "target_operation_version": definition.operation_version},
        source="business_operations", transaction_connection=transaction_connection,
    )


def _create_execution(definition, actor, fingerprint, entity_type, entity_id, expected_version, key, correlation):
    execution_id = str(uuid.uuid4())
    now = _now()
    db = _factory()()
    try:
        db.execute(
            """INSERT INTO internal_operation_executions(
                execution_id,operation,operation_version,actor_id,actor_type,permission,
                risk_level,entity_type,entity_id,idempotency_key,input_fingerprint,status,
                created_at,request_id,correlation_id,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,'CREATED',?,?,?,?)""",
            (execution_id, definition.operation_name, definition.operation_version,
             actor.actor_id, actor.actor_type, definition.required_permission,
             definition.risk_level, entity_type, entity_id, key or None, fingerprint,
             now, actor.request_id or str(uuid.uuid4()), correlation, now),
        )
        row = db.execute("SELECT * FROM internal_operation_executions WHERE execution_id=?", (execution_id,)).fetchone()
        if not definition.read_only:
            _audit("business_operation.requested", definition, actor, row, SUCCESS, transaction_connection=db)
        db.commit()
        return row
    except sqlite3.IntegrityError:
        db.rollback()
        existing = _idempotent_execution(definition, actor, key)
        if existing is not None:
            return existing
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _persist_gate_denial(definition, actor, input_data, correlation, error):
    fingerprint = _fingerprint(definition, actor, input_data if isinstance(input_data, Mapping) else {"invalid_input": True})
    row = _create_execution(definition, actor, fingerprint, "business_operation", definition.operation_name, None, "", correlation)
    changed = _transition(
        row["execution_id"], definition, actor, "DENIED", "business_operation.denied", DENIED,
        error_code=error.error_code, message=error.safe_message, completed=True,
        expected_statuses=("CREATED",),
    )
    return _result_from_row(changed)


def _transition(execution_id, definition, actor, status, event, result, *, approval_id=None, error_code="", message="", data=None, started=False, completed=False, expected_statuses=()):
    now = _now()
    db = _factory()()
    try:
        assignments = ["status=?", "updated_at=?"]
        values: list[Any] = [status, now]
        if approval_id is not None:
            assignments.append("approval_id=?"); values.append(approval_id or None)
        if started:
            assignments.append("started_at=COALESCE(started_at,?)"); values.append(now)
        if completed:
            assignments.append("completed_at=?"); values.append(now)
        if error_code:
            assignments.extend(["error_code=?", "safe_error_message=?"])
            values.extend([error_code[:128], sanitize_audit_text(message)])
        if data is not None:
            assignments.append("result_summary=?")
            values.append(json.dumps(sanitize_audit_data(data), ensure_ascii=False, separators=(",", ":")))
        sql = "UPDATE internal_operation_executions SET " + ",".join(assignments) + " WHERE execution_id=?"
        values.append(execution_id)
        if expected_statuses:
            sql += " AND status IN (" + ",".join("?" for _ in expected_statuses) + ")"
            values.extend(expected_statuses)
        cursor = db.execute(sql, values)
        if cursor.rowcount != 1:
            db.rollback(); return None
        row = db.execute("SELECT * FROM internal_operation_executions WHERE execution_id=?", (execution_id,)).fetchone()
        if event:
            _audit(event, definition, actor, row, result, error_code=error_code, message=message, transaction_connection=db)
        db.commit()
        return row
    except Exception:
        db.rollback(); raise
    finally:
        db.close()


def _product_get(data, actor, correlation_id, transaction_connection=None):
    db = transaction_connection or _factory()()
    try:
        row = db.execute(
            """SELECT p.id,p.sku,p.model,p.ean,p.name,COALESCE(s.qty,0) AS stock
               FROM products p LEFT JOIN stock s ON s.product_id=p.id
               WHERE p.id=? AND COALESCE(p.archived,0)=0""",
            (data["product_id"],),
        ).fetchone()
    finally:
        if transaction_connection is None:
            db.close()
    if row is None:
        raise ControlledOperationError("PRODUCT_NOT_FOUND", "Nie znaleziono produktu", status=NOOP)
    return {"ok": True, "id": row["id"], "sku": row["sku"], "model": row["model"],
            "ean": row["ean"], "name": row["name"], "stock": int(row["stock"])}


def _product_search(data, actor, correlation_id, transaction_connection=None):
    query = " ".join(str(data["query"]).strip().lower().split())
    tokens = [token for token in re.split(r"[^a-z0-9ąćęłńóśźż]+", query) if token][:8]
    if not tokens:
        raise ControlledOperationError("INVALID_INPUT", "Fraza wyszukiwania jest pusta", status=DENIED)
    haystack = "lower(coalesce(p.sku,'') || ' ' || coalesce(p.model,'') || ' ' || coalesce(p.name,'') || ' ' || coalesce(p.ean,''))"
    where = " AND ".join(f"{haystack} LIKE ?" for _ in tokens)
    params = [f"%{token}%" for token in tokens]
    db = transaction_connection or _factory()()
    try:
        found = db.execute(
            f"""SELECT p.id,p.sku,p.model,p.name,COALESCE(s.qty,0) stock
                FROM products p LEFT JOIN stock s ON s.product_id=p.id
                WHERE COALESCE(p.archived,0)=0 AND {where}
                ORDER BY CASE WHEN lower(coalesce(p.sku,''))=? THEN 0
                              WHEN lower(coalesce(p.model,''))=? THEN 1 ELSE 2 END,
                         p.sku,p.id LIMIT 11""",
            (*params, query, query),
        ).fetchall()
    finally:
        if transaction_connection is None:
            db.close()
    candidates = [{"id": int(row["id"]), "sku": row["sku"] or "", "model": row["model"],
                   "name": row["name"], "stock": int(row["stock"])} for row in found[:10]]
    return {"ok": True, "query": query, "candidates": candidates,
            "count": len(candidates), "truncated": len(found) > 10}


def _pilot_change(data, actor, correlation_id, transaction_connection=None):
    if transaction_connection is None:
        raise ControlledOperationError("TRANSACTION_REQUIRED", "Pilot wymaga wspólnej transakcji")
    db = transaction_connection
    current = db.execute(
        "SELECT * FROM internal_versioned_resources WHERE resource_id=?", (data["resource_id"],)
    ).fetchone()
    if current is None:
        raise ControlledOperationError("RESOURCE_NOT_FOUND", "Nie znaleziono zasobu", status=CONFLICT)
    if int(current["version"]) != data["expected_version"]:
        raise ControlledOperationError("ENTITY_VERSION_CONFLICT", "Wersja obiektu zmieniła się", status=CONFLICT)
    before = json.loads(current["payload_json"])
    after = {"amount": data["amount"]}
    new_version = data["expected_version"] + 1
    cursor = db.execute(
        """UPDATE internal_versioned_resources SET payload_json=?,version=?,updated_at=?
           WHERE resource_id=? AND version=?""",
        (json.dumps(after, ensure_ascii=False), new_version, _now(), data["resource_id"], data["expected_version"]),
    )
    if cursor.rowcount != 1:
        raise ControlledOperationError("ENTITY_VERSION_CONFLICT", "Wersja obiektu zmieniła się", status=CONFLICT)
    record_audit_event(
        "internal.versioned_resource.update", result=SUCCESS, actor_context=actor,
        entity_type="internal_versioned_resource", entity_id=data["resource_id"],
        correlation_id=correlation_id, before_state=before, after_state=after,
        entity_version_before=data["expected_version"], entity_version_after=new_version,
        transaction_connection=db,
    )
    return {"resource_id": data["resource_id"], "version": new_version, "amount": data["amount"]}


_HANDLERS: dict[str, Callable[[Mapping[str, Any], ActorContext, str, sqlite3.Connection | None], Any]] = {
    "inventory.product.search": _product_search,
    "inventory.product.get": _product_get,
    "internal.test.change_setting": _pilot_change,
    # External operations are queued below and are never called through this map.
    "internal.test.external.execute": lambda *_args, **_kwargs: None,
}


def _execute_local_approved(
    execution_id, definition, actor, data, stored_approval, entity_type, entity_id, expected_version, correlation_id
):
    """Consume approval, mutate pilot and record success in one SQLite transaction."""
    db = _factory()()
    try:
        db.execute("BEGIN IMMEDIATE")
        current = db.execute(
            "SELECT version FROM internal_versioned_resources WHERE resource_id=?", (entity_id,)
        ).fetchone()
        current_version = int(current["version"]) if current else None
        approvals.authorize_execution(
            stored_approval, actor, definition.operation_name, payload=data,
            entity_type=entity_type, entity_id=entity_id,
            operation_version=definition.operation_version,
            expected_entity_version=expected_version,
            current_entity_version=current_version,
            transaction_connection=db,
        )
        output = validate_output(
            definition,
            _HANDLERS[definition.operation_name](data, actor, correlation_id, db),
        )
        now = _now()
        db.execute(
            """UPDATE internal_operation_executions
               SET status='SUCCESS',completed_at=?,updated_at=?,result_summary=?
               WHERE execution_id=? AND status='RUNNING'""",
            (now, now, json.dumps(sanitize_audit_data(output), ensure_ascii=False, separators=(",", ":")), execution_id),
        )
        row = db.execute(
            "SELECT * FROM internal_operation_executions WHERE execution_id=?", (execution_id,)
        ).fetchone()
        _audit("business_operation.success", definition, actor, row, SUCCESS, transaction_connection=db)
        db.commit()
        return _result_from_row(row)
    except approvals.StaleApproval as exc:
        db.rollback()
        row = _transition(execution_id, definition, actor, "CONFLICT", "business_operation.conflict", CONFLICT,
                          error_code=exc.code, message=str(exc), completed=True)
        return _result_from_row(row)
    except approvals.ApprovalDenied as exc:
        db.rollback()
        row = _transition(execution_id, definition, actor, "DENIED", "business_operation.denied", DENIED,
                          error_code=exc.code, message=str(exc), completed=True)
        return _result_from_row(row)
    except ControlledOperationError as exc:
        db.rollback()
        terminal = "CONFLICT" if exc.status == CONFLICT else "FAILED"
        event = "business_operation.conflict" if exc.status == CONFLICT else "business_operation.failed"
        result = CONFLICT if exc.status == CONFLICT else FAILED
        row = _transition(execution_id, definition, actor, terminal, event, result,
                          error_code=exc.error_code, message=exc.safe_message, completed=True)
        return _result_from_row(row)
    except Exception as exc:
        db.rollback()
        row = _transition(execution_id, definition, actor, "FAILED", "business_operation.failed", FAILED,
                          error_code="HANDLER_FAILED", message=sanitize_audit_text(exc), completed=True)
        return _result_from_row(row)
    finally:
        db.close()


def _safe_denial(operation: str, version: int, execution_id: str, actor, correlation, code: str, message: str, *, status=DENIED):
    return OperationResult(
        status=status, data=None, operation=operation, operation_version=version,
        execution_id=execution_id, request_id=getattr(actor, "request_id", "") or str(uuid.uuid4()),
        correlation_id=correlation, error_code=code,
        safe_error_message=sanitize_audit_text(message),
    )


def execute_business_operation(
    actor_context: ActorContext,
    operation_name: str,
    input_data: Mapping[str, Any],
    *,
    idempotency_key: str = "",
    approval_id: str = "",
    correlation_id: str = "",
    claimed_permission: str = "",
    claimed_risk_level: str = "",
    claimed_actor_type: str = "",
) -> OperationResult:
    """Execute one explicitly registered operation through the central gate."""
    del claimed_permission, claimed_risk_level, claimed_actor_type
    execution_id = str(uuid.uuid4())
    correlation = current_correlation_id(correlation_id)
    definition = OPERATION_REGISTRY.get(str(operation_name))
    if definition is None:
        return _safe_denial(str(operation_name), 0, execution_id, actor_context, correlation, "UNKNOWN_OPERATION", "Operacja nie jest zarejestrowana")
    try:
        actor = _trusted_actor(actor_context)
        if not definition.enabled:
            raise ControlledOperationError("OPERATION_DISABLED", "Operacja jest wyłączona", status=DENIED)
        if definition.operation_name not in _HANDLERS:
            raise ControlledOperationError("HANDLER_NOT_FOUND", "Brak bezpiecznego handlera", status=DENIED)
        data = validate_input(definition, input_data)
        if actor.actor_type not in definition.actor_types_allowed:
            raise ControlledOperationError("ACTOR_TYPE_DENIED", "Ten typ aktora nie może wykonać operacji", status=DENIED)
        policy = approvals.get_policy(definition.operation_name, definition.operation_version)
        if policy.permission != definition.required_permission or policy.risk_level != definition.risk_level:
            raise ControlledOperationError("REGISTRY_POLICY_MISMATCH", "Registry i polityka są niespójne", status=DENIED)
        if actor.permission_decision(definition.required_permission) == PERMISSION_DENY:
            raise ControlledOperationError("PERMISSION_DENIED", "Brak wymaganego permission", status=DENIED)
        if definition.idempotency_requirement == IDEMPOTENCY_REQUIRED and not SAFE_IDEMPOTENCY_KEY.fullmatch(idempotency_key or ""):
            raise ControlledOperationError("IDEMPOTENCY_KEY_REQUIRED", "Wymagany jest prawidłowy idempotency key", status=DENIED)
        if idempotency_key and not SAFE_IDEMPOTENCY_KEY.fullmatch(idempotency_key):
            raise ControlledOperationError("INVALID_IDEMPOTENCY_KEY", "Nieprawidłowy idempotency key", status=DENIED)
        effective_key = "" if definition.idempotency_requirement == IDEMPOTENCY_NONE else idempotency_key
        fingerprint = _fingerprint(definition, actor, data)
        entity_type, entity_id, expected_version = _entity(definition, data)
        existing = _idempotent_execution(definition, actor, effective_key)
        if existing is not None:
            if existing["input_fingerprint"] != fingerprint:
                return _safe_denial(definition.operation_name, definition.operation_version, existing["execution_id"], actor, existing["correlation_id"], "IDEMPOTENCY_CONFLICT", "Idempotency key został użyty dla innego inputu", status=CONFLICT)
            row = existing
        else:
            row = _create_execution(definition, actor, fingerprint, entity_type, entity_id, expected_version, effective_key, correlation)
        execution_id = row["execution_id"]
        if row["status"] in TERMINAL_STATUSES:
            return _result_from_row(row)
        evaluation = approvals.evaluate_operation(actor, definition.operation_name, operation_version=definition.operation_version)
        if not evaluation.allowed:
            row = _transition(execution_id, definition, actor, "DENIED", "business_operation.denied", DENIED,
                              error_code="PERMISSION_DENIED", message="Brak wymaganego permission", completed=True)
            return _result_from_row(row)
        if evaluation.requires_approval:
            stored_approval = row["approval_id"] or ""
            if not stored_approval:
                created_approval = approvals.request_approval(
                    actor, definition.operation_name, payload=data,
                    entity_type=entity_type, entity_id=entity_id,
                    operation_version=definition.operation_version,
                    expected_entity_version=expected_version,
                    correlation_id=row["correlation_id"], reason="Business Operation wymaga zgody",
                )
                row = _transition(
                    execution_id, definition, actor, "PENDING_APPROVAL",
                    "business_operation.pending_approval", PENDING_APPROVAL,
                    approval_id=created_approval, expected_statuses=("CREATED",),
                )
                return _result_from_row(row)
            if approval_id and approval_id != stored_approval:
                return _safe_denial(definition.operation_name, definition.operation_version, execution_id, actor, row["correlation_id"], "APPROVAL_MISMATCH", "Approval nie należy do execution")
            snapshot = approvals.get_request_snapshot(stored_approval)
            if snapshot is None or snapshot["status"] == "PENDING":
                return _result_from_row(row)
            if snapshot["status"] != "APPROVED":
                row = _transition(execution_id, definition, actor, "DENIED", "business_operation.denied", DENIED,
                                  error_code="APPROVAL_NOT_APPROVED", message=f"Approval ma status {snapshot['status']}", completed=True)
                return _result_from_row(row)
            if definition.operation_name == "internal.test.external.execute":
                from external_execution import queue_execution
                queued = queue_execution(execution_id, actor, data, external_system="test")
                return _result_from_row(_execution(execution_id))
            claimed = _transition(
                execution_id, definition, actor, "RUNNING", "business_operation.started", SUCCESS,
                started=True, expected_statuses=("PENDING_APPROVAL",),
            )
            if claimed is None:
                current = _execution(execution_id)
                return _result_from_row(current, status=NOOP if current["status"] == "RUNNING" else None)
            if definition.operation_name != "internal.test.change_setting":
                row = _transition(
                    execution_id, definition, actor, "DENIED", "business_operation.denied", DENIED,
                    error_code="EXECUTION_MODE_NOT_IMPLEMENTED",
                    message="Brak bezpiecznej semantyki wykonania dla tej operacji", completed=True,
                )
                return _result_from_row(row)
            return _execute_local_approved(
                execution_id, definition, actor, data, stored_approval,
                entity_type, entity_id, expected_version, row["correlation_id"],
            )
        else:
            claimed = _transition(execution_id, definition, actor, "RUNNING", "", SUCCESS,
                                  started=True, expected_statuses=("CREATED",))
            if claimed is None:
                return _result_from_row(_execution(execution_id), status=NOOP)
        try:
            output = validate_output(
                definition,
                _HANDLERS[definition.operation_name](data, actor, row["correlation_id"], None),
            )
        except ControlledOperationError as exc:
            event = "business_operation.conflict" if exc.status == CONFLICT else "business_operation.failed"
            audit_result = CONFLICT if exc.status == CONFLICT else FAILED
            terminal = "CONFLICT" if exc.status == CONFLICT else "FAILED"
            row = _transition(execution_id, definition, actor, terminal, event, audit_result,
                              error_code=exc.error_code, message=exc.safe_message, completed=True)
            return _result_from_row(row)
        except Exception as exc:
            safe_message = sanitize_audit_text(exc)
            row = _transition(execution_id, definition, actor, "FAILED", "business_operation.failed", FAILED,
                              error_code="HANDLER_FAILED", message=safe_message, completed=True)
            return _result_from_row(row)
        row = _transition(execution_id, definition, actor, "SUCCESS", "business_operation.success", SUCCESS,
                          data=output, completed=True, expected_statuses=("RUNNING",))
        return _result_from_row(row)
    except ControlledOperationError as exc:
        if 'actor' in locals() and isinstance(actor, ActorContext):
            try:
                return _persist_gate_denial(definition, actor, input_data, correlation, exc)
            except Exception:
                pass
        return _safe_denial(definition.operation_name, definition.operation_version, execution_id, actor_context, correlation, exc.error_code, exc.safe_message, status=exc.status)
    except Exception:
        return _safe_denial(definition.operation_name, definition.operation_version, execution_id, actor_context, correlation, "EXECUTION_GATE_FAILED", "Nie udało się bezpiecznie wykonać operacji", status=FAILED)


def operation_descriptor(definition: BusinessOperationDefinition) -> dict[str, Any]:
    return {
        "name": definition.operation_name,
        "version": definition.operation_version,
        "description": definition.description,
        "input_schema": definition.input_schema,
        "read_only": definition.read_only,
        "idempotency": definition.idempotency_requirement,
    }


def list_available_operations(actor_context: ActorContext) -> list[dict[str, Any]]:
    actor = _trusted_actor(actor_context)
    visible = []
    for definition in OPERATION_REGISTRY.values():
        if not definition.enabled or actor.actor_type not in definition.actor_types_allowed:
            continue
        if actor.permission_decision(definition.required_permission) == PERMISSION_DENY:
            continue
        try:
            policy = approvals.get_policy(definition.operation_name, definition.operation_version)
        except approvals.ApprovalDenied:
            continue
        if policy.permission != definition.required_permission or policy.risk_level != definition.risk_level:
            continue
        visible.append(operation_descriptor(definition))
    return sorted(visible, key=lambda item: item["name"])
