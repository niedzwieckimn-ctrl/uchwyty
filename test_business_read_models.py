import json
import uuid

import pytest

import app as backend
import agent_runtime
import business_operations as operations
import business_read_models
import internal_rbac as rbac


@pytest.fixture()
def read_models(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "DB_PATH", str(tmp_path / "read-models.db"))
    backend.init_db()
    db = backend.conn()
    now = "2026-09-14T10:00:00+02:00"
    db.execute("INSERT INTO products(id,sku,model,name,archived,created_at) VALUES(1,'SKU-A','Hugo','Hugo',0,?)", (now,))
    db.execute("INSERT INTO stock(product_id,qty) VALUES(1,0)")
    db.execute("""INSERT INTO orders(id,order_no,customer_name,status,currency,created_at)
                  VALUES(1,'ZAM-1','Firma A','confirmed','PLN',?)""", (now,))
    db.execute("""INSERT INTO order_items(id,order_id,product_id,sku,qty,unit_net_price,currency,created_at)
                  VALUES(1,1,1,'SKU-A',3,10,'PLN',?)""", (now,))
    db.commit(); db.close()
    return rbac.load_actor_context(rbac.BOOTSTRAP_OWNER_ACTOR_ID, request_id="read-model-owner")


def test_high_level_read_models_are_feature_gated(read_models, monkeypatch):
    monkeypatch.delenv(business_read_models.FEATURE_FLAG, raising=False)
    names = {item["name"] for item in operations.list_available_operations(read_models)}
    assert not business_read_models.READ_OPERATIONS & names
    denied = operations.execute_business_operation(read_models, "business.orders.state", {})
    assert denied.status == "DENIED" and denied.error_code == "OPERATION_DISABLED"

    monkeypatch.setenv(business_read_models.FEATURE_FLAG, "1")
    names = {item["name"] for item in operations.list_available_operations(read_models)}
    assert business_read_models.READ_OPERATIONS <= names


def test_orders_state_is_compact_and_uses_existing_coverage(read_models, monkeypatch):
    monkeypatch.setenv(business_read_models.FEATURE_FLAG, "1")
    result = operations.execute_business_operation(read_models, "business.orders.state", {})

    assert result.status == "SUCCESS", (result.error_code, result.safe_error_message)
    assert result.data["read_model"] == "orders_state"
    assert result.data["complete"] is True and result.data["truncated"] is False
    blocked = [
        order for order in result.data["sections"]["orders"]
        if not order["fulfillment_ready"]
    ]
    assert len(blocked) == 1
    assert blocked[0]["customer_name"] == "Firma A"
    assert blocked[0]["missing_items"][0]["model"] == "Hugo"
    assert blocked[0]["missing_items"][0]["missing_qty_against_stock"] == 3
    coverage = result.data["sections"]["product_demand_coverage"][0]
    assert coverage["covered_qty"] == 0
    assert coverage["uncovered_qty"] == 3
    assert coverage["fully_covered"] is False
    assert coverage["planned_ignored"] is True
    encoded = json.dumps(result.data, ensure_ascii=False).lower()
    assert "order_items" not in encoded and "china_packages" not in encoded


def test_daily_state_uses_same_read_gate_and_compact_contract(read_models, monkeypatch):
    monkeypatch.setenv(business_read_models.FEATURE_FLAG, "1")
    result = operations.execute_business_operation(read_models, "business.daily.state", {"limit": 10})

    assert result.status == "SUCCESS", (result.error_code, result.safe_error_message)
    assert result.data["read_model"] == "daily_state"
    assert set(result.data["sections"]) == {
        "ready_to_ship", "overdue_payments", "uncovered_order_shortages",
        "deliveries_requiring_attention", "other_urgent_exceptions",
    }


def test_existing_permissions_filter_each_read_model(read_models, monkeypatch):
    monkeypatch.setenv(business_read_models.FEATURE_FLAG, "1")
    actor_id = str(uuid.uuid4())
    db = backend.conn(); now = backend.now_iso()
    db.execute("INSERT INTO internal_actors VALUES(?,?,?,'active',?,?)",
               (actor_id, "HUMAN", "Warehouse", now, now))
    db.execute("INSERT INTO internal_actor_roles VALUES(?,?,?)", (actor_id, "MAGAZYN", now))
    db.commit(); db.close()
    warehouse = rbac.load_actor_context(actor_id)
    names = {item["name"] for item in operations.list_available_operations(warehouse)}

    assert "business.orders.state" in names
    assert "business.daily.state" not in names


