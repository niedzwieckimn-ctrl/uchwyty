import json
import uuid

import pytest

import app as backend
import agent_runtime
import business_operations as operations
import business_query
import internal_rbac as rbac


@pytest.fixture()
def canonical(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "DB_PATH", str(tmp_path / "canonical.db"))
    backend.init_db()
    monkeypatch.setenv(business_query.FEATURE_FLAG, "1")
    db = backend.conn()
    now = "2026-09-14T09:00:00+02:00"
    db.executemany(
        "INSERT INTO products(id,sku,model,ean,name,archived,created_at) VALUES(?,?,?,?,?,0,?)",
        [(1, "SKU-A", "A", "111", "Alpha", now), (2, "SKU-B", "B", "222", "Beta", now)],
    )
    db.executemany("INSERT INTO stock(product_id,qty) VALUES(?,?)", [(1, 7), (2, 3)])
    db.execute("""INSERT INTO orders(id,order_no,customer_id,customer_name,status,currency,created_at)
                  VALUES(1,'ZAM-1',NULL,'Klient A','confirmed','PLN',?)""", (now,))
    db.executemany("""INSERT INTO order_items(id,order_id,product_id,sku,qty,unit_net_price,currency,created_at)
                        VALUES(?,?,?,?,?,?,?,?)""", [
        (1, 1, 1, "SKU-A", 10, 12.5, "PLN", now),
        (2, 1, 2, "SKU-B", 1, 20.0, "PLN", now),
    ])
    db.executemany("""INSERT INTO china_packages(id,package_no,status,supplier,created_at)
                        VALUES(?,?,?,?,?)""", [
        (1, "PO-ORDERED", "ordered", "Supplier", now),
        (2, "PO-SHIPPED", "shipped", "Supplier", now),
        (3, "PO-PLANNED", "planned", "Supplier", now),
    ])
    db.executemany("""INSERT INTO china_items(id,package_id,product_id,sku,qty,created_at)
                        VALUES(?,?,?,?,?,?)""", [
        (1, 1, 1, "SKU-A", 4, now),
        (2, 2, 1, "SKU-A", 1, now),
        (3, 3, 1, "SKU-A", 100, now),
    ])
    db.commit(); db.close()
    return rbac.load_actor_context(rbac.BOOTSTRAP_OWNER_ACTOR_ID, request_id="canonical-owner")


def run(owner, operation, payload):
    result = operations.execute_business_operation(owner, operation, payload)
    assert result.status == "SUCCESS", (result.error_code, result.safe_error_message)
    return result.data


def one(entity, **kwargs):
    return {"queries": [{"key": "result", "entity": entity, **kwargs}]}


def test_describe_schema_exposes_six_canonical_entities_without_storage_details(canonical):
    result = run(canonical, "business.describe_schema", {})
    assert {item["name"] for item in result["entities"]} == {
        "orders", "order_items", "products", "inventory",
        "purchase_orders", "purchase_order_items",
    }
    encoded = json.dumps(result).lower()
    for forbidden in ("china_packages", "china_items", "select *", "sqlite", "supabase", "provider_json"):
        assert forbidden not in encoded


def test_describe_schema_exposes_semantic_computed_business_fields(canonical):
    result = run(canonical, "business.describe_schema", {"entities": ["orders", "inventory"]})
    entities = {entity["name"]: {field["name"]: field for field in entity["fields"]}
                for entity in result["entities"]}
    ready = entities["orders"]["fulfillment_ready"]
    missing = entities["orders"]["fulfillment_missing_items"]
    coverage = entities["inventory"]["covered_by_stock_and_confirmed_incoming"]
    assert ready["computed"] is True and "kompletne" in ready["description"]
    assert ready["source"] == "fulfillment_readiness.calculate_fulfillment_readiness"
    assert missing["computed"] is True and missing["filterable"] is False
    assert coverage["computed"] is True and "planned P/O" in coverage["description"]
    assert coverage["source"] == "inventory_analytics.inventory_business_status"


def test_products_filter_limit_and_order(canonical):
    result = run(canonical, "business.query", one(
        "products", select=["id", "sku", "name"],
        where=[{"field": "sku", "op": "starts_with", "value": "SKU-"}],
        order_by=[{"field": "sku", "direction": "desc"}], limit=1,
    ))["results"][0]
    assert result["rows"] == [{"id": 2, "sku": "SKU-B", "name": "Beta"}]
    assert result["truncated"] is True and result["matched_count"] == 2


