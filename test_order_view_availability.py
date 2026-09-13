import pytest

import app as backend


@pytest.fixture()
def order_view_db(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "DB_PATH", str(tmp_path / "order-view.db"))
    monkeypatch.setattr(backend, "supabase_enabled", lambda: False)
    monkeypatch.setattr(backend, "maybe_pull_shared_from_supabase", lambda *args, **kwargs: None)
    backend.init_db()
    backend.app.secret_key = "order-view-test"
    return backend.app.test_client()


@pytest.mark.parametrize(
    ("stock_qty", "shows_shortage"),
    [
        (7, False),  # stock 7 - reserved 1 = available 6
        (1, True),   # stock 1 - reserved 1 = available 0
    ],
)
def test_order_view_uses_available_stock(order_view_db, stock_qty, shows_shortage):
    db = backend.conn()
    now = backend.now_iso()
    db.execute(
        "INSERT INTO products(id,sku,model,name,created_at) VALUES(1,?,?,?,?)",
        ("CH011-BB-128162", "CH011-BB-128162", "Uchwyt", now),
    )
    db.execute("INSERT INTO stock(product_id,qty) VALUES(1,?)", (stock_qty,))
    db.execute(
        """INSERT INTO orders(
             id,order_no,customer_name,status,created_at,warehouse_issued,currency
           ) VALUES(1,'ZAM-1','Klient','confirmed',?,0,'PLN')""",
        (now,),
    )
    db.execute(
        """INSERT INTO order_items(
             id,order_id,product_id,sku,qty,unit_net_price,unit_gross_price,currency,created_at
           ) VALUES(1,1,1,'CH011-BB-128162',1,10,12.3,'PLN',?)""",
        (now,),
    )
    db.commit()
    db.close()

    with order_view_db.session_transaction() as session:
        session["admin_authenticated"] = True
        session["csrf_token"] = "test"
    response = order_view_db.get("/orders/1")

    assert response.status_code == 200
    assert ("Brak towaru" in response.get_data(as_text=True)) is shows_shortage
