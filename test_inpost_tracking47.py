"""Synthetic shipment checks; no real InPost, email or shared database."""
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import app as b
import inpost_tracking as tracking
from test_agent_shipping_draft47 import ORDER_IDS, parcel
from test_fulfillment_orchestrator import flow


@pytest.fixture
def tracked(parcel, monkeypatch):
    db = b.conn()
    db.execute("UPDATE orders SET inpost_shipment_id='123456',tracking_no='TRACK-TEST',carrier='inpost' "
               "WHERE id IN (702,704,706,708)")
    db.commit()
    db.close()
    calls = []
    mail = []
    def carrier(sid):
        calls.append(sid)
        return {'id': sid, 'tracking_number': 'TRACK-TEST',
                'status': 'collected_from_sender',
                'tracking_details': [{'status': 'collected_from_sender',
                                      'datetime': '2026-09-28T08:00:00+00:00'}]}
    monkeypatch.setattr(b, 'inpost_get_shipment', carrier)
    monkeypatch.setattr(b, '_send_orders_shipped_email',
                        lambda *args: (mail.append(args) or {'ok': True}))
    monkeypatch.setattr(b, 'supabase_enabled', lambda: False)
    return {'carrier': calls, 'mail': mail, **parcel}


def _order(order_id):
    db = b.conn()
    try:
        return dict(db.execute('SELECT * FROM orders WHERE id=?', (order_id,)).fetchone())
    finally:
        db.close()


def hydrated_cloud_fixture():
    """The fake remote contains the parcel evidence already present locally.

    These tests exercise write reconciliation, not recovery of a missing cache.
    Mark every member's acknowledged revision, as a real hydration does.
    """
    db = b.conn()
    try:
        db.executemany('INSERT OR REPLACE INTO fulfillment_reconciliation_versions VALUES(?,1)',
                       [(oid,) for oid in ORDER_IDS])
        db.commit()
    finally:
        db.close()


def test_four_orders_any_member_one_mail_and_durable_status(tracked):
    db = b.conn()
    before_stock = [tuple(row) for row in db.execute('SELECT product_id,qty FROM stock ORDER BY product_id')]
    before_invoices = db.execute('SELECT COUNT(*) FROM invoices').fetchone()[0]
    db.close()
    first = tracking.process(b, _order(708), source='manual')
    assert first['orders_applied'] and first['ok'], first
    assert first['orders'] == ORDER_IDS
    assert len(tracked['mail']) == 1
    db = b.conn()
    try:
        assert [r[0] for r in db.execute('SELECT status FROM orders WHERE id IN (702,704,706,708)')] == ['partially_shipped'] * 4
        state = tracking.read(db, '123456')
        assert state['status_code'] == 'collected_from_sender'
        assert state['status_event_at'].startswith('2026-09-28T08:00:00')
        assert state['verified_at']
        assert tracking.notification(db, '123456')['state'] == 'accepted'
        assert [tuple(row) for row in db.execute('SELECT product_id,qty FROM stock ORDER BY product_id')] == before_stock
        assert db.execute('SELECT COUNT(*) FROM invoices').fetchone()[0] == before_invoices
    finally:
        db.close()
    assert tracking.process(b, _order(702), source='webhook')['ok']
    assert len(tracked['mail']) == 1
    assert not tracked['calls'] and not tracked['pickup']
    b.init_db()  # Re-enter startup schema on the same persistent synthetic DB.
    db = b.conn()
    try:
        assert tracking.read(db, '123456')['status_code'] == 'collected_from_sender'
    finally:
        db.close()


def test_api_failure_preserves_verified_status(tracked, monkeypatch):
    assert tracking.process(b, _order(702), source='webhook')['ok']
    monkeypatch.setattr(b, 'inpost_get_shipment', lambda sid: (_ for _ in ()).throw(TimeoutError('synthetic timeout')))
    result = tracking.process(b, _order(704), source='webhook')
    assert not result['ok'] and result['stage'] == 'api'
    db = b.conn()
    try:
        state = tracking.read(db, '123456')
        assert state['status_code'] == 'collected_from_sender' and state['verified_at']
        assert state['last_result'] == 'api_error' and 'timeout' in state['last_error']
    finally:
        db.close()


