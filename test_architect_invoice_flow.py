"""Exercise real invoice routes, PDFs, allocations and stock with local test data.

Only the external program snapshot is substituted. No live account, email,
database or payment is involved.
"""
import json
from pathlib import Path

import pytest

import app as backend
import invoice_jobs
import routes.invoices as invoice_routes


@pytest.fixture
def program_flow(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, 'DB_PATH', str(tmp_path / 'program.db'))
    monkeypatch.setattr(backend, 'DATA_DIR', str(tmp_path / 'data'))
    monkeypatch.setattr(backend, 'supabase_enabled', lambda: False)
    monkeypatch.setattr(invoice_routes, 'supabase_enabled', lambda: False)
    monkeypatch.setattr(backend.app, 'secret_key', 'local-program-invoice-test')
    backend.init_db()
    db = backend.conn()
    now = backend.now_iso()
    db.execute("INSERT INTO products(id,sku,model,name,created_at) VALUES(1,'TEST-001','TEST','Uchwyt testowy',?)", (now,))
    db.execute('INSERT INTO stock(product_id,qty) VALUES(1,20)')
    db.execute("INSERT INTO customers(id,name,address,email,language,price_list,created_at) VALUES(1,'Salon testowy','Testowa 1','invoice-test@example.com','pl','pln',?)", (now,))
    db.execute("INSERT INTO company_profile(id,company_name,address,nip,updated_at) VALUES(1,'Firma testowa','Testowa 2','1234567890',?)", (now,))
    for oid, price in ((1, 92.75), (2, 100)):
        db.execute("INSERT INTO orders(id,order_no,customer_id,customer_name,customer_address,customer_email,status,created_at,currency) VALUES(?, ?,1,'Salon testowy','Testowa 1','invoice-test@example.com','new',?,'PLN')", (oid, f'TEST-{oid}', now))
        db.execute("INSERT INTO order_items(id,order_id,product_id,sku,qty,unit_net_price,currency,created_at) VALUES(?,?,1,'TEST-001',3,?,'PLN',?)", (oid, oid, price, now))
    db.commit()
    db.close()
    snapshots = [{'order_item_id': 1, 'qty': 3, 'vat_rate': '0.23',
                  'wholesale_net_after': '278.25'}]
    monkeypatch.setattr(backend, 'architect_order_redemption',
                        lambda oid: {'id': 'test-redemption'} if oid == 1 else None)
    def read_snapshots(path, **kwargs):
        assert path == '/rest/v1/architect_sale_lines'
        assert kwargs['params']['redemption_id'] == 'eq.test-redemption'
        return snapshots
    monkeypatch.setattr(backend, 'supabase_request', read_snapshots)
    client = backend.app.test_client()
    with client.session_transaction() as session:
        session['admin_authenticated'] = True
        session['csrf_token'] = 'test'
    return client, snapshots


def form(number, **quantities):
    return dict(csrf_token='test', invoice_no=number, invoice_no_manual='1',
                issue_date='2026-10-28', sell_date='2026-10-28',
                payment_type='przelew', payment_to='2026-11-04',
                buyer_name='Salon testowy', buyer_tax_no='1234567890',
                buyer_address='Testowa 1\n00-001 Warszawa', buyer_country='PL',
                buyer_email='invoice-test@example.com', buyer_phone='',
                invoice_type='domestic', currency='PLN', place='Test',
                discount_percent='0', submit_action='invoice', **quantities)


def rows(sql, args=()):
    db = backend.conn()
    try:
        return [dict(row) for row in db.execute(sql, args)]
    finally:
        db.close()


