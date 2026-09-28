"""Regression scenarios: real BO/SQLite, deterministic cloud and mail doubles."""
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import invoice_numbering
import payment_reminders
import reconciliation_store
from test_fulfillment_orchestrator import flow, b, actor, run, success
from test_invoice_refactor import numbering_db, _store_number


def cloud_for(monkeypatch):
    cloud = {}
    def remote(path, method='GET', payload=None, **kw):
        if method == 'GET' and path == '/rest/v1/fulfillment_reconciliation':
            oid = int(kw['params']['order_id'].split('.')[-1])
            return [copy.deepcopy(cloud[oid])] if oid in cloud else []
        assert path == '/rest/v1/rpc/save_fulfillment_reconciliation'
        oid = payload['p_order_id']
        revision = cloud.get(oid, {}).get('revision', 0)
        if revision != payload['p_expected_revision']:
            return {'saved': False, 'revision': revision}
        cloud[oid] = {'revision': revision+1, 'payload': copy.deepcopy(payload['p_payload'])}
        return {'saved': True, 'revision': revision+1}
    monkeypatch.setattr(b, 'supabase_enabled', lambda: True)
    monkeypatch.setattr(b, 'supabase_request', remote)
    return cloud


def test_shipping_requirements_survive_update_state_publish_restore(flow, monkeypatch):
    success('orders.packing_list.generate')
    cloud = cloud_for(monkeypatch)
    reconciliation_store.publish(b, 702)
    original = copy.deepcopy(cloud[702])
    result = run('shipping.requirements.update', weight=5, length=45, width=20, height=20, sms=True, email=True)
    assert result.status == 'SUCCESS', result
    assert result.data['state']['requirements']['known']['weight'] == 5
    reconciliation_store.restore(b, 702)
    cloud[702] = original  # Simulate a delayed response from an older revision.
    reconciliation_store.restore(b, 702)
    db = b.conn()
    try:
        saved = json.loads(db.execute('SELECT payload FROM order_shipping_requirements WHERE order_id=702').fetchone()[0])
        assert saved['weight'] == 5 and saved['length'] == 45 and saved['sms'] is True
    finally:
        db.close()


def test_same_revision_cannot_replay_over_unpublished_local_metadata(flow, monkeypatch):
    success('orders.packing_list.generate')
    cloud_for(monkeypatch)
    reconciliation_store.publish(b, 702)
    db = b.conn()
    db.execute('INSERT INTO order_shipping_requirements VALUES(?,?,?)', (702, '{"weight":5}', b.now_iso()))
    db.commit(); db.close()
    reconciliation_store.restore(b, 702)
    db = b.conn()
    assert json.loads(db.execute('SELECT payload FROM order_shipping_requirements WHERE order_id=702').fetchone()[0])['weight'] == 5
    db.close()


def test_same_revision_repairs_lost_pdf_without_replaying_metadata(flow, monkeypatch):
    success('orders.packing_list.generate')
    cloud_for(monkeypatch)
    reconciliation_store.publish(b, 702)
    db = b.conn()
    try:
        path = Path(db.execute("SELECT path FROM fulfillment_documents WHERE order_id=702 AND kind='packing_list'").fetchone()[0])
        original = path.read_bytes()
        db.execute('INSERT INTO order_shipping_requirements VALUES(?,?,?)', (702, '{"weight":5}', b.now_iso()))
        db.commit()
    finally:
        db.close()
    path.unlink()
    reconciliation_store.restore(b, 702)
    assert path.read_bytes() == original
    db = b.conn()
    try:
        assert json.loads(db.execute('SELECT payload FROM order_shipping_requirements WHERE order_id=702').fetchone()[0])['weight'] == 5
    finally:
        db.close()


def test_remote_conflict_retains_local_pending_payload(flow, monkeypatch):
    success('orders.packing_list.generate')
    cloud = cloud_for(monkeypatch)
    reconciliation_store.publish(b, 702)
    db = b.conn()
    db.execute('INSERT INTO order_shipping_requirements VALUES(?,?,?)', (702, '{"weight":5}', b.now_iso()))
    reconciliation_store.stage(b, 702, connection=db)
    db.commit(); db.close()
    cloud[702]['revision'] += 1
    cloud[702]['payload']['order_shipping_requirements'] = [{'order_id':702,'payload':'{"weight":9}','updated_at':b.now_iso()}]
    reconciliation_store.restore(b, 702)
    with pytest.raises(ValueError, match='konfliktu wersji'):
        reconciliation_store.retry_pending(b, 702)
    db = b.conn()
    assert db.execute('SELECT 1 FROM fulfillment_reconciliation_pending WHERE order_id=702').fetchone()
    assert json.loads(db.execute('SELECT payload FROM order_shipping_requirements WHERE order_id=702').fetchone()[0])['weight'] == 5
    db.close()


