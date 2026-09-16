import re

import pytest

import app as backend
import routes.shipping as shipping_routes


ORDER_ID = 10
ORDER_ITEM_ID = 101
PRODUCT_ID = 1


@pytest.fixture
def packing_app(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "DB_PATH", str(tmp_path / "packing-autofill.db"))
    monkeypatch.setattr(backend, "DATA_DIR", str(tmp_path))
    backend.init_db()
    backend.app.secret_key = "packing-autofill-test"
    monkeypatch.setattr(backend, "maybe_pull_shared_from_supabase", lambda *a, **k: None)
    monkeypatch.setattr(shipping_routes, "maybe_pull_shared_from_supabase", lambda *a, **k: None)

    now = backend.now_iso()
    db = backend.conn()
    db.execute(
        "INSERT INTO products(id,sku,model,name,created_at) VALUES(1,'SKU-SHARED','M1','Uchwyt',?)",
        (now,),
    )
    db.execute("INSERT INTO stock(product_id,qty) VALUES(1,10)")
    db.execute(
        """INSERT INTO orders(
               id,order_no,customer_name,customer_email,status,created_at,currency
             ) VALUES(10,'ZAM-10','Klient','buyer@example.invalid','confirmed',?,'PLN')""",
        (now,),
    )
    db.execute(
        """INSERT INTO order_items(
               id,order_id,product_id,sku,qty,unit_net_price,unit_gross_price,currency,created_at
             ) VALUES(101,10,1,'SKU-SHARED',7,10,12.3,'PLN',?)""",
        (now,),
    )
    db.commit()
    db.close()

    client = backend.app.test_client()
    with client.session_transaction() as session:
        session["admin_authenticated"] = True
        session["csrf_token"] = "csrf"
    return client


def _set_stock(qty):
    db = backend.conn()
    db.execute("UPDATE stock SET qty=? WHERE product_id=?", (qty, PRODUCT_ID))
    db.commit()
    db.close()


def _add_shipped_allocation(qty, *, invoice_id=201, order_id=ORDER_ID, item_id=ORDER_ITEM_ID):
    now = backend.now_iso()
    db = backend.conn()
    db.execute(
        """INSERT INTO invoices(
               id,order_id,invoice_no,issue_date,sell_date,payment_type,total_net,total_gross,created_at
             ) VALUES(?,?,?,?,?,'transfer',0,0,?)""",
        (invoice_id, order_id, f"FV/{invoice_id}", "2026-09-01", "2026-09-01", now),
    )
    db.execute(
        """INSERT INTO invoice_allocations(
               invoice_id,order_id,order_item_id,product_id,sku,qty,created_at
             ) VALUES(?,?,?,?,?,?,?)""",
        (invoice_id, order_id, item_id, PRODUCT_ID, "SKU-SHARED", qty, now),
    )
    db.commit()
    db.close()


def _packing_input(response, item_id=ORDER_ITEM_ID):
    html = response.get_data(as_text=True)
    tag = re.search(
        rf'<input\b(?=[^>]*\bname="pack_qty_{item_id}")[^>]*>',
        html,
        flags=re.I,
    )
    assert tag, html
    return dict(re.findall(r'([\w-]+)="([^"]*)"', tag.group(0)))


def _get_input(client, item_id=ORDER_ITEM_ID):
    response = client.get(f"/orders/{ORDER_ID}/packing-list")
    assert response.status_code == 200
    return _packing_input(response, item_id)


def test_no_previous_shipment_prefills_full_order_when_stock_allows(packing_app):
    attrs = _get_input(packing_app)
    assert attrs["max"] == "7"
    assert attrs["value"] == "7"


def test_partial_previous_shipment_is_subtracted(packing_app):
    _add_shipped_allocation(3)
    attrs = _get_input(packing_app)
    assert attrs["max"] == "4"
    assert attrs["value"] == "4"