def test_scope_conflict_records_status_without_partial_order_write(tracked):
    db = b.conn()
    db.execute("UPDATE orders SET inpost_shipment_id='' WHERE id=704")
    db.execute("UPDATE inpost_tracking_state SET last_attempt_at='' WHERE shipment_id='123456'")
    db.commit()
    db.close()
    result = tracking.process(b, _order(702), source='webhook')
    assert not result['ok'] and not result['orders_applied']
    assert '704' in result['error']
    assert _order(702)['status'] == 'packed_partial'
    db = b.conn()
    try:
        assert tracking.read(db, '123456')['status_code'] == 'collected_from_sender'
        assert tracking.notification(db, '123456') is None
    finally:
        db.close()
    assert not tracked['mail']


def test_mail_failure_does_not_undo_courier_or_resend(tracked, monkeypatch):
    monkeypatch.setattr(b, '_send_orders_shipped_email', lambda *args: {'ok': False, 'error': 'synthetic mail failure'})
    result = tracking.process(b, _order(702), source='webhook')
    assert not result['ok'] and result['orders_applied']
    assert _order(702)['status'] == 'partially_shipped'
    db = b.conn()
    try:
        assert tracking.notification(db, '123456')['state'] == 'failed'
    finally:
        db.close()
    assert not tracking.process(b, _order(708), source='webhook')['ok']
    assert not tracked['mail']


def test_parallel_calls_one_provider_read_and_one_mail(tracked):
    def attempt(order_id):
        return tracking.process(b, _order(order_id), source='webhook')
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(attempt, ORDER_IDS))
    assert len(tracked['carrier']) == 1
    assert len(tracked['mail']) == 1
    assert any(result.get('orders_applied') for result in results)
    assert all(_order(order_id)['status'] in {'partially_shipped', 'shipped'} for order_id in ORDER_IDS)


def test_poll_deduplicates_and_terminal_stops(tracked, monkeypatch):
    monkeypatch.setattr(tracking.time, 'sleep', lambda _: None)
    assert len(tracking.due_shipments(b)) == 1
    assert len(tracking.process_due(b)) == 1
    assert len(tracked['carrier']) == 1
    assert tracking.due_shipments(b) == []
    db = b.conn()
    db.execute("UPDATE inpost_tracking_state SET terminal=1,next_check_at=0 WHERE shipment_id='123456'")
    db.commit()
    db.close()
    assert tracking.due_shipments(b) == []


def test_older_webhook_cannot_roll_back(tracked, monkeypatch):
    assert tracking.process(b, _order(702), source='webhook')['ok']
    monkeypatch.setattr(b, 'inpost_get_shipment', lambda sid: {'id': sid, 'tracking_number': 'TRACK-TEST',
        'status': 'confirmed', 'tracking_details': [{'status': 'confirmed', 'datetime': '2026-09-27T08:00:00+00:00'}]})
    result = tracking.process(b, _order(704), source='webhook')
    assert result['result'] == 'older_ignored'
    db = b.conn()
    try:
        assert tracking.read(db, '123456')['status_code'] == 'collected_from_sender'
    finally:
        db.close()


def test_order_page_keeps_status_and_shows_apply_error(tracked):
    client = b.app.test_client()
    with client.session_transaction() as session:
        session['admin_authenticated'] = True
    ok = client.get('/orders/702?refresh_shipment=1')
    assert ok.status_code == 200, ok.data[:500]
    assert 'Odebrana od nadawcy'.encode() in ok.data
    again = client.get('/orders/702')
    assert again.status_code == 200
    assert 'Odebrana od nadawcy'.encode() in again.data
    db = b.conn()
    db.execute("UPDATE orders SET inpost_shipment_id='',tracking_no='CONFLICTING-TRACK' WHERE id=704")
    db.execute("UPDATE inpost_tracking_state SET last_attempt_at='' WHERE shipment_id='123456'")
    db.commit()
    db.close()
    conflict = client.get('/orders/702?refresh_shipment=1')
    assert conflict.status_code == 200
    assert 'Odebrana od nadawcy'.encode() in conflict.data
    assert 'Zakres przesyłki nie zgadza'.encode() in conflict.data