def test_orders_expand_items_and_products_to_depth_two(canonical):
    result = run(canonical, "business.query", one(
        "orders", select=["number", "status"],
        expand=[{"relationship": "items", "select": ["sku", "quantity", "product_id"],
                 "expand": [{"relationship": "product", "select": ["sku", "name"]}]}],
    ))["results"][0]["rows"][0]
    assert result["number"] == "ZAM-1"
    assert result["items"][0]["quantity"] == 10
    assert result["items"][0]["product"] == {"sku": "SKU-A", "name": "Alpha"}


def test_purchase_orders_expand_items_and_keep_planned_visible(canonical):
    rows = run(canonical, "business.query", one(
        "purchase_orders", select=["number", "status"],
        order_by=[{"field": "id", "direction": "asc"}],
        expand=[{"relationship": "items", "select": ["sku", "quantity"]}],
    ))["results"][0]["rows"]
    assert [row["status"] for row in rows] == ["ordered", "shipped", "planned"]
    assert rows[2]["items"] == [{"sku": "SKU-A", "quantity": 100}]


def test_inventory_reuses_existing_availability_and_incoming_semantics(canonical):
    row = run(canonical, "business.query", one(
        "inventory", select=["sku", "on_hand", "reserved", "available", "incoming_confirmed",
                             "reserved_incoming", "available_after_incoming"],
        where=[{"field": "sku", "op": "eq", "value": "SKU-A"}],
    ))["results"][0]["rows"][0]
    assert row == {"sku": "SKU-A", "on_hand": 7, "reserved": 10, "available": 0,
                   "incoming_confirmed": 5, "reserved_incoming": 3,
                   "available_after_incoming": 2}


def test_order_query_projects_existing_fulfillment_readiness_without_recalculation(canonical, monkeypatch):
    expected_missing = [{"product_id": 1, "sku": "SKU-A", "required_quantity": 10,
                         "available_quantity": 7, "shortage_quantity": 3}]
    calls = []

    def existing_helper(_db):
        calls.append(True)
        return [{"order_id": 1, "ready": False, "missing_items": expected_missing,
                 "total_units": 10}]

    monkeypatch.setattr(business_query.fulfillment_readiness,
                        "calculate_fulfillment_readiness", existing_helper)
    row = run(canonical, "business.query", one(
        "orders", select=["number", "fulfillment_ready", "fulfillment_missing_items",
                          "fulfillment_total_units"],
        where=[{"field": "fulfillment_ready", "op": "eq", "value": False}],
    ))["results"][0]["rows"][0]
    assert calls == [True]
    assert row == {"number": "ZAM-1", "fulfillment_ready": False,
                   "fulfillment_missing_items": expected_missing,
                   "fulfillment_total_units": 10}


def test_coverage_question_uses_ready_business_fields_and_ignores_planned_po(canonical):
    db = backend.conn(); now = backend.now_iso()
    db.execute("INSERT INTO products(id,sku,model,name,archived,created_at) VALUES(3,'SKU-C','C','Gamma',0,?)", (now,))
    db.execute("INSERT INTO stock(product_id,qty) VALUES(3,0)")
    db.execute("""INSERT INTO order_items(id,order_id,product_id,sku,qty,unit_net_price,currency,created_at)
                  VALUES(3,1,3,'SKU-C',4,10,'PLN',?)""", (now,))
    db.execute("INSERT INTO china_items(id,package_id,product_id,sku,qty,created_at) VALUES(4,3,3,'SKU-C',100,?)", (now,))
    db.commit(); db.close()

    result = run(canonical, "business.query", {"queries": [
        {"key": "blocked_orders", "entity": "orders",
         "select": ["number", "fulfillment_ready", "fulfillment_missing_items"],
         "where": [{"field": "fulfillment_ready", "op": "eq", "value": False}]},
        {"key": "uncovered_products", "entity": "inventory",
         "select": ["sku", "coverage_status", "covered_by_stock_and_confirmed_incoming"],
         "where": [{"field": "covered_by_stock_and_confirmed_incoming", "op": "eq", "value": False}]},
    ]})
    datasets = {item["key"]: item["rows"] for item in result["results"]}
    assert datasets["blocked_orders"][0]["fulfillment_ready"] is False
    assert {item["sku"] for item in datasets["blocked_orders"][0]["fulfillment_missing_items"]} == {"SKU-A", "SKU-C"}
    assert datasets["uncovered_products"] == [{
        "sku": "SKU-C", "coverage_status": "Problem",
        "covered_by_stock_and_confirmed_incoming": False,
    }]


