"""Reader regression: one parcel's corrected contents are not two shipments."""
import hashlib
from pathlib import Path

import pytest

import app as backend
import shipment_read
import shipments_page
from test_multi_order_packing_agent import multi_order_flow


TRACKING = '520000000000000000000001'
ORIGINAL = 'inpost:original'
SENT_AT = '2026-10-19 12:14:00'


def _snapshot():
    db = backend.conn()
    try:
        return {table: [tuple(row) for row in db.execute('SELECT * FROM ' + table)]
                for table in ('orders', 'stock', 'invoices', 'packing_shipments',
                              'packing_batches', 'packing_allocations', 'fulfillment_verifications')}
    finally:
        db.close()


@pytest.fixture
def amended_parcel(multi_order_flow):
    db = backend.conn()
    for oid in (139, 141, 142):
        db.execute('''INSERT INTO orders(id,order_no,customer_id,customer_name,
            customer_email,status,created_at,currency,shipped_at,carrier,tracking_no)
            VALUES(?,?,77,'Klient','client@example.invalid','shipped',?,'PLN',?,'inpost',?)''',
            (oid, 'ZAM-' + str(oid), SENT_AT, SENT_AT, TRACKING))
        db.execute('''INSERT INTO order_items(id,order_id,product_id,sku,qty,
            unit_net_price,unit_gross_price,currency,created_at)
            VALUES(?,?,?,?,50,10,12.3,'PLN',?)''',
            (oid * 10, oid, 1, 'SKU-' + str(oid), SENT_AT))
    for invoice_id, batch_id, key, root, quantities, created in (
        (105, 500, 'original-list', 139, [(139, 4), (141, 32)], SENT_AT),
        (106, 501, 'replacement-list', 141, [(139, 4), (141, 28), (142, 18)], '2026-10-19 15:11:17'),
    ):
        db.execute('''INSERT INTO invoices(id,order_id,invoice_no,issue_date,sell_date,
            payment_type,total_net,total_gross,created_at,publication_state)
            VALUES(?,?,?,'2026-10-19','2026-10-19','transfer',0,0,?,'complete')''',
            (invoice_id, root, 'FV-' + str(invoice_id), created))
        db.execute('INSERT INTO packing_lists VALUES(?,?,?,?,1,?)',
                   (key, root, invoice_id, batch_id, created))
        db.execute('''INSERT INTO packing_batches(id,root_order_id,invoice_id,
            created_at,packing_list_id,selection_hash) VALUES(?,?,?,?,?,?)''',
            (batch_id, root, invoice_id, created, key, 'selection-' + key))
        pdf = Path(multi_order_flow['tmp_path']) / (key + '.pdf')
        pdf.write_bytes(b'%PDF-1.4\n' + key.encode())
        file_hash = hashlib.sha256(pdf.read_bytes()).hexdigest()
        for oid, qty in quantities:
            db.execute('''INSERT INTO packing_allocations(batch_id,order_id,order_item_id,qty,
                created_at,sku_snapshot,model_name_snapshot,order_number_snapshot,customer_name_snapshot)
                VALUES(?,?,?,?,?,?,?,?,?)''',
                (batch_id, oid, oid * 10, qty, created, 'SKU-' + str(oid), 'Model',
                 'ZAM-' + str(oid), 'Klient'))
            db.execute('''INSERT INTO invoice_allocations(invoice_id,order_id,order_item_id,
                product_id,sku,qty,created_at) VALUES(?,?,?,?,?,?,?)''',
                (invoice_id, oid, oid * 10, 1, 'SKU-' + str(oid), qty, created))
            db.execute('''INSERT INTO fulfillment_document_history(order_id,kind,document_id,
                content_hash,path,created_at,file_hash) VALUES(?,'packing_list',?,?,?,?,?)''',
                (oid, batch_id, 'selection-' + key, str(pdf), created, file_hash))
    db.execute('INSERT INTO packing_shipments VALUES(?,?,?,?,?,?)',
               (ORIGINAL, 'original-list', 500, SENT_AT, 'inpost', TRACKING))
    db.commit()
    db.close()
    def amend():
        import shipment_amendments
        db = backend.conn()
        db.execute('BEGIN IMMEDIATE')
        original = db.execute('SELECT * FROM packing_shipments WHERE shipment_key=?', (ORIGINAL,)).fetchone()
        result = shipment_amendments.amend(db, original,
            dict(batch_id=501, packing_list_key='replacement-list'),
            actor='test-owner', reason='Owner confirmed 50 units in the same collected parcel')
        db.commit()
        db.close()
        return result
    return amend


@pytest.mark.parametrize('selector', [
    {'mode': 'tracking', 'tracking': TRACKING},
    {'mode': 'date', 'date': '2026-10-19', 'customer_id': 77},
    {'mode': 'latest', 'order_number': 'ZAM-142'},
])
def test_read_uses_only_effective_contents_and_invoice(amended_parcel, selector):
    amended_parcel()
    before = _snapshot()
    result = shipment_read.read(selector, connection_factory=backend.conn)
    assert result['complete'], result
    assert result['count'] == 1 and result['total_units'] == 50
    parcel = result['shipments'][0]
    assert parcel['invoice_ids'] == [106]
    assert parcel['final_packing_batch_ids'] == [501]
    assert parcel['packing_verified'] and sum(i['qty'] for i in parcel['final_packing_items']) == 50
    assert parcel['shipped_at'] == SENT_AT
    assert {o['order_id'] for o in parcel['orders']} == {139, 141, 142}
    assert _snapshot() == before


def test_page_filters_only_explicitly_superseded_list(amended_parcel):
    rows, _ = shipments_page.records(backend)
    # No proof: the same number by itself cannot remove either saved final list.
    rows[0]['packing_shipments'].append(dict(shipment_key='inpost:unproven',
        packing_list_id='replacement-list', final_batch_id=501, confirmed_at=SENT_AT,
        carrier='inpost', tracking=TRACKING))
    cards = shipments_page.cards_from_records(rows)
    assert len(cards) == 2 and {c['total'] for c in cards} == {36, 50}
    amended_parcel()
    rows, _ = shipments_page.records(backend)
    cards = shipments_page.cards_from_records(rows)
    assert len(cards) == 1
    card = cards[0]
    assert card['total'] == 50 and card['invoice_id'] == 106 and card['sent']
    assert card['tracking'] == TRACKING and card['key'] == 'replacement-list'
    assert len(card['orders']) == 3
    # Rendering must not delete historical list evidence.
    assert len(rows[0]['packing_shipments']) == 2


def test_shipment_page_proof_projection_never_fetches_pdf_content():
    assert 'payload->fulfillment_verifications' in shipments_page.PROJECTION
    assert 'pdf' not in shipments_page.PROJECTION
    assert 'payload->fulfillment_documents,' not in shipments_page.PROJECTION
