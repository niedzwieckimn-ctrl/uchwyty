"""Tracking consumers of an audited content amendment; no external IO."""
import json

import pytest

import app as b
import inpost_reconciliation as work
import inpost_tracking as tracking
import packing_versions
import reconciliation_store
import shipment_amendments
from test_fulfillment_orchestrator import flow


SID = '123456'
NUMBER = 'TRACK-TEST'
MEMBERS = [702, 704, 706]


def _order(oid):
    db = b.conn()
    try:
        return dict(db.execute('SELECT * FROM orders WHERE id=?', (oid,)).fetchone())
    finally:
        db.close()


@pytest.fixture
def corrected_parcel(flow, monkeypatch, request):
    def pdf(order, items, metadata, **kwargs):
        path = flow['dir'] / 'amendment-packing.pdf'
        path.write_bytes(b'%PDF-1.4\n' + json.dumps(items).encode())
        return str(path)

    monkeypatch.setattr(b, 'generate_invoice_packing_list_pdf', pdf)
    monkeypatch.setattr(b, '_send_orders_shipped_email', lambda *a, **kw: pytest.fail('No new mail allowed'))
    db = b.conn()
    for oid in MEMBERS[1:]:
        db.execute("""INSERT INTO orders(id,order_no,customer_name,customer_email,
            customer_address,customer_phone,status,currency,created_at)
            VALUES(?,?,'Magmar','test@example.invalid','Testowa 1, 00-001 Warszawa',
            '501502503','packed','PLN',?)""", (oid, 'MAG-' + str(oid), b.now_iso()))
        db.execute("""INSERT INTO order_items(id,order_id,product_id,sku,qty,
            unit_net_price,currency,created_at) VALUES(?,?,701,'ANDRE-128-AB',5,10,'PLN',?)""",
            (oid + 100, oid, b.now_iso()))
    old_items = [{'source_order_id': 702, 'order_item_id': 703, 'qty': 2},
                 {'source_order_id': 704, 'order_item_id': 804, 'qty': 3}]
    old = packing_versions.publish(b, db, 702, old_items, '')
    original_key = 'inpost:' + (NUMBER if getattr(request, 'param', '') == 'manual' else SID)
    packing_versions.confirm_shipment(db, batch_id=old, shipment_key=original_key,
        confirmed_at='2026-10-19T12:14:00', carrier='inpost', tracking=NUMBER,
        order_ids=MEMBERS[:2])
    new_items = [*old_items, {'source_order_id': 706, 'order_item_id': 806, 'qty': 5}]
    new = packing_versions.publish(b, db, 702, new_items, '')
    original = dict(db.execute('SELECT * FROM packing_shipments WHERE shipment_key=?',
                              (original_key,)).fetchone())
    proof = shipment_amendments.amend(db, original, packing_versions.current_for_order(db, 702),
                                     'Test operator', 'All three orders packed in this same parcel')
    resolved = dict(db.execute('SELECT * FROM packing_shipments WHERE shipment_key=?',
                              (proof['final_key'],)).fetchone())
    db.execute("UPDATE orders SET inpost_shipment_id=?,tracking_no=?,carrier='inpost',status='packed'",
               (SID, NUMBER))
    db.execute("""INSERT INTO inpost_tracking_notifications(shipment_id,state,updated_at)
                  VALUES(?,'accepted',?)""", (SID, b.now_iso()))
    db.commit()
    stock = [tuple(row) for row in db.execute('SELECT * FROM stock')]
    db.close()
    calls = []
    resolver = shipment_amendments.effective_shipment

    def effective(connection, row):
        assert row['shipment_key'] in {original_key, resolved['shipment_key'], 'inpost:' + NUMBER}
        calls.append(row['shipment_key'])
        return resolver(connection, row)

    monkeypatch.setattr(shipment_amendments, 'effective_shipment', effective)
    return {**flow, 'old': old, 'new': new, 'resolved': resolved,
            'original': original, 'stock': stock, 'resolver_calls': calls}


