import app as backend


def test_marking_order_shipped_does_not_change_stock(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "DB_PATH", str(tmp_path / "shipping.db"))
    backend.init_db()
    c = backend.conn()
    cur = c.cursor()
    cur.execute(
        "INSERT INTO products(id, sku, model, name, ean, created_at) VALUES(?,?,?,?,?,?)",
        (880001, "SHIP-001", "SHIP", "Test", "", backend.now_iso()),
    )
    cur.execute("INSERT INTO stock(product_id, qty) VALUES(?,?)", (880001, 10))
    cur.execute(
        """INSERT INTO orders(id, order_no, customer_name, customer_email, status, packed_at,
                              warehouse_issued, created_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (880101, "ZAM-TEST", "Klient", "klient@example.com", "packed", backend.now_iso(), 0, backend.now_iso()),
    )
    cur.execute(
        "INSERT INTO order_items(order_id, product_id, sku, qty, created_at) VALUES(?,?,?,?,?)",
        (880101, 880001, "SHIP-001", 2, backend.now_iso()),
    )
    c.commit()
    c.close()

    monkeypatch.setattr(backend, "maybe_pull_shared_from_supabase", lambda **kwargs: None)
    monkeypatch.setattr(backend, "supabase_enabled", lambda: False)
    monkeypatch.setattr(backend, "_order_packing_list_email_attachment", lambda order: {})
    monkeypatch.setattr(backend, "_send_orders_shipped_email", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr(backend, "_record_email_event", lambda *args, **kwargs: None)
    backend.app.secret_key = "test-secret"

    client = backend.app.test_client()
    with client.session_transaction() as admin_session:
        admin_session["admin_authenticated"] = True
        admin_session["csrf_token"] = "test-csrf"
    response = client.post(
        "/orders/880101/shipped",
        data={"tracking_no": "123456789", "carrier": "inpost", "notify_customer": "1", "csrf_token": "test-csrf"},
    )
    assert response.status_code == 302

    c = backend.conn()
    stock = c.execute("SELECT qty FROM stock WHERE product_id=?", (880001,)).fetchone()
    order = c.execute("SELECT status, warehouse_issued FROM orders WHERE id=?", (880101,)).fetchone()
    c.close()
    assert stock["qty"] == 10
    assert order["status"] == "shipped"
    assert order["warehouse_issued"] == 0
