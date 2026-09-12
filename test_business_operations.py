import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

import app as backend
import business_operations as operations
import internal_approval as approval
import internal_concurrency as concurrency
import internal_rbac as rbac


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "DB_PATH", str(tmp_path / "business-operations.db"))
    backend.init_db()
    backend.app.secret_key = "business-operation-test"
    monkeypatch.setattr(backend, "maybe_pull_shared_from_supabase", lambda **kwargs: None)
    monkeypatch.setattr(backend, "trigger_background_supabase_sync", lambda **kwargs: None)
    return backend.app.test_client()


def _owner(request_id="bo-owner"):
    return rbac.load_actor_context(rbac.BOOTSTRAP_OWNER_ACTOR_ID, request_id=request_id)


def _actor(actor_type="HUMAN", role="OWNER"):
    actor_id = str(uuid.uuid4())
    db = backend.conn()
    try:
        now = backend.now_iso()
        db.execute(
            """INSERT INTO internal_actors(actor_id,actor_type,display_name,status,created_at,updated_at)
               VALUES(?,?,?,'active',?,?)""",
            (actor_id, actor_type, "BO test", now, now),
        )
        if role:
            db.execute(
                "INSERT INTO internal_actor_roles(actor_id,role_key,assigned_at) VALUES(?,?,?)",
                (actor_id, role, now),
            )
        db.commit()
    finally:
        db.close()
    return rbac.load_actor_context(actor_id, request_id=f"bo-{actor_id[:8]}")


def _product():
    db = backend.conn()
    try:
        db.execute(
            "INSERT INTO products(id,sku,model,ean,name,archived,created_at) VALUES(1,'BO-1','Andre','123','Uchwyt',0,?)",
            (backend.now_iso(),),
        )
        db.execute("INSERT INTO stock(product_id,qty) VALUES(1,7)")
        db.commit()
    finally:
        db.close()


def _search_products():
    rows = (
        (10, "CH030-BB-128148", "CH030", "Victor", 7),
        (11, "CH034-BB-128160", "CH034", "Avery", 12),
        (12, "CH034-AB-160192", "CH034", "Avery", 4),
        (13, "CH034-GM-192224", "CH034", "Avery", 9),
        (14, "CH101-BLK-160", "CH101", "Andre", 20),
    )
    db = backend.conn()
    try:
        for product_id, sku, model, name, stock in rows:
            db.execute(
                "INSERT INTO products(id,sku,model,name,archived,created_at) VALUES(?,?,?,?,0,?)",
                (product_id, sku, model, name, backend.now_iso()),
            )
            db.execute("INSERT INTO stock(product_id,qty) VALUES(?,?)", (product_id, stock))
        db.commit()
    finally:
        db.close()


def _resource():
    return concurrency.create_versioned_resource("business-operation-pilot", {"amount": 0}, actor_context=_owner())


def _pending(requester, resource, key="pilot-key"):
    return operations.execute_business_operation(
        requester, "internal.test.change_setting",
        {"resource_id": resource.resource_id, "expected_version": 1, "amount": 2000},
        idempotency_key=key, correlation_id="bo-process-1",
    )


def _approve(approval_id):
    approval.approve_request(approval_id, _actor(), reason="Zatwierdzam pilot")


def _execution(execution_id):
    db = backend.conn()
    try:
        return db.execute(
            "SELECT * FROM internal_operation_executions WHERE execution_id=?", (execution_id,)
        ).fetchone()
    finally:
        db.close()


def test_registry_is_closed_and_contains_required_contracts(isolated):
    assert set(operations.OPERATION_REGISTRY) == {
        "orders.summary", "agent.terminology.search", "agent.terminology.remember",
        "inventory.product.get", "inventory.product.search", "inventory.summary",
        "orders.search", "orders.get", "orders.fulfillment.readiness", "invoices.search", "invoices.get", "invoices.overdue",
        "customers.search", "customers.get", "china.orders.summary", "china.orders.search", "china.orders.get", "business.sales.summary",
        "internal.test.change_setting", "internal.test.external.execute",
        "orders.internal_note.add", "orders.status.transition"
    }
    for item in operations.OPERATION_REGISTRY.values():
        assert item.operation_version == 1
        assert item.required_permission and item.input_schema and item.output_schema
        assert item.risk_level in approval.RISK_LEVELS
        assert item.idempotency_requirement in operations.IDEMPOTENCY_MODES


