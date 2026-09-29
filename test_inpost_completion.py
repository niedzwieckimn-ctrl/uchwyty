"""Failure injection for the completion patch. All carrier/cloud/mail IO is fake."""
import io
import json
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import pytest
import app as b
import email_module
import inpost_history
import inpost_module
import inpost_reconciliation as work
import inpost_tracking as tracking
import invoice_payment_sync as payment
import packing_versions
from test_fulfillment_orchestrator import flow
from test_agent_shipping_draft47 import parcel, ORDER_IDS
from test_inpost_tracking47 import tracked, _order

REAL_MAIL = b._send_orders_shipped_email
REAL_UPDATE = b.supabase_update_rows


def state():
    db = b.conn()
    try:
        return tracking.read(db, '123456'), work.read(db, '123456')
    finally:
        db.close()


@pytest.mark.parametrize('old,old_at,new,new_at,older', [
    ('sent_from_source_branch','2023-12-17T19:10:51+01:00','adopted_at_source_branch','2023-12-18T05:56:59+01:00',False),
    ('adopted_at_source_branch','2023-12-18T05:56:59+01:00','sent_from_source_branch','2023-12-17T19:10:51+01:00',True),
    ('adopted_at_source_branch','2023-12-18T05:56:59+01:00','adopted_at_source_branch','2023-12-17T19:10:51+01:00',True),
    ('delivered','','adopted_at_source_branch','',True),
    ('sent_from_source_branch','','adopted_at_source_branch','',True),
    ('sent_from_source_branch','2023-12-17T19:10:51+01:00','new_carrier_code','2023-12-18T05:56:59+01:00',False),
    ('sent_from_source_branch','','new_carrier_code','',True),
    ('confirmed','','new_carrier_code','',False),
    ('sent_from_source_branch','2023-12-17T19:10:51+01:00','adopted_at_source_branch','2023-12-17T19:10:51+01:00',True),
])
def test_chronology_policy(old, old_at, new, new_at, older):
    assert tracking._older({'status_code': old, 'status_event_at': old_at}, new, new_at) is older


def test_contract_shipment_and_tracking_are_separate_resources(monkeypatch):
    paths = []
    shipment = {'id': 123456, 'tracking_number': 'TRACK-TEST', 'status': 'delivered',
                'created_at': '2026-09-20T00:00:00Z', 'updated_at': '2026-09-29T09:00:00Z'}
    history = {'tracking_number': 'TRACK-TEST', 'status': 'delivered',
               'tracking_details': [{'status': 'delivered', 'datetime': '2026-09-29T08:00:00Z'}]}
    def request(path):
        paths.append(path)
        return shipment if path.startswith('/shipments/') else history
    monkeypatch.setattr(inpost_module, '_request', request)
    assert inpost_module.get_shipment('123456') == shipment
    assert inpost_module.get_tracking('TRACK-TEST') == history
    assert paths == ['/shipments/123456', '/tracking/TRACK-TEST']
    assert 'tracking_details' not in shipment


def test_history_enriches_only_authenticated_matching_identity(tracked, monkeypatch):
    calls = []
    monkeypatch.setattr(b, 'inpost_get_shipment', lambda sid: {
        'id': sid, 'status': 'delivered', 'tracking_number': 'TRACK-TEST', 'updated_at': '2099-01-01T01:00:00Z'})
    monkeypatch.setattr(b, 'inpost_get_tracking', lambda number: calls.append(number) or {
        'tracking_number': number, 'status': 'delivered',
        'tracking_details': [{'status': 'delivered', 'datetime': '2026-09-29T08:00:00Z'}]})
    assert tracking.process(b, _order(702), source='webhook')['orders_applied']
    assert calls == ['TRACK-TEST']
    assert state()[0]['status_event_at'] == '2026-09-29T08:00:00+00:00'
    monkeypatch.setattr(b, 'inpost_get_shipment', lambda sid: {'id': '999', 'tracking_number': 'TRACK-TEST', 'status': 'delivered'})
    assert not tracking.process(b, _order(702), source='webhook')['ok']
    assert calls == ['TRACK-TEST']


