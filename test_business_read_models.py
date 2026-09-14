import json
import uuid

import pytest

import app as backend
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
    blocked = result.data["sections"]["blocked"]
    assert len(blocked) == 1
    assert blocked[0]["customer_name"] == "Firma A"
    assert blocked[0]["missing_items"][0]["model"] == "Hugo"
    assert blocked[0]["missing_items"][0]["quantity"] == 3
    assert blocked[0]["missing_items"][0]["source_state"][
        "covered_by_stock_and_confirmed_incoming"] is False
    encoded = json.dumps(result.data, ensure_ascii=False).lower()
    assert "order_items" not in encoded and "china_packages" not in encoded


def test_daily_state_uses_same_read_gate_and_compact_contract(read_models, monkeypatch):
    monkeypatch.setenv(business_read_models.FEATURE_FLAG, "1")
    result = operations.execute_business_operation(read_models, "business.daily.state", {"limit": 10})

    assert result.status == "SUCCESS", (result.error_code, result.safe_error_message)
    assert result.data["read_model"] == "daily_state"
    assert set(result.data["sections"]) == {
        "ready_to_ship", "overdue_payments", "uncovered_order_shortages",
        "covered_order_shortages", "deliveries_requiring_attention",
        "other_urgent_exceptions",
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
