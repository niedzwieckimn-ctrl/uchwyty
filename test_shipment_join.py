import re
import pytest
import app as b
import packing_versions
import shipments_page
from test_multi_order_packing_agent import multi_order_flow
from test_shipment_amendment_views import amended_parcel, TRACKING
from test_shipment_rebind import rebind_client, version, financial_state


@pytest.fixture
def join_client(rebind_client):
    db = b.conn()
    db.execute('''INSERT INTO invoices(id,order_id,invoice_no,issue_date,sell_date,payment_type,
        total_net,total_gross,created_at,publication_state)
        VALUES(105,139,'FV-105','2026-10-19','2026-10-19','transfer',0,0,'2026-10-19','complete')''')
    db.execute('UPDATE packing_batches SET invoice_id=105 WHERE id=500')
    db.execute('''INSERT INTO invoice_allocations(invoice_id,order_id,order_item_id,product_id,sku,qty,created_at)
        SELECT 105,order_id,order_item_id,1,sku_snapshot,qty,created_at FROM packing_allocations WHERE batch_id=500''')
    db.execute("INSERT INTO packing_lists VALUES('additional-list',142,106,502,1,'2026-10-19 16:00:00')")
    db.execute("INSERT INTO packing_batches(id,root_order_id,invoice_id,created_at,packing_list_id,selection_hash) VALUES(502,142,106,'2026-10-19 16:00:00','additional-list','additional')")
    db.execute('''INSERT INTO packing_allocations(batch_id,order_id,order_item_id,qty,created_at,sku_snapshot,model_name_snapshot,order_number_snapshot,customer_name_snapshot)
        SELECT 502,order_id,order_item_id,qty,created_at,sku_snapshot,model_name_snapshot,order_number_snapshot,customer_name_snapshot
        FROM packing_allocations WHERE batch_id=501 AND order_id=142''')
    db.execute('DELETE FROM invoice_allocations WHERE invoice_id=106 AND order_id<>142')
    db.execute("UPDATE fulfillment_documents SET document_id=502,content_hash='additional' WHERE order_id=142 AND kind='packing_list'")
    db.execute("DELETE FROM fulfillment_documents WHERE order_id IN (139,141) AND kind='packing_list'")
    db.execute('''INSERT INTO fulfillment_document_history(order_id,kind,document_id,content_hash,path,created_at,file_hash)
        SELECT order_id,kind,502,'additional',path,created_at,file_hash FROM fulfillment_document_history
        WHERE document_id=501 AND order_id=142''')
    db.commit(); db.close()
    return rebind_client


def submit(client, token, confirmation=''):
    return client.post('/orders/142/shipped', data=dict(csrf_token='csrf', carrier='inpost',
        tracking_no=TRACKING, shipment_form_version=token, join_existing=confirmation))


def test_join_preserves_both_invoices_lists_stock_and_retry(join_client):
    before = financial_state()
    token = version()
    response = submit(join_client, token)
    assert response.status_code == 409, response.text
    assert 'FV-105' in response.text and 'FV-106' in response.text
    confirmation = re.search(r'name="join_existing" value="([a-f0-9]+)"', response.text)[1]
    assert financial_state() == before
    for _ in range(2):
        response = submit(join_client, token, confirmation)
        assert response.status_code == 302, response.text
    db = b.conn()
    assert db.execute('SELECT COUNT(*) FROM packing_shipments').fetchone()[0] == 2
    assert db.execute("SELECT COUNT(*) FROM fulfillment_verifications WHERE kind LIKE 'shipment_amendment:%'").fetchone()[0] == 0
    db.close()
    assert financial_state() == before
    rows, _ = shipments_page.records(b)
    cards = shipments_page.cards_from_records(rows)
    shipments_page.invoice_labels(b, cards)
    grouped = shipments_page.group_joined_cards(cards, rows)
    assert len(grouped) == 1 and grouped[0]['total'] == 54
    assert {c['invoice_id'] for c in grouped[0]['documents']} == {105, 106}


def test_cold_final_is_restored_before_offering_to_join(join_client, monkeypatch):
    import json
    import reconciliation_store
    monkeypatch.setattr(b, 'supabase_enabled', lambda: True)
    reconciliation_store.stage(b, 139)
    db = b.conn()
    payload = json.loads(db.execute('SELECT payload FROM fulfillment_reconciliation_pending WHERE order_id=139').fetchone()[0])
    db.execute('DELETE FROM fulfillment_reconciliation_pending WHERE order_id=139')
    # Simulate a newly initialized local shipment table in the disposable test DB.
    db.execute('DROP TABLE packing_shipments')
    packing_versions.initialize(db)
    db.commit()
    db.close()
    seen = []

    def remote(path, params, **kwargs):
        if path.endswith('/architect_code_redemptions'):
            return []
        assert path.endswith('/fulfillment_reconciliation')
        if 'payload->packing_shipments' in params:
            assert json.loads(params['payload->packing_shipments'][3:]) == [{'carrier':'inpost','tracking':TRACKING}]
            seen.append('lookup')
            return [{'order_id':139,'packing_shipments':payload['packing_shipments']}]
        if params.get('select') == 'order_id,revision':
            assert '139' in params['order_id']
            return [{'order_id':139,'revision':1}]
        assert params['order_id'] == 'eq.139'
        seen.append('restore')
        return [{'revision':1,'payload':payload}]

    monkeypatch.setattr(b, 'supabase_request', remote)
    before = financial_state()
    response = submit(join_client, version())
    assert response.status_code == 409 and 'Tak, dołącz do tej paczki' in response.text, response.text
    assert seen == ['lookup','restore'] and financial_state() == before
    db = b.conn()
    assert db.execute('SELECT COUNT(*) FROM packing_shipments').fetchone()[0] == 1
    db.close()


def test_missing_durable_history_never_registers_an_independent_parcel(join_client, monkeypatch):
    import shipment_join
    before = financial_state()
    monkeypatch.setattr(shipment_join, 'durable_matches', lambda *args: {
        139: {('missing-key', 'missing-list', '2026-10-29', 'inpost', TRACKING)}})
    response = submit(join_client, version())
    assert response.status_code == 409 and 'Brakuje wcześniejszej listy' in response.text
    db = b.conn()
    assert db.execute('SELECT COUNT(*) FROM packing_shipments').fetchone()[0] == 1
    db.close()
    assert financial_state() == before


@pytest.mark.parametrize('change', ['customer', 'quantity', 'stale_confirmation'])
def test_join_rejects_changed_evidence(join_client, change):
    token = version()
    response = submit(join_client, token)
    confirmation = re.search(r'name="join_existing" value="([a-f0-9]+)"', response.text)[1]
    db = b.conn()
    if change == 'customer':
        db.execute('UPDATE orders SET customer_id=99 WHERE id=142')
    elif change == 'quantity':
        db.execute('UPDATE invoice_allocations SET qty=19 WHERE invoice_id=106')
    else:
        confirmation = 'stale'
    db.commit(); db.close()
    before = financial_state()
    assert submit(join_client, token, confirmation).status_code == 409
    db = b.conn()
    assert db.execute('SELECT COUNT(*) FROM packing_shipments').fetchone()[0] == 1
    db.close()
    assert financial_state() == before
