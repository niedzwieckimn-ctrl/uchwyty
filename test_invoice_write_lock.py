import threading

import pytest

import app as backend
import fulfillment_operations
import invoice_numbering
import routes.invoices as invoice_routes
from test_invoice_refactor import (
    _invoice_create_form,
    _invoice_edit_client,
    historical_db,
)


def _add_invoice_order():
    db = backend.conn()
    db.execute(
        "INSERT INTO orders(id,order_no,customer_id,customer_name,customer_address,customer_email,status,created_at,currency) "
        "VALUES(99,'ZAM-99',1,'Kunde','Street 1','buyer@example.com','new',?,'EUR')",
        (backend.now_iso(),),
    )
    db.execute(
        "INSERT INTO order_items(id,order_id,product_id,sku,qty,unit_net_price,currency,created_at) "
        "VALUES(99,99,1,'SKU-1',1,10,'EUR',?)",
        (backend.now_iso(),),
    )
    db.commit()
    db.close()


def test_ui_write_order_lease_reenters_sqlite_write_lock(historical_db):
    lock = backend._sqlite_write_lock
    assert isinstance(lock, type(threading.RLock()))
    assert lock.acquire(timeout=0.2)
    try:
        with backend.app.test_request_context(), fulfillment_operations.ui_write(1):
            db = backend.conn()
            marker = db.execute(
                "SELECT token FROM fulfillment_locks WHERE order_id=1"
            ).fetchone()
            db.close()
            assert marker and marker[0].startswith('ui:')
    finally:
        lock.release()
    db = backend.conn()
    assert db.execute(
        "SELECT 1 FROM fulfillment_locks WHERE order_id=1"
    ).fetchone() is None
    db.close()


def test_invoice_post_finishes_without_500_and_keeps_number_guard(
    historical_db, monkeypatch,
):
    _add_invoice_order()
    client = _invoice_edit_client(monkeypatch)
    monkeypatch.setattr(invoice_routes, 'finalize_fully_invoiced_orders', lambda _ids: ([], []))

    form = _invoice_create_form('FVAT 30/09/2026')
    form.pop('invoice_qty_2')
    form['invoice_qty_99'] = '1'
    response = client.post('/orders/99/invoice', data=form)
    assert response.status_code == 302
    db = backend.conn()
    invoice = db.execute(
        "SELECT id,invoice_no FROM invoices WHERE order_id=99"
    ).fetchone()
    lease = db.execute(
        "SELECT 1 FROM fulfillment_locks WHERE order_id=99"
    ).fetchone()
    db.close()
    assert invoice and invoice['invoice_no'] == 'FVAT 30/09/2026'
    assert lease is None

    with pytest.raises(ValueError, match='już wykorzystany'):
        invoice_numbering.reserve(
            backend, '2026-09-15', 'FVAT 30/09/2026', manual=True,
        )