@pytest.mark.parametrize('history', ['unavailable', 'different_tracking', 'different_status'])
def test_missing_or_mismatched_history_never_uses_updated_at(tracked, monkeypatch, history):
    monkeypatch.setattr(b, 'inpost_get_shipment', lambda sid: {
        'id': sid, 'status': 'delivered', 'tracking_number': 'TRACK-TEST', 'updated_at': '2099-01-01T01:00:00Z'})
    def get(number):
        if history == 'unavailable':
            raise TimeoutError('history unavailable')
        return {'tracking_number': 'WRONG' if history == 'different_tracking' else number,
                'status': 'confirmed' if history == 'different_status' else 'delivered',
                'tracking_details': [{'status': 'delivered', 'datetime': '2099-01-01T01:00:00Z'}]}
    monkeypatch.setattr(b, 'inpost_get_tracking', get)
    assert tracking.process(b, _order(702), source='webhook')['orders_applied']
    assert state()[0]['status_event_at'] == ''


def test_later_transport_description_never_reverses_shipped_effect(tracked, monkeypatch):
    assert tracking.process(b, _order(702), source='webhook')['ok']
    monkeypatch.setattr(b, 'inpost_get_shipment', lambda sid: {
        'id': sid, 'tracking_number': 'TRACK-TEST', 'status': 'confirmed',
        'tracking_details': [{'status': 'confirmed', 'datetime': '2026-09-29T08:00:00Z'}]})
    tracking.process(b, _order(702), source='webhook')
    assert state()[0]['status_code'] == 'confirmed'
    assert _order(702)['status'] == 'partially_shipped'
    assert len(tracked['mail']) == 1


def test_stage_failure_rolls_back_orders_final_evidence_and_intent(tracked, monkeypatch):
    monkeypatch.setattr(work, 'stage', lambda *a: (_ for _ in ()).throw(sqlite3.OperationalError('outbox unavailable')))
    result = tracking.process(b, _order(702), source='webhook')
    assert not result['orders_applied']
    assert _order(702)['status'] == 'packed_partial'
    db = b.conn()
    try:
        assert not db.execute("SELECT 1 FROM packing_shipments WHERE shipment_key='inpost:123456'").fetchone()
        assert work.read(db, '123456')['apply_state'] == 'pending'
    finally:
        db.close()
    assert not tracked['mail']


def test_terminal_local_retry_does_not_read_carrier_again(tracked, monkeypatch):
    calls = []
    monkeypatch.setattr(b, 'inpost_get_shipment', lambda sid: calls.append(sid) or {
        'id': sid, 'tracking_number': 'TRACK-TEST', 'status': 'delivered'})
    real = b.apply_verified_inpost_status
    monkeypatch.setattr(b, 'apply_verified_inpost_status', lambda *a: (_ for _ in ()).throw(sqlite3.OperationalError('temporary')))
    assert not tracking.process(b, _order(702), source='poll')['ok']
    monkeypatch.setattr(b, 'apply_verified_inpost_status', real)
    db = b.conn()
    db.execute('UPDATE inpost_reconciliation SET retry_at=0')
    db.commit(); db.close()
    tracking.process_due(b)
    assert calls == ['123456']
    assert _order(702)['status'] == 'partially_shipped'
    assert state()[1]['apply_state'] == 'applied'