def invoice_for_mail():
    success('orders.packing_list.generate'); success('orders.invoice.create')
    db = b.conn()
    iid = db.execute('SELECT id FROM invoices').fetchone()[0]
    db.close()
    return iid


def test_mail_acceptance_survives_local_flag_failure_and_prevents_new_attempt(flow, monkeypatch):
    iid = invoice_for_mail()
    sent = []
    monkeypatch.setattr(b, 'send_payment_reminder', lambda *a, **kw: sent.append(kw) or {'ok':True,'body':{'id':'mail-1'}})
    setter = b._set_invoice_payment_state
    monkeypatch.setattr(b, '_set_invoice_payment_state', lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('local write interrupted')))
    first = payment_reminders.send(iid, trigger_source='agent', attempt_id='mail-once')
    second = payment_reminders.send(iid, trigger_source='agent', attempt_id='different-key')
    assert first['ok'] and first['reminder_sent'] and first['reconciliation_required']
    assert second['attempt_id'] == 'mail-once' and len(sent) == 1
    assert sent[0]['idempotency_key'] == 'payment-reminder/mail-once'
    monkeypatch.setattr(b, '_set_invoice_payment_state', setter)
    reconciled = payment_reminders.reconcile_confirmed('mail-once')
    assert reconciled['delivery_status'] == 'SUCCESS' and not reconciled['reconciliation_required']
    assert len(sent) == 1


def test_unknown_provider_outcome_is_never_resent_after_restart(flow, monkeypatch):
    iid = invoice_for_mail()
    sent = []
    monkeypatch.setattr(b, 'send_payment_reminder', lambda *a, **kw: sent.append(1) or {'ok':False,'delivery_outcome':'unknown','error':'timeout'})
    first = payment_reminders.send(iid, trigger_source='agent', attempt_id='uncertain')
    db = b.conn(); payment_reminders.initialize(db); db.commit(); db.close()
    second = payment_reminders.send(iid, trigger_source='manual_ui', attempt_id='new-after-restart')
    assert first['delivery_status'] == second['delivery_status'] == 'UNKNOWN'
    assert first['reconciliation_required'] and len(sent) == 1


def test_mail_accepted_with_non_object_body_still_records_acceptance(flow, monkeypatch):
    iid = invoice_for_mail()
    monkeypatch.setattr(b, 'send_payment_reminder', lambda *a, **kw: {'ok':True, 'body':'accepted'})
    result = payment_reminders.send(iid, trigger_source='agent', attempt_id='text-provider-body')
    assert result['ok'] and result['provider_confirmed'] and result['delivery_status'] == 'SUCCESS'


def test_parallel_reminders_claim_one_unresolved_attempt(flow, monkeypatch):
    import threading
    iid = invoice_for_mail()
    entered = threading.Event(); release = threading.Event(); sends = []
    def provider(*a, **kw):
        sends.append(1); entered.set()
        assert release.wait(5)
        return {'ok':True}
    monkeypatch.setattr(b, 'send_payment_reminder', provider)
    monkeypatch.setattr(b, '_invoice_email_context', lambda _iid: ({}, ''))
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(payment_reminders.send, iid, trigger_source='agent', attempt_id='concurrent-1')
        assert entered.wait(5)
        second = pool.submit(payment_reminders.send, iid, trigger_source='agent', attempt_id='concurrent-2')
        try:
            assert second.result(timeout=5)['reconciliation_required']
        finally:
            release.set()
        assert first.result(timeout=5)['ok']
    assert sends == [1]


def test_final_invoice_number_stays_protected_after_ttl_and_restart(numbering_db):
    _store_number('FVAT 6/09/2026', invoice_id=6)
    db = b.conn()
    try:
        db.execute("INSERT INTO ksef_documents(invoice_id,status,ksef_number,updated_at) VALUES(6,'accepted','KSEF-6',?)", (b.now_iso(),))
        db.execute("UPDATE invoice_number_claims SET created_at='2000-01-01' WHERE invoice_no='FVAT 6/09/2026'")
        db.execute('DELETE FROM invoices WHERE id=6')
        invoice_numbering.initialize(db)
        db.commit()
    finally:
        db.close()
    with pytest.raises(ValueError, match='już wykorzystany'):
        invoice_numbering.reserve(b, '2026-09-15', 'FVAT 6/09/2026', manual=True)


def test_live_number_claim_survives_restart_and_stale_claim_releases(numbering_db):
    assert invoice_numbering.reserve(b, '2026-09-15') == 'FVAT 1/09/2026'
    db = b.conn(); invoice_numbering.initialize(db); db.commit(); db.close()
    with pytest.raises(ValueError, match='obecnie rezerwowany'):
        invoice_numbering.reserve(b, '2026-09-15')
    db = b.conn(); db.execute("UPDATE invoice_number_claims SET created_at='2000-01-01'"); db.commit(); db.close()
    assert invoice_numbering.reserve(b, '2026-09-15') == 'FVAT 1/09/2026'
