"""Full BO approval path for payment changes; no external services."""
import uuid

import pytest
import app as b
import business_operations as ops
import internal_approval as approvals
import internal_rbac as rbac
import invoice_payment_operations as payment
from test_invoice_refactor import numbering_db, _store_number


@pytest.fixture
def invoice(numbering_db, monkeypatch):
    monkeypatch.setattr(ops, '_freshness_provider', None)
    monkeypatch.setattr(ops, '_write_success_observer', None)
    _store_number('FVAT 1/09/2026', invoice_id=1)
    b.upsert_invoice_meta(1, '', '[]', sent_to_client=1)
    db = b.conn()
    db.execute("UPDATE orders SET status='shipped',tracking_no='TRACK-KEEP' WHERE id=1")
    db.commit(); db.close()
    return 1


def ai():
    return rbac.load_actor_context(rbac.AI_OWNER_ASSISTANT_ACTOR_ID,
                                  delegated_by_actor_id=rbac.BOOTSTRAP_OWNER_ACTOR_ID)


def payload(invoice_id, paid=True):
    read = ops.execute_business_operation(ai(), 'invoices.get', {'id': invoice_id})
    assert read.status == 'SUCCESS', read
    return {'invoice_id': invoice_id, 'paid': paid,
            'expected_version': read.data['record']['expected_version'],
            'idempotency_key': str(uuid.uuid4())}


def request(data):
    result = ops.execute_business_operation(ai(), payment.WRITE, data)
    assert result.status == 'PENDING_APPROVAL', result
    return result


def execute(data, pending):
    approvals.approve_request(pending.approval_id,
                             rbac.load_actor_context(rbac.BOOTSTRAP_OWNER_ACTOR_ID))
    return ops.execute_business_operation(ai(), payment.WRITE, data, approval_id=pending.approval_id)


def state():
    db = b.conn()
    try:
        return (dict(db.execute('SELECT * FROM invoice_meta WHERE invoice_id=1').fetchone()),
                dict(db.execute('SELECT * FROM orders WHERE id=1').fetchone()))
    finally:
        db.close()


def test_payment_requires_approval_and_replay_changes_nothing(invoice):
    data = payload(invoice)
    pending = request(data)
    assert not state()[0]['paid']
    result = execute(data, pending)
    assert result.status == 'SUCCESS' and result.data['paid'] and result.data['changed'], result
    saved = state()
    assert saved[0]['paid_at'] and saved[1]['status'] == 'completed'
    assert saved[1]['tracking_no'] == 'TRACK-KEEP'
    replay = ops.execute_business_operation(ai(), payment.WRITE, data, approval_id=pending.approval_id)
    assert replay.status == 'SUCCESS' and state() == saved
    same_status = payload(invoice)
    noop = execute(same_status, request(same_status))
    assert noop.status == 'SUCCESS' and not noop.data['changed']
    assert state() == saved
    unset = payload(invoice, False)
    reverted = execute(unset, request(unset))
    assert reverted.status == 'SUCCESS' and not reverted.data['paid']
    meta, order = state()
    assert meta['paid_at'] is None and order['status'] == 'shipped'
    assert order['tracking_no'] == 'TRACK-KEEP'


def test_payment_rejects_invoice_changed_after_approval_request(invoice):
    data = payload(invoice)
    pending = request(data)
    db = b.conn(); db.execute('UPDATE invoices SET total_gross=123 WHERE id=1'); db.commit(); db.close()
    result = execute(data, pending)
    assert result.status == 'CONFLICT' and result.error_code == 'ENTITY_VERSION_CONFLICT', result
    assert not state()[0]['paid']


def test_payment_failure_rolls_back_meta_orders_and_approval_consumption(invoice, monkeypatch):
    data = payload(invoice)
    pending = request(data)
    saved = state()
    def fail(*args):
        raise RuntimeError('failure after payment meta write')
    monkeypatch.setattr(b, '_all_order_invoices_paid', fail)
    result = execute(data, pending)
    assert result.status == 'FAILED', result
    assert state() == saved
    assert approvals.get_request_snapshot(pending.approval_id)['status'] == 'APPROVED'


def test_payment_rejects_staged_invoice(invoice):
    db = b.conn(); db.execute("UPDATE invoices SET publication_state='staged' WHERE id=1"); db.commit(); db.close()
    result = ops.execute_business_operation(ai(), payment.WRITE, payload(invoice))
    assert result.status == 'DENIED' and result.error_code == 'INVOICE_NOT_PUBLISHED', result
    assert not state()[0]['paid']


def test_payment_rechecks_human_permission_before_mutation(invoice):
    data = payload(invoice)
    pending = request(data)
    approvals.approve_request(pending.approval_id,
                             rbac.load_actor_context(rbac.BOOTSTRAP_OWNER_ACTOR_ID))
    db = b.conn()
    db.execute("UPDATE internal_role_permissions SET decision='DENY' WHERE role_key='OWNER' AND permission_key='payments.set_status'")
    db.commit(); db.close()
    result = ops.execute_business_operation(ai(), payment.WRITE, data, approval_id=pending.approval_id)
    assert result.status == 'DENIED' and result.error_code == 'PERMISSION_DENIED', result
    assert not state()[0]['paid']
