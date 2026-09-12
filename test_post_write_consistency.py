import json
import time

import pytest

import agent_runtime as runtime
import app as backend
import business_operations as operations
import internal_approval as approval
from test_business_operations import _owner
from test_first_supervised_write import ai, args, isolated, note, order, state, status


def _login(client):
    with client.session_transaction() as session:
        session['admin_authenticated'] = True
        session['csrf_token'] = 'post-write-test'
        backend.bind_bootstrap_owner_session(session)


def _conversation():
    result = runtime.run_agent_turn(
        _owner(), 'Rozpocznij rozmowę.',
        runtime.FakeModelProvider([runtime.ProviderResponse(text='Gotowe.', model='fake')]),
    )
    return result['conversation_id']


def _decision(client, pending, decision, provider):
    backend.AGENT_MODEL_PROVIDER = provider
    _login(client)
    return client.post(
        f'/api/internal/ai/approvals/{pending.approval_id}/{decision}',
        json={'conversation_id': _conversation()},
    )


def _model_outcome(provider):
    outputs = [
        item for item in provider.calls[0]['input_items']
        if item.get('type') == 'function_call_output'
        and item.get('call_id', '').startswith('approval-outcome-')
    ]
    assert len(outputs) == 1
    return json.loads(outputs[0]['output'])


def test_a_approval_success_reaches_model_as_structured_execution_outcome(order, isolated):
    pending = status(order)
    provider = runtime.FakeModelProvider([
        runtime.ProviderResponse(text='Status zamówienia został zatwierdzony i wykonany.', model='fake')
    ])
    response = _decision(isolated, pending, 'approve', provider)
    body = response.get_json()
    evidence = _model_outcome(provider)
    assert response.status_code == 200
    assert body['message'] == 'Status zamówienia został zatwierdzony i wykonany.'
    assert evidence == body['execution_outcome']
    assert evidence['approval_id'] == pending.approval_id
    assert evidence['approval_status'] == 'CONSUMED'
    assert evidence['execution_status'] == 'SUCCESS'
    assert evidence['operation'] == 'orders.status.transition'
    assert evidence['entity_type'] == 'order' and evidence['entity_id'] == order
    assert evidence['before'] == {'status': 'new'}
    assert evidence['after'] == {'status': 'confirmed'}
    assert evidence['result']['status'] == 'SUCCESS'
    assert evidence['failure'] is None and evidence['conflict'] is None
    assert provider.calls[0]['tool_choice'] == 'auto'


def test_b_rejection_reaches_model_without_execution_success(order, isolated):
    pending = status(order)
    provider = runtime.FakeModelProvider([
        runtime.ProviderResponse(text='Prośba została odrzucona.', model='fake')
    ])
    response = _decision(isolated, pending, 'reject', provider)
    evidence = _model_outcome(provider)
    assert response.get_json()['status'] == 'REJECTED'
    assert evidence['approval_status'] == 'REJECTED'
    assert evidence['execution_status'] == 'DENIED'
    assert evidence['result']['status'] == 'REJECTED'
    assert evidence['after'] is None
    assert state(order)['status'] == 'new'


def _stale_remote(monkeypatch, order):
    calls = []
    monkeypatch.setattr(backend, 'SUPABASE_URL', 'https://example.test')
    monkeypatch.setattr(backend, 'SUPABASE_SERVICE_ROLE_KEY', 'test-key')
    monkeypatch.setattr(backend, 'BUSINESS_FRESHNESS_TTL_SECONDS', 45)

    def select(table, order_by='id', **_kwargs):
        calls.append(table)
        if table == 'orders':
            current = state(order)
            current['status'] = 'new'
            current.pop('notes', None)
            return [current]
        return []

    monkeypatch.setattr(backend, 'supabase_select_rows', select)
    return calls


def _approve_direct(order):
    pending = status(order)
    approval.approve_request(pending.approval_id, _owner())
    result = status(order)
    assert result.status == 'SUCCESS'
    return pending