def test_collected_status_uses_corrected_scope_and_original_provider_id(corrected_parcel):
    for _ in range(2):
        result = b.apply_verified_inpost_status(_order(702), {
            'id': SID, 'status': 'collected_by_courier', 'tracking_number': NUMBER})
        assert result['ok'], result
        assert result['orders'] == MEMBERS
    db = b.conn()
    assert all(row['status'] == 'shipped' and row['inpost_shipment_id'] == SID
               for row in db.execute('SELECT * FROM orders'))
    assert dict(db.execute('SELECT * FROM packing_shipments WHERE shipment_key=?',
                          ('inpost:' + SID,)).fetchone()) == corrected_parcel['original']
    assert db.execute('SELECT COUNT(*) FROM packing_shipments').fetchone()[0] == 2
    assert [tuple(row) for row in db.execute('SELECT * FROM stock')] == corrected_parcel['stock']
    receipt = work.read(db, SID)
    payload = json.loads(receipt['sync_payload'])
    assert payload['shipment_id'] == SID
    assert payload['final_batch_id'] == corrected_parcel['new']
    assert payload['member_ids'] == MEMBERS
    assert tracking.notification(db, SID)['state'] == 'accepted'
    assert work.read(db, corrected_parcel['resolved']['shipment_key']) is None
    db.close()
    assert corrected_parcel['resolver_calls'] == ['inpost:' + SID, 'inpost:' + SID]


def test_corrected_member_without_matching_tracking_remains_blocked(corrected_parcel):
    db = b.conn()
    db.execute("UPDATE orders SET inpost_shipment_id='',tracking_no='' WHERE id=706")
    db.commit()
    db.close()
    with pytest.raises(packing_versions.PackingConflict, match='Zakres przesyłki'):
        b.apply_verified_inpost_status(_order(702), {
            'id': SID, 'status': 'collected_by_courier', 'tracking_number': NUMBER})
    assert _order(702)['status'] == 'packed'
    assert _order(706)['inpost_shipment_id'] == ''


def test_corrected_member_with_exact_tracking_can_repair_only_missing_sid(corrected_parcel):
    db = b.conn()
    db.execute("UPDATE orders SET inpost_shipment_id='' WHERE id=706")
    db.commit()
    db.close()
    result = b.apply_verified_inpost_status(_order(702), {
        'id': SID, 'status': 'collected_by_courier', 'tracking_number': NUMBER})
    assert result['ok'], result
    assert result['repaired_order_ids'] == [706]
    assert _order(706)['inpost_shipment_id'] == SID


def test_diagnostic_reads_effective_list_without_mutating_history(corrected_parcel):
    db = b.conn()
    before = db.total_changes
    result = tracking.scope_diagnostic(db, _order(702))
    assert result['packing_batch_id'] == corrected_parcel['new']
    assert result['packing_order_ids'] == MEMBERS
    assert result['order_ids'] == MEMBERS
    assert result['missing_shipment_id'] == result['outside_packing_list'] == []
    assert result['final_tracking'] == NUMBER
    assert db.total_changes == before
    db.close()


def test_restoration_includes_added_member_revision(corrected_parcel, monkeypatch):
    db = b.conn()
    db.execute("INSERT INTO fulfillment_reconciliation_versions(order_id,revision) VALUES(702,1)")
    db.execute("INSERT INTO fulfillment_reconciliation_versions(order_id,revision) VALUES(704,1)")
    db.commit()
    db.close()
    restored = []

    def restore(backend, oid):
        restored.append(oid)
        connection = backend.conn()
        connection.execute("""INSERT OR IGNORE INTO fulfillment_reconciliation_versions
            (order_id,revision) VALUES(?,1)""", (oid,))
        connection.commit()
        connection.close()

    monkeypatch.setattr(b, 'supabase_enabled', lambda: True)
    monkeypatch.setattr(reconciliation_store, 'restore', restore)
    monkeypatch.setattr(b, 'supabase_request', lambda *a, **kw: pytest.fail('No history request expected'))
    tracking.restore_packing_scope(b, _order(702))
    assert restored == MEMBERS
    assert corrected_parcel['resolver_calls']


@pytest.mark.parametrize('corrected_parcel', ['manual'], indirect=True)
def test_manual_final_key_resolves_amendment_but_never_becomes_provider_id(corrected_parcel):
    result = b.apply_verified_inpost_status(_order(702), {
        'id': SID, 'status': 'collected_by_courier', 'tracking_number': NUMBER})
    assert result['ok'] and result['orders'] == MEMBERS, result
    db = b.conn()
    diagnostic = tracking.scope_diagnostic(db, _order(702))
    assert diagnostic['packing_batch_id'] == corrected_parcel['new']
    assert diagnostic['packing_order_ids'] == MEMBERS
    assert diagnostic['missing_shipment_id'] == diagnostic['outside_packing_list'] == []
    assert db.execute('SELECT COUNT(*) FROM packing_shipments').fetchone()[0] == 2
    assert work.read(db, SID) is not None
    assert work.read(db, NUMBER) is None
    assert all(row['inpost_shipment_id'] == SID for row in db.execute('SELECT * FROM orders'))
    db.close()


