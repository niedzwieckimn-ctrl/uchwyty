"""Closed registry and execution gate for trusted business operations.

The module deliberately exposes operations, not SQL, tables, endpoints or
connections.  Identity, permissions and risk are reloaded from backend-owned
state for every execution.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import logging
import re
import sqlite3
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

import agent_conversation
import internal_approval as approvals
from cash_flow_module import cash_flow_overdue_invoices
from inventory_analytics import build_replenishment_analysis
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
MAX_PRODUCT_SEARCH_RESULTS = 50
MAX_BUSINESS_SEARCH_RESULTS = 50
WARSAW_TZ = ZoneInfo("Europe/Warsaw")
logger = logging.getLogger(__name__)


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
        "candidates": {"type": "array", "maxItems": MAX_PRODUCT_SEARCH_RESULTS, "items": _PRODUCT_FIELDS},
        "count": {"type": "integer"}, "truncated": {"type": "boolean"},
    },
}

_LIMIT = {"type": "integer", "minimum": 1, "maximum": MAX_BUSINESS_SEARCH_RESULTS}
_QUERY = {"type": "string", "minLength": 1, "maxLength": 160}
_DATE = {"type": "string", "minLength": 10, "maxLength": 10}
_PERIOD = {"type": "string", "enum": [
    "all", "today", "yesterday", "this_week", "this_month", "previous_month", "custom",
]}
_ORDER_SUMMARY_PERIOD = {"type": "string", "enum": [
    "all", "today", "yesterday", "this_week", "this_month", "previous_month",
    "last_7_days", "last_30_days", "custom",
]}
_SEARCH_INPUT = {
    "type": "object", "additionalProperties": False, "properties": {
        "product_id": {"type": "integer", "minimum": 1, "maximum": 9223372036854775807},
        "query": _QUERY, "status": {"type": "string", "minLength": 1, "maxLength": 64},
        "period": _PERIOD, "date_from": _DATE, "date_to": _DATE, "limit": _LIMIT,
        "customer_id": {"type": "integer", "minimum": 1, "maximum": 9_223_372_036_854_775_807},
    },
}
_GET_INPUT = {
    "type": "object", "additionalProperties": False, "properties": {
        "id": {"type": "integer", "minimum": 1, "maximum": 9_223_372_036_854_775_807},
        "number": _QUERY,
    },
}
ORDER_GET_INPUT = {
    "type": "object", "additionalProperties": False, "properties": {
        "id": {"type": "integer", "minimum": 1, "maximum": 9_223_372_036_854_775_807},
        "number": _QUERY, "latest": {"type": "boolean"},
        "customer_id": {"type": "integer", "minimum": 1, "maximum": 9_223_372_036_854_775_807},
    },
}
ORDER_SUMMARY_INPUT = {
    "type": "object", "additionalProperties": False, "properties": {
        "customer_id": {"type": "integer", "minimum": 1, "maximum": 9_223_372_036_854_775_807},
        "period": _ORDER_SUMMARY_PERIOD, "date_from": _DATE, "date_to": _DATE,
        "status": {"type": "string", "minLength": 1, "maxLength": 64},
    },
}
_ORDER_CURRENCY_SUMMARY = {
    "type": "object", "additionalProperties": False,
    "required": ["currency", "total_net", "total_gross"],
    "properties": {
        "currency": {"type": "string"}, "total_net": {"type": ["integer", "number"]},
        "total_gross": {"type": ["integer", "number"]},
    },
}
_ORDER_STATUS_SUMMARY = {
    "type": "object", "additionalProperties": False,
    "required": ["status", "order_count", "total_units"],
    "properties": {
        "status": {"type": "string"}, "order_count": {"type": "integer"},
        "total_units": {"type": "integer"},
    },
}
ORDER_SUMMARY_OUTPUT = {
    "type": "object", "additionalProperties": False,
    "required": ["ok", "date_from", "date_to", "order_count", "total_units", "by_currency", "statuses"],
    "properties": {
        "ok": {"type": "boolean"}, "date_from": {"type": ["string", "null"]},
        "date_to": {"type": ["string", "null"]}, "order_count": {"type": "integer"},
        "total_units": {"type": "integer"},
        "by_currency": {"type": "array", "maxItems": 20, "items": _ORDER_CURRENCY_SUMMARY},
        "statuses": {"type": "array", "maxItems": 100, "items": _ORDER_STATUS_SUMMARY},
    },
}
INVOICE_GET_INPUT = {
    "type": "object", "additionalProperties": False, "properties": {
        "id": {"type": "integer", "minimum": 1, "maximum": 9_223_372_036_854_775_807},
        "number": _QUERY, "latest": {"type": "boolean"},
    },
}
_RESULTS_OUTPUT = {
    "type": "object", "additionalProperties": False,
    "required": ["ok", "results", "count", "truncated"],
    "properties": {
        "ok": {"type": "boolean"}, "results": {"type": "array", "maxItems": MAX_BUSINESS_SEARCH_RESULTS},
        "count": {"type": "integer"}, "truncated": {"type": "boolean"},
    },
}
INVOICE_OVERDUE_OUTPUT = {
    "type": "object", "additionalProperties": False,
    "required": ["ok", "results", "count", "truncated", "totals_by_currency", "customer_count", "customers"],
    "properties": {
        "ok": {"type": "boolean"}, "results": {"type": "array", "maxItems": MAX_BUSINESS_SEARCH_RESULTS},
        "count": {"type": "integer"}, "truncated": {"type": "boolean"},
        "totals_by_currency": {"type": "object"},
        "customer_count": {"type": "integer"}, "customers": {"type": "array", "maxItems": MAX_BUSINESS_SEARCH_RESULTS},
    },
}
_DETAIL_OUTPUT = {
    "type": "object", "additionalProperties": False, "required": ["ok", "record"],
    "properties": {"ok": {"type": "boolean"}, "record": {"type": "object"}},
}
INVOICE_SEARCH_INPUT = {
    "type": "object", "additionalProperties": False, "properties": {
        "query": _QUERY,
        "payment_status": {"type": "string", "enum": ["all", "paid", "unpaid", "overdue"]},
        "date_field": {"type": "string", "enum": ["issue_date", "due_date"]},
        "period": _PERIOD, "date_from": _DATE, "date_to": _DATE, "limit": _LIMIT,
        "customer_id": {"type": "integer", "minimum": 1, "maximum": 9_223_372_036_854_775_807},
    },
}
INVOICE_OVERDUE_INPUT = {
    "type": "object", "additionalProperties": False, "properties": {
        "query": _QUERY, "as_of": _DATE, "limit": _LIMIT,
        "customer_id": {"type": "integer", "minimum": 1, "maximum": 9_223_372_036_854_775_807},
    },
}
CUSTOMER_SEARCH_INPUT = {
    "type": "object", "additionalProperties": False, "required": ["query"],
    "properties": {"query": _QUERY, "limit": _LIMIT},
}
CUSTOMER_GET_INPUT = {
    "type": "object", "additionalProperties": False, "required": ["customer_id"],
    "properties": {"customer_id": {"type": "integer", "minimum": 1, "maximum": 9_223_372_036_854_775_807}},
}
SALES_SUMMARY_INPUT = {
    "type": "object", "additionalProperties": False, "properties": {
        "period": _PERIOD, "date_from": _DATE, "date_to": _DATE,
    },
}
SALES_SUMMARY_OUTPUT = {
    "type": "object", "additionalProperties": False,
    "required": ["ok", "date_from", "date_to", "order_count", "invoice_count", "by_currency", "top_customers"],
    "properties": {
        "ok": {"type": "boolean"}, "date_from": {"type": ["string", "null"]},
        "date_to": {"type": ["string", "null"]}, "order_count": {"type": "integer"},
        "invoice_count": {"type": "integer"}, "by_currency": {"type": "array", "maxItems": 20},
        "top_customers": {"type": "array", "maxItems": 20},
    },
}
INVENTORY_SUMMARY_INPUT = {"type": "object", "additionalProperties": False, "properties": {}}
INVENTORY_SUMMARY_OUTPUT = {
    "type": "object", "additionalProperties": False,
    "required": ["ok", "product_count", "stock_units", "available_units", "reserved_units", "incoming_units"],
    "properties": {
        "ok": {"type": "boolean"}, "product_count": {"type": "integer"},
        "stock_units": {"type": "integer"}, "available_units": {"type": "integer"},
        "reserved_units": {"type": "integer"}, "incoming_units": {"type": "integer"},
    },
}
CHINA_SUMMARY_INPUT = {
    "type": "object", "additionalProperties": False,
    "properties": {"scope": {"type": "string", "enum": ["active", "all", "arrived"]}},
}
CHINA_SUMMARY_OUTPUT = {
    "type": "object", "additionalProperties": False,
    "required": ["ok", "scope", "order_count", "item_units", "by_status", "packages_by_status", "pieces_by_status", "pieces_excluding_planned"],
    "properties": {
        "ok": {"type": "boolean"}, "scope": {"type": "string"},
        "order_count": {"type": "integer"}, "item_units": {"type": "integer"},
        "by_status": {"type": "object"}, "packages_by_status": {"type": "object"},
        "pieces_by_status": {"type": "object"}, "pieces_excluding_planned": {"type": "integer"},
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
    "agent.terminology.search": BusinessOperationDefinition(
        "agent.terminology.search", 1, "Odczytuje zapisane znaczenie terminu firmy i wersję. Query jest fragmentem nazwy terminu, nie zdaniem do interpretacji.",
        "inventory.read", approvals.GREEN, "NONE", frozenset({"HUMAN", "AI_AGENT"}),
        {"type":"object","additionalProperties":False,"required":["query"],"properties":{"query":{"type":"string","minLength":1,"maxLength":80}}},
        {"type":"object","required":["ok","results"],"properties":{"ok":{"type":"boolean"},"results":{"type":"array"}}},
        IDEMPOTENCY_NONE, "READ_STANDARD", True,
    ),
    "agent.terminology.remember": BusinessOperationDefinition(
        "agent.terminology.remember", 1, "Zapisuje wyłącznie potwierdzoną przez użytkownika terminologię firmy. Nie zapisuj przypuszczeń; przy niepewności najpierw dopytaj. expected_version=0 tworzy termin; zmiana wymaga aktualnej wersji z agent.terminology.search.",
        "agent.terminology.remember", approvals.GREEN, "NONE", frozenset({"AI_AGENT"}),
        {"type":"object","additionalProperties":False,"required":["term","meaning","confirmed_by_user","expected_version","source_run_id"],
         "properties":{"term":{"type":"string","minLength":1,"maxLength":80},
                       "meaning":{"type":"string","minLength":1,"maxLength":500},
                       "confirmed_by_user":{"type":"boolean"},
                       "expected_version":{"type":"integer","minimum":0,"maximum":2147483647},
                       "source_run_id":{"type":"string","format":"uuid","maxLength":36}}},
        {"type":"object","required":["ok","term","version"],"properties":{"ok":{"type":"boolean"},"term":{"type":"string"},"version":{"type":"integer"}}},
        IDEMPOTENCY_REQUIRED, "WRITE", False,
    ),
    "inventory.product.search": BusinessOperationDefinition(
        "inventory.product.search", 1, "Wyszukuje wyłącznie produkty po SKU, modelu, wariancie lub nazwie produktu i zwraca ograniczony stan.",
        "inventory.read", approvals.GREEN, "NONE", frozenset({"HUMAN", "SYSTEM", "AI_AGENT"}),
        PRODUCT_SEARCH_INPUT, PRODUCT_SEARCH_OUTPUT, IDEMPOTENCY_NONE, "READ_STANDARD", True,
    ),
    "inventory.product.get": BusinessOperationDefinition(
        "inventory.product.get", 1, "Pobiera minimalny wewnętrzny widok produktu i stanu.",
        "inventory.read", approvals.GREEN, "NONE",
        frozenset({"HUMAN", "SYSTEM", "AI_AGENT"}),
        PRODUCT_GET_INPUT, PRODUCT_GET_OUTPUT, IDEMPOTENCY_NONE, "READ_STANDARD", True,
    ),
    "inventory.summary": BusinessOperationDefinition(
        "inventory.summary", 1, "Podsumowuje cały magazyn: fizyczny stan, dostępność, rezerwacje i dostawy w drodze.",
        "inventory.read", approvals.GREEN, "NONE", frozenset({"HUMAN", "SYSTEM", "AI_AGENT"}),
        INVENTORY_SUMMARY_INPUT, INVENTORY_SUMMARY_OUTPUT, IDEMPOTENCY_NONE, "READ_STANDARD", True,
    ),
    "orders.search": BusinessOperationDefinition(
        "orders.search", 1, "Wyszukuje zamówienia po numerze lub nazwie klienta (query), customer_id, product_id, statusie i okresie; limit=1 zwraca najnowszy pasujący rekord z customer_id. product_id pozwala ustalić ostatniego nabywcę produktu.",
        "orders.read_full", approvals.GREEN, "NONE", frozenset({"HUMAN", "AI_AGENT"}),
        _SEARCH_INPUT, _RESULTS_OUTPUT, IDEMPOTENCY_NONE, "READ_STANDARD", True,
    ),
    "orders.get": BusinessOperationDefinition(
        "orders.get", 1, "Pobiera zamówienie z pozycjami po id/number albo najnowsze przez latest=true, opcjonalnie dla customer_id.",
        "orders.read_full", approvals.GREEN, "NONE", frozenset({"HUMAN", "AI_AGENT"}),
        ORDER_GET_INPUT, _DETAIL_OUTPUT, IDEMPOTENCY_NONE, "READ_STANDARD", True,
    ),
    "orders.summary": BusinessOperationDefinition(
        "orders.summary", 1,
        "Podsumowuje zamówienia w zadanym okresie i opcjonalnie dla konkretnego klienta. Używaj do pytań o łączną wartość, liczbę zamówień, liczbę sztuk lub podsumowanie statusów. Nie używaj do wyświetlania pojedynczego zamówienia.",
        "orders.read_full", approvals.GREEN, "NONE", frozenset({"HUMAN", "AI_AGENT"}),
        ORDER_SUMMARY_INPUT, ORDER_SUMMARY_OUTPUT, IDEMPOTENCY_NONE, "READ_STANDARD", True,
    ),
    "invoices.search": BusinessOperationDefinition(
        "invoices.search", 1, "Wyszukuje faktury, opcjonalnie dla customer_id; payment_status=unpaid oznacza nieopłacone, a zaległe obsługuje invoices.overdue.",
        "invoices.read", approvals.GREEN, "NONE", frozenset({"HUMAN", "AI_AGENT"}),
        INVOICE_SEARCH_INPUT, _RESULTS_OUTPUT, IDEMPOTENCY_NONE, "READ_STANDARD", True,
    ),
    "invoices.get": BusinessOperationDefinition(
        "invoices.get", 1, "Pobiera jedną fakturę po dokładnym id lub number; latest=true zwraca globalnie ostatnio wystawioną wraz z pozycjami.",
        "invoices.read", approvals.GREEN, "NONE", frozenset({"HUMAN", "AI_AGENT"}),
        INVOICE_GET_INPUT, _DETAIL_OUTPUT, IDEMPOTENCY_NONE, "READ_STANDARD", True,
    ),
    "invoices.overdue": BusinessOperationDefinition(
        "invoices.overdue", 1, "Zwraca zaległe faktury po terminie według Cash Flow, opcjonalnie ograniczone przez customer_id.",
        "payments.read", approvals.GREEN, "NONE", frozenset({"HUMAN", "AI_AGENT"}),
        INVOICE_OVERDUE_INPUT, INVOICE_OVERDUE_OUTPUT, IDEMPOTENCY_NONE, "READ_STANDARD", True,
    ),
    "customers.search": BusinessOperationDefinition(
        "customers.search", 1, "Wyszukuje firmy i klientów po pełnej lub skróconej nazwie, także bez spacji, oraz po NIP, e-mailu lub telefonie.",
        "customers.read", approvals.GREEN, "NONE", frozenset({"HUMAN", "AI_AGENT"}),
        CUSTOMER_SEARCH_INPUT, _RESULTS_OUTPUT, IDEMPOTENCY_NONE, "READ_STANDARD", True,
    ),
    "customers.get": BusinessOperationDefinition(
        "customers.get", 1, "Pobiera dane klienta oraz zagregowaną historię zamówień i faktur.",
        "customers.read", approvals.GREEN, "NONE", frozenset({"HUMAN", "AI_AGENT"}),
        CUSTOMER_GET_INPUT, _DETAIL_OUTPUT, IDEMPOTENCY_NONE, "READ_STANDARD", True,
    ),
    "china.orders.summary": BusinessOperationDefinition(
        "china.orders.summary", 1, "Liczy zamówienia zakupowe Chiny/P/O z tabel dostaw, domyślnie aktywne.",
        "purchases.read", approvals.GREEN, "NONE", frozenset({"HUMAN", "AI_AGENT"}),
        CHINA_SUMMARY_INPUT, CHINA_SUMMARY_OUTPUT, IDEMPOTENCY_NONE, "READ_STANDARD", True,
    ),
    "business.sales.summary": BusinessOperationDefinition(
        "business.sales.summary", 1, "Zwraca podsumowanie sprzedaży za kontrolowany okres, osobno dla każdej waluty.",
        "reports.read", approvals.GREEN, "NONE", frozenset({"HUMAN", "AI_AGENT"}),
        SALES_SUMMARY_INPUT, SALES_SUMMARY_OUTPUT, IDEMPOTENCY_NONE, "READ_STANDARD", True,
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
                if item_schema.get("properties") and (set(item_schema.get("required", ())) - set(item) or set(item) - set(item_schema["properties"])):
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
    if definition.operation_name == "agent.terminology.search":
        return "agent_terminology_search", data["query"], None
    if definition.operation_name == "agent.terminology.remember":
        return "agent_terminology", data["term"], data["expected_version"]
    if definition.operation_name == "inventory.product.search":
        return "product_search", sanitize_audit_text(data["query"])[:120], None
    if definition.operation_name == "inventory.product.get":
        return "product", str(data["product_id"]), None
    if definition.operation_name == "inventory.summary":
        return "inventory_summary", "all", None
    if definition.operation_name in {"orders.search", "invoices.search", "invoices.overdue", "customers.search"}:
        return definition.operation_name.replace(".", "_"), sanitize_audit_text(data.get("query", "all"))[:160], None
    if definition.operation_name == "orders.summary":
        return "orders_summary", str(data.get("customer_id") or data.get("period") or "all"), None
    if definition.operation_name in {"orders.get", "invoices.get"}:
        return definition.operation_name.split(".")[0][:-1], str(data.get("id") or data.get("number") or ("latest" if data.get("latest") else "")), None
    if definition.operation_name == "customers.get":
        return "customer", str(data["customer_id"]), None
    if definition.operation_name == "china.orders.summary":
        return "china_orders_summary", str(data.get("scope") or "active"), None
    if definition.operation_name == "business.sales.summary":
        return "sales_summary", str(data.get("period") or "this_month"), None
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
    query = " ".join(str(data["query"]).strip().casefold().split())
    tokens = [token for token in re.split(r"[^\w]+", query, flags=re.UNICODE) if token][:8]
    if not tokens:
        raise ControlledOperationError("INVALID_INPUT", "Fraza wyszukiwania jest pusta", status=DENIED)
    db = transaction_connection or _factory()()
    try:
        catalog = db.execute(
            """SELECT p.id,p.sku,p.model,p.name,p.ean,COALESCE(s.qty,0) stock
               FROM products p LEFT JOIN stock s ON s.product_id=p.id
               WHERE COALESCE(p.archived,0)=0"""
        ).fetchall()
    finally:
        if transaction_connection is None:
            db.close()

    query_compact = re.sub(r"[^\w]+", "", query, flags=re.UNICODE)

    def rank(row):
        fields = [" ".join(str(row[field] or "").strip().casefold().split())
                  for field in ("sku", "model", "name", "ean")]
        combined = " ".join(fields)
        compact_fields = [re.sub(r"[^\w]+", "", value, flags=re.UNICODE) for value in fields]
        direct = any(query in value for value in fields)
        compact = bool(query_compact) and any(query_compact in value for value in compact_fields)
        token_match = all(token in combined for token in tokens)
        if not (direct or compact or token_match):
            return None
        if query == fields[0]:
            priority = 0  # exact SKU
        elif query in fields[1:3]:
            priority = 1  # exact model or product family/name
        elif fields[0].startswith(query) or (query_compact and compact_fields[0].startswith(query_compact)):
            priority = 2  # SKU family, e.g. CH030
        elif any(value.startswith(query) for value in fields[1:3]):
            priority = 3
        else:
            priority = 4  # model + variant spread across fields
        return priority, fields[0], int(row["id"])

    found = [(match_rank, row) for row in catalog if (match_rank := rank(row)) is not None]
    found.sort(key=lambda item: item[0])
    selected = [row for _, row in found[:MAX_PRODUCT_SEARCH_RESULTS]]
    candidates = [{"id": int(row["id"]), "sku": row["sku"] or "", "model": row["model"],
                   "name": row["name"], "stock": int(row["stock"])} for row in selected]
    return {"ok": True, "query": query, "candidates": candidates,
            "count": len(candidates), "truncated": len(found) > MAX_PRODUCT_SEARCH_RESULTS}


def _business_now() -> datetime:
    return datetime.now(WARSAW_TZ)


def _iso_date(value: str, field: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise ControlledOperationError("INVALID_DATE", f"Pole {field} musi mieć format RRRR-MM-DD", status=DENIED)


def _date_bounds(data: Mapping[str, Any], *, default_period="all") -> tuple[str | None, str | None]:
    period = data.get("period") or default_period
    today = _business_now().date()
    if period == "all":
        start = end = None
    elif period == "today":
        start = end = today
    elif period == "yesterday":
        start = end = today - timedelta(days=1)
    elif period == "this_week":
        start, end = today - timedelta(days=today.weekday()), today
    elif period == "this_month":
        start, end = today.replace(day=1), today
    elif period == "previous_month":
        end = today.replace(day=1) - timedelta(days=1)
        start = end.replace(day=1)
    elif period == "last_7_days":
        start, end = today - timedelta(days=6), today
    elif period == "last_30_days":
        start, end = today - timedelta(days=29), today
    elif period == "custom":
        if not data.get("date_from") or not data.get("date_to"):
            raise ControlledOperationError("DATE_RANGE_REQUIRED", "Okres custom wymaga date_from i date_to", status=DENIED)
        start, end = _iso_date(data["date_from"], "date_from"), _iso_date(data["date_to"], "date_to")
    else:
        raise ControlledOperationError("INVALID_PERIOD", "Nieprawidłowy okres", status=DENIED)
    if data.get("date_from") and period != "custom":
        start = _iso_date(data["date_from"], "date_from")
    if data.get("date_to") and period != "custom":
        end = _iso_date(data["date_to"], "date_to")
    if start and end and start > end:
        raise ControlledOperationError("INVALID_DATE_RANGE", "Początek okresu jest po końcu", status=DENIED)
    return (start.isoformat() if start else None, end.isoformat() if end else None)


def _money(value: Any) -> float | int:
    rounded = round(float(value or 0), 2)
    # JSON should carry whole monetary values as 123 rather than 123.0. The
    # runtime's numeric grounding compares literal number representations and
    # models naturally omit a redundant decimal zero in final answers.
    return int(rounded) if rounded.is_integer() else rounded


def _limit(data: Mapping[str, Any]) -> int:
    return int(data.get("limit") or 20)


def _order_totals(db: sqlite3.Connection, order_id: int) -> dict[str, dict[str, float]]:
    rows = db.execute(
        """SELECT UPPER(COALESCE(NULLIF(TRIM(oi.currency),''),NULLIF(TRIM(o.currency),''),'PLN')) currency,
                  SUM(oi.qty * COALESCE(oi.unit_net_price,0)) net,
                  SUM(oi.qty * COALESCE(oi.unit_gross_price,oi.unit_net_price,0)) gross
           FROM orders o LEFT JOIN order_items oi ON oi.order_id=o.id WHERE o.id=? GROUP BY 1""", (order_id,),
    ).fetchall()
    return {str(row["currency"]): {"net": _money(row["net"]), "gross": _money(row["gross"])} for row in rows if row["currency"]}


def _order_filter(data: Mapping[str, Any]) -> tuple[str | None, str | None, list[str], list[Any]]:
    """Build the same date and status predicates for order reads and aggregates."""
    start, end = _date_bounds(data)
    clauses, params = ["1=1"], []
    if data.get("customer_id"):
        clauses.append("o.customer_id=?"); params.append(int(data["customer_id"]))
    status = str(data.get("status") or "").strip().casefold()
    if status == "not_shipped":
        clauses.append("LOWER(o.status) NOT IN ('shipped','completed','cancelled')")
    elif status == "in_progress":
        clauses.append("LOWER(o.status) IN ('confirmed','issued','packed','packed_partial','in_delivery','partially_shipped')")
    elif status:
        clauses.append("LOWER(o.status)=?"); params.append(status)
    if start:
        clauses.append("SUBSTR(TRIM(o.created_at),1,10)>=?"); params.append(start)
    if end:
        clauses.append("SUBSTR(TRIM(o.created_at),1,10)<=?"); params.append(end)
    return start, end, clauses, params


def _orders_search(data, actor, correlation_id, transaction_connection=None):
    db = transaction_connection or _factory()()
    try:
        _start, _end, clauses, params = _order_filter(data)
        if data.get("product_id"):
            clauses.append("EXISTS (SELECT 1 FROM order_items matched WHERE matched.order_id=o.id AND matched.product_id=?)")
            params.append(data["product_id"])
        query = " ".join(str(data.get("query") or "").split()).casefold()
        if query:
            clauses.append("(LOWER(o.order_no) LIKE ? OR LOWER(o.customer_name) LIKE ? OR LOWER(COALESCE(o.customer_email,'')) LIKE ?)")
            params.extend([f"%{query}%"] * 3)
        limit = _limit(data)
        rows = db.execute(
            f"""SELECT o.*, COUNT(oi.id) item_lines, COALESCE(SUM(oi.qty),0) item_qty
                 FROM orders o LEFT JOIN order_items oi ON oi.order_id=o.id
                 WHERE {' AND '.join(clauses)} GROUP BY o.id ORDER BY o.created_at DESC,o.id DESC LIMIT ?""",
            (*params, limit + 1),
        ).fetchall()
        selected = rows[:limit]
        results = [{
            "id": int(r["id"]), "order_number": r["order_no"], "customer_id": r["customer_id"],
            "customer_name": r["customer_name"], "status": r["status"], "created_at": r["created_at"],
            "currency": str(r["currency"] or "PLN").upper(), "item_lines": int(r["item_lines"]),
            "item_qty": int(r["item_qty"]), "totals": _order_totals(db, int(r["id"])),
        } for r in selected]
        return {"ok": True, "results": results, "count": len(results), "truncated": len(rows) > limit}
    finally:
        if transaction_connection is None: db.close()


def _orders_summary(data, actor, correlation_id, transaction_connection=None):
    db = transaction_connection or _factory()()
    try:
        start, end, clauses, params = _order_filter(data)
        orders = db.execute(
            f"SELECT o.id,o.status,UPPER(COALESCE(NULLIF(TRIM(o.currency),''),'PLN')) currency "
            f"FROM orders o WHERE {' AND '.join(clauses)} ORDER BY o.id",
            params,
        ).fetchall()
        if not orders:
            return {"ok": True, "date_from": start, "date_to": end, "order_count": 0,
                    "total_units": 0, "by_currency": [], "statuses": []}
        order_ids = [int(row["id"]) for row in orders]
        items = db.execute(
            f"""SELECT oi.order_id,oi.qty,oi.unit_net_price,oi.unit_gross_price,
                       UPPER(COALESCE(NULLIF(TRIM(oi.currency),''),NULLIF(TRIM(o.currency),''),'PLN')) currency
                FROM order_items oi JOIN orders o ON o.id=oi.order_id
                WHERE oi.order_id IN ({','.join('?' for _ in order_ids)})""",
            order_ids,
        ).fetchall()
        units_by_order = {order_id: 0 for order_id in order_ids}
        currency_totals: dict[str, dict[str, Decimal]] = {}
        for order in orders:
            currency_totals.setdefault(str(order["currency"]), {"net": Decimal("0"), "gross": Decimal("0")})
        for item in items:
            qty = int(item["qty"] or 0)
            units_by_order[int(item["order_id"])] += qty
            totals = currency_totals.setdefault(str(item["currency"]), {"net": Decimal("0"), "gross": Decimal("0")})
            net = Decimal(str(item["unit_net_price"] or 0))
            gross = Decimal(str(item["unit_gross_price"] if item["unit_gross_price"] is not None else item["unit_net_price"] or 0))
            totals["net"] += Decimal(qty) * net
            totals["gross"] += Decimal(qty) * gross
        status_totals: dict[str, dict[str, int]] = {}
        for order in orders:
            status = str(order["status"] or "")
            bucket = status_totals.setdefault(status, {"order_count": 0, "total_units": 0})
            bucket["order_count"] += 1
            bucket["total_units"] += units_by_order[int(order["id"])]
        return {
            "ok": True, "date_from": start, "date_to": end,
            "order_count": len(orders), "total_units": sum(units_by_order.values()),
            "by_currency": [{"currency": currency, "total_net": _money(values["net"]),
                             "total_gross": _money(values["gross"])}
                            for currency, values in sorted(currency_totals.items())],
            "statuses": [{"status": status, **values} for status, values in sorted(status_totals.items())],
        }
    finally:
        if transaction_connection is None: db.close()


def _resolve_by_id_or_number(db, table: str, data, number_column: str):
    if bool(data.get("id")) == bool(str(data.get("number") or "").strip()):
        raise ControlledOperationError("IDENTIFIER_REQUIRED", "Podaj dokładnie jedno: id albo number", status=DENIED)
    if data.get("id"):
        return db.execute(f"SELECT * FROM {table} WHERE id=?", (data["id"],)).fetchone()
    return db.execute(f"SELECT * FROM {table} WHERE LOWER({number_column})=LOWER(?)", (str(data["number"]).strip(),)).fetchone()


def _orders_get(data, actor, correlation_id, transaction_connection=None):
    db = transaction_connection or _factory()()
    try:
        identifiers = int(bool(data.get("id"))) + int(bool(str(data.get("number") or "").strip())) + int(data.get("latest") is True)
        if identifiers != 1:
            raise ControlledOperationError("IDENTIFIER_REQUIRED", "Podaj dokładnie jedno: id, number albo latest=true", status=DENIED)
        if data.get("latest") is True:
            if data.get("customer_id"):
                row = db.execute("SELECT * FROM orders WHERE customer_id=? ORDER BY created_at DESC,id DESC LIMIT 1",
                                 (int(data["customer_id"]),)).fetchone()
            else:
                row = db.execute("SELECT * FROM orders ORDER BY created_at DESC,id DESC LIMIT 1").fetchone()
        else:
            row = _resolve_by_id_or_number(db, "orders", data, "order_no")
        if row is None: raise ControlledOperationError("ORDER_NOT_FOUND", "Nie znaleziono zamówienia", status=NOOP)
        record = {"id": int(row["id"]), "order_number": row["order_no"], "customer_id": row["customer_id"],
                  "customer_name": row["customer_name"], "status": row["status"], "note": row["note"],
                  "created_at": row["created_at"], "currency": str(row["currency"] or "PLN").upper(),
                  "tracking_number": row["tracking_no"], "carrier": row["carrier"],
                  "packed_at": row["packed_at"], "shipped_at": row["shipped_at"]}
        items = db.execute("""SELECT oi.id,oi.product_id,oi.sku,oi.qty,oi.unit_net_price,oi.unit_gross_price,
                                      UPPER(COALESCE(NULLIF(TRIM(oi.currency),''),NULLIF(TRIM(?),''),'PLN')) currency,
                                      p.model,p.name
                               FROM order_items oi LEFT JOIN products p ON p.id=oi.product_id
                               WHERE oi.order_id=? ORDER BY oi.id LIMIT 100""", (record.get("currency"), record["id"])).fetchall()
        record["items"] = [{**dict(i), "qty": int(i["qty"]), "unit_net_price": _money(i["unit_net_price"]),
                            "unit_gross_price": _money(i["unit_gross_price"])} for i in items]
        record["totals"] = _order_totals(db, record["id"])
        return {"ok": True, "record": record}
    finally:
        if transaction_connection is None: db.close()


def _overdue_ids(db, now=None) -> set[int]:
    return {int(row["id"]) for row in cash_flow_overdue_invoices(db, current_time=now or _business_now())}


def _invoice_view(row, overdue: bool) -> dict[str, Any]:
    currency = str(row["currency"] or row["order_currency"] or "PLN").upper()
    paid = bool(row["paid"])
    return {"id": int(row["id"]), "invoice_number": row["invoice_no"], "order_id": int(row["order_id"]),
            "customer_id": row["customer_id"],
            "buyer_name": row["buyer_name"], "issue_date": row["issue_date"], "due_date": row["payment_to"],
            "currency": currency, "total_net": _money(row["total_net"]), "total_gross": _money(row["total_gross"]),
            "paid": paid, "paid_at": row["paid_at"] or None,
            "amount_outstanding": 0.0 if paid else _money(row["total_gross"]),
            "payment_status": "paid" if paid else "overdue" if overdue else "unpaid"}


def _invoice_rows(db, where="1=1", params=(), limit=51):
    return db.execute(f"""SELECT i.*,COALESCE(m.paid,0) paid,m.paid_at,m.invoice_items_json,
                                  COALESCE(i.currency,o.currency,'PLN') currency,
                                  COALESCE(o.currency,'PLN') order_currency,o.customer_id
                           FROM invoices i LEFT JOIN invoice_meta m ON m.invoice_id=i.id
                           LEFT JOIN orders o ON o.id=i.order_id WHERE {where}
                           ORDER BY i.issue_date DESC,i.id DESC LIMIT ?""", (*params, limit)).fetchall()


def _invoices_search(data, actor, correlation_id, transaction_connection=None):
    db = transaction_connection or _factory()()
    try:
        start, end = _date_bounds(data)
        clauses, params = ["1=1"], []
        query = " ".join(str(data.get("query") or "").split()).casefold()
        if data.get("customer_id"):
            clauses.append("o.customer_id=?"); params.append(int(data["customer_id"]))
        if query:
            compact_query = re.sub(r"\s+", "", query)
            clauses.append("(LOWER(i.invoice_no) LIKE ? OR REPLACE(LOWER(i.invoice_no),' ','') LIKE ? OR LOWER(COALESCE(i.buyer_name,'')) LIKE ? OR LOWER(COALESCE(i.buyer_tax_no,'')) LIKE ?)")
            params.extend([f"%{query}%", f"%{compact_query}%", f"%{query}%", f"%{query}%"])
        date_column = "i.payment_to" if data.get("date_field") == "due_date" else "i.issue_date"
        if start: clauses.append(f"SUBSTR(TRIM({date_column}),1,10)>=?"); params.append(start)
        if end: clauses.append(f"SUBSTR(TRIM({date_column}),1,10)<=?"); params.append(end)
        status = data.get("payment_status") or "all"
        if status == "paid": clauses.append("COALESCE(m.paid,0)=1")
        elif status in {"unpaid", "overdue"}: clauses.append("COALESCE(m.paid,0)=0")
        limit = _limit(data); overdue_ids = _overdue_ids(db)
        rows = _invoice_rows(db, " AND ".join(clauses), params, limit=1001 if status == "overdue" else limit + 1)
        if status == "overdue": rows = [row for row in rows if int(row["id"]) in overdue_ids]
        selected = rows[:limit]
        return {"ok": True, "results": [_invoice_view(r, int(r["id"]) in overdue_ids) for r in selected],
                "count": len(selected), "truncated": len(rows) > limit}
    finally:
        if transaction_connection is None: db.close()


def _invoices_get(data, actor, correlation_id, transaction_connection=None):
    db = transaction_connection or _factory()()
    try:
        identifiers = int(bool(data.get("id"))) + int(bool(str(data.get("number") or "").strip())) + int(data.get("latest") is True)
        if identifiers != 1:
            raise ControlledOperationError("IDENTIFIER_REQUIRED", "Podaj dokładnie jedno: id, number albo latest=true", status=DENIED)
        if data.get("latest") is True:
            # created_at is written at the moment the invoice record is issued;
            # id resolves ties without interpreting the human invoice number.
            base = db.execute(
                """SELECT * FROM invoices
                   ORDER BY COALESCE(NULLIF(TRIM(created_at),''),issue_date) DESC,id DESC LIMIT 1"""
            ).fetchone()
        elif data.get("id"):
            base = db.execute("SELECT * FROM invoices WHERE id=?", (data["id"],)).fetchone()
        else:
            wanted = re.sub(r"\s+", "", str(data["number"])).casefold()
            base = next((row for row in db.execute("SELECT * FROM invoices ORDER BY id DESC").fetchall()
                         if re.sub(r"\s+", "", str(row["invoice_no"] or "")).casefold() == wanted), None)
        if base is None: raise ControlledOperationError("INVOICE_NOT_FOUND", "Nie znaleziono faktury", status=NOOP)
        rows = _invoice_rows(db, "i.id=?", (int(base["id"]),), 1)
        row = rows[0]; record = _invoice_view(row, int(row["id"]) in _overdue_ids(db))
        try:
            items = json.loads(row["invoice_items_json"] or "[]")
            if not isinstance(items, list): items = []
        except Exception: items = []
        record["items"] = sanitize_audit_data(items[:100])
        record["buyer_tax_no"] = row["buyer_tax_no"]
        record["payment_type"] = row["payment_type"]
        return {"ok": True, "record": record}
    finally:
        if transaction_connection is None: db.close()


def _invoices_overdue(data, actor, correlation_id, transaction_connection=None):
    db = transaction_connection or _factory()()
    try:
        now = _business_now()
        if data.get("as_of"):
            requested = _iso_date(data["as_of"], "as_of")
            now = datetime.combine(requested, datetime.min.time(), tzinfo=WARSAW_TZ).replace(hour=12)
        query = " ".join(str(data.get("query") or "").split()).casefold()
        overdue = cash_flow_overdue_invoices(db, current_time=now)
        if data.get("customer_id"):
            customer_id = int(data["customer_id"])
            filtered = []
            for item in overdue:
                invoice = db.execute("SELECT order_id FROM invoices WHERE id=?", (int(item["id"]),)).fetchone()
                order = db.execute("SELECT customer_id FROM orders WHERE id=?", (invoice["order_id"],)).fetchone() if invoice else None
                if order and int(order["customer_id"] or 0) == customer_id:
                    filtered.append(item)
            overdue = filtered
        if query:
            overdue = [r for r in overdue if query in str(r.get("invoice_no") or "").casefold()
                       or query in str(r.get("buyer_name") or "").casefold()]
        limit = _limit(data); selected = overdue[:limit]
        results = []
        totals_by_currency = {}
        customers_by_name = {}
        for item in selected:
            rows = _invoice_rows(db, "i.id=?", (int(item["id"]),), 1)
            view = _invoice_view(rows[0], True); view["overdue_days"] = int(item["overdue_days"]); results.append(view)
            currency = view["currency"]
            summary = totals_by_currency.setdefault(currency, {"currency": currency, "invoice_count": 0, "amount_outstanding": 0.0})
            summary["invoice_count"] += 1
            summary["amount_outstanding"] = _money(summary["amount_outstanding"] + view["amount_outstanding"])
            customer_name = str(view.get("buyer_name") or "Bez klienta")
            customer = customers_by_name.setdefault(customer_name.casefold(), {
                "buyer_name": customer_name, "invoice_count": 0, "oldest_overdue_days": 0,
                "totals_by_currency": {},
            })
            customer["invoice_count"] += 1
            customer["oldest_overdue_days"] = max(customer["oldest_overdue_days"], view["overdue_days"])
            customer["totals_by_currency"][currency] = _money(
                customer["totals_by_currency"].get(currency, 0) + view["amount_outstanding"]
            )
        customers = sorted(customers_by_name.values(), key=lambda value: (-value["oldest_overdue_days"], value["buyer_name"].casefold()))
        return {"ok": True, "results": results, "count": len(results), "truncated": len(overdue) > limit,
                "totals_by_currency": totals_by_currency, "customer_count": len(customers), "customers": customers}
    finally:
        if transaction_connection is None: db.close()


def _customers_search(data, actor, correlation_id, transaction_connection=None):
    db = transaction_connection or _factory()()
    try:
        q = " ".join(str(data["query"]).split()).casefold(); limit = _limit(data)
        tokens = q.split()
        registered = [dict(row) for row in db.execute(
            "SELECT id,name,nip,email,phone FROM customers ORDER BY name,id"
        ).fetchall()]
        # The Wyszukiwania screen also resolves identities from order e-mail/name.
        # Include those identities when the CRM row is missing from the local copy.
        order_identities = [dict(row) for row in db.execute(
            """SELECT customer_id AS id,customer_name AS name,'' AS nip,
                      customer_email AS email,customer_phone AS phone
                 FROM orders
                WHERE TRIM(COALESCE(customer_name,''))<>'' OR TRIM(COALESCE(customer_email,''))<>''
                ORDER BY created_at DESC,id DESC"""
        ).fetchall()]
        matches, seen = [], set()
        for row in registered + order_identities:
            fields = [" ".join(str(row.get(key) or "").split()).casefold()
                      for key in ("name", "nip", "email", "phone")]
            haystack = " ".join(fields)
            compact_haystack = re.sub(r"[^\w]+", "", haystack, flags=re.UNICODE)
            compact_query = re.sub(r"[^\w]+", "", q, flags=re.UNICODE)
            if not (all(token in haystack for token in tokens)
                    or (len(compact_query) >= 3 and compact_query in compact_haystack)):
                continue
            identity = (("id", int(row["id"])) if row.get("id") is not None
                        else ("identity", fields[0], fields[2]))
            if identity in seen:
                continue
            seen.add(identity)
            matches.append({"id": int(row["id"]) if row.get("id") is not None else None,
                            "name": row.get("name") or row.get("email") or "", "nip": row.get("nip") or ""})
        selected = matches[:limit]
        return {"ok": True, "results": selected, "count": len(selected), "truncated": len(matches) > limit}
    finally:
        if transaction_connection is None: db.close()


def _inventory_summary(data, actor, correlation_id, transaction_connection=None):
    del data, actor, correlation_id
    if transaction_connection is not None:
        rows = build_replenishment_analysis(lambda: transaction_connection, today=_business_now().date())
    else:
        rows = build_replenishment_analysis(_factory(), today=_business_now().date())
    return {
        "ok": True,
        "product_count": len(rows),
        "stock_units": sum(int(row.get("stock_qty") or 0) for row in rows),
        "available_units": sum(int(row.get("available_qty") or 0) for row in rows),
        "reserved_units": sum(int(row.get("reserved_qty") or 0) for row in rows),
        "incoming_units": sum(int(row.get("incoming_qty") or 0) for row in rows),
    }


def _china_orders_summary(data, actor, correlation_id, transaction_connection=None):
    del actor, correlation_id
    db = transaction_connection or _factory()()
    try:
        scope = str(data.get("scope") or "active")
        active = ("planned", "ordered", "shipped", "problem")
        clauses, params = ["1=1"], []
        if scope == "active":
            clauses.append("LOWER(COALESCE(cp.status,'')) IN (?,?,?,?)"); params.extend(active)
        elif scope == "arrived":
            clauses.append("LOWER(COALESCE(cp.status,''))='arrived'")
        rows = db.execute(
            f"""SELECT cp.id,LOWER(COALESCE(cp.status,'')) status,COALESCE(SUM(ci.qty),0) units
                  FROM china_packages cp LEFT JOIN china_items ci ON ci.package_id=cp.id
                 WHERE {' AND '.join(clauses)} GROUP BY cp.id,cp.status""", params,
        ).fetchall()
        packages_by_status, pieces_by_status = {}, {}
        for row in rows:
            key = str(row["status"] or "unknown")
            packages_by_status[key] = packages_by_status.get(key, 0) + 1
            pieces_by_status[key] = pieces_by_status.get(key, 0) + int(row["units"] or 0)
        return {"ok": True, "scope": scope, "order_count": len(rows),
                "item_units": sum(int(row["units"] or 0) for row in rows),
                "by_status": packages_by_status, "packages_by_status": packages_by_status,
                "pieces_by_status": pieces_by_status,
                "pieces_excluding_planned": sum(
                    units for status, units in pieces_by_status.items() if status != "planned"
                )}
    finally:
        if transaction_connection is None: db.close()


def _customers_get(data, actor, correlation_id, transaction_connection=None):
    db = transaction_connection or _factory()()
    try:
        row = db.execute("SELECT * FROM customers WHERE id=?", (data["customer_id"],)).fetchone()
        if row is None: raise ControlledOperationError("CUSTOMER_NOT_FOUND", "Nie znaleziono klienta", status=NOOP)
        record = {"id": int(row["id"]), "name": row["name"], "nip": row["nip"],
                  "language": row["language"], "price_list": row["price_list"]}
        record["order_count"] = int(db.execute("SELECT COUNT(*) FROM orders WHERE customer_id=?", (row["id"],)).fetchone()[0])
        last_order = db.execute("SELECT id,order_no,status,created_at,currency FROM orders WHERE customer_id=? ORDER BY created_at DESC,id DESC LIMIT 1", (row["id"],)).fetchone()
        record["last_order"] = ({"id": int(last_order["id"]), "order_number": last_order["order_no"],
                                 "status": last_order["status"], "created_at": last_order["created_at"],
                                 "currency": str(last_order["currency"] or "PLN").upper(),
                                 "totals": _order_totals(db, int(last_order["id"]))} if last_order else None)
        sums = db.execute("""SELECT UPPER(COALESCE(i.currency,o.currency,'PLN')) currency,COUNT(*) invoice_count,
                                    SUM(i.total_net) total_net,SUM(i.total_gross) total_gross,
                                    SUM(CASE WHEN COALESCE(m.paid,0)=1 THEN i.total_gross ELSE 0 END) paid_gross
                             FROM invoices i LEFT JOIN orders o ON o.id=i.order_id
                             LEFT JOIN invoice_meta m ON m.invoice_id=i.id
                             WHERE o.customer_id=? GROUP BY 1 ORDER BY 1""", (row["id"],)).fetchall()
        record["invoice_totals"] = [{"currency": r["currency"], "invoice_count": int(r["invoice_count"]),
                                     "total_net": _money(r["total_net"]), "total_gross": _money(r["total_gross"]),
                                     "paid_gross": _money(r["paid_gross"])} for r in sums]
        return {"ok": True, "record": record}
    finally:
        if transaction_connection is None: db.close()


def _sales_summary(data, actor, correlation_id, transaction_connection=None):
    db = transaction_connection or _factory()()
    try:
        start, end = _date_bounds(data, default_period="this_month")
        order_where, order_params = ["1=1"], []
        invoice_where, invoice_params = ["1=1"], []
        if start:
            order_where.append("SUBSTR(TRIM(o.created_at),1,10)>=?"); order_params.append(start)
            invoice_where.append("SUBSTR(TRIM(i.issue_date),1,10)>=?"); invoice_params.append(start)
        if end:
            order_where.append("SUBSTR(TRIM(o.created_at),1,10)<=?"); order_params.append(end)
            invoice_where.append("SUBSTR(TRIM(i.issue_date),1,10)<=?"); invoice_params.append(end)
        order_count = int(db.execute(f"SELECT COUNT(*) FROM orders o WHERE {' AND '.join(order_where)}", order_params).fetchone()[0])
        invoice_count = int(db.execute(f"SELECT COUNT(*) FROM invoices i WHERE {' AND '.join(invoice_where)}", invoice_params).fetchone()[0])
        rows = db.execute(f"""SELECT UPPER(COALESCE(i.currency,o.currency,'PLN')) currency,
                                      SUM(i.total_net) invoice_net,SUM(i.total_gross) invoice_gross,
                                      SUM(CASE WHEN COALESCE(m.paid,0)=1 THEN i.total_gross ELSE 0 END) paid_gross
                               FROM invoices i LEFT JOIN orders o ON o.id=i.order_id
                               LEFT JOIN invoice_meta m ON m.invoice_id=i.id
                               WHERE {' AND '.join(invoice_where)} GROUP BY 1 ORDER BY 1""", invoice_params).fetchall()
        currency_map = {r["currency"]: {"currency": r["currency"], "order_count": 0, "order_net": 0.0,
                        "order_gross": 0.0, "average_order_gross": 0.0,
                        "invoice_net": _money(r["invoice_net"]), "invoice_gross": _money(r["invoice_gross"]),
                        "paid_gross": _money(r["paid_gross"])} for r in rows}
        order_rows = db.execute(f"""SELECT currency,COUNT(*) order_count,SUM(order_net) order_net,SUM(order_gross) order_gross
            FROM (SELECT o.id,UPPER(COALESCE(NULLIF(TRIM(oi.currency),''),NULLIF(TRIM(o.currency),''),'PLN')) currency,
                         SUM(oi.qty*COALESCE(oi.unit_net_price,0)) order_net,
                         SUM(oi.qty*COALESCE(oi.unit_gross_price,oi.unit_net_price,0)) order_gross
                  FROM orders o LEFT JOIN order_items oi ON oi.order_id=o.id
                  WHERE {' AND '.join(order_where)} GROUP BY o.id,2) grouped GROUP BY currency ORDER BY currency""", order_params).fetchall()
        for r in order_rows:
            entry = currency_map.setdefault(r["currency"], {"currency": r["currency"], "invoice_net": 0.0,
                "invoice_gross": 0.0, "paid_gross": 0.0})
            entry.update({"order_count": int(r["order_count"]), "order_net": _money(r["order_net"]),
                          "order_gross": _money(r["order_gross"]),
                          "average_order_gross": _money(float(r["order_gross"] or 0) / max(1, int(r["order_count"])))})
        for entry in currency_map.values():
            entry.setdefault("order_count", 0); entry.setdefault("order_net", 0.0)
            entry.setdefault("order_gross", 0.0); entry.setdefault("average_order_gross", 0.0)
        by_currency = [currency_map[key] for key in sorted(currency_map)]
        top = db.execute(f"""SELECT COALESCE(NULLIF(TRIM(i.buyer_name),''),o.customer_name,'-') customer,
                                    UPPER(COALESCE(i.currency,o.currency,'PLN')) currency,
                                    COUNT(*) invoice_count,SUM(i.total_net) invoice_net
                             FROM invoices i LEFT JOIN orders o ON o.id=i.order_id
                             WHERE {' AND '.join(invoice_where)} GROUP BY 1,2 ORDER BY invoice_net DESC LIMIT 20""", invoice_params).fetchall()
        top_customers = [{"customer": r["customer"], "currency": r["currency"],
                          "invoice_count": int(r["invoice_count"]), "invoice_net": _money(r["invoice_net"])} for r in top]
        return {"ok": True, "date_from": start, "date_to": end, "order_count": order_count,
                "invoice_count": invoice_count, "by_currency": by_currency, "top_customers": top_customers}
    finally:
        if transaction_connection is None: db.close()


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
    "agent.terminology.search": agent_conversation.search_terminology,
    "agent.terminology.remember": agent_conversation.remember_terminology,
    "inventory.product.search": _product_search,
    "inventory.product.get": _product_get,
    "inventory.summary": _inventory_summary,
    "orders.search": _orders_search,
    "orders.get": _orders_get,
    "orders.summary": _orders_summary,
    "invoices.search": _invoices_search,
    "invoices.get": _invoices_get,
    "invoices.overdue": _invoices_overdue,
    "customers.search": _customers_search,
    "customers.get": _customers_get,
    "china.orders.summary": _china_orders_summary,
    "business.sales.summary": _sales_summary,
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


def _safe_diagnostic_args(data: Mapping[str, Any]) -> dict[str, Any]:
    safe = {"keys": sorted(str(key) for key in data)}
    if "query" in data:
        safe["query_length"] = len(str(data.get("query") or ""))
    for key in ("status", "payment_status", "period", "date_field", "as_of", "scope", "limit"):
        if key in data:
            safe[key] = data[key]
    return safe


def _diagnostic_result(operation: str, *, status: str, started: float,
                       output: Any = None, error_code: str = "", stage: str = "complete") -> None:
    count = None
    if isinstance(output, Mapping):
        count = output.get("count", output.get("order_count", output.get("product_count")))
    payload_size = len(json.dumps(sanitize_audit_data(output), ensure_ascii=False, separators=(",", ":")).encode("utf-8")) if output is not None else 0
    logger.info("BUSINESS_OPERATION_RESULT %s", json.dumps({
        "operation": operation, "status": status, "count": count,
        "error_code": error_code, "payload_size": payload_size, "stage": stage,
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
    }, ensure_ascii=False, sort_keys=True))


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
        handler_started = time.perf_counter()
        logger.info("BUSINESS_OPERATION_INPUT %s", json.dumps({
            "operation": definition.operation_name, "args": _safe_diagnostic_args(data),
        }, ensure_ascii=False, sort_keys=True))
        try:
            raw_output = _HANDLERS[definition.operation_name](data, actor, row["correlation_id"], None)
            try:
                output = validate_output(definition, raw_output)
            except Exception:
                _diagnostic_result(definition.operation_name, status=FAILED, started=handler_started,
                                   output=raw_output, error_code="INVALID_HANDLER_OUTPUT", stage="output_validation")
                raise
        except ControlledOperationError as exc:
            if exc.error_code != "INVALID_HANDLER_OUTPUT":
                _diagnostic_result(definition.operation_name, status=exc.status, started=handler_started,
                                   error_code=exc.error_code, stage="handler")
            event = "business_operation.conflict" if exc.status == CONFLICT else "business_operation.failed"
            audit_result = CONFLICT if exc.status == CONFLICT else FAILED
            terminal = "CONFLICT" if exc.status == CONFLICT else "FAILED"
            row = _transition(execution_id, definition, actor, terminal, event, audit_result,
                              error_code=exc.error_code, message=exc.safe_message, completed=True)
            return _result_from_row(row)
        except Exception as exc:
            _diagnostic_result(definition.operation_name, status=FAILED, started=handler_started,
                               error_code="HANDLER_FAILED", stage="handler")
            safe_message = sanitize_audit_text(exc)
            row = _transition(execution_id, definition, actor, "FAILED", "business_operation.failed", FAILED,
                              error_code="HANDLER_FAILED", message=safe_message, completed=True)
            return _result_from_row(row)
        _diagnostic_result(definition.operation_name, status=SUCCESS, started=handler_started, output=output)
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