def test_c_status_success_keeps_next_orders_read_on_updated_local_snapshot(order, monkeypatch):
    calls = _stale_remote(monkeypatch, order)
    _approve_direct(order)
    markers = {
        group: backend._business_freshness_marker(group)
        for group in backend.BUSINESS_FRESHNESS_GROUPS
    }
    assert all(markers[group] is not None for group in ('orders', 'inventory', 'fulfillment', 'customers'))
    assert all(markers[group] is None for group in ('invoices', 'china', 'sales'))
    read = operations.execute_business_operation(ai(), 'orders.get', {'id': order})
    assert read.status == 'SUCCESS'
    assert read.data['record']['status'] == 'confirmed'
    assert calls == []


def test_d_note_success_does_not_disturb_orders_freshness(order):
    marker = time.time()
    backend._mark_business_freshness('orders', marker)
    assert note(order).status == 'SUCCESS'
    assert backend._business_freshness_marker('orders') == marker
    assert all(
        backend._business_freshness_marker(group) is None
        for group in ('inventory', 'fulfillment', 'customers', 'invoices', 'china', 'sales')
    )
    read = operations.execute_business_operation(ai(), 'orders.get', {'id': order})
    assert read.status == 'SUCCESS' and read.data['record']['status'] == 'new'


def test_e_pending_does_not_change_freshness_marker(order):
    marker = time.time()
    backend._mark_business_freshness('orders', marker)
    assert status(order).status == 'PENDING_APPROVAL'
    assert backend._business_freshness_marker('orders') == marker


def test_f_failed_execution_has_no_false_after_and_no_freshness_change(order, isolated, monkeypatch):
    marker = 123.0
    backend._mark_business_freshness('orders', marker)
    pending = status(order)

    def fail(data, actor, correlation_id, db):
        db.execute("UPDATE orders SET status='confirmed' WHERE id=?", (order,))
        raise RuntimeError('injected handler failure')

    monkeypatch.setitem(operations._HANDLERS, 'orders.status.transition', fail)
    provider = runtime.FakeModelProvider([runtime.ProviderResponse(text='Operacja nie powiodła się.', model='fake')])
    response = _decision(isolated, pending, 'approve', provider)
    evidence = _model_outcome(provider)
    assert evidence['execution_status'] == 'FAILED'
    assert evidence['result']['status'] == 'FAILED'
    assert evidence['after'] is None and evidence['failure']['code'] == 'HANDLER_FAILED'
    assert state(order)['status'] == 'new'
    assert backend._business_freshness_marker('orders') == marker


def test_g_conflict_has_no_false_success_or_freshness_change(order, isolated):
    marker = 456.0
    backend._mark_business_freshness('orders', marker)
    pending = status(order)
    db = backend.conn()
    db.execute("UPDATE orders SET note='changed concurrently' WHERE id=?", (order,))
    db.commit()
    db.close()
    provider = runtime.FakeModelProvider([runtime.ProviderResponse(text='Wykryto konflikt wersji.', model='fake')])
    response = _decision(isolated, pending, 'approve', provider)
    evidence = _model_outcome(provider)
    assert evidence['execution_status'] == 'CONFLICT'
    assert evidence['result']['status'] == 'CONFLICT'
    assert evidence['after'] is None and evidence['conflict']['code'] == 'ENTITY_VERSION_CONFLICT'
    assert state(order)['status'] == 'new'
    assert backend._business_freshness_marker('orders') == marker


def test_h_second_read_adds_no_refresh_beyond_existing_policy(order, monkeypatch):
    calls = _stale_remote(monkeypatch, order)
    _approve_direct(order)
    first = operations.execute_business_operation(ai(), 'orders.get', {'id': order})
    second = operations.execute_business_operation(ai(), 'orders.get', {'id': order})
    assert first.data['record']['status'] == second.data['record']['status'] == 'confirmed'
    assert calls == []