def test_webhook_uses_current_authoritative_shipment_not_payload_status(tracked):
    client = b.app.test_client()
    assert client.get('/webhooks/inpost').status_code == 405
    unknown = client.post('/webhooks/inpost', json={'shipment_id': 'unknown'})
    assert unknown.status_code == 200 and unknown.json['ignored'] == 'shipment_not_found'
    assert not tracked['carrier']
    old = client.post('/webhooks/inpost', json={'shipment_id': 'old-123456',
                                                'tracking_number': 'TRACK-TEST', 'status': 'confirmed'})
    assert old.status_code == 200
    assert tracked['carrier'] == ['123456']
    assert all(_order(i)['status'] == 'partially_shipped' for i in ORDER_IDS)
    current = client.post('/webhooks/inpost', json={'shipment_id': '123456',
                                                    'status': 'confirmed'})
    assert current.status_code == 200, current.json
    assert tracked['carrier'] == ['123456', '123456']
    assert all(_order(i)['status'] == 'partially_shipped' for i in ORDER_IDS)


def test_supabase_failure_does_not_roll_back_local_status(tracked, monkeypatch):
    import packing_versions
    hydrated_cloud_fixture()
    monkeypatch.setattr(b, 'supabase_enabled', lambda: True)
    monkeypatch.setattr(packing_versions, 'sync_evidence', lambda *args: None)
    def failed(*args):
        raise ConnectionError('synthetic cloud failure')
    monkeypatch.setattr(b, 'supabase_update_rows', failed)
    result = tracking.process(b, _order(702), source='webhook')
    assert result['orders_applied'] and result['sync_state'] == 'pending'
    assert all(_order(i)['status'] == 'partially_shipped' for i in ORDER_IDS)
    incoming = [{**_order(702), 'status': 'packed_partial'}]
    b.sqlite_upsert_rows('orders', incoming, 'id')
    assert _order(702)['status'] == 'partially_shipped'
    pushed = []
    monkeypatch.setattr(b, 'supabase_update_rows', lambda *args: pushed.append(args))
    assert tracking.flush_pending(b) == 1
    assert len(pushed) == 4
    db = b.conn()
    try:
        assert tracking.read(db, '123456')['sync_state'] == 'synced'
    finally:
        db.close()


def test_uncertain_email_is_not_retried_without_reconciliation(tracked, monkeypatch):
    monkeypatch.setattr(b, '_send_orders_shipped_email',
                        lambda *args: (_ for _ in ()).throw(TimeoutError('unknown provider result')))
    result = tracking.process(b, _order(702), source='webhook')
    assert result['orders_applied'] and result['notification_state'] == 'unknown'
    db = b.conn()
    try:
        assert tracking.notification(db, '123456')['state'] == 'unknown'
    finally:
        db.close()
    monkeypatch.setattr(b, '_send_orders_shipped_email',
                        lambda *args: (_ for _ in ()).throw(AssertionError('duplicate email')))
    tracking.process(b, _order(704), source='webhook')


def test_conflicting_carrier_tracking_does_not_change_orders(tracked, monkeypatch):
    monkeypatch.setattr(b, 'inpost_get_shipment', lambda sid: {
        'id': sid, 'tracking_number': 'OTHER-TRACK', 'status': 'collected_from_sender'})
    result = tracking.process(b, _order(702), source='webhook')
    assert not result['ok'] and result['stage'] == 'scope'
    assert _order(702)['status'] == 'packed_partial'
    assert not tracked['mail']


def test_final_batch_repairs_only_unambiguous_missing_member(tracked):
    first = tracking.process(b, _order(702), source='webhook')
    assert first['ok'] and not first['repaired_order_ids']
    db = b.conn()
    db.execute("UPDATE orders SET inpost_shipment_id='',status='packed_partial' WHERE id=704")
    db.commit()
    db.close()
    repaired = tracking.process(b, _order(708), source='webhook')
    assert repaired['ok'] and repaired['repaired_order_ids'] == [704], repaired
    assert _order(704)['inpost_shipment_id'] == '123456'
    assert _order(704)['status'] == 'partially_shipped'
    assert len(tracked['mail']) == 1