def test_unknown_operation_is_denied_without_handler(isolated):
    before = concurrency.get_versioned_resource("missing")
    result = operations.execute_business_operation(
        _owner(), "finance.transfer_everything", {"amount": 1}
    )
    assert result.status == "DENIED" and result.error_code == "UNKNOWN_OPERATION"
    assert concurrency.get_versioned_resource("missing") is before


@pytest.mark.parametrize("bad_input", [
    {}, {"product_id": "1"}, {"product_id": 1, "sql": "DROP TABLE products"},
    {"product_id": 0}, {"product_id": True},
])
def test_input_schema_rejects_missing_unknown_wrong_and_out_of_range_fields(isolated, bad_input):
    result = operations.execute_business_operation(_owner(), "inventory.product.get", bad_input)
    assert result.status == "DENIED"
    assert result.error_code in {"MISSING_INPUT_FIELD", "UNKNOWN_INPUT_FIELD", "INVALID_INPUT"}


def test_permission_and_risk_claims_do_not_override_registry(isolated):
    _product()
    read = operations.execute_business_operation(
        _owner(), "inventory.product.get", {"product_id": 1},
        claimed_permission="inventory.adjust", claimed_risk_level="RED",
    )
    assert read.status == "SUCCESS"
    resource = _resource()
    yellow = operations.execute_business_operation(
        _owner(), "internal.test.change_setting",
        {"resource_id": resource.resource_id, "expected_version": 1, "amount": 2000},
        idempotency_key="risk-spoof", claimed_risk_level="GREEN",
    )
    assert yellow.status == "PENDING_APPROVAL"
    assert _execution(yellow.execution_id)["risk_level"] == "YELLOW"


def test_actor_type_spoof_is_denied(isolated):
    ai = _actor("AI_AGENT", "AI_WAREHOUSE")
    forged = replace(ai, actor_type="HUMAN")
    result = operations.execute_business_operation(
        forged, "inventory.product.get", {"product_id": 1}, claimed_actor_type="OWNER"
    )
    assert result.status == "DENIED" and result.error_code == "UNTRUSTED_ACTOR"


def test_disabled_operation_fails_closed(isolated, monkeypatch):
    definition = operations.OPERATION_REGISTRY["inventory.product.get"]
    monkeypatch.setitem(
        operations.OPERATION_REGISTRY, definition.operation_name,
        replace(definition, enabled=False),
    )
    result = operations.execute_business_operation(_owner(), definition.operation_name, {"product_id": 1})
    assert result.status == "DENIED" and result.error_code == "OPERATION_DISABLED"


def test_registry_policy_mismatch_fails_closed(isolated, monkeypatch):
    definition = operations.OPERATION_REGISTRY["inventory.product.get"]
    monkeypatch.setitem(
        operations.OPERATION_REGISTRY, definition.operation_name,
        replace(definition, risk_level="RED"),
    )
    result = operations.execute_business_operation(_owner(), definition.operation_name, {"product_id": 1})
    assert result.status == "DENIED" and result.error_code == "REGISTRY_POLICY_MISMATCH"


def test_read_operation_owner_success_and_least_privilege_output(isolated):
    _product()
    result = operations.execute_business_operation(_owner(), "inventory.product.get", {"product_id": 1})
    assert result.status == "SUCCESS"
    assert result.data == {"ok": True, "id": 1, "sku": "BO-1", "model": "Andre", "ean": "123", "name": "Uchwyt", "stock": 7}
    assert "archived" not in result.data and "created_at" not in result.data


@pytest.mark.parametrize(("query", "expected_skus"), [
    ("CH030-BB-128148", {"CH030-BB-128148"}),
    ("CH030", {"CH030-BB-128148"}),
    ("avery", {"CH034-BB-128160", "CH034-AB-160192", "CH034-GM-192224"}),
    ("  AvErY   160  ", {"CH034-BB-128160", "CH034-AB-160192"}),
    ("  ch030-bb-128148  ", {"CH030-BB-128148"}),
])
def test_product_search_supports_catalog_terms_through_execution_gate(isolated, query, expected_skus):
    _search_products()
    result = operations.execute_business_operation(
        _owner(), "inventory.product.search", {"query": query}
    )
    assert result.status == "SUCCESS"
    assert {item["sku"] for item in result.data["candidates"]} == expected_skus
    assert result.data["count"] == len(expected_skus)
    assert result.data["truncated"] is False


