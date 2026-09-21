"""Feature-gated high-level READ models for the internal agent."""

from __future__ import annotations

from datetime import datetime
import os
from typing import Callable
from zoneinfo import ZoneInfo

import daily_operational_state
import deliveries_operational_state
import finance_operational_state
import inventory_operational_state
import orders_operational_state


FEATURE_FLAG = "AGENT_HIGH_LEVEL_READ_MODELS_ENABLED"
READ_OPERATIONS = frozenset({
    "business.orders.state", "business.inventory.state", "business.finance.state",
    "business.deliveries.state", "business.daily.state", "dashboard.read",
})
PLANNER_READ_OPERATIONS = READ_OPERATIONS | {"business.query"}
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
INVENTORY_STATE_INPUT = DAILY_STATE_INPUT
DELIVERIES_STATE_INPUT = DAILY_STATE_INPUT
FINANCE_STATE_INPUT = {
    "type": "object", "additionalProperties": False, "properties": {
        "period": {"type": "string", "enum": ["today", "this_week", "this_month", "last_month", "this_year"]},
        "date_from": {"type": "string", "format": "date"},
        "date_to": {"type": "string", "format": "date"},
        "limit": {"type": "integer", "minimum": 1, "maximum": MAX_SECTION_ITEMS},
    },
}
STATE_OUTPUT = {
    "type": "object",
    "required": ["ok", "read_model", "as_of", "complete", "truncated", "scope", "sections"],
    "properties": {
        "ok": {"type": "boolean"}, "read_model": {"type": "string"},
        "as_of": {"type": "string"}, "complete": {"type": "boolean"},
        "truncated": {"type": "boolean"},
        "scope": {
            "type": "object", "additionalProperties": False,
            "required": ["kind", "entity_existence_authoritative", "empty_means"],
            "properties": {
                "kind": {"type": "string", "enum": ["operational_view"]},
                "entity_existence_authoritative": {"type": "boolean"},
                "empty_means": {"type": "string"},
            },
        },
        "sections": {"type": "object"},
    },
}


def enabled() -> bool:
    return str(os.environ.get(FEATURE_FLAG, "0")).strip().lower() in {"1", "true", "yes", "on"}


def _bounded(sections: dict, limit: int) -> tuple[dict, bool]:
    if "orders" in sections and "product_demand_coverage" in sections:
        all_orders = sections["orders"]
        truncated = len(all_orders) > limit
        selected_orders = []
        remaining_shortages = limit
        product_ids = set()
        for original in all_orders[:limit]:
            item = dict(original)
            missing_items = list(item.get("missing_items") or [])
            selected_missing = missing_items[:remaining_shortages]
            if len(selected_missing) < len(missing_items):
                truncated = True
            remaining_shortages -= len(selected_missing)
            item["missing_items"] = selected_missing
            product_ids.update(int(row["product_id"]) for row in selected_missing)
            selected_orders.append(item)
        coverage = [
            item for item in sections["product_demand_coverage"]
            if int(item["product_id"]) in product_ids
        ]
        if len(coverage) > limit:
            coverage = coverage[:limit]
            truncated = True
        return {
            "orders": selected_orders,
            "product_demand_coverage": coverage,
        }, truncated

    truncated = False
    bounded = {}
    for key, value in sections.items():
        if not isinstance(value, list):
            bounded[key] = value
            continue
        truncated = truncated or len(value) > limit
        selected = []
        remaining_nested = limit
        for item in value[:limit]:
            if isinstance(item, dict) and isinstance(item.get("items"), list):
                selected_items = item["items"][:remaining_nested]
                truncated = truncated or len(selected_items) < len(item["items"])
                remaining_nested -= len(selected_items)
                item = {**item, "items": selected_items}
            if isinstance(item, dict) and isinstance(item.get("missing_items"), list):
                selected_items = item["missing_items"][:remaining_nested]
                truncated = truncated or len(selected_items) < len(item["missing_items"])
                remaining_nested -= len(selected_items)
                item = {**item, "missing_items": selected_items}
            selected.append(item)
        bounded[key] = selected
    return bounded, truncated


def _response(read_model: str, sections: dict, now: datetime, limit: int) -> dict:
    sections, truncated = _bounded(sections, limit)
    return {"ok": True, "read_model": read_model, "as_of": now.isoformat(),
            "complete": not truncated, "truncated": truncated,
            "scope": {
                "kind": "operational_view",
                "entity_existence_authoritative": False,
                "empty_means": "no_matching_rows_in_this_operational_view",
            },
            "sections": sections}


def orders_state(data, actor, correlation_id="", transaction_connection=None,
                 *, connection_factory: Callable):
    del actor, correlation_id, transaction_connection
    now = datetime.now(ZoneInfo("Europe/Warsaw"))
    sections = orders_operational_state.build_orders_operational_state(
        connection_factory, current_time=now,
        order_id=data.get("order_id"), customer_id=data.get("customer_id"),
    )
    return _response("orders_state", sections, now, int(data.get("limit") or 25))


def inventory_state(data, actor, correlation_id="", transaction_connection=None,
                    *, connection_factory: Callable):
    del actor, correlation_id, transaction_connection
    now = datetime.now(ZoneInfo("Europe/Warsaw"))
    sections = inventory_operational_state.build_inventory_operational_state(
        connection_factory, current_time=now)
    return _response("inventory_state", sections, now, int(data.get("limit") or 25))


def finance_state(data, actor, correlation_id="", transaction_connection=None,
                  *, invoice_search: Callable, invoice_overdue: Callable,
                  sales_summary: Callable):
    del actor, correlation_id, transaction_connection
    now = datetime.now(ZoneInfo("Europe/Warsaw"))
    limit = int(data.get("limit") or 25)
    overdue = invoice_overdue({"limit": limit})
    unpaid = invoice_search({"payment_status": "unpaid", "limit": limit})
    period = {key: data[key] for key in ("period", "date_from", "date_to") if data.get(key)}
    sales = sales_summary(period)
    sections = finance_operational_state.compose_finance_operational_state(overdue, unpaid, sales)
    return _response("finance_state", sections, now, limit)


def deliveries_state(data, actor, correlation_id="", transaction_connection=None,
                     *, connection_factory: Callable):
    del actor, correlation_id, transaction_connection
    now = datetime.now(ZoneInfo("Europe/Warsaw"))
    sections = deliveries_operational_state.build_deliveries_operational_state(
        connection_factory, current_time=now)
    return _response("deliveries_state", sections, now, int(data.get("limit") or 25))


def daily_state(data, actor, correlation_id="", transaction_connection=None,
                *, connection_factory: Callable):
    del actor, correlation_id, transaction_connection
    now = datetime.now(ZoneInfo("Europe/Warsaw"))
    sections = daily_operational_state.build_daily_operational_state(
        connection_factory, current_time=now)
    return _response("daily_state", sections, now, int(data.get("limit") or 25))