def test_partial_final_invoice_preserves_discount_and_stock(program_flow):
    client, _ = program_flow
    first = client.post('/orders/1/invoice', data=form('TEST/1', invoice_qty_1='2'))
    assert first.status_code == 302, first.get_data(as_text=True)[-2000:]
    invoice = rows('SELECT * FROM invoices')[0]
    assert (invoice['total_net'], invoice['total_gross'], invoice['publication_state']) == (185.5, 228.17, 'complete')
    assert rows('SELECT qty FROM stock') == [{'qty': 18}]
    meta = rows('SELECT * FROM invoice_meta')[0]
    assert (Path(backend.DATA_DIR) / meta['pdf_path']).is_file()
    saved = json.loads(meta['invoice_items_json'])
    assert saved[0]['unit_net_price'] == 92.75
    assert saved[0]['qty'] == 2
    # Resuming the same durable document must never issue stock twice.
    invoice_jobs.finish(backend, invoice['id'])
    invoice_jobs.finish(backend, invoice['id'])
    assert rows('SELECT qty FROM stock') == [{'qty': 18}]
    second = client.post('/orders/1/invoice', data=form('TEST/2', invoice_qty_1='1'))
    assert second.status_code == 302, second.get_data(as_text=True)[-2000:]
    assert [(i['total_net'], i['total_gross'], i['publication_state']) for i in rows('SELECT * FROM invoices ORDER BY id')] == [(185.5, 228.17, 'complete'), (92.75, 114.08, 'complete')]
    assert rows('SELECT sum(qty) AS qty FROM invoice_allocations WHERE order_item_id=1') == [{'qty': 3}]
    assert rows('SELECT qty FROM stock') == [{'qty': 17}]
    client.post('/orders/1/invoice', data=form('TEST/3', invoice_qty_1='1'))
    assert len(rows('SELECT id FROM invoices')) == 2
    assert rows('SELECT qty FROM stock') == [{'qty': 17}]


def test_normal_order_uses_full_price(program_flow):
    client, _ = program_flow
    response = client.post('/orders/2/invoice', data=form('NORMAL/1', invoice_qty_2='1'))
    assert response.status_code == 302
    invoice = rows('SELECT * FROM invoices')[0]
    assert (invoice['total_net'], invoice['total_gross']) == (100, 123)
    assert rows('SELECT qty FROM stock') == [{'qty': 19}]


@pytest.mark.parametrize('discount', ['10', 'bad', 'NaN', 'Infinity', '0.001'])
def test_second_or_invalid_discount_cannot_create_document(program_flow, discount):
    client, _ = program_flow
    data = form('REJECT/1', invoice_qty_1='1')
    data['discount_percent'] = discount
    response = client.post('/orders/1/invoice', data=data)
    assert response.status_code == 409
    assert rows('SELECT id FROM invoices') == []
    assert rows('SELECT qty FROM stock') == [{'qty': 20}]


def test_snapshot_drift_blocks_before_invoice_number_and_stock(program_flow):
    client, snapshots = program_flow
    snapshots[0]['wholesale_net_after'] = '300.00'
    response = client.post('/orders/1/invoice', data=form('DRIFT/1', invoice_qty_1='1'))
    assert response.status_code == 409
    assert rows('SELECT id FROM invoices') == []
    assert rows('SELECT qty FROM stock') == [{'qty': 20}]


@pytest.mark.parametrize('root_order', [1, 2])
def test_combined_invoice_preserves_each_source_price(program_flow, root_order):
    client, _ = program_flow
    response = client.post(f'/orders/{root_order}/invoice', data=form(
        'COMBINED/1', invoice_qty_1='2', invoice_qty_2='1'))
    assert response.status_code == 302, response.get_data(as_text=True)[-1500:]
    invoice = rows('SELECT * FROM invoices')[0]
    assert (invoice['order_id'], invoice['total_net'], invoice['total_gross'], invoice['publication_state']) == (root_order, 285.5, 351.17, 'complete')
    assert rows('SELECT order_id, qty FROM invoice_allocations ORDER BY order_id') == [
        {'order_id': 1, 'qty': 2}, {'order_id': 2, 'qty': 1}]
    assert rows('SELECT qty FROM stock') == [{'qty': 17}]
    invoice_jobs.finish(backend, invoice['id'])
    assert rows('SELECT qty FROM stock') == [{'qty': 17}]


