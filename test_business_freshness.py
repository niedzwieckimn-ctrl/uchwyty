from datetime import datetime

import pytest

import app as backend
import business_operations as operations
import internal_rbac as rbac


NOW = "2026-09-10T09:00:00+02:00"


def _remote_rows():
    return {
        "customers": [{"id": 1, "name": "AMInteriors", "language": "pl", "price_list": "pln", "created_at": NOW}],
        "products": [{"id": 1, "sku": "CH034-BB-160", "model": "Avery 160", "name": "Avery 160", "archived": 0, "created_at": NOW}],
        "stock": [{"product_id": 1, "qty": 12}],
        "orders": [{"id": 1, "order_no": "ZAM-1", "customer_id": 1, "customer_name": "AMInteriors", "status": "confirmed", "created_at": NOW, "currency": "PLN", "price_list": "pln"}],
        "order_items": [{"id": 1, "order_id": 1, "product_id": 1, "sku": "CH034-BB-160", "qty": 2, "unit_net_price": 100, "unit_gross_price": 123, "currency": "PLN", "created_at": NOW}],
        "invoices": [{"id": 1, "order_id": 1, "invoice_no": "FV/1", "issue_date": "2026-09-01", "sell_date": "2026-09-01", "payment_type": "transfer", "payment_to": "2026-09-08", "buyer_name": "AMInteriors", "buyer_tax_no": "123", "total_net": 200, "total_gross": 246, "currency": "PLN", "created_at": NOW}],
        "invoice_meta": [{"invoice_id": 1, "invoice_items_json": "[]", "paid": 0, "updated_at": NOW}],
        "invoice_allocations": [],
        "china_packages": [{"id": 1, "package_no": "PO-1", "status": "ordered", "created_at": NOW}],
        "china_items": [{"id": 1, "package_id": 1, "product_id": 1, "sku": "CH034-BB-160", "qty": 20, "created_at": NOW}],
        "cash_flow_settings": [],
    }


@pytest.fixture()
def freshness(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "DB_PATH", str(tmp_path / "freshness.db"))
    backend.init_db()
    monkeypatch.setattr(backend, "SUPABASE_URL", "https://example.test")
    monkeypatch.setattr(backend, "SUPABASE_SERVICE_ROLE_KEY", "test-key")
    monkeypatch.setattr(backend, "BUSINESS_FRESHNESS_TTL_SECONDS", 45)
    monkeypatch.setattr(operations, "_business_now", lambda: datetime.fromisoformat("2026-09-10T12:00:00+02:00"))
    calls = []
    rows = _remote_rows()

    def select(table, order_by="id", **_kwargs):
        calls.append(table)
        return [dict(row) for row in rows.get(table, [])]

    monkeypatch.setattr(backend, "supabase_select_rows", select)
    actor = rbac.load_actor_context(rbac.AI_OWNER_ASSISTANT_ACTOR_ID, request_id="freshness")
    return actor, calls, rows


def _run(actor, name, payload):
    result = operations.execute_business_operation(actor, name, payload)
    assert result.status == "SUCCESS", (result.error_code, result.safe_error_message)
    return result.data


def test_cold_inventory_syncs_then_hits_group_ttl(freshness):
    actor, calls, _rows = freshness
    first = _run(actor, "inventory.summary", {})
    assert first["product_count"] == 1 and first["stock_units"] == 12
    assert "products" in calls and "invoices" not in calls
    first_call_count = len(calls)
    assert _run(actor, "inventory.summary", {})["available_units"] == 10
    assert len(calls) == first_call_count


def test_fresh_marker_does_not_hide_empty_local_group(freshness):
    actor, calls, _rows = freshness
    backend._mark_business_freshness("inventory", __import__("time").time())

    result = _run(actor, "inventory.summary", {})

    assert result["product_count"] == 1
    assert "products" in calls


def test_empty_group_after_successful_pull_is_data_unavailable(freshness, monkeypatch):
    actor, _calls, _rows = freshness
    backend._mark_business_freshness("inventory", __import__("time").time())
    monkeypatch.setattr(backend, "supabase_select_rows", lambda *_args, **_kwargs: [])

    result = operations.execute_business_operation(actor, "inventory.summary", {})

    assert result.status == "FAILED"
    assert result.error_code == "DATA_UNAVAILABLE"
    assert backend._business_freshness_marker("inventory") is not None
    assert backend._business_snapshot_age("inventory", __import__("time").time()) is None