@pytest.mark.parametrize('response,expected', [('timeout','unknown'), (503,'unknown'), (400,'failed'), ('disabled','skipped'), (200,'accepted')])
def test_real_adapter_receipts_and_no_second_attempt(tracked, monkeypatch, response, expected):
    calls = []
    monkeypatch.setattr(b, '_send_orders_shipped_email', REAL_MAIL)
    monkeypatch.setattr(b, 'send_email', email_module.send_email)
    monkeypatch.setattr(email_module, 'email_config_summary', lambda: {
        'enabled': response != 'disabled', 'configured': True, 'from': 'sender@example.invalid', 'missing': []})
    class OK:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return b'{"id":"synthetic-provider-receipt"}'
    def transport(*args, **kwargs):
        calls.append(1)
        if response == 'timeout': raise TimeoutError('ambiguous')
        if response == 200: return OK()
        raise urllib.error.HTTPError('https://example.invalid', response, 'synthetic', {}, io.BytesIO(b'{"message":"fake"}'))
    monkeypatch.setattr(urllib.request, 'urlopen', transport)
    result = tracking.process(b, _order(702), source='webhook')
    assert result['notification_state'] == expected
    db = b.conn()
    rows = [json.loads(row[0]) for row in db.execute("SELECT result_json FROM email_events WHERE event_type='order_shipped'")]
    assert len(rows) == 4
    assert all(row['delivery_outcome'] == ('rejected' if expected == 'failed' else expected) for row in rows)
    assert all(row['attempted'] is (expected != 'skipped') for row in rows)
    db.close()
    tracking.process(b, _order(704), source='webhook')
    assert len(calls) == (0 if response == 'disabled' else 1)


def cloud(tracked, monkeypatch, fail_at=0):
    remote = {oid: _order(oid) for oid in ORDER_IDS}
    calls = []
    monkeypatch.setattr(b, 'supabase_enabled', lambda: True)
    monkeypatch.setattr(packing_versions, 'sync_evidence', lambda *args: None)
    def update(table, values, filters):
        calls.append(filters['id'])
        if len(calls) == fail_at:
            raise ConnectionError('partial group failure')
        oid = filters['id']
        assert remote[oid]['status'] in filters['status']
        assert remote[oid]['inpost_shipment_id'] in filters['inpost_shipment_id']
        remote[oid].update(values)
    monkeypatch.setattr(b, 'supabase_update_rows', update)
    return remote, calls


def test_partial_cloud_failure_restart_snapshot_retry_no_duplicate(tracked, monkeypatch):
    remote, calls = cloud(tracked, monkeypatch, fail_at=3)
    result = tracking.process(b, _order(702), source='webhook')
    assert result['orders_applied'] and result['sync_state'] == 'pending'
    payload = state()[1]['sync_payload']
    assert remote[704]['status'] == 'partially_shipped' and remote[706]['status'] == 'packed_partial'
    b.init_db()
    assert tracking.flush_pending(b) == 1
    assert all(remote[i]['status'] == 'partially_shipped' for i in ORDER_IDS)
    assert state()[1]['sync_payload'] == payload
    assert state()[0]['sync_state'] == 'synced'
    assert len(tracked['mail']) == 1 and not tracked['pickup'] and not tracked['calls']
    assert tracking.process(b, _order(708), source='webhook')['ok']
    assert len(calls) == 7  # Three initial attempts plus four idempotent retries.


def test_guarded_cloud_patch_requires_returned_matching_row(tracked, monkeypatch):
    seen = []
    def request(path, **kwargs):
        seen.append(kwargs)
        return []  # HTTP success, but a newer remote parcel failed CAS.
    monkeypatch.setattr(b, 'supabase_request', request)
    with pytest.raises(work.SyncConflict):
        REAL_UPDATE('orders', {'status':'partially_shipped'}, {
            'id':702, 'inpost_shipment_id':['123456'], 'status':['packed_partial','partially_shipped']})
    assert seen[0]['prefer'] == 'return=representation'
    assert seen[0]['params']['inpost_shipment_id'] == 'in.("123456")'


def test_sync_does_not_send_live_mutated_row(tracked, monkeypatch):
    remote, calls = cloud(tracked, monkeypatch, fail_at=1)
    tracking.process(b, _order(702), source='webhook')
    db=b.conn(); db.execute("UPDATE orders SET status='completed' WHERE id=702"); db.commit(); db.close()
    assert tracking.flush_pending(b) == 0
    assert calls == [702]
    assert state()[0]['sync_state'] == 'conflict'
    assert remote[702]['status'] == 'packed_partial'