def test_fully_shipped_item_prefills_zero(packing_app):
    _add_shipped_allocation(7)
    attrs = _get_input(packing_app)
    assert attrs["max"] == "0"
    assert attrs["value"] == "0"


def test_availability_below_remaining_caps_packable(packing_app):
    _set_stock(2)
    attrs = _get_input(packing_app)
    assert attrs["max"] == "2"
    assert attrs["value"] == "2"


def test_availability_above_remaining_uses_remaining(packing_app):
    _add_shipped_allocation(3)
    _set_stock(10)
    attrs = _get_input(packing_app)
    assert attrs["max"] == "4"
    assert attrs["value"] == "4"


def test_multiple_previous_shipments_are_summed_for_same_order_item(packing_app):
    _add_shipped_allocation(2, invoice_id=201)
    _add_shipped_allocation(1, invoice_id=202)
    attrs = _get_input(packing_app)
    assert attrs["max"] == "4"
    assert attrs["value"] == "4"


def test_same_sku_in_other_order_does_not_inherit_shipped_quantity(packing_app):
    _set_stock(20)
    _add_shipped_allocation(3)
    now = backend.now_iso()
    db = backend.conn()
    db.execute(
        """INSERT INTO orders(
               id,order_no,customer_name,customer_email,status,created_at,currency
             ) VALUES(20,'ZAM-20','Klient','buyer@example.invalid','confirmed',?,'PLN')""",
        (now,),
    )
    db.execute(
        """INSERT INTO order_items(
               id,order_id,product_id,sku,qty,unit_net_price,unit_gross_price,currency,created_at
             ) VALUES(201,20,1,'SKU-SHARED',7,10,12.3,'PLN',?)""",
        (now,),
    )
    db.commit()
    db.close()

    response = packing_app.get(f"/orders/{ORDER_ID}/packing-list")
    assert response.status_code == 200
    assert _packing_input(response, ORDER_ITEM_ID)["value"] == "4"
    assert _packing_input(response, 201)["value"] == "7"


def test_overallocated_history_never_produces_negative_packable(packing_app):
    _add_shipped_allocation(9)
    attrs = _get_input(packing_app)
    assert attrs["max"] == "0"
    assert attrs["value"] == "0"


def test_post_cannot_pack_more_than_remaining(packing_app, tmp_path, monkeypatch):
    _add_shipped_allocation(4)
    fake_pdf = lambda *a, **k: str(tmp_path / "packing.pdf")
    monkeypatch.setattr(backend, "generate_invoice_packing_list_pdf", fake_pdf)
    monkeypatch.setattr(shipping_routes, "generate_invoice_packing_list_pdf", fake_pdf)

    response = packing_app.post(
        f"/orders/{ORDER_ID}/packing-list",
        data={"csrf_token": "csrf", "carrier": "pending", f"pack_qty_{ORDER_ITEM_ID}": "99"},
    )

    assert response.status_code == 302
    db = backend.conn()
    saved_qty = db.execute(
        "SELECT SUM(qty) FROM packing_allocations WHERE order_item_id=?",
        (ORDER_ITEM_ID,),
    ).fetchone()[0]
    db.close()
    assert saved_qty == 3


def test_pack_input_value_equals_backend_packable_value(packing_app, monkeypatch):
    _add_shipped_allocation(2)
    _set_stock(3)
    captured = {}
    original_render = shipping_routes.render_template_string

    def capture_render(template, **context):
        if context.get("rows") is not None:
            captured["rows"] = context["rows"]
        return original_render(template, **context)

    monkeypatch.setattr(backend, "render_template_string", capture_render)
    monkeypatch.setattr(shipping_routes, "render_template_string", capture_render)
    response = packing_app.get(f"/orders/{ORDER_ID}/packing-list")
    assert response.status_code == 200
    backend_packable = captured["rows"][0]["packable_now"]
    attrs = _packing_input(response)
    assert backend_packable == 3
    assert int(attrs["value"]) == backend_packable
    assert int(attrs["max"]) == backend_packable