def test_unrelated_manual_final_for_same_tracking_does_not_override_sid_evidence(corrected_parcel):
    db = b.conn()
    old = corrected_parcel['original']
    db.execute('INSERT INTO packing_shipments VALUES(?,?,?,?,?,?)',
               ('inpost:' + NUMBER, old['packing_list_id'], old['final_batch_id'],
                old['confirmed_at'], old['carrier'], old['tracking']))
    db.commit()
    db.close()
    with pytest.raises(packing_versions.PackingConflict, match='różne listy'):
        b.apply_verified_inpost_status(_order(702), {
            'id': SID, 'status': 'collected_by_courier', 'tracking_number': NUMBER})
    assert all(_order(oid)['status'] == 'packed' for oid in MEMBERS)


def test_historical_corrected_members_do_not_overwrite_later_parcels(corrected_parcel):
    original = _order(702)
    db = b.conn()
    for oid in MEMBERS:
        old = _order(oid)
        db.execute('INSERT INTO inpost_shipment_history VALUES(?,?,?,?)',
                   (oid, SID, json.dumps(old), b.now_iso()))
        db.execute('UPDATE orders SET inpost_shipment_id=?,tracking_no=? WHERE id=?',
                   ('NEXT-' + str(oid), 'TRACK-NEXT-' + str(oid), oid))
    db.commit()
    db.close()
    result = b.apply_verified_inpost_status(original, {
        'id': SID, 'status': 'delivered', 'tracking_number': NUMBER})
    assert result['ok'], result
    assert result['orders'] == [] and result['historical_order_ids'] == MEMBERS
    assert all(_order(oid)['status'] == 'packed' for oid in MEMBERS)
    assert all(_order(oid)['inpost_shipment_id'] == 'NEXT-' + str(oid) for oid in MEMBERS)


@pytest.mark.parametrize('corrected_parcel', ['manual'], indirect=True)
def test_full_tracking_process_uses_authentic_sid_for_corrected_manual_parcel(corrected_parcel, monkeypatch):
    calls = []

    def shipment(sid):
        calls.append(sid)
        return {'id': sid, 'tracking_number': NUMBER, 'status': 'collected_by_courier'}

    monkeypatch.setattr(b, 'inpost_get_shipment', shipment)
    monkeypatch.setattr(b, 'inpost_get_tracking', lambda number: {
        'tracking_number': number, 'status': 'collected_by_courier', 'tracking_details': []})
    result = tracking.process(b, _order(706), source='webhook')
    assert result['ok'] and result['orders'] == MEMBERS, result
    assert calls == [SID]
    assert corrected_parcel['calls'] == corrected_parcel['pickup'] == []


def _pending_notification(db, corrected_parcel, notice_state):
    old_payload = work.encoded({'batch_id': corrected_parcel['old'], 'tracking_no': NUMBER,
                                'orders': [_order(oid) for oid in MEMBERS[:2]]})
    db.execute('INSERT INTO inpost_reconciliation(shipment_id,root_order_id,notification_payload) VALUES(?,?,?)',
               (SID, 702, old_payload))
    if notice_state is None:
        db.execute('DELETE FROM inpost_tracking_notifications WHERE shipment_id=?', (SID,))
    else:
        db.execute('UPDATE inpost_tracking_notifications SET state=? WHERE shipment_id=?', (notice_state, SID))
    return old_payload


@pytest.mark.parametrize('notice_state', [None, 'pending', 'accepted', 'unknown', 'sending', 'failed', 'skipped'])
def test_correction_refreshes_only_unattempted_notification_contents(corrected_parcel, notice_state):
    db = b.conn()
    before_payload = _pending_notification(db, corrected_parcel, notice_state)
    before_notice = tracking.notification(db, SID)
    changed = tracking.reconcile_amendment_notification(db, SID, corrected_parcel['new'],
                                                       [_order(oid) for oid in MEMBERS])
    assert changed is (notice_state in (None, 'pending'))
    assert tracking.notification(db, SID) == before_notice
    result = work.read(db, SID)['notification_payload']
    if changed:
        payload = json.loads(result)
        assert payload['batch_id'] == corrected_parcel['new']
        assert [row['id'] for row in payload['orders']] == MEMBERS
        assert payload['tracking_no'] == NUMBER
        assert not tracking.reconcile_amendment_notification(db, SID, corrected_parcel['new'],
                                                            [_order(oid) for oid in MEMBERS])
    else:
        assert result == before_payload
    db.rollback()
    db.close()