def test_combined_invoice_cannot_cross_distributors_sharing_email(program_flow):
    client, _ = program_flow
    db = backend.conn()
    db.execute('UPDATE orders SET customer_id=2 WHERE id=2')
    db.commit(); db.close()
    response = client.post('/orders/1/invoice', data=form(
        'WRONG-BUYER/1', invoice_qty_1='1', invoice_qty_2='1'))
    assert response.status_code == 409
    assert rows('SELECT id FROM invoices') == []
    assert rows('SELECT qty FROM stock') == [{'qty': 20}]


def test_interrupted_publication_resumes_same_invoice_once(program_flow, monkeypatch):
    client, _ = program_flow
    finish = backend.resume_invoice_job
    def unavailable(_invoice_id):
        raise TimeoutError('isolated test outage')
    monkeypatch.setattr(backend, 'resume_invoice_job', unavailable)
    response = client.post('/orders/1/invoice', data=form('RECOVER/1', invoice_qty_1='2'))
    assert response.status_code == 503
    invoice = rows('SELECT * FROM invoices')[0]
    assert invoice['publication_state'] == 'preparing'
    assert rows('SELECT qty FROM stock') == [{'qty': 20}]
    # Retrying creation must direct the user to the existing durable invoice.
    retry = client.post('/orders/1/invoice', data=form('RECOVER/2', invoice_qty_1='2'))
    assert retry.status_code == 409
    assert len(rows('SELECT id FROM invoices')) == 1
    monkeypatch.setattr(backend, 'resume_invoice_job', finish)
    for _ in range(2):
        resumed = client.post(f"/invoices/{invoice['id']}/resume", data={'csrf_token': 'test'})
        assert resumed.status_code == 302
    assert rows('SELECT publication_state FROM invoices') == [{'publication_state': 'complete'}]
    assert rows('SELECT qty FROM stock') == [{'qty': 18}]
    assert rows('SELECT qty FROM invoice_allocations') == [{'qty': 2}]


def test_unpublished_invoice_edit_preserves_price_and_adjusts_stock(program_flow):
    client, _ = program_flow
    assert client.post('/orders/1/invoice', data=form('EDIT/1', invoice_qty_1='2')).status_code == 302
    invoice_id = rows('SELECT id FROM invoices')[0]['id']
    response = client.post(f'/invoices/{invoice_id}/edit', data=form('EDIT/1', invoice_qty_1='1'))
    assert response.status_code == 302
    assert rows('SELECT total_net,total_gross FROM invoices') == [{'total_net':92.75, 'total_gross':114.08}]
    assert rows('SELECT qty FROM stock') == [{'qty':19}]
    saved = json.loads(rows('SELECT invoice_items_json FROM invoice_meta')[0]['invoice_items_json'])
    assert saved[0]['net_price'] == 92.75


def test_unsent_invoice_removal_restores_stock_and_releases_allocation(program_flow):
    client, _ = program_flow
    assert client.post('/orders/1/invoice', data=form('REMOVE/1', invoice_qty_1='2')).status_code == 302
    invoice_id = rows('SELECT id FROM invoices')[0]['id']
    with backend.app.test_request_context():
        backend._delete_invoice_everywhere(invoice_id)
    assert rows('SELECT id FROM invoices') == []
    assert rows('SELECT qty FROM invoice_allocations') == []
    assert rows('SELECT qty FROM stock') == [{'qty':20}]
    assert rows('SELECT unit_net_price FROM order_items WHERE id=1') == [{'unit_net_price':92.75}]


def test_order_view_gross_matches_invoice_rounding(program_flow, monkeypatch):
    client, _ = program_flow
    monkeypatch.setattr(backend, 'architect_order_redemption', lambda oid: None)
    db = backend.conn()
    db.execute('UPDATE order_items SET qty=2,unit_net_price=46.37,unit_gross_price=57.04 WHERE id=2')
    db.commit(); db.close()
    response=client.get('/orders/2')
    assert response.status_code == 200
    html=response.get_data(as_text=True)
    assert '114.07 PLN' in html
    assert '114.08 PLN' not in html
