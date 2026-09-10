"""Durable, minimal short-term context for the internal read-only agent."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import sqlite3
import threading
import uuid
from typing import Any, Callable, Mapping

from internal_audit import SUCCESS, record_audit_event, sanitize_audit_data
from internal_rbac import ActorContext


CONVERSATION_TTL_MINUTES = 45
MAX_STORED_CONTEXT_BYTES = 8_000
MAX_CANDIDATES = 20


class ConversationAccessDenied(RuntimeError):
    pass


_connection_factory: Callable[[], sqlite3.Connection] | None = None
_lock = threading.Lock()


def configure(connection_factory: Callable[[], sqlite3.Connection]) -> None:
    global _connection_factory
    if not callable(connection_factory):
        raise TypeError("connection_factory musi być wywoływalne")
    with _lock:
        _connection_factory = connection_factory


def _factory():
    if _connection_factory is None:
        raise RuntimeError("Conversation context nie został skonfigurowany")
    return _connection_factory


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def initialize_schema(db: sqlite3.Connection) -> None:
    db.executescript("""
    CREATE TABLE IF NOT EXISTS internal_agent_conversations(
        conversation_id TEXT PRIMARY KEY,
        human_actor_id TEXT NOT NULL REFERENCES internal_actors(actor_id) ON DELETE RESTRICT,
        ai_actor_id TEXT NOT NULL REFERENCES internal_actors(actor_id) ON DELETE RESTRICT,
        state_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        last_active_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        reset_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_internal_agent_conversations_owner
        ON internal_agent_conversations(human_actor_id,last_active_at);
    CREATE INDEX IF NOT EXISTS idx_internal_agent_conversations_expiry
        ON internal_agent_conversations(expires_at);
    """)
    db.commit()


def _audit(event: str, human: ActorContext, conversation_id: str, ai_actor_id: str) -> None:
    record_audit_event(
        event, result=SUCCESS, actor_context=human, entity_type="agent_conversation",
        entity_id=conversation_id, correlation_id=conversation_id,
        after_state={"conversation_id": conversation_id, "initiated_by_actor_id": human.actor_id,
                     "executed_by_actor_id": ai_actor_id}, source="agent_conversation",
    )


def _valid_id(value: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise ConversationAccessDenied("Nieprawidłowa rozmowa")


def open_conversation(human: ActorContext, ai: ActorContext, conversation_id: str = "") -> tuple[str, dict[str, Any], str]:
    now = _utc_now(); expiry = now + timedelta(minutes=CONVERSATION_TTL_MINUTES)
    db = _factory()()
    try:
        if conversation_id:
            conversation_id = _valid_id(conversation_id)
            row = db.execute("SELECT * FROM internal_agent_conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
            if row is None or row["human_actor_id"] != human.actor_id or row["ai_actor_id"] != ai.actor_id:
                raise ConversationAccessDenied("Brak dostępu do rozmowy")
            expired = datetime.fromisoformat(row["expires_at"]) <= now
            state = {} if expired else json.loads(row["state_json"] or "{}")
            db.execute("UPDATE internal_agent_conversations SET state_json=?,last_active_at=?,expires_at=? WHERE conversation_id=?",
                       (json.dumps(state, ensure_ascii=False, separators=(",", ":")), _iso(now), _iso(expiry), conversation_id))
            db.commit()
            _audit("agent.conversation.expired" if expired else "agent.conversation.resumed", human, conversation_id, ai.actor_id)
            return conversation_id, state, "expired" if expired else "resumed"
        conversation_id = str(uuid.uuid4())
        db.execute("INSERT INTO internal_agent_conversations(conversation_id,human_actor_id,ai_actor_id,state_json,created_at,last_active_at,expires_at) VALUES(?,?,?,'{}',?,?,?)",
                   (conversation_id, human.actor_id, ai.actor_id, _iso(now), _iso(now), _iso(expiry)))
        db.commit()
    finally:
        db.close()
    _audit("agent.conversation.created", human, conversation_id, ai.actor_id)
    return conversation_id, {}, "created"


def reset_conversation(human: ActorContext, ai: ActorContext, conversation_id: str) -> None:
    conversation_id = _valid_id(conversation_id); now = _utc_now()
    db = _factory()()
    try:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT human_actor_id,ai_actor_id FROM internal_agent_conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
        if row is None or row["human_actor_id"] != human.actor_id or row["ai_actor_id"] != ai.actor_id:
            raise ConversationAccessDenied("Brak dostępu do rozmowy")
        db.execute("UPDATE internal_agent_conversations SET state_json='{}',reset_at=?,last_active_at=?,expires_at=? WHERE conversation_id=?",
                   (_iso(now), _iso(now), _iso(now + timedelta(minutes=CONVERSATION_TTL_MINUTES)), conversation_id))
        db.commit()
    finally:
        db.close()
    _audit("agent.conversation.reset", human, conversation_id, ai.actor_id)


def _pick(item: Mapping[str, Any], names) -> dict[str, Any]:
    return {name: item.get(name) for name in names if item.get(name) not in (None, "")}


def update_context(conversation_id: str, operation: str, arguments: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, Any]:
    db = _factory()()
    try:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT state_json FROM internal_agent_conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
        if row is None:
            return {}
        state = json.loads(row["state_json"] or "{}")
        state["last_operation"] = operation
        if arguments.get("period") or arguments.get("date_from") or arguments.get("date_to") or arguments.get("as_of"):
            state["last_period"] = _pick(arguments, ("period", "date_from", "date_to", "date_field", "as_of"))
        records = result.get("candidates") if operation.startswith("inventory.product") else result.get("results")
        if operation == "inventory.product.get": records = [result]
        if operation == "orders.get": records = [result.get("record") or {}]
        if operation == "invoices.get": records = [result.get("record") or {}]
        if operation == "customers.get": records = [result.get("record") or {}]
        if isinstance(records, list):
            if operation.startswith("inventory.product"):
                fields, key = ("id", "sku", "model", "name", "stock"), "products"
            elif operation.startswith("orders."):
                fields, key = ("id", "order_number", "customer_id", "customer_name", "status"), "orders"
            elif operation.startswith("invoices."):
                fields, key = ("id", "invoice_number", "order_id", "buyer_name", "due_date", "currency", "amount_outstanding", "overdue_days"), "invoices"
            else:
                fields, key = ("id", "name", "nip"), "customers"
            state[key] = [_pick(item, fields) for item in records[:MAX_CANDIDATES] if isinstance(item, Mapping)]
        if operation == "customers.get" and isinstance(result.get("record"), Mapping):
            customer = result["record"]
            state["customer_detail"] = _pick(customer, ("id", "name", "nip", "order_count", "last_order", "invoice_totals"))
        if operation == "business.sales.summary":
            state["sales_summary"] = _pick(result, ("date_from", "date_to", "order_count", "invoice_count", "by_currency", "top_customers"))
        state = sanitize_audit_data(state)
        encoded = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_STORED_CONTEXT_BYTES:
            state = {"last_operation": operation, "last_period": state.get("last_period", {}),
                     "context_truncated": True}
            encoded = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
        now = _utc_now()
        db.execute("UPDATE internal_agent_conversations SET state_json=?,last_active_at=?,expires_at=? WHERE conversation_id=?",
                   (encoded, _iso(now), _iso(now + timedelta(minutes=CONVERSATION_TTL_MINUTES)), conversation_id))
        db.commit(); return state
    finally:
        db.close()