def test_expired_sync_worker_cannot_publish_or_ack_new_revision(tracked, monkeypatch):
    remote, calls = cloud(tracked, monkeypatch)
    monkeypatch.setattr(work, 'flush', lambda *a: 0)
    tracking.process(b, _order(702), source='webhook')
    old = work._claim_sync(b, '123456')
    db=b.conn()
    db.execute('UPDATE inpost_reconciliation SET sync_until=0')
    db.execute("UPDATE orders SET status='completed' WHERE id=702")
    db.commit(); db.close()
    tracking.process(b, _order(702), source='webhook')
    new = work._claim_sync(b, '123456')
    assert new['sync_revision'] > old['sync_revision']
    with pytest.raises(ValueError, match='Nieaktualna'):
        work.publish(b, old)
    assert not work._finish_sync(b, old)
    assert not calls
    assert state()[1]['sync_token'] == new['sync_token']
    assert state()[0]['sync_state'] == 'pending'


def test_expired_api_worker_cannot_replace_newer_observation_or_apply(tracked, monkeypatch):
    started, release = threading.Event(), threading.Event()
    calls=[]
    def carrier(sid):
        calls.append(sid)
        first=len(calls)==1
        if first:
            started.set()
            assert release.wait(10)
        code='collected_from_sender' if first else 'delivered'
        return {'id':sid,'tracking_number':'TRACK-TEST','status':code,
                'tracking_details':[{'status':code,'datetime':'2026-09-28T08:00:00Z' if first else '2026-09-29T08:00:00Z'}]}
    monkeypatch.setattr(b,'inpost_get_shipment',carrier)
    with ThreadPoolExecutor(max_workers=2) as pool:
        older=pool.submit(tracking.process,b,_order(702),source='webhook')
        assert started.wait(5)
        db=b.conn(); db.execute('UPDATE inpost_tracking_state SET lease_until=0'); db.commit(); db.close()
        newer=tracking.process(b,_order(704),source='webhook')
        release.set()
        old_result=older.result(timeout=10)
    assert newer['orders_applied']
    assert old_result['result']=='lost_lease'
    assert state()[0]['status_code']=='delivered'
    assert len(tracked['mail'])==1


def test_lease_lost_between_observation_and_business_commit_is_fenced(tracked):
    token,_=tracking._claim(b,'123456','webhook')
    shipment={'id':'123456','tracking_number':'TRACK-TEST','status':'collected_from_sender'}
    tracking._verified(b,'123456',token,shipment,702)
    claim={'sid':'123456','token':token,'revision':state()[1]['revision']}
    db=b.conn(); db.execute('UPDATE inpost_tracking_state SET lease_until=0'); db.commit(); db.close()
    with pytest.raises(ValueError,match='dzierżawa'):
        b.apply_verified_inpost_status(_order(702),{**shipment,'_local_claim':claim})
    assert _order(702)['status']=='packed_partial'
    assert not tracked['mail']


def test_all_members_move_to_new_parcels_old_job_and_diagnostics_remain_valid(tracked):
    assert tracking.process(b,_order(702),source='webhook')['ok']
    old_order=_order(702)
    inpost_history.prepare_next(b,ORDER_IDS)
    db=b.conn()
    for oid in ORDER_IDS:
        db.execute("UPDATE orders SET inpost_shipment_id=?,tracking_no=?,status='packed_partial' WHERE id=?",
                   ('NEXT-'+str(oid),'NEW-'+str(oid),oid))
    db.commit()
    diagnostic=tracking.scope_diagnostic(db,old_order)
    assert diagnostic['missing_shipment_id']==[]
    db.close()
    result=tracking.process(b,old_order,source='webhook')
    assert result['ok'] and result['orders']==[]
    assert result['historical_order_ids']==ORDER_IDS
    assert all(_order(oid)['status']=='packed_partial' for oid in ORDER_IDS)
    assert all(_order(oid)['inpost_shipment_id']=='NEXT-'+str(oid) for oid in ORDER_IDS)
    assert len(tracked['mail'])==1


def test_mismatched_history_still_blocks_real_scope_conflict(tracked):
    tracking.process(b,_order(702),source='webhook')
    inpost_history.prepare_next(b,[704])
    db=b.conn()
    saved=json.loads(db.execute('SELECT snapshot_json FROM inpost_shipment_history WHERE order_id=704').fetchone()[0])
    saved['tracking_no']='WRONG'
    db.execute('UPDATE inpost_shipment_history SET snapshot_json=? WHERE order_id=704',(json.dumps(saved),))
    db.commit(); db.close()
    assert not tracking.process(b,_order(702),source='webhook')['ok']
    assert _order(704)['inpost_shipment_id']==''


