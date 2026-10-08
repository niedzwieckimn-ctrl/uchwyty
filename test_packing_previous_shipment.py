import json
import pytest

import app as b
import packing_versions
from test_packing_correction import ready, document, pack
from test_packing_ui_reconciliation import packing_cloud
from test_multi_order_packing_agent import multi_order_flow


def old_shipment(member=101):
    db = b.conn()
    db.execute("INSERT INTO packing_batches(id,root_order_id,invoice_id,created_at,packing_list_id) VALUES(99,101,301,'2026-09-01 09:00:00','older-parcel')")
    db.execute("INSERT INTO packing_lists VALUES('older-parcel',101,301,99,1,'2026-09-01 09:00:00')")
    db.execute("INSERT INTO packing_allocations(batch_id,order_id,order_item_id,qty,created_at) VALUES(99,?,?,2,'2026-09-01 09:00:00')", (member, 1001 if member==101 else 2001))
    db.execute("INSERT INTO packing_shipments VALUES('inpost:older','older-parcel',99,'2026-09-02 09:00:00','inpost','OLDER-TRACK')")
    db.execute("UPDATE orders SET carrier='inpost',inpost_shipment_id='older',tracking_no='OLDER-TRACK',shipped_at='2026-09-02 09:00:00' WHERE id=101")
    db.commit(); db.close()


def test_withdraw_current_list_after_prior_partial_shipment(ready):
    client, cloud, _, sent = ready
    old_shipment()
    doc = document()
    response = client.post('/orders/103/packing-withdraw/' + doc['file_hash'], data={'csrf_token':'csrf'})
    assert response.status_code == 200, response.text
    assert not b.load_open_packing_selection(103)
    db = b.conn()
    assert db.execute('SELECT COUNT(*) FROM packing_shipments').fetchone()[0] == 1
    assert db.execute("SELECT tracking_no FROM orders WHERE id=101").fetchone()[0] == 'OLDER-TRACK'
    assert [r[0] for r in db.execute('SELECT qty FROM stock ORDER BY product_id')] == [3,0,2]
    assert db.execute('SELECT SUM(qty) FROM invoice_allocations').fetchone()[0] == 2
    db.close()
    assert not sent
    assert pack(client, qty=1).status_code == 302
    assert document()['total_qty'] == 3


@pytest.mark.parametrize('change', ['tracking', 'shipment_id', 'carrier', 'member', 'late_shipment', 'warehouse', 'current_shipment'])
def test_unproven_or_current_shipping_still_blocks_withdrawal(ready, change):
    client, _, _, sent = ready
    old_shipment(member=201 if change=='member' else 101)
    doc = document()
    db = b.conn()
    if change == 'tracking':
        db.execute("UPDATE orders SET tracking_no='NEW-TRACK' WHERE id=101")
    elif change == 'shipment_id':
        db.execute("UPDATE orders SET inpost_shipment_id='newer' WHERE id=101")
    elif change == 'carrier':
        db.execute("UPDATE orders SET carrier='dpd' WHERE id=101")
    elif change == 'late_shipment':
        db.execute("UPDATE packing_batches SET created_at='2026-09-01 10:00:00' WHERE id=?", (doc['batch_id'],))
    elif change == 'warehouse':
        db.execute('UPDATE orders SET warehouse_issued=1 WHERE id=101')
    elif change == 'current_shipment':
        key = db.execute('SELECT packing_list_id FROM packing_batches WHERE id=?', (doc['batch_id'],)).fetchone()[0]
        db.execute("INSERT INTO packing_shipments VALUES('inpost:new',?,?,?,'inpost','NEW-TRACK')", (key,doc['batch_id'],b.now_iso()))
    db.commit(); db.close()
    response = client.post('/orders/103/packing-withdraw/' + doc['file_hash'], data={'csrf_token':'csrf'})
    assert response.status_code == 409, response.text
    assert b.load_open_packing_selection(103)
    assert not sent


@pytest.mark.parametrize('state,sid,expected', [('SUCCESS','older',200), ('SUCCESS','new',409), ('UNKNOWN','older',409), ('SENDING','',409)])
def test_unresolved_booking_remains_blocked(ready, state, sid, expected):
    client, _, _, sent = ready
    old_shipment()
    doc = document()
    db=b.conn()
    db.execute('INSERT INTO fulfillment_shipping_attempts VALUES(?,?,?,?,?,?,?)',
               (101,'booking-reference','{}','hash',state,json.dumps({'id':sid}),b.now_iso()))
    db.execute('INSERT INTO fulfillment_shipping_members VALUES(101,101)')
    db.commit(); db.close()
    response=client.post('/orders/103/packing-withdraw/'+doc['file_hash'],data={'csrf_token':'csrf'})
    assert response.status_code==expected
    assert not sent