def test_notification_refresh_rolls_back_with_caller_and_keeps_legacy_send_evidence(corrected_parcel):
    db = b.conn()
    before = _pending_notification(db, corrected_parcel, 'pending')
    db.commit()
    db.execute('BEGIN IMMEDIATE')
    assert tracking.reconcile_amendment_notification(db, SID, corrected_parcel['new'],
                                                    [_order(oid) for oid in MEMBERS])
    db.rollback()
    assert work.read(db, SID)['notification_payload'] == before
    db.close()
    import hashlib
    digest = hashlib.sha256(NUMBER.encode()).hexdigest()[:16]
    b._record_email_event('order_shipped:702:inpost:' + digest, 'order_shipped',
                          702, 'test@example.invalid', {'ok': True, 'delivery_outcome': 'accepted'})
    db = b.conn()
    db.execute('BEGIN IMMEDIATE')
    assert not tracking.reconcile_amendment_notification(db, SID, corrected_parcel['new'],
                                                        [_order(oid) for oid in MEMBERS])
    assert work.read(db, SID)['notification_payload'] == before
    db.rollback()
    db.close()


def test_pending_notification_claim_is_never_reset_by_correction(corrected_parcel):
    db = b.conn()
    before = _pending_notification(db, corrected_parcel, 'pending')
    db.execute("UPDATE inpost_tracking_notifications SET claim_token='existing-reservation',lease_until=1 WHERE shipment_id=?", (SID,))
    notice = tracking.notification(db, SID)
    assert not tracking.reconcile_amendment_notification(db, SID, corrected_parcel['new'],
                                                        [_order(oid) for oid in MEMBERS])
    assert work.read(db, SID)['notification_payload'] == before
    assert tracking.notification(db, SID) == notice
    db.rollback()
    db.close()


def test_guarded_cloud_cas_constrains_each_nullable_pointer_independently(monkeypatch):
    requests = []
    values = {'inpost_shipment_id': SID, 'tracking_no': NUMBER, 'carrier': 'inpost'}

    def request(path, **kwargs):
        requests.append((path, kwargs))
        return [{'id': 706, **values}]

    monkeypatch.setattr(b, 'supabase_request', request)
    b.supabase_update_rows('orders', values, {
        'id': 706, 'inpost_shipment_id': [None, SID], 'tracking_no': [None, '', NUMBER],
        'carrier': [None, 'inpost'], 'status': ['packed', 'shipped'], 'warehouse_issued': 0})
    path, call = requests[0]
    assert path == '/rest/v1/orders' and call['method'] == 'PATCH'
    assert call['params'] == {
        'id': 'eq.706', 'status': 'in.("packed","shipped")', 'warehouse_issued': 'eq.0',
        'and': '(or(inpost_shipment_id.is.null,inpost_shipment_id.in.("123456")),'
               'or(tracking_no.is.null,tracking_no.in.("","TRACK-TEST")),'
               'or(carrier.is.null,carrier.in.("inpost")))'}
    assert call['prefer'] == 'return=representation' and call['timeout'] == 20


def test_guarded_cloud_cas_scalar_null_and_null_only_list_use_is_null(monkeypatch):
    calls = []
    monkeypatch.setattr(b, 'supabase_request', lambda path, **kwargs:
                        calls.append(kwargs) or [{'id': 706, 'carrier': 'inpost'}])
    b.supabase_update_rows('orders', {'carrier': 'inpost'}, {
        'id': 706, 'inpost_shipment_id': [None], 'tracking_no': None, 'carrier': [None]})
    assert calls[0]['params'] == {'id': 'eq.706', 'inpost_shipment_id': 'is.null',
                                 'tracking_no': 'is.null', 'carrier': 'is.null'}


@pytest.mark.parametrize('response', [[], [{'id': 706, 'carrier': 'dpd'}],
                                     [{'id': 707, 'carrier': 'inpost'}]])
def test_nullable_guarded_cas_still_requires_exact_updated_row(monkeypatch, response):
    monkeypatch.setattr(b, 'supabase_request', lambda *args, **kwargs: response)
    with pytest.raises(work.SyncConflict):
        b.supabase_update_rows('orders', {'carrier': 'inpost'},
                              {'id': 706, 'inpost_shipment_id': [None, SID]})


def test_generic_cloud_update_contract_is_unchanged(monkeypatch):
    calls = []
    monkeypatch.setattr(b, 'supabase_request', lambda path, **kwargs: calls.append(kwargs))
    b.supabase_update_rows('orders', {'carrier': 'inpost'}, {'id': 706, 'tracking_no': None})
    assert calls[0]['params'] == {'id': 'eq.706', 'tracking_no': 'eq.None'}
    assert calls[0]['prefer'] == 'return=minimal' and calls[0]['timeout'] == 60
