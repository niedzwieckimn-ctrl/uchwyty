"""Canonical, owner-only, read-only query layer for the internal agent."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
import os
import re
from typing import Any, Callable, Mapping

from inventory_analytics import build_replenishment_analysis
from internal_rbac import DENY, load_actor_context


FEATURE_FLAG = "AGENT_GENERIC_READ_ENABLED"
SCHEMA_VERSION = "1"
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
MAX_SELECTED_FIELDS = 30
MAX_RELATIONSHIP_DEPTH = 2
MAX_AGGREGATES = 5
MAX_RESULT_CELLS = 5000
MAX_QUERIES = 6
MAX_FILTERS = 20

OPERATORS = frozenset({
    "eq", "ne", "in", "not_in", "gt", "gte", "lt", "lte", "between",
    "contains", "starts_with", "is_null",
})
AGGREGATIONS = frozenset({"count", "count_distinct", "sum", "min", "max", "avg"})
_SAFE_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")

_connection_factory: Callable | None = None


def configure(connection_factory: Callable) -> None:
    global _connection_factory
    if not callable(connection_factory):
        raise TypeError("connection_factory musi być wywoływalne")
    _connection_factory = connection_factory


def enabled() -> bool:
    return str(os.environ.get(FEATURE_FLAG, "0")).strip().lower() in {"1", "true", "yes", "on"}


def _error(code: str, message: str):
    from business_operations import ControlledOperationError, DENIED
    raise ControlledOperationError(code, message, status=DENIED)


def _owner(actor) -> None:
    human_id = actor.delegated_by_actor_id or actor.actor_id
    human = load_actor_context(human_id)
    if human is None or human.actor_type != "HUMAN" or "OWNER" not in human.roles:
        _error("PERMISSION_DENIED", "Generic business read jest dostępny wyłącznie dla OWNER")
    if human.permission_decision("business.generic_read") == DENY:
        _error("PERMISSION_DENIED", "Brak uprawnienia do generic business read")


def _field(field_type: str, *, nullable: bool = False, description: str = "") -> dict[str, Any]:
    value = {"type": field_type, "nullable": nullable, "filterable": True,
             "sortable": True, "aggregatable": field_type in {"integer", "number"}}
    if description:
        value["description"] = description
    return value


SCHEMA: dict[str, dict[str, Any]] = {
    "orders": {
        "fields": {
            "id": _field("integer"), "number": _field("string"),
            "customer_id": _field("integer", nullable=True), "customer_name": _field("string"),
            "status": _field("string"), "currency": _field("string"),
            "note": _field("string", nullable=True), "warehouse_issued": _field("boolean"),
            "created_at": _field("string"), "packed_at": _field("string", nullable=True),
            "shipped_at": _field("string", nullable=True),
        },
        "relationships": {
            "items": {"target": "order_items", "local_field": "id", "target_field": "order_id", "many": True},
        },
    },
    "order_items": {
        "fields": {
            "id": _field("integer"), "order_id": _field("integer"),
            "product_id": _field("integer"), "sku": _field("string"),
            "quantity": _field("integer"), "unit_net_price": _field("number", nullable=True),
            "unit_gross_price": _field("number", nullable=True), "currency": _field("string", nullable=True),
            "created_at": _field("string"),
        },
        "relationships": {
            "product": {"target": "products", "local_field": "product_id", "target_field": "id", "many": False},
        },
    },
    "products": {
        "fields": {
            "id": _field("integer"), "sku": _field("string"),
            "model": _field("string", nullable=True), "ean": _field("string", nullable=True),
            "name": _field("string", nullable=True), "archived": _field("boolean"),
            "created_at": _field("string"),
        },
        "relationships": {},
    },
    "inventory": {
        "fields": {
            "product_id": _field("integer"), "sku": _field("string"),
            "model": _field("string", nullable=True), "name": _field("string", nullable=True),
            "ean": _field("string", nullable=True),
            "on_hand": _field("integer", description="Fizyczny stan magazynowy."),
            "reserved": _field("integer", description="Ilość zarezerwowana przez aktywne zamówienia."),
            "available": _field("integer", description="Stan dostępny po rezerwacjach."),
            "incoming_confirmed": _field("integer", description="Ilość w potwierdzonych aktywnych P/O według istniejącej logiki inventory."),
            "reserved_incoming": _field("integer"),
            "available_after_incoming": _field("integer"),
        },
        "relationships": {},
    },
    "purchase_orders": {
        "fields": {
            "id": _field("integer"), "number": _field("string"),
            "supplier": _field("string", nullable=True), "status": _field("string"),
            "tracking_number": _field("string", nullable=True),
            "delivery_stage": _field("string", nullable=True),
            "shipping_method": _field("string", nullable=True),
            "ordered_at": _field("string", nullable=True), "shipped_at": _field("string", nullable=True),
            "arrived_at": _field("string", nullable=True), "created_at": _field("string"),
        },
        "relationships": {
            "items": {"target": "purchase_order_items", "local_field": "id", "target_field": "purchase_order_id", "many": True},
        },
    },
    "purchase_order_items": {
        "fields": {
            "id": _field("integer"), "purchase_order_id": _field("integer"),
            "product_id": _field("integer"), "sku": _field("string"),
            "quantity": _field("integer"), "created_at": _field("string"),
        },
        "relationships": {
            "product": {"target": "products", "local_field": "product_id", "target_field": "id", "many": False},
        },
    },
}


def _predicate_schema() -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False,
            "required": ["field", "op", "value"], "properties": {
                "field": {"type": "string"}, "op": {"type": "string", "enum": sorted(OPERATORS)},
                "value": {"type": ["string", "number", "integer", "boolean", "array", "null"]},
            }}


def _order_schema() -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False,
            "required": ["field", "direction"], "properties": {
                "field": {"type": "string"},
                "direction": {"type": "string", "enum": ["asc", "desc"]},
            }}


def _expand_schema(depth: int) -> dict[str, Any]:
    properties = {
        "relationship": {"type": "string"},
        "select": {"type": "array", "maxItems": MAX_SELECTED_FIELDS, "items": {"type": "string"}},
        "where": {"type": "array", "maxItems": MAX_FILTERS, "items": _predicate_schema()},
        "order_by": {"type": "array", "maxItems": 5, "items": _order_schema()},
        "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT},
    }
    if depth < MAX_RELATIONSHIP_DEPTH:
        properties["expand"] = {"type": "array", "maxItems": 10,
                                "items": _expand_schema(depth + 1)}
    return {"type": "object", "additionalProperties": False,
            "required": ["relationship"], "properties": properties}


DESCRIBE_INPUT_SCHEMA = {
    "type": "object", "additionalProperties": False, "properties": {
        "entities": {"type": "array", "maxItems": 6,
                     "items": {"type": "string", "enum": list(SCHEMA)}},
    },
}

QUERY_INPUT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["queries"], "properties": {
        "queries": {"type": "array", "minItems": 1, "maxItems": MAX_QUERIES, "items": {
            "type": "object", "additionalProperties": False, "required": ["key", "entity"],
            "properties": {
                "key": {"type": "string", "minLength": 1, "maxLength": 64},
                "entity": {"type": "string", "enum": list(SCHEMA)},
                "select": {"type": "array", "maxItems": MAX_SELECTED_FIELDS,
                           "items": {"type": "string"}},
                "where": {"type": "array", "maxItems": MAX_FILTERS,
                          "items": _predicate_schema()},
                "expand": {"type": "array", "maxItems": 10, "items": _expand_schema(1)},
                "group_by": {"type": "array", "maxItems": 10, "items": {"type": "string"}},
                "aggregates": {"type": "array", "maxItems": MAX_AGGREGATES, "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["function", "as"], "properties": {
                        "function": {"type": "string", "enum": sorted(AGGREGATIONS)},
                        "field": {"type": ["string", "null"]}, "as": {"type": "string"},
                    }}},
                "order_by": {"type": "array", "maxItems": 5, "items": _order_schema()},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT},
            },
        }},
    },
}


_BASE_SQL = {
    "orders": """SELECT id,order_no AS number,customer_id,customer_name,status,
        UPPER(COALESCE(currency,'PLN')) AS currency,note,
        CASE WHEN COALESCE(warehouse_issued,0)=1 THEN 1 ELSE 0 END AS warehouse_issued,
        created_at,packed_at,shipped_at FROM orders""",
    "order_items": """SELECT id,order_id,product_id,sku,qty AS quantity,
        unit_net_price,unit_gross_price,currency,created_at FROM order_items""",
    "products": """SELECT id,sku,model,ean,name,
        CASE WHEN COALESCE(archived,0)=1 THEN 1 ELSE 0 END AS archived,created_at FROM products""",
    "purchase_orders": """SELECT id,package_no AS number,supplier,status,tracking AS tracking_number,
        tracking_status AS delivery_stage,shipping_method,ordered_at,shipped_at,arrived_at,created_at
        FROM china_packages""",
    "purchase_order_items": """SELECT id,package_id AS purchase_order_id,product_id,sku,
        qty AS quantity,created_at FROM china_items""",
}


def _read_connection():
    if _connection_factory is None:
        raise RuntimeError("Canonical business query nie został skonfigurowany")
    db = _connection_factory()
    db.execute("PRAGMA query_only=ON")
    return db


def _inventory_rows() -> list[dict[str, Any]]:
    rows = build_replenishment_analysis(_read_connection, today=date.today())
    return [{
        "product_id": int(row["id"]), "sku": row.get("sku") or "",
        "model": row.get("model"), "name": row.get("name"), "ean": row.get("ean"),
        "on_hand": int(row.get("stock_qty") or 0),
        "reserved": int(row.get("reserved_qty") or 0),
        "available": int(row.get("available_qty") or 0),
        "incoming_confirmed": int(row.get("incoming_qty") or 0),
        "reserved_incoming": int(row.get("reserved_incoming") or 0),
        "available_after_incoming": int(row.get("available_qty") or 0)
            + int(row.get("available_incoming") or 0),
    } for row in rows]


def _load_entity(entity: str, cache: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    if entity in cache:
        return cache[entity]
    if entity == "inventory":
        rows = _inventory_rows()
    else:
        db = _read_connection()
        try:
            rows = [dict(row) for row in db.execute(_BASE_SQL[entity]).fetchall()]
        finally:
            db.close()
    for row in rows:
        for name, definition in SCHEMA[entity]["fields"].items():
            if definition["type"] == "boolean" and row.get(name) is not None:
                row[name] = bool(row[name])
    cache[entity] = rows
    return rows


def describe_schema(data, actor, correlation_id="", transaction_connection=None):
    del correlation_id, transaction_connection
    _owner(actor)
    requested = data.get("entities") or list(SCHEMA)
    if not isinstance(requested, list) or any(not isinstance(name, str) for name in requested):
        _error("QUERY_VALIDATION_FAILED", "entities musi być listą nazw encji")
    unknown = set(requested) - set(SCHEMA)
    if unknown:
        _error("ENTITY_NOT_ALLOWED", "Żądana encja nie jest dostępna")
    entities = []
    for name in requested:
        definition = SCHEMA[name]
        entities.append({
            "name": name,
            "fields": [{"name": field_name, **field} for field_name, field in definition["fields"].items()],
            "relationships": [{"name": rel_name, "target": rel["target"],
                               "cardinality": "many" if rel["many"] else "one"}
                              for rel_name, rel in definition["relationships"].items()],
        })
    return {"ok": True, "schema_version": SCHEMA_VERSION, "entities": entities}


def _check_field(entity: str, field: Any, *, code="FIELD_ACCESS_DENIED") -> str:
    if not isinstance(field, str) or field not in SCHEMA[entity]["fields"]:
        _error(code, "Pole nie jest dostępne w canonical schema")
    return field


def _check_scalar(field_type: str, value: Any, *, nullable=False) -> None:
    if value is None and nullable:
        return
    valid = (
        field_type == "integer" and isinstance(value, int) and not isinstance(value, bool)
        or field_type == "number" and isinstance(value, (int, float)) and not isinstance(value, bool)
        or field_type == "string" and isinstance(value, str)
        or field_type == "boolean" and isinstance(value, bool)
    )
    if not valid:
        _error("QUERY_VALIDATION_FAILED", "Wartość filtra ma nieprawidłowy typ")


def _predicates(entity: str, value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, Mapping) and set(value) == {"and"}:
        value = value["and"]
    if not isinstance(value, list) or len(value) > MAX_FILTERS:
        _error("QUERY_VALIDATION_FAILED", "where musi być listą maksymalnie 20 filtrów")
    checked = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) - {"field", "op", "value"}:
            _error("QUERY_VALIDATION_FAILED", "Filtr ma nieprawidłową strukturę")
        field = _check_field(entity, item.get("field"))
        op = item.get("op")
        if op not in OPERATORS:
            _error("QUERY_VALIDATION_FAILED", "Nieznany operator")
        definition = SCHEMA[entity]["fields"][field]
        candidate = item.get("value")
        if op == "is_null":
            if not isinstance(candidate, bool):
                _error("QUERY_VALIDATION_FAILED", "is_null wymaga wartości boolean")
        elif op in {"in", "not_in"}:
            if not isinstance(candidate, list) or len(candidate) > 100:
                _error("QUERY_VALIDATION_FAILED", "Operator listowy wymaga ograniczonej listy")
            for member in candidate:
                _check_scalar(definition["type"], member, nullable=definition["nullable"])
        elif op == "between":
            if not isinstance(candidate, list) or len(candidate) != 2:
                _error("QUERY_VALIDATION_FAILED", "between wymaga dwóch wartości")
            for member in candidate:
                _check_scalar(definition["type"], member, nullable=False)
        else:
            _check_scalar(definition["type"], candidate, nullable=definition["nullable"] and op in {"eq", "ne"})
        if op in {"contains", "starts_with"} and definition["type"] != "string":
            _error("QUERY_VALIDATION_FAILED", "Operator tekstowy wymaga pola string")
        checked.append({"field": field, "op": op, "value": candidate})
    return checked


def _matches(row: Mapping[str, Any], predicate: Mapping[str, Any]) -> bool:
    actual, expected, op = row.get(predicate["field"]), predicate["value"], predicate["op"]
    if op == "is_null": return (actual is None) is expected
    if op == "eq": return actual == expected
    if op == "ne": return actual != expected
    if op == "in": return actual in expected
    if op == "not_in": return actual not in expected
    if op == "gt": return actual is not None and actual > expected
    if op == "gte": return actual is not None and actual >= expected
    if op == "lt": return actual is not None and actual < expected
    if op == "lte": return actual is not None and actual <= expected
    if op == "between": return actual is not None and expected[0] <= actual <= expected[1]
    if op == "contains": return str(expected).casefold() in str(actual or "").casefold()
    if op == "starts_with": return str(actual or "").casefold().startswith(str(expected).casefold())
    return False


def _apply_where(rows: list[dict[str, Any]], predicates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if all(_matches(row, item) for item in predicates)]


def _order_spec(entity: str, value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 5:
        _error("QUERY_VALIDATION_FAILED", "order_by ma nieprawidłową strukturę")
    result = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"field", "direction"}:
            _error("QUERY_VALIDATION_FAILED", "order_by ma nieprawidłową strukturę")
        field = _check_field(entity, item["field"])
        if item["direction"] not in {"asc", "desc"}:
            _error("QUERY_VALIDATION_FAILED", "Nieznany kierunek sortowania")
        result.append({"field": field, "direction": item["direction"]})
    return result


def _sort(rows: list[dict[str, Any]], order_by: list[dict[str, str]]) -> list[dict[str, Any]]:
    result = list(rows)
    for item in reversed(order_by):
        field = item["field"]
        result.sort(key=lambda row: (row.get(field) is None, row.get(field)),
                    reverse=item["direction"] == "desc")
    return result


def _selected_fields(entity: str, value: Any) -> list[str]:
    if value is None:
        return list(SCHEMA[entity]["fields"])
    if not isinstance(value, list) or not value or len(value) > MAX_SELECTED_FIELDS:
        _error("QUERY_VALIDATION_FAILED", "select przekracza dozwolony zakres")
    fields = [_check_field(entity, field) for field in value]
    if len(set(fields)) != len(fields):
        _error("QUERY_VALIDATION_FAILED", "select zawiera duplikaty")
    return fields


def _aggregate(entity: str, rows: list[dict[str, Any]], group_by: Any, aggregates: Any) -> list[dict[str, Any]] | None:
    if aggregates is None:
        if group_by:
            _error("QUERY_VALIDATION_FAILED", "group_by wymaga aggregates")
        return None
    if not isinstance(aggregates, list) or not 1 <= len(aggregates) <= MAX_AGGREGATES:
        _error("QUERY_VALIDATION_FAILED", "aggregates przekracza dozwolony zakres")
    if group_by is None: group_by = []
    if not isinstance(group_by, list) or len(group_by) > 10:
        _error("QUERY_VALIDATION_FAILED", "group_by ma nieprawidłową strukturę")
    groups = [_check_field(entity, field) for field in group_by]
    specs = []
    for item in aggregates:
        if not isinstance(item, Mapping) or set(item) - {"function", "field", "as"}:
            _error("QUERY_VALIDATION_FAILED", "Agregacja ma nieprawidłową strukturę")
        function, field, alias = item.get("function"), item.get("field"), item.get("as")
        if function not in AGGREGATIONS or not isinstance(alias, str) or not _SAFE_NAME.fullmatch(alias):
            _error("QUERY_VALIDATION_FAILED", "Agregacja jest niedozwolona")
        if function == "count" and field in {None, "*"}:
            field = None
        else:
            field = _check_field(entity, field)
        if function in {"sum", "avg"} and SCHEMA[entity]["fields"][field]["type"] not in {"integer", "number"}:
            _error("QUERY_VALIDATION_FAILED", "Agregacja liczbowa wymaga pola number")
        specs.append((function, field, alias))
    buckets = defaultdict(list)
    for row in rows:
        buckets[tuple(row.get(field) for field in groups)].append(row)
    if not rows and not groups:
        buckets[()] = []
    output = []
    for key, members in buckets.items():
        record = {field: key[index] for index, field in enumerate(groups)}
        for function, field, alias in specs:
            values = [member.get(field) for member in members if field is not None and member.get(field) is not None]
            if function == "count": value = len(members) if field is None else len(values)
            elif function == "count_distinct": value = len(set(values))
            elif function == "sum": value = sum(values)
            elif function == "min": value = min(values) if values else None
            elif function == "max": value = max(values) if values else None
            else: value = (sum(values) / len(values)) if values else None
            record[alias] = value
        output.append(record)
    return output


def _expand(entity: str, rows: list[dict[str, Any]], specs: Any,
            cache: dict[str, list[dict[str, Any]]], depth: int) -> list[dict[str, Any]]:
    if specs is None:
        return rows
    if not isinstance(specs, list) or len(specs) > 10 or depth >= MAX_RELATIONSHIP_DEPTH:
        _error("QUERY_VALIDATION_FAILED", "expand przekracza dozwolony zakres")
    result = [dict(row) for row in rows]
    for spec in specs:
        if not isinstance(spec, Mapping) or set(spec) - {"relationship", "select", "where", "expand", "order_by", "limit"}:
            _error("QUERY_VALIDATION_FAILED", "Relacja ma nieprawidłową strukturę")
        relationship = spec.get("relationship")
        relation = SCHEMA[entity]["relationships"].get(relationship)
        if relation is None:
            _error("RELATIONSHIP_NOT_ALLOWED", "Relacja nie jest dostępna")
        target = relation["target"]
        selected = _selected_fields(target, spec.get("select"))
        predicates = _predicates(target, spec.get("where"))
        ordering = _order_spec(target, spec.get("order_by"))
        limit = spec.get("limit", DEFAULT_LIMIT)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_LIMIT:
            _error("QUERY_LIMIT_EXCEEDED", "Limit relacji jest poza dozwolonym zakresem")
        targets = _apply_where(_load_entity(target, cache), predicates)
        targets = _sort(targets, ordering)
        by_key = defaultdict(list)
        for target_row in targets:
            by_key[target_row.get(relation["target_field"])].append(target_row)
        for parent in result:
            matched = by_key.get(parent.get(relation["local_field"]), [])[:limit]
            matched = _expand(target, matched, spec.get("expand"), cache, depth + 1)
            projected = [{field: item.get(field) for field in selected} |
                         {key: value for key, value in item.items() if key in SCHEMA[target]["relationships"]}
                         for item in matched]
            parent[relationship] = projected if relation["many"] else (projected[0] if projected else None)
    return result


def _cell_count(value: Any) -> int:
    if isinstance(value, Mapping): return sum(_cell_count(item) for item in value.values())
    if isinstance(value, list): return sum(_cell_count(item) for item in value)
    return 1


def query(data, actor, correlation_id="", transaction_connection=None):
    del correlation_id, transaction_connection
    _owner(actor)
    queries = data.get("queries")
    if not isinstance(queries, list) or not 1 <= len(queries) <= MAX_QUERIES:
        _error("QUERY_VALIDATION_FAILED", "queries musi zawierać od 1 do 6 zapytań")
    cache: dict[str, list[dict[str, Any]]] = {}
    results, keys = [], set()
    for request in queries:
        allowed = {"key", "entity", "select", "where", "expand", "group_by", "aggregates", "order_by", "limit"}
        if not isinstance(request, Mapping) or set(request) - allowed:
            _error("QUERY_VALIDATION_FAILED", "Zapytanie zawiera niedozwolone elementy")
        key, entity = request.get("key"), request.get("entity")
        if not isinstance(key, str) or not _SAFE_NAME.fullmatch(key) or key in keys:
            _error("QUERY_VALIDATION_FAILED", "Każde zapytanie wymaga unikalnego bezpiecznego key")
        keys.add(key)
        if entity not in SCHEMA:
            _error("ENTITY_NOT_ALLOWED", "Encja nie jest dostępna")
        selected = _selected_fields(entity, request.get("select"))
        predicates = _predicates(entity, request.get("where"))
        ordering = _order_spec(entity, request.get("order_by"))
        limit = request.get("limit", DEFAULT_LIMIT)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_LIMIT:
            _error("QUERY_LIMIT_EXCEEDED", "Limit jest poza dozwolonym zakresem")
        rows = _apply_where(_load_entity(entity, cache), predicates)
        total = len(rows)
        aggregate_rows = _aggregate(entity, rows, request.get("group_by"), request.get("aggregates"))
        if aggregate_rows is not None:
            if request.get("expand"):
                _error("QUERY_VALIDATION_FAILED", "Agregacji nie można łączyć z expand")
            rows = aggregate_rows
        rows = _sort(rows, ordering)
        truncated = len(rows) > limit
        rows = rows[:limit]
        if aggregate_rows is None:
            rows = _expand(entity, rows, request.get("expand"), cache, 0)
            relationship_names = set(SCHEMA[entity]["relationships"])
            rows = [{field: row.get(field) for field in selected} |
                    {name: row[name] for name in relationship_names if name in row} for row in rows]
        results.append({"key": key, "entity": entity, "rows": rows,
                        "count": len(rows), "matched_count": total, "truncated": truncated})
    cells = _cell_count(results)
    if cells > MAX_RESULT_CELLS:
        _error("QUERY_RESULT_TOO_LARGE", "Wynik przekracza limit 5000 komórek")
    return {"ok": True, "schema_version": SCHEMA_VERSION, "results": results,
            "result_cells": cells}
