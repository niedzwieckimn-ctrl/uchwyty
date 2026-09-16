import pytest

import app as backend
import fulfillment_operations


@pytest.fixture()
def order_99(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "DB_PATH", str(tmp_path / "fulfillment-99.db"))
    monkeypatch.setattr(backend, "supabase_enabled", lambda: False)
    monkeypatch.setattr(
        backend, "maybe_pull_shared_from_supabase", lambda *args, **kwargs: None
    )
    backend.init_db()
    now = backend.now_iso()
    db = backend.conn()
    db.execute(
        "INSERT INTO products(id,sku,model,name,created_at) VALUES(99,'SKU-99','M99','Produkt 99',?)",
        (now,),
    )
    db.execute("INSERT INTO stock(product_id,qty) VALUES(99,8)")
    db.execute(
        """INSERT INTO orders(
             id,order_no,customer_name,status,created_at,warehouse_issued,currency
           ) VALUES(99,'ZAM-99','Klient','confirmed',?,0,'PLN')""",
        (now,),
    )
    db.execute(
        """INSERT INTO order_items(
             id,order_id,product_id,sku,qty,unit_net_price,unit_gross_price,currency,created_at
           ) VALUES(99,99,99,'SKU-99',7,10,12.3,'PLN',?)""",
        (now,),
    )
    db.commit()
    db.close()
    backend.app.secret_key = "fulfillment-hotfix"
    client = backend.app.test_client()
    with client.session_transaction() as session:
        session["admin_authenticated"] = True
        session["csrf_token"] = "test"
    return client


def _set_stock(qty):
    db = backend.conn()
    db.execute("UPDATE stock SET qty=? WHERE product_id=99", (qty,))
    db.commit()
    db.close()


def _order_page(client):
    response = client.get("/orders/99")
    assert response.status_code == 200
    return response.get_data(as_text=True)


def test_qty_seven_stock_eight_is_ready_from_warehouse(order_99):
    page = _order_page(order_99)
    assert "Z magazynu" in page
    assert "Brak towaru" not in page


def test_qty_seven_stock_six_reports_shortage(order_99):
    _set_stock(6)
    page = _order_page(order_99)
    assert "Brak towaru" in page
    assert "Z magazynu" not in page


def test_stock_refresh_changes_status_and_packing_uses_current_available_quantity(order_99):
    snapshot = fulfillment_operations.snapshot(99)
    payload = {
        "order_id": 99,
        "expected_version": fulfillment_operations.version(snapshot),
    }
    _set_stock(6)
    current = fulfillment_operations.preflight("orders.packing_list.generate", payload)
    proposal = fulfillment_operations.packing_list_preview(99)
    assert current["readiness"]["complete"] is False
    assert proposal["total_quantity"] == 6
    assert "Brak towaru" in _order_page(order_99)

    _set_stock(8)
    current = fulfillment_operations.preflight(
        "orders.packing_list.generate", payload
    )
    assert current["readiness"]["complete"] is True
    assert fulfillment_operations.packing_list_preview(99)["total_quantity"] == 7
    page = _order_page(order_99)
    assert "Z magazynu" in page
    assert "Brak towaru" not in page