def test_aggregations(canonical):
    rows = run(canonical, "business.query", one(
        "order_items", group_by=["order_id"],
        aggregates=[{"function": "sum", "field": "quantity", "as": "units"},
                    {"function": "count", "field": "*", "as": "lines"}],
        order_by=[{"field": "order_id", "direction": "asc"}],
    ))["results"][0]["rows"]
    assert rows == [{"order_id": 1, "units": 11, "lines": 2}]

    average = run(canonical, "business.query", one(
        "order_items", group_by=["currency"],
        aggregates=[{"function": "avg", "field": "unit_net_price", "as": "average_net"}],
    ))["results"][0]["rows"]
    assert average == [{"currency": "PLN", "average_net": 16.25}]


@pytest.mark.parametrize("payload,code", [
    (one("products", select=["cost_price"]), "FIELD_ACCESS_DENIED"),
    (one("customers"), "ENTITY_NOT_ALLOWED"),
    (one("products", where=[{"field": "sku", "op": "regex", "value": ".*"}]),
     "QUERY_VALIDATION_FAILED"),
    ({"queries": [{"key": "result", "entity": "products", "sql": "DELETE FROM products"}]},
     "QUERY_VALIDATION_FAILED"),
    (one("products", limit=201), "QUERY_LIMIT_EXCEEDED"),
])
def test_query_rejects_disallowed_shapes(canonical, payload, code):
    result = operations.execute_business_operation(canonical, "business.query", payload)
    assert result.status == "DENIED" and result.error_code == code


def test_feature_flag_and_owner_visibility(canonical, monkeypatch):
    monkeypatch.setenv(business_query.FEATURE_FLAG, "0")
    assert not any(item["name"] == "business.query" for item in operations.list_available_operations(canonical))
    denied = operations.execute_business_operation(canonical, "business.query", one("products"))
    assert denied.status == "DENIED" and denied.error_code == "OPERATION_DISABLED"

    monkeypatch.setenv(business_query.FEATURE_FLAG, "1")
    assert any(item["name"] == "business.query" for item in operations.list_available_operations(canonical))
    actor_id = str(uuid.uuid4())
    db = backend.conn(); now = backend.now_iso()
    db.execute("INSERT INTO internal_actors VALUES(?,?,?,'active',?,?)", (actor_id, "HUMAN", "Warehouse", now, now))
    db.execute("INSERT INTO internal_actor_roles VALUES(?,?,?)", (actor_id, "MAGAZYN", now))
    db.commit(); db.close()
    warehouse = rbac.load_actor_context(actor_id)
    assert not any(item["name"] == "business.query" for item in operations.list_available_operations(warehouse))

    owner_ai = rbac.load_actor_context(
        rbac.AI_OWNER_ASSISTANT_ACTOR_ID,
        delegated_by_actor_id=rbac.BOOTSTRAP_OWNER_ACTOR_ID,
    )
    assert any(item["name"] == "business.query"
               for item in agent_runtime._tool_descriptors(owner_ai, canonical))
    warehouse_ai = rbac.load_actor_context(
        rbac.AI_OWNER_ASSISTANT_ACTOR_ID,
        delegated_by_actor_id=warehouse.actor_id,
    )
    assert not any(item["name"] == "business.query"
                   for item in agent_runtime._tool_descriptors(warehouse_ai, warehouse))


def test_one_business_query_returns_orders_inventory_and_active_purchase_order_data(canonical):
    result = run(canonical, "business.query", {"queries": [
        {"key": "open_orders", "entity": "orders", "select": ["id", "number", "status"],
         "where": [{"field": "status", "op": "in", "value": ["confirmed"]}],
         "expand": [{"relationship": "items", "select": ["sku", "quantity"]}]},
        {"key": "availability", "entity": "inventory",
         "select": ["sku", "on_hand", "reserved", "available", "incoming_confirmed"]},
        {"key": "active_purchase_orders", "entity": "purchase_orders",
         "select": ["id", "number", "status"],
         "where": [{"field": "status", "op": "in", "value": ["planned", "ordered", "shipped"]}],
         "expand": [{"relationship": "items", "select": ["sku", "quantity"]}]},
    ]})
    datasets = {item["key"]: item for item in result["results"]}
    assert set(datasets) == {"open_orders", "availability", "active_purchase_orders"}
    assert datasets["open_orders"]["rows"][0]["items"][0] == {"sku": "SKU-A", "quantity": 10}
    assert {row["status"] for row in datasets["active_purchase_orders"]["rows"]} == {"planned", "ordered", "shipped"}


def test_old_read_tools_still_work(canonical):
    result = run(canonical, "inventory.product.search", {"query": "SKU-A"})
    assert result["candidates"][0]["sku"] == "SKU-A"
