"""Real manual tracking POST for a regenerated 36 -> 50 unit parcel."""
import pytest

import app as b
import invoice_removal_packing
import packing_versions
import shipment_amendments
from test_shipment_amendment_views import amended_parcel, TRACKING, ORIGINAL
from test_multi_order_packing_agent import multi_order_flow


@pytest.fixture
def rebind_client(amended_parcel, monkeypatch):
    db = b.conn()
    invoice = dict(db.execute('SELECT * FROM invoices WHERE id=105').fetchone())
    invoice_removal_packing.detach(b, db, invoice)
    db.execute('DELETE FROM invoice_allocations WHERE invoice_id=105')
    db.execute('DELETE FROM invoices WHERE id=105')
    db.execute('''INSERT INTO fulfillment_documents(order_id,kind,document_id,content_hash,path,created_at,file_hash)
        SELECT order_id,kind,document_id,content_hash,path,created_at,file_hash
        FROM fulfillment_document_history WHERE document_id=501''')
    db.execute("UPDATE orders SET inpost_shipment_id='original',inpost_label_format='pdf' WHERE id IN (139,141)")
    db.execute("UPDATE orders SET status='confirmed',tracking_no='',carrier='' WHERE id=142")
    db.commit(); db.close()
    def unexpected(*a, **kw):
        pytest.fail('Rebinding must not book, invoice, issue stock, or email')
    for name in ('_send_orders_shipped_email', 'inpost_create_shipment', 'enqueue_automatic_inpost_pickup'):
        if hasattr(b, name):
            monkeypatch.setattr(b, name, unexpected)
    client = b.app.test_client()
    with client.session_transaction() as session:
        session['admin_authenticated'] = True
        session['csrf_token'] = 'csrf'
    with b.app.test_request_context():
        b._refresh_domain_route_context()
    return client


def version():
    db = b.conn()
    try:
        return packing_versions.shipment_form_version(db, packing_versions.current_for_order(db, 142))
    finally:
        db.close()


def post(client, token=None):
    return client.post('/orders/142/shipped', data={'csrf_token': 'csrf', 'carrier': 'inpost',
        'tracking_no': TRACKING, 'shipment_form_version': token if token is not None else version()})


def financial_state():
    db = b.conn()
    try:
        return {table: list(map(tuple, db.execute('SELECT * FROM ' + table)))
                for table in ('stock', 'invoices', 'invoice_allocations', 'order_items')}
    finally:
        db.close()


def test_automatic_same_parcel_rebind_and_double_click(rebind_client):
    before = financial_state()
    token = version()
    for _ in range(2):
        response = post(rebind_client, token)
        assert response.status_code == 302, response.text
        assert 'shipment_rebound=1' in response.location
    db = b.conn()
    assert db.execute('SELECT COUNT(*) FROM packing_shipments').fetchone()[0] == 2
    original = dict(db.execute('SELECT * FROM packing_shipments WHERE shipment_key=?', (ORIGINAL,)).fetchone())
    assert original['final_batch_id'] == 500
    assert shipment_amendments.effective_shipment(db, original)['final_batch_id'] == 501
    for row in db.execute('SELECT * FROM orders WHERE id IN (139,141,142)'):
        assert row['tracking_no'] == TRACKING
        assert row['inpost_shipment_id'] == 'original'
        assert row['shipped_at'] == original['confirmed_at']
    assert db.execute('SELECT COUNT(*) FROM fulfillment_locks').fetchone()[0] == 0
    db.close()
    assert financial_state() == before


@pytest.mark.parametrize('change', ['other_invoice', 'incomplete', 'invoice_quantity', 'other_tracking',
                                  'unresolved_booking', 'missing_removal_proof', 'stale_form', 'wrong_provider_id'])
def test_unrelated_or_unproven_rebind_never_writes(rebind_client, change):
    token = version()
    db = b.conn()
    if change == 'other_invoice':
        db.execute('INSERT INTO invoices(id,order_id,invoice_no,issue_date,sell_date,payment_type,total_net,total_gross,created_at) '
                   "VALUES(105,139,'OLD','2026-10-19','2026-10-19','transfer',0,0,'2026-10-19')")
    elif change == 'incomplete':
        db.execute("UPDATE invoices SET publication_state='pending' WHERE id=106")
    elif change == 'invoice_quantity':
        db.execute('UPDATE invoice_allocations SET qty=qty+1 WHERE invoice_id=106 AND order_id=142')
    elif change == 'other_tracking':
        db.execute("UPDATE orders SET tracking_no='OTHER',carrier='inpost' WHERE id=142")
    elif change == 'unresolved_booking':
        db.execute("INSERT INTO fulfillment_shipping_attempts VALUES(142,'pending','{}','x','UNKNOWN','{}','2026-10-19')")
    elif change == 'missing_removal_proof':
        db.execute("DELETE FROM fulfillment_verifications WHERE kind LIKE 'invoice_removed:%'")
    elif change == 'stale_form':
        token = 'old-form'
    elif change == 'wrong_provider_id':
        db.execute("UPDATE orders SET inpost_shipment_id='other' WHERE id IN (139,141)")
    db.commit(); db.close()
    if change == 'other_tracking':
        token = version()  # even a fresh form cannot override another parcel
    before = financial_state()
    response = post(rebind_client, token)
    assert response.status_code == 409, response.text
    db = b.conn()
    assert db.execute('SELECT COUNT(*) FROM packing_shipments').fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM fulfillment_verifications WHERE kind LIKE 'shipment_amendment:%'").fetchone()[0] == 0
    db.close()
    assert financial_state() == before


def test_same_number_on_previous_invoice_is_not_another_partial_shipment(rebind_client):
    # A missing/deleted predecessor invoice is required, not just same customer.
    db = b.conn()
    db.execute("UPDATE packing_batches SET invoice_id=301 WHERE id=500")
    db.commit(); db.close()
    response = post(rebind_client)
    assert response.status_code == 409 and 'wcześniejszej paczki' in response.text


def test_rebind_requires_authentication_and_csrf(rebind_client):
    response = rebind_client.post('/orders/142/shipped', data={'carrier': 'inpost', 'tracking_no': TRACKING})
    assert response.status_code in (400, 403)
    with rebind_client.session_transaction() as session:
        session.pop('admin_authenticated', None)
    assert post(rebind_client).status_code == 302
    db = b.conn()
    assert db.execute('SELECT COUNT(*) FROM packing_shipments').fetchone()[0] == 1
    db.close()


def test_rebind_keeps_partial_status_and_rejects_unrelated_stale_token(rebind_client):
    db = b.conn()
    db.execute("UPDATE orders SET status='partially_shipped' WHERE id=141")
    db.commit(); db.close()
    assert post(rebind_client).status_code == 302
    db = b.conn()
    assert db.execute('SELECT status FROM orders WHERE id=141').fetchone()[0] == 'partially_shipped'
    before = list(map(tuple, db.execute('SELECT * FROM packing_shipments')))
    db.close()
    assert post(rebind_client, 'unrelated-stale-form').status_code == 409
    db = b.conn()
    assert list(map(tuple, db.execute('SELECT * FROM packing_shipments'))) == before
    db.close()
