"""Feature-gated high-level READ models for the internal agent."""

from __future__ import annotations

from datetime import datetime
import os
from typing import Callable
from zoneinfo import ZoneInfo

import daily_operational_state
import orders_operational_state


FEATURE_FLAG = "AGENT_HIGH_LEVEL_READ_MODELS_ENABLED"
READ_OPERATIONS = frozenset({"business.orders.state", "business.daily.state"})
MAX_SECTION_ITEMS = 50

ORDERS_STATE_INPUT = {
    "type": "object", "additionalProperties": False, "properties": {
        "order_id": {"type": "integer", "minimum": 1},
        "customer_id": {"type": "integer", "minimum": 1},
        "limit": {"type": "integer", "minimum": 1, "maximum": MAX_SECTION_ITEMS},
    },
}
DAILY_STATE_INPUT = {
    "type": "object", "additionalProperties": False, "properties": {
        "limit": {"type": "integer", "minimum": 1, "maximum": MAX_SECTION_ITEMS},
    },
}
STATE_OUTPUT = {
    "type": "object",
    "required": ["ok", "read_model", "as_of", "complete", "truncated", "sections"],
    "properties": {
        "ok": {"type": "boolean"}, "read_model": {"type": "string"},
        "as_of": {"type": "string"}, "complete": {"type": "boolean"},
        "truncated": {"type": "boolean"}, "sections": {"type": "object"},
    },
}


def enabled() -> bool:
    return str(os.environ.get(FEATURE_FLAG, "0")).strip().lower() in {"1", "true", "yes", "on"}


def _bounded(sections: dict, limit: int) -> tuple[dict, bool]:
    truncated = any(isinstance(value, list) and len(value) > limit for value in sections.values())
    return {
        key: value[:limit] if isinstance(value, list) else value
        for key, value in sections.items()
    }, truncated


def orders_state(data, actor, correlation_id="", transaction_connection=None,
                 *, connection_factory: Callable):
    del actor, correlation_id, transaction_connection
    now = datetime.now(ZoneInfo("Europe/Warsaw"))
    sections = orders_operational_state.build_orders_operational_state(
        connection_factory, current_time=now,
        order_id=data.get("order_id"), customer_id=data.get("customer_id"),
    )
    sections, truncated = _bounded(sections, int(data.get("limit") or 25))
    return {"ok": True, "read_model": "orders_state", "as_of": now.isoformat(),
            "complete": not truncated, "truncated": truncated, "sections": sections}


def daily_state(data, actor, correlation_id="", transaction_connection=None,
                *, connection_factory: Callable):
    del actor, correlation_id, transaction_connection
    now = datetime.now(ZoneInfo("Europe/Warsaw"))
    sections = daily_operational_state.build_daily_operational_state(
        connection_factory, current_time=now)
    sections, truncated = _bounded(sections, int(data.get("limit") or 25))
    return {"ok": True, "read_model": "daily_state", "as_of": now.isoformat(),
            "complete": not truncated, "truncated": truncated, "sections": sections}