def test_empty_remote_group_does_not_erase_existing_local_snapshot(freshness, monkeypatch):
    actor, _calls, _rows = freshness
    assert _run(actor, "inventory.summary", {})["product_count"] == 1
    monkeypatch.setattr(backend, "BUSINESS_FRESHNESS_TTL_SECONDS", -1)
    monkeypatch.setattr(backend, "supabase_select_rows", lambda *_args, **_kwargs: [])

    result = _run(actor, "inventory.summary", {})

    assert result["product_count"] == 1
    db = backend.conn()
    try:
        assert db.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 1
    finally:
        db.close()


def test_global_bootstrap_marker_requires_local_business_data(freshness):
    _actor, _calls, _rows = freshness
    backend._mark_local_supabase_bootstrap_complete()

    assert backend._local_supabase_bootstrap_complete() is False


def test_first_get_repulls_when_marker_exists_but_sqlite_is_empty(freshness, monkeypatch):
    _actor, _calls, rows = freshness
    backend._mark_local_supabase_bootstrap_complete()
    pull_calls = []

    def successful_pull(**_kwargs):
        pull_calls.append(True)
        backend.sqlite_upsert_rows("products", rows["products"], "id")
        return {"ok": True, "tables": {"products": {"status": "ok", "rows": 1}}}

    monkeypatch.setattr(backend, "pull_shared_tables_from_supabase", successful_pull)
    monkeypatch.setattr(backend, "_run_post_pull_reconciliation", lambda: None)
    with backend._supabase_sync_lock:
        backend._supabase_sync_state["initial_pull_attempted"] = False

    with backend.app.test_request_context("/", method="GET"):
        result = backend.maybe_pull_shared_from_supabase()

    assert pull_calls == [True]
    assert result["ok"] is True
    assert backend._local_supabase_bootstrap_complete() is True


def test_cold_reads_find_inventory_customers_invoices_and_orders(freshness):
    actor, _calls, _rows = freshness
    assert _run(actor, "inventory.product.search", {"query": "Avery 160"})["count"] == 1
    assert _run(actor, "customers.search", {"query": "AMInteriors"})["count"] == 1
    assert _run(actor, "invoices.overdue", {"as_of": "2026-09-10"})["count"] == 1
    assert _run(actor, "orders.search", {"query": "ZAM-1"})["count"] == 1


def test_failed_cold_sync_returns_data_unavailable(freshness, monkeypatch):
    actor, _calls, _rows = freshness
    monkeypatch.setattr(backend, "supabase_select_rows", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("offline")))
    result = operations.execute_business_operation(actor, "inventory.summary", {})
    assert result.status == "FAILED" and result.error_code == "DATA_UNAVAILABLE"


def test_failed_refresh_uses_existing_marked_snapshot(freshness, monkeypatch):
    actor, _calls, _rows = freshness
    assert _run(actor, "inventory.summary", {})["stock_units"] == 12
    monkeypatch.setattr(backend, "BUSINESS_FRESHNESS_TTL_SECONDS", -1)
    monkeypatch.setattr(backend, "supabase_select_rows", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("offline")))
    assert _run(actor, "inventory.summary", {})["product_count"] == 1


def test_inventory_does_not_pull_invoice_tables(freshness):
    actor, calls, _rows = freshness
    _run(actor, "inventory.product.search", {"query": "CH034"})
    assert "invoices" not in calls and "invoice_meta" not in calls


def test_new_read_only_operations_use_narrow_freshness_groups(freshness):
    actor, calls, _rows = freshness
    readiness = _run(actor, "orders.fulfillment.readiness", {})
    assert readiness["ready_count"] == 1
    assert set(calls) == {"products", "stock", "orders", "order_items", "invoice_allocations"}
    calls.clear()
    china = _run(actor, "china.orders.get", {"id": 1})["record"]
    assert china["po_number"] == "PO-1" and china["items"][0]["quantity"] == 20
    assert set(calls) == {"products", "china_packages", "china_items"}


def test_new_readiness_preserves_data_unavailable_on_cold_failure(freshness, monkeypatch):
    actor, _calls, _rows = freshness
    monkeypatch.setattr(backend, "supabase_select_rows", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("offline")))
    result = operations.execute_business_operation(actor, "orders.fulfillment.readiness", {})
    assert result.status == "FAILED" and result.error_code == "DATA_UNAVAILABLE"
