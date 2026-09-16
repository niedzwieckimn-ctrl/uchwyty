import app as backend
import business_operations as operations
import internal_approval as approvals
import internal_rbac as rbac
import payment_reminders
from test_agent_runtime import isolated, owner


def _meta(invoice_id=10):
    db = backend.conn()
    try:
        return dict(db.execute(
            'SELECT payment_reminder,paid FROM invoice_meta WHERE invoice_id=?',
            (invoice_id,)).fetchone())
    finally:
        db.close()


def test_manual_failed_reminder_does_not_change_invoice_state(monkeypatch):
    monkeypatch.setattr(backend, 'send_payment_reminder',
                        lambda *_a, **_k: {'ok': False, 'error': 'simulated failure'})
    before = _meta()
    with backend.app.test_request_context('/invoices/10/payment-reminder', method='POST'):
        response = backend.app.view_functions['invoice_payment_reminder_admin'](10)
    after = _meta()
    assert response[1] == 502
    assert after == before == {'payment_reminder': 0, 'paid': 0}
    db = backend.conn()
    attempt = dict(db.execute(
        'SELECT status,error FROM payment_reminder_attempts WHERE invoice_id=10').fetchone())
    db.close()
    assert attempt['status'] == 'FAILED'
    assert 'simulated failure' in attempt['error']


def test_successful_reminder_sets_only_reminder_flag(monkeypatch):
    monkeypatch.setattr(backend, 'send_payment_reminder', lambda *_a, **_k: {'ok': True})
    with backend.app.test_request_context('/invoices/10/payment-reminder', method='POST'):
        response = backend.app.view_functions['invoice_payment_reminder_admin'](10)
    assert response.status_code == 302
    assert _meta() == {'payment_reminder': 1, 'paid': 0}


def test_reminder_read_exposes_authoritative_history(monkeypatch):
    monkeypatch.setattr(backend, 'send_payment_reminder',
                        lambda *_a, **_k: {'ok': False, 'error': 'provider rejected'})
    payment_reminders.send(10, trigger_source='test')
    result = operations.execute_business_operation(
        owner(), payment_reminders.READ, {'invoice_id': 10, 'only_overdue': False})
    assert result.status == 'SUCCESS'
    assert result.data['scope']['entity_existence_authoritative'] is True
    assert result.data['records'][0]['attempt_count'] == 1
    assert result.data['records'][0]['last_attempt']['status'] == 'FAILED'
    assert result.data['records'][0]['reminder_sent'] is False


def test_send_operation_is_registered_as_approved_existing_permission():
    definition = operations.OPERATION_REGISTRY[payment_reminders.WRITE]
    assert definition.required_permission == 'payments.remind'
    assert definition.risk_level == 'YELLOW'
    assert definition.approval_requirement == 'REQUIRED'
    assert definition.operation_name in operations.SUPERVISED_WRITES


def test_finance_agent_can_send_only_after_human_approval(monkeypatch):
    monkeypatch.setattr(backend, 'send_payment_reminder', lambda *_a, **_k: {'ok': True})
    human = owner()
    finance_actor_id = '20000000-0000-4000-8000-000000000099'
    db = backend.conn()
    now = backend.now_iso()
    db.execute('''INSERT INTO internal_actors(
                      actor_id,actor_type,display_name,status,created_at,updated_at)
                  VALUES(?,'AI_AGENT','AI Finance Test','active',?,?)''',
               (finance_actor_id, now, now))
    db.execute('''INSERT INTO internal_actor_roles(actor_id,role_key,assigned_at)
                  VALUES(?,'AI_FINANCE',?)''', (finance_actor_id, now))
    db.commit(); db.close()
    ai = rbac.load_actor_context(
        finance_actor_id,
        request_id='agent-reminder',
        delegated_by_actor_id=human.actor_id,
        source='agent_runtime',
    )
    assert payment_reminders.WRITE in {
        item['name'] for item in operations.list_available_operations(ai)
    }
    payload = {'invoice_id': 10, 'idempotency_key': 'reminder-agent-10'}
    pending = operations.execute_business_operation(ai, payment_reminders.WRITE, payload)
    assert pending.status == 'PENDING_APPROVAL'
    assert _meta() == {'payment_reminder': 0, 'paid': 0}

    approvals.approve_request(pending.approval_id, human, reason='Test przypomnienia')
    sent = operations.execute_business_operation(
        ai, payment_reminders.WRITE, payload, approval_id=pending.approval_id)
    assert sent.status == 'SUCCESS'
    assert sent.data['reminder_sent'] is True
    assert _meta() == {'payment_reminder': 1, 'paid': 0}

    owner_ai = rbac.load_actor_context(rbac.AI_OWNER_ASSISTANT_ACTOR_ID)
    assert payment_reminders.WRITE not in {
        item['name'] for item in operations.list_available_operations(owner_ai)
    }


def test_reminder_operations_publish_machine_readable_capability_contract():
    read_descriptor = operations.operation_descriptor(
        operations.OPERATION_REGISTRY[payment_reminders.READ])
    write_descriptor = operations.operation_descriptor(
        operations.OPERATION_REGISTRY[payment_reminders.WRITE])
    assert read_descriptor['capability_contract']['implemented'] is True
    assert write_descriptor['capability_contract']['approval_required'] is True