def test_product_family_search_returns_all_variants_and_does_not_change_stock(isolated):
    _search_products()
    db = backend.conn()
    before = [tuple(row) for row in db.execute("SELECT product_id,qty FROM stock ORDER BY product_id")]
    db.close()

    result = operations.execute_business_operation(
        _owner(), "inventory.product.search", {"query": "Avery"}
    )

    db = backend.conn()
    after = [tuple(row) for row in db.execute("SELECT product_id,qty FROM stock ORDER BY product_id")]
    db.close()
    assert result.status == "SUCCESS"
    assert [item["sku"] for item in result.data["candidates"]] == [
        "CH034-AB-160192", "CH034-BB-128160", "CH034-GM-192224"
    ]
    assert before == after


def test_read_operation_without_permission_is_denied(isolated):
    result = operations.execute_business_operation(
        _actor("HUMAN", "KSIEGOWOSC"), "inventory.product.get", {"product_id": 1}
    )
    assert result.status == "DENIED" and result.error_code == "PERMISSION_DENIED"


def test_existing_http_endpoint_keeps_response_contract(isolated):
    _product()
    with isolated.session_transaction() as session:
        session["admin_authenticated"] = True
        session["internal_actor_id"] = rbac.BOOTSTRAP_OWNER_ACTOR_ID
    response = isolated.get("/api/product/1")
    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "id": 1, "sku": "BO-1", "model": "Andre", "ean": "123", "name": "Uchwyt", "stock": 7}


def test_yellow_requires_idempotency_key(isolated):
    resource = _resource()
    result = operations.execute_business_operation(
        _owner(), "internal.test.change_setting",
        {"resource_id": resource.resource_id, "expected_version": 1, "amount": 2000},
    )
    assert result.status == "DENIED" and result.error_code == "IDEMPOTENCY_KEY_REQUIRED"


def test_yellow_stays_pending_then_executes_after_human_approval(isolated):
    requester, resource = _owner(), _resource()
    pending = _pending(requester, resource)
    assert pending.status == "PENDING_APPROVAL" and pending.approval_id
    assert concurrency.get_versioned_resource(resource.resource_id).payload == {"amount": 0}
    _approve(pending.approval_id)
    completed = _pending(requester, resource)
    assert completed.status == "SUCCESS"
    assert completed.execution_id == pending.execution_id
    assert completed.data["amount"] == 2000
    assert concurrency.get_versioned_resource(resource.resource_id).version == 2


def test_idempotency_conflict_does_not_execute_changed_input(isolated):
    requester, resource = _owner(), _resource()
    pending = _pending(requester, resource, "same-key")
    changed = operations.execute_business_operation(
        requester, "internal.test.change_setting",
        {"resource_id": resource.resource_id, "expected_version": 1, "amount": 20000},
        idempotency_key="same-key",
    )
    assert pending.status == "PENDING_APPROVAL"
    assert changed.status == "CONFLICT" and changed.error_code == "IDEMPOTENCY_CONFLICT"
    assert concurrency.get_versioned_resource(resource.resource_id).payload == {"amount": 0}


def test_yellow_detects_entity_change_before_transactional_execution(isolated):
    requester, resource = _owner(), _resource()
    pending = _pending(requester, resource, "stale-entity-key")
    _approve(pending.approval_id)
    concurrency.update_versioned_resource(resource.resource_id, 1, {"amount": 10}, actor_context=requester)
    result = _pending(requester, resource, "stale-entity-key")
    assert result.status == "CONFLICT"
    assert result.error_code == "ENTITY_VERSION_CONFLICT"
    assert approval.get_request_snapshot(pending.approval_id)["status"] == "APPROVED"


def test_handler_failure_after_approval_is_explicit_and_not_retried(isolated, monkeypatch):
    requester, resource = _owner(), _resource()
    pending = _pending(requester, resource, "failure-key")
    _approve(pending.approval_id)
    calls = []

    def fail_handler(data, actor, correlation, transaction_connection):
        calls.append(data["amount"])
        raise RuntimeError("api_token=artificial-secret")

    monkeypatch.setitem(operations._HANDLERS, "internal.test.change_setting", fail_handler)
    failed = _pending(requester, resource, "failure-key")
    replay = _pending(requester, resource, "failure-key")
    assert failed.status == replay.status == "FAILED"
    assert failed.error_code == "HANDLER_FAILED"
    assert "artificial-secret" not in failed.safe_error_message
    assert calls == [2000]
    assert approval.get_request_snapshot(pending.approval_id)["status"] == "APPROVED"
    assert _execution(pending.execution_id)["completed_at"]
    assert concurrency.get_versioned_resource(resource.resource_id).payload == {"amount": 0}
    db = backend.conn()
    try:
        consumed_events = db.execute(
            "SELECT COUNT(*) FROM internal_audit_log WHERE approval_id=? AND operation='approval.consumed'",
            (pending.approval_id,),
        ).fetchone()[0]
        failed_events = db.execute(
            "SELECT COUNT(*) FROM internal_audit_log WHERE correlation_id=? AND operation='business_operation.failed'",
            (pending.correlation_id,),
        ).fetchone()[0]
    finally:
        db.close()
    assert consumed_events == 0 and failed_events == 1