def test_all_v1_read_models_are_registered_behind_one_flag(read_models, monkeypatch):
    monkeypatch.setenv(business_read_models.FEATURE_FLAG, "1")
    names = {item["name"] for item in operations.list_available_operations(read_models)}
    assert business_read_models.READ_OPERATIONS <= names

    for operation, expected_model in (
        ("business.inventory.state", "inventory_state"),
        ("business.finance.state", "finance_state"),
        ("business.deliveries.state", "deliveries_state"),
    ):
        result = operations.execute_business_operation(read_models, operation, {})
        assert result.status == "SUCCESS", (operation, result.error_code, result.safe_error_message)
        assert result.data["read_model"] == expected_model


def _new_read_db(tmp_path, monkeypatch, name):
    monkeypatch.setattr(backend, "DB_PATH", str(tmp_path / name))
    monkeypatch.setenv(business_read_models.FEATURE_FLAG, "1")
    monkeypatch.setattr(backend, "SUPABASE_URL", "")
    monkeypatch.setattr(backend, "SUPABASE_SERVICE_ROLE_KEY", "")
    backend.init_db()
    return backend.conn()


def test_real_cross_area_coverage_uses_existing_helpers_and_ignores_planned(tmp_path, monkeypatch):
    db = _new_read_db(tmp_path, monkeypatch, "real-coverage.db")
    now = "2026-09-14T10:00:00+02:00"
    products = [
        (1, "VICTOR", "Victor"), (2, "AOSTA", "Aosta"),
        (3, "WINSOR-AB", "Winsor AB"), (4, "SAM", "Sam"),
        (5, "HUGO", "Hugo"), (6, "WINSOR-BB", "Winsor BB"),
        (7, "OTHER", "Other"),
    ]
    db.executemany(
        "INSERT INTO products(id,sku,model,name,archived,created_at) VALUES(?,?,?,?,0,?)",
        [(product_id, sku, model, model, now) for product_id, sku, model in products],
    )
    db.executemany("INSERT INTO stock(product_id,qty) VALUES(?,0)",
                   [(product_id,) for product_id, _sku, _model in products])
    orders = [
        (1, "ZAM-VICTOR", "Victor customer", 1, "VICTOR", 4),
        (2, "ZAM-AOSTA", "Aosta customer", 2, "AOSTA", 2),
        (3, "ZAM-WINSOR-AB", "Winsor AB customer", 3, "WINSOR-AB", 7),
        (4, "ZAM-SAM-1", "Sam customer 1", 4, "SAM", 3),
        (5, "ZAM-SAM-2", "Sam customer 2", 4, "SAM", 2),
        (6, "ZAM-HUGO", "Hugo customer", 5, "HUGO", 7),
        (7, "ZAM-WINSOR-BB", "Winsor BB customer", 6, "WINSOR-BB", 1),
    ]
    for order_id, number, customer, product_id, sku, qty in orders:
        db.execute(
            "INSERT INTO orders(id,order_no,customer_name,status,currency,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (order_id, number, customer, "confirmed", "PLN", now),
        )
        db.execute(
            "INSERT INTO order_items(id,order_id,product_id,sku,qty,unit_net_price,currency,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (order_id, order_id, product_id, sku, qty, 10, "PLN", now),
        )
    db.executemany(
        "INSERT INTO china_packages(id,package_no,status,supplier,tracking,tracking_error,cost_amount,created_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        [
            (1, "PO-ORDERED", "ordered", "Supplier", "", "", 100, now),
            (2, "PO-SHIPPED", "shipped", "Supplier", "", "", 100, now),
            (3, "PO-PLANNED", "planned", "Supplier", "", "", 100, now),
        ],
    )
    db.executemany(
        "INSERT INTO china_items(id,package_id,product_id,sku,qty,created_at) VALUES(?,?,?,?,?,?)",
        [
            (1, 1, 1, "VICTOR", 12, now),
            (2, 1, 2, "AOSTA", 50, now),
            (3, 2, 3, "WINSOR-AB", 18, now),
            (4, 2, 7, "OTHER", 50, now),
            (5, 3, 6, "WINSOR-BB", 50, now),
        ],
    )
    db.commit()
    db.close()
    actor = rbac.load_actor_context(rbac.BOOTSTRAP_OWNER_ACTOR_ID, request_id="real-coverage")

    orders_result = operations.execute_business_operation(actor, "business.orders.state", {})
    inventory_result = operations.execute_business_operation(actor, "business.inventory.state", {})

    assert orders_result.status == inventory_result.status == "SUCCESS"
    order_coverage = {
        item["sku"]: item
        for item in orders_result.data["sections"]["product_demand_coverage"]
    }
    inventory_coverage = {
        item["sku"]: item for item in inventory_result.data["sections"]["products"]
        if item["missing_qty_against_stock"] > 0
    }
    for sku in ("VICTOR", "AOSTA", "WINSOR-AB"):
        assert order_coverage[sku]["fully_covered"] is True
        assert order_coverage[sku]["uncovered_qty"] == 0
    expected_uncovered = {"SAM": 5, "HUGO": 7, "WINSOR-BB": 1}
    assert {sku: order_coverage[sku]["uncovered_qty"] for sku in expected_uncovered} == expected_uncovered
    assert sum(item["uncovered_qty"] for item in order_coverage.values()) == 13
    assert order_coverage["WINSOR-BB"]["confirmed_incoming_qty"] == 0
    assert order_coverage["WINSOR-BB"]["planned_ignored"] is True
    assert {
        sku: (
            item["missing_qty_against_stock"], item["covered_qty"],
            item["uncovered_qty"], item["fully_covered"],
        )
        for sku, item in inventory_coverage.items()
    } == {
        sku: (
            item["missing_qty_against_stock"], item["covered_qty"],
            item["uncovered_qty"], item["fully_covered"],
        )
        for sku, item in order_coverage.items()
    }


