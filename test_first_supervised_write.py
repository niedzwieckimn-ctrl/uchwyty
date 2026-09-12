import json
from concurrent.futures import ThreadPoolExecutor

import pytest

import app as backend
import agent_runtime as runtime
import business_operations as operations
import internal_approval as approval
import internal_rbac as rbac
from test_business_operations import isolated, _owner, _actor


@pytest.fixture
def order(isolated):
    db = backend.conn()
    db.execute("INSERT INTO orders(id,order_no,customer_name,status,note,created_at) VALUES(901,'TEST-901','Test','new','customer note',?)", (backend.now_iso(),))
    db.commit()
    db.close()
    return 901


def ai():
    return rbac.load_actor_context(rbac.AI_OWNER_ASSISTANT_ACTOR_ID,
        delegated_by_actor_id=rbac.BOOTSTRAP_OWNER_ACTOR_ID)


def args(order, key='write-1', version=0, **extra):
    return dict(order_id=order, expected_version=version, idempotency_key=key, **extra)


def note(order, **kw):
    return operations.execute_business_operation(ai(), 'orders.internal_note.add', args(order, note='Private note', **kw))


def status(order, **kw):
    return operations.execute_business_operation(ai(), 'orders.status.transition', args(order, target_status='confirmed', **kw))


def state(order):
    db = backend.conn()
    try:
        row = dict(db.execute('SELECT * FROM orders WHERE id=?', (order,)).fetchone())
        row['notes'] = db.execute('SELECT COUNT(*) FROM internal_order_notes WHERE order_id=?', (order,)).fetchone()[0]
        return row
    finally:
        db.close()


def test_a_b_note_retry_once_and_no_other_changes(order):
    before = state(order)
    result = note(order)
    assert result.status == 'SUCCESS', result
    assert note(order) == result
    after = state(order)
    assert after.pop('notes') == 1
    before.pop('notes')
    assert before == after


def test_c_permission_denied(order):
    actor = _actor('AI_AGENT', 'AI_FINANCE')
    result = operations.execute_business_operation(actor, 'orders.internal_note.add', args(order, note='x'))
    assert result.status == 'DENIED'
    assert state(order)['notes'] == 0


def test_d_stale_version_conflict(order):
    assert note(order).status == 'SUCCESS'
    assert note(order, key='other').status == 'CONFLICT'
    assert status(order).status == 'CONFLICT'
    assert state(order)['notes'] == 1


def test_e_f_g_h_k_approval_once(order):
    pending = status(order)
    assert pending.status == 'PENDING_APPROVAL'
    assert state(order)['status'] == 'new'
    with pytest.raises(approval.ApprovalDenied):
        approval.approve_request(pending.approval_id, ai())
    approval.approve_request(pending.approval_id, _owner())
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: status(order), range(2)))
    assert any(r.status == 'SUCCESS' for r in results), results
    assert state(order)['status'] == 'confirmed'
    assert status(order).data['version'] == 1
    assert approval.get_request_snapshot(pending.approval_id)['status'] == 'CONSUMED'
    with pytest.raises(approval.ApprovalDenied):
        approval.approve_request(pending.approval_id, _owner())


def test_i_reject(order):
    pending = status(order)
    approval.reject_request(pending.approval_id, _owner())
    assert status(order).status == 'DENIED'
    assert state(order)['status'] == 'new'


@pytest.mark.parametrize('approve_first', [False, True])
def test_j_expired(order, approve_first):
    pending = status(order)
    if approve_first:
        approval.approve_request(pending.approval_id, _owner())
    db = backend.conn()
    db.execute("UPDATE internal_approval_requests SET expires_at='2000-01-01T00:00:00+00:00' WHERE approval_id=?", (pending.approval_id,))
    db.commit(); db.close()
    if not approve_first:
        with pytest.raises(approval.ApprovalDenied):
            approval.approve_request(pending.approval_id, _owner())
    assert status(order).status == 'DENIED'
    assert state(order)['status'] == 'new'


def test_l_failure_rolls_back_consumption_and_write(order, monkeypatch):
    pending = status(order)
    approval.approve_request(pending.approval_id, _owner())
    def fail(data, actor, correlation, db):
        db.execute("UPDATE orders SET status='confirmed' WHERE id=?", (order,))
        raise RuntimeError('injected failure')
    monkeypatch.setitem(operations._HANDLERS, 'orders.status.transition', fail)
    result = status(order)
    assert result.status == 'FAILED'
    assert status(order) == result
    assert state(order)['status'] == 'new'
    assert approval.get_request_snapshot(pending.approval_id)['status'] == 'APPROVED'


def test_m_only_two_new_writes(order):
    descriptors = runtime._tool_descriptors(ai(), _owner())
    writes = {d['name'] for d in descriptors if not operations.OPERATION_REGISTRY[d['name']].read_only}
    assert writes == operations.ORDER_WRITES | {runtime.MEMORY_WRITE}
    assert not any('approve' in d['name'] for d in descriptors)


def test_n_o_read_and_freshness(order, monkeypatch):
    seen = []
    monkeypatch.setattr(operations, '_freshness_provider', lambda op: seen.append(op) or {})
    result = operations.execute_business_operation(ai(), 'orders.get', {'id': order})
    assert result.status == 'SUCCESS'
    assert result.data['record']['status'] == 'new'
    assert seen == ['orders.get']


