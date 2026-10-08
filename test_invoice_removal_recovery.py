import json

import app as b
import packing_versions
from test_post_invoice_amendment import amendment, snapshot


def previous_paid_shipment():
    db = b.conn()
    db.execute("UPDATE orders SET status='partially_shipped',shipped_at='2026-09-29 10:00:00'")
    db.execute('UPDATE order_items SET qty=4')
    db.execute("INSERT INTO invoices(id,order_id,invoice_no,issue_date,sell_date,payment_type,created_at) VALUES(91000,91002,'OLD/PAID','2026-09-01','2026-09-01','przelew',?)", (b.now_iso(),))
    db.execute("INSERT INTO invoice_meta(invoice_id,paid,sent_to_client,updated_at) VALUES(91000,1,1,?)", (b.now_iso(),))
    for oid, iid in [(91002, 91003), (91004, 91005)]:
        db.execute('INSERT INTO invoice_allocations(invoice_id,order_id,order_item_id,qty,created_at) VALUES(91000,?,?,2,?)', (oid,iid,b.now_iso()))
    db.commit()
    db.close()


def test_unsent_second_invoice_must_not_close_partially_shipped_orders(amendment):
    previous_paid_shipment()
    assert b.reconcile_paid_order_statuses() == []
    assert {o['status'] for o in snapshot()['orders']} == {'partially_shipped'}


def test_delete_reopens_remainder_and_preserves_previous_invoice_stock_and_shipment(amendment):
    previous_paid_shipment()
    db = b.conn()
    db.execute("UPDATE orders SET status='completed'")
    db.commit(); db.close()
    with b.app.test_request_context():
        b._delete_invoice_everywhere(91006)
    after = snapshot()
    assert [i['id'] for i in after['invoices']] == [91000]
    assert sum(a['qty'] for a in after['allocations']) == 4
    assert after['stock'][0]['qty'] == 20
    assert {o['status'] for o in after['orders']} == {'partially_shipped'}
    assert all(o['warehouse_issued'] == 0 and o['tracking_no']=='TRACK' for o in after['orders'])
    assert b.reconcile_paid_order_statuses() == []
    assert {o['status'] for o in snapshot()['orders']} == {'partially_shipped'}


def test_delete_releases_current_packing_for_reinvoice_retains_allocations(amendment):
    db = b.conn()
    db.execute("INSERT INTO packing_batches(id,root_order_id,invoice_id,created_at,packing_list_id,selection_hash) VALUES(71,91002,91006,?,'parcel','hash')", (b.now_iso(),))
    db.execute("INSERT INTO packing_lists VALUES('parcel',91002,91006,71,1,?)", (b.now_iso(),))
    for oid,iid in [(91002,91003),(91004,91005)]:
        db.execute('INSERT INTO packing_allocations(batch_id,order_id,order_item_id,qty,created_at) VALUES(71,?,?,2,?)', (oid,iid,b.now_iso()))
        db.execute("INSERT INTO fulfillment_documents VALUES(?,'packing_list',71,'content','unused.pdf',?,'file')", (oid,b.now_iso()))
    db.commit(); db.close()
    with b.app.test_request_context():
        b._delete_invoice_everywhere(91006)
    db = b.conn()
    try:
        logical = db.execute("SELECT * FROM packing_lists WHERE packing_list_id='parcel'").fetchone()
        assert logical['invoice_id'] is None and logical['current_batch_id']==71 and logical['revision']==2
        assert db.execute('SELECT invoice_id FROM packing_batches WHERE id=71').fetchone()[0] is None
        assert db.execute('SELECT SUM(qty) FROM packing_allocations WHERE batch_id=71').fetchone()[0]==4
        receipt = json.loads(db.execute("SELECT payload FROM fulfillment_verifications WHERE order_id=91004 AND kind='invoice_removed:91006'").fetchone()[0])
        assert receipt['invoice_no']=='AMEND/1'
        assert packing_versions.resolve_list(db,91004)['packing_list_id']=='parcel'
    finally:
        db.close()
    assert b.load_open_packing_selection(91004)['items']==[[91003,2],[91005,2]]
