import pytest

import app as backend
from test_business_freshness import _remote_rows


@pytest.fixture()
def panel(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "DB_PATH", str(tmp_path / "panel.db"))
    backend.init_db()
    monkeypatch.setattr(backend, "SUPABASE_URL", "https://example.test")
    monkeypatch.setattr(backend, "SUPABASE_SERVICE_ROLE_KEY", "test-key")
    monkeypatch.setattr(backend.app, "secret_key", "test-secret")
    monkeypatch.setattr(backend, "_run_post_pull_reconciliation", lambda: None)
    monkeypatch.setattr(backend, "normalize_temp_order_numbers", lambda: None)
    monkeypatch.setattr(backend, "link_orders_to_customers_by_email", lambda **_kwargs: 0)
    monkeypatch.setattr(backend, "trigger_background_supabase_pull", lambda **_kwargs: (False, "test"))
    with backend._supabase_sync_lock:
        backend._supabase_sync_state["initial_pull_attempted"] = False
        backend._supabase_sync_state["pull_running"] = False
        backend._supabase_sync_state["last_pull_finished_ts"] = 0.0
    return _remote_rows()


def _client():
    client = backend.app.test_client()
    with client.session_transaction() as session:
        session["admin_authenticated"] = True
        session["csrf_token"] = "test-csrf"
    return client


@pytest.mark.parametrize("path", ["/", "/orders", "/invoices", "/stock"])
def test_main_panel_routes_return_data_unavailable_when_bootstrap_fails(panel, monkeypatch, path):
    monkeypatch.setattr(
        backend,
        "pull_shared_tables_from_supabase",
        lambda **_kwargs: {
            "ok": False,
            "error": "DATA_UNAVAILABLE",
            "reason": "SYNC_FAILED",
            "tables": {"products": {"status": "error", "stage": "fetch"}},
        },
    )

    response = _client().get(path)

    assert response.status_code == 503
    assert b"DATA_UNAVAILABLE" in response.data


@pytest.mark.parametrize("path", ["/", "/orders", "/invoices", "/stock"])
def test_main_panel_routes_bootstrap_empty_sqlite_before_render(panel, monkeypatch, path):
    calls = []

    def select(table, order_by="id", **_kwargs):
        calls.append(table)
        return [dict(row) for row in panel.get(table, [])]

    monkeypatch.setattr(backend, "supabase_select_rows", select)

    response = _client().get(path)

    assert response.status_code == 200
    assert {"products", "stock", "customers", "orders", "invoices"}.issubset(calls)
    db = backend.conn()
    try:
        assert db.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM stock").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == 1
    finally:
        db.close()


def test_cloud_to_local_bootstrap_does_not_write_customer_links_to_supabase(panel, monkeypatch):
    sync_remote_values = []
    monkeypatch.setattr(
        backend,
        "supabase_select_rows",
        lambda table, **_kwargs: [dict(row) for row in panel.get(table, [])],
    )
    monkeypatch.setattr(
        backend,
        "link_orders_to_customers_by_email",
        lambda sync_remote=True: sync_remote_values.append(sync_remote) or 0,
    )

    result = backend.pull_shared_tables_from_supabase(force=True, delete_missing=False)

    assert result["ok"] is True
    assert sync_remote_values == [False]