def test_revalidation_after_legacy_update(order):
    pending = status(order)
    approval.approve_request(pending.approval_id, _owner())
    db = backend.conn()
    db.execute("UPDATE orders SET note='changed by existing path' WHERE id=?", (order,))
    db.commit(); db.close()
    assert status(order).status == 'CONFLICT'
    assert state(order)['status'] == 'new'


def test_payload_fingerprint_change_denied(order):
    pending = status(order)
    approval.approve_request(pending.approval_id, _owner())
    db = backend.conn()
    db.execute("UPDATE internal_approval_requests SET operation_fingerprint='tampered' WHERE approval_id=?", (pending.approval_id,))
    db.commit(); db.close()
    assert status(order).status == 'CONFLICT'
    assert state(order)['status'] == 'new'


def test_key_cannot_be_reused_with_new_payload(order):
    assert note(order).status == 'SUCCESS'
    result = operations.execute_business_operation(ai(), 'orders.internal_note.add', args(order, note='other'))
    assert result.status == 'CONFLICT'
    assert state(order)['notes'] == 1


def test_http_approval_engine_and_double_click(order, isolated):
    pending = status(order)
    with isolated.session_transaction() as session:
        session['admin_authenticated'] = True
        rbac.bind_bootstrap_owner_session(session)
    url = f'/api/internal/ai/approvals/{pending.approval_id}/approve'
    first = isolated.post(url, json={})
    assert first.status_code == 200, first.json
    assert first.json['status'] == 'SUCCESS', first.json
    assert isolated.post(url, json={}).json['status'] == 'SUCCESS'
    assert status(order).data['version'] == 1


def test_runtime_exposes_pending_card_data(order):
    provider = runtime.FakeModelProvider([
        runtime.ProviderResponse(tool_calls=(runtime.ToolCall('status-call', 'orders.status.transition',
            json.dumps(args(order, target_status='confirmed'))),), model='fake'),
        runtime.ProviderResponse(text='Zmiana oczekuje na zatwierdzenie.', model='fake'),
    ])
    result = runtime.run_agent_turn(_owner(), 'Potwierdź zamówienie 901', provider)
    assert result['status'] == 'SUCCESS', result
    assert result['pending_approvals'][0]['order_id'] == order
    assert state(order)['status'] == 'new'


def test_parallel_note_retries(order):
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: note(order), range(3)))
    assert any(result.status == 'SUCCESS' for result in results)
    assert state(order)['notes'] == 1
    assert note(order).data['version'] == 1


def test_fingerprint_uses_original_note_not_redacted_audit(order):
    first = args(order, note='password=first-test-value')
    second = args(order, note='password=other-test-value')
    assert operations.execute_business_operation(ai(), 'orders.internal_note.add', first).status == 'SUCCESS'
    assert operations.execute_business_operation(ai(), 'orders.internal_note.add', second).status == 'CONFLICT'
    assert state(order)['notes'] == 1


@pytest.mark.parametrize('role,permission', [
    ('AI_OWNER_ASSISTANT', 'orders.change_status'),
    ('OWNER', 'orders.change_status'),
    ('OWNER', 'approvals.decide'),
])
def test_permission_rechecked_at_execution(order, role, permission):
    pending = status(order)
    approval.approve_request(pending.approval_id, _owner())
    db = backend.conn()
    db.execute("UPDATE internal_role_permissions SET decision='DENY' WHERE role_key=? AND permission_key=?", (role, permission))
    db.commit(); db.close()
    assert status(order).status == 'DENIED'
    assert state(order)['status'] == 'new'
    assert approval.get_request_snapshot(pending.approval_id)['status'] == 'APPROVED'


def test_missing_entity_after_approval(order):
    pending = status(order)
    approval.approve_request(pending.approval_id, _owner())
    db = backend.conn()
    db.execute('DELETE FROM orders WHERE id=?', (order,))
    db.commit(); db.close()
    assert status(order).status == 'CONFLICT'


def test_handler_output_failure_rolls_back_green_note(order, monkeypatch):
    original = operations._HANDLERS['orders.internal_note.add']
    def invalid(*arguments):
        original(*arguments)
        return {'invalid': True}
    monkeypatch.setitem(operations._HANDLERS, 'orders.internal_note.add', invalid)
    result = note(order)
    assert result.status == 'FAILED'
    assert state(order)['notes'] == 0
    assert note(order) == result


def test_existing_pilot_does_not_consume_approval_on_handler_failure(order, monkeypatch):
    import internal_concurrency as concurrency
    from test_internal_approval import _resource, _request, _approve
    resource = _resource()
    approval_id = _request(_owner(), resource)
    _approve(approval_id)
    original = concurrency.update_versioned_resource
    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('failure after mutation')
    monkeypatch.setattr(concurrency, 'update_versioned_resource', fail)
    with pytest.raises(RuntimeError):
        approval.execute_pilot_change(approval_id, _owner(), resource.resource_id,
            payload={'amount': 2000}, expected_entity_version=1)
    assert approval.get_request_snapshot(approval_id)['status'] == 'APPROVED'
    assert concurrency.get_versioned_resource(resource.resource_id).version == 1