def test_realistic_read_results_are_compact_bounded_and_keep_coverage(tmp_path, monkeypatch):
    db = _new_read_db(tmp_path, monkeypatch, "realistic-size.db")
    now = "2026-09-14T10:00:00+02:00"
    for product_id in range(1, 31):
        sku = f"SKU-{product_id:03}"
        db.execute(
            "INSERT INTO products(id,sku,model,name,archived,created_at) VALUES(?,?,?,?,0,?)",
            (product_id, sku, f"Model {product_id}", f"Product {product_id}", now),
        )
        db.execute("INSERT INTO stock(product_id,qty) VALUES(?,0)", (product_id,))
    for order_id in range(1, 76):
        product_id = ((order_id - 1) % 30) + 1
        sku = f"SKU-{product_id:03}"
        db.execute(
            "INSERT INTO orders(id,order_no,customer_name,status,currency,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (order_id, f"ZAM-{order_id:07}", f"Customer {order_id}", "confirmed", "PLN", now),
        )
        db.execute(
            "INSERT INTO order_items(id,order_id,product_id,sku,qty,unit_net_price,currency,created_at) "
            "VALUES(?,?,?,?,3,10,'PLN',?)",
            (order_id, order_id, product_id, sku, now),
        )
    for package_id in range(1, 9):
        status = "planned" if package_id > 5 else ("shipped" if package_id % 2 == 0 else "ordered")
        db.execute(
            "INSERT INTO china_packages(id,package_no,status,supplier,tracking,tracking_error,cost_amount,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (package_id, f"PO-{package_id}", status, f"Supplier {package_id}",
             f"TRACK-{package_id}", "", 100, now),
        )
        for offset in range(1, 11):
            product_id = ((package_id * 10 + offset - 2) % 30) + 1
            item_id = (package_id - 1) * 10 + offset
            db.execute(
                "INSERT INTO china_items(id,package_id,product_id,sku,qty,created_at) "
                "VALUES(?,?,?,?,8,?)",
                (item_id, package_id, product_id, f"SKU-{product_id:03}", now),
            )
    db.commit()
    db.close()
    actor = rbac.load_actor_context(rbac.BOOTSTRAP_OWNER_ACTOR_ID, request_id="realistic-size")

    orders_result = operations.execute_business_operation(actor, "business.orders.state", {})
    inventory_result = operations.execute_business_operation(actor, "business.inventory.state", {})
    assert orders_result.status == inventory_result.status == "SUCCESS"
    orders_bytes = len(json.dumps(orders_result.data, ensure_ascii=False, separators=(",", ":")).encode())
    inventory_bytes = len(json.dumps(inventory_result.data, ensure_ascii=False, separators=(",", ":")).encode())

    assert orders_bytes < agent_runtime.MAX_TOOL_RESULT_BYTES
    assert inventory_bytes < agent_runtime.MAX_TOOL_RESULT_BYTES
    assert orders_result.data["truncated"] is True
    assert inventory_result.data["truncated"] is True
    assert orders_result.data["complete"] is False
    assert inventory_result.data["complete"] is False
    assert orders_result.data["as_of"] and inventory_result.data["as_of"]
    assert set(orders_result.data["sections"]) == {"orders", "product_demand_coverage"}
    assert set(inventory_result.data["sections"]) == {"products"}
    visible_products = {
        item["product_id"]
        for order in orders_result.data["sections"]["orders"]
        for item in order["missing_items"]
    }
    coverage_products = {
        item["product_id"]
        for item in orders_result.data["sections"]["product_demand_coverage"]
    }
    assert visible_products == coverage_products
    assert all("covered_qty" in item and "uncovered_qty" in item
               for item in orders_result.data["sections"]["product_demand_coverage"])