def test_double_execution_runs_handler_once(isolated, monkeypatch):
    requester, resource = _owner(), _resource()
    pending = _pending(requester, resource, "parallel-key")
    _approve(pending.approval_id)
    original = operations._HANDLERS["internal.test.change_setting"]
    calls = []
    lock = threading.Lock()

    def counted(data, actor, correlation, transaction_connection):
        with lock:
            calls.append(data["amount"])
        time.sleep(0.15)
        return original(data, actor, correlation, transaction_connection)

    monkeypatch.setitem(operations._HANDLERS, "internal.test.change_setting", counted)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: _pending(requester, resource, "parallel-key"), range(2)))
    assert calls == [2000]
    assert "SUCCESS" in {item.status for item in results}
    assert concurrency.get_versioned_resource(resource.resource_id).version == 2


def test_invalid_handler_output_fails_closed(isolated, monkeypatch):
    _product()
    monkeypatch.setitem(operations._HANDLERS, "inventory.product.get", lambda *args: {"all_database_rows": []})
    result = operations.execute_business_operation(_owner(), "inventory.product.get", {"product_id": 1})
    assert result.status == "FAILED" and result.error_code == "INVALID_HANDLER_OUTPUT"


def test_tool_descriptors_are_safe_and_filtered_by_actor(isolated):
    warehouse = _actor("HUMAN", "MAGAZYN")
    accountant = _actor("HUMAN", "KSIEGOWOSC")
    ai = _actor("AI_AGENT", "AI_WAREHOUSE")
    warehouse_names = {item["name"] for item in operations.list_available_operations(warehouse)}
    accountant_names = {item["name"] for item in operations.list_available_operations(accountant)}
    ai_names = {item["name"] for item in operations.list_available_operations(ai)}
    assert "inventory.product.get" in warehouse_names and "inventory.product.get" in ai_names
    assert "inventory.product.get" not in accountant_names
    assert "internal.test.change_setting" not in ai_names
    descriptor = operations.operation_descriptor(operations.OPERATION_REGISTRY["inventory.product.get"])
    encoded = json.dumps(descriptor).lower()
    assert "permission" not in descriptor and "risk_level" not in descriptor
    assert "select " not in encoded and "service_role" not in encoded and "credential" not in encoded


def test_execution_audit_preserves_ids_and_uses_existing_outbox(isolated):
    _product()
    result = operations.execute_business_operation(
        _owner("bo-request-77"), "inventory.product.get", {"product_id": 1},
        correlation_id="bo-correlation-77",
    )
    db = backend.conn()
    try:
        rows = db.execute(
            """SELECT a.operation,a.request_id,a.correlation_id,o.status,a.after_state
               FROM internal_audit_log a JOIN internal_audit_outbox o ON o.audit_id=a.audit_id
               WHERE a.correlation_id=? AND a.operation LIKE 'business_operation.%'""",
            ("bo-correlation-77",),
        ).fetchall()
    finally:
        db.close()
    assert {row["operation"] for row in rows} == {"business_operation.success"}
    assert {row["request_id"] for row in rows} == {"bo-request-77"}
    assert all(row["status"] == "PENDING" for row in rows)
    assert all(result.execution_id in row["after_state"] for row in rows)


def test_execution_table_actor_foreign_key_is_text_compatible(isolated):
    db = backend.conn()
    try:
        actor_type = next(row[2] for row in db.execute("PRAGMA table_info(internal_actors)") if row[1] == "actor_id")
        execution_type = next(row[2] for row in db.execute("PRAGMA table_info(internal_operation_executions)") if row[1] == "actor_id")
    finally:
        db.close()
    assert actor_type == execution_type == "TEXT"