def test_notification_receipt_failure_ui_truth_and_no_resend(tracked,monkeypatch):
    monkeypatch.setattr(tracking,'notification_finish',lambda *a: (_ for _ in ()).throw(sqlite3.OperationalError('receipt write')))
    result=tracking.process(b,_order(702),source='webhook')
    assert result['orders_applied'] and result['stage']=='notification'
    assert result['notification_state']=='unknown'
    assert state()[1]['receipt_error']
    client=b.app.test_client()
    with client.session_transaction() as session: session['admin_authenticated']=True
    response=client.get('/orders/702')
    assert response.status_code==200
    assert 'Zamówienia zapisano lokalnie'.encode() in response.data
    assert 'Wynik wysyłki niepewny'.encode() in response.data
    tracking.process_due(b)
    tracking.process(b,_order(708),source='webhook')
    assert len(tracked['mail'])==1


def test_migration_repeatable_does_not_commit_caller_transaction(tracked):
    db=b.conn(); db.execute('BEGIN IMMEDIATE')
    db.execute("UPDATE orders SET status='completed' WHERE id=702")
    work.initialize(db); work.initialize(db)
    db.rollback(); db.close()
    assert _order(702)['status']=='packed_partial'


def test_shipping_advances_existing_payment_order_intent_in_same_transaction(tracked):
    db=b.conn()
    row=_order(702)
    db.execute("INSERT INTO invoice_payment_sync_outbox(table_name,record_id,revision,payload,state,updated_at) VALUES('orders',702,1,?,'PENDING','now')",
               (json.dumps(row),))
    db.commit(); db.close()
    assert tracking.process(b,_order(702),source='webhook')['ok']
    db=b.conn()
    receipt=db.execute("SELECT * FROM invoice_payment_sync_outbox WHERE table_name='orders' AND record_id=702").fetchone()
    assert receipt['revision']==2
    assert json.loads(receipt['payload'])['status']=='partially_shipped'
    db.close()


def test_temporary_attachment_failure_retries_preparation_without_carrier_read(tracked, monkeypatch):
    document=packing_versions.document
    monkeypatch.setattr(packing_versions,'document',lambda *a: (_ for _ in ()).throw(OSError('temporary PDF cache failure')))
    result=tracking.process(b,_order(702),source='webhook')
    assert result['orders_applied'] and result['notification_state']=='pending'
    assert not tracked['mail']
    monkeypatch.setattr(packing_versions,'document',document)
    db=b.conn(); db.execute('UPDATE inpost_reconciliation SET retry_at=0'); db.commit(); db.close()
    tracking.process_due(b)
    assert len(tracked['carrier'])==1
    assert len(tracked['mail'])==1


def test_full_receipt_storage_outage_keeps_in_memory_commit_truth(tracked, monkeypatch):
    connection=b.conn
    def mail(*args):
        tracked['mail'].append(args)
        monkeypatch.setattr(b,'conn',lambda: (_ for _ in ()).throw(sqlite3.OperationalError('storage offline after send')))
        return {'ok':True,'delivery_outcome':'accepted'}
    monkeypatch.setattr(b,'_send_orders_shipped_email',mail)
    result=tracking.process(b,_order(702),source='webhook')
    assert result['orders_applied'] is True
    assert result['stage']=='notification' and not result['ok']
    monkeypatch.setattr(b,'conn',connection)
    db=b.conn()
    assert tracking.notification(db,'123456')['state']=='sending'
    assert work.read(db,'123456')['apply_state']=='applied'
    db.execute('UPDATE inpost_tracking_state SET lease_until=0')
    db.execute('UPDATE inpost_tracking_notifications SET lease_until=0')
    db.commit(); db.close()
    tracking.process_due(b)
    tracking.process(b,_order(708),source='webhook')
    assert len(tracked['mail'])==1
