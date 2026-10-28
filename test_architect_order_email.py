"""Durable delivery contracts. No network, provider configuration or real mail."""
import copy
from types import SimpleNamespace

import pytest
import architect_order_email as delivery
import email_module


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(email_module, 'email_config_summary', lambda: {'configured': True})


def job():
    return {'id': 'job-1', 'idempotency_key': 'architect-order:17:recipient-hash',
            'recipient': 'distributor@example.invalid',
            'payload': {'order': {'id': 17, 'order_no': 'TEST-17', 'customer_email': 'distributor@example.invalid'},
                        'items': [{'sku': 'TEST', 'qty': 2, 'net_price': 92.75}]}}


def test_no_configuration_means_no_claim_or_provider_call(monkeypatch):
    monkeypatch.setattr(email_module, 'email_config_summary', lambda: {'configured': False})
    def forbidden(*args, **kwargs):
        raise AssertionError('No network while unconfigured')
    monkeypatch.setattr(email_module, 'send_order_confirmation', forbidden)
    backend = SimpleNamespace(supabase_request=forbidden, supabase_enabled=lambda: True)
    assert delivery.process_batch(backend) == {'claimed': 0, 'accepted': 0, 'status': 'awaiting_configuration'}
    assert delivery.start_worker(backend) is False


@pytest.mark.parametrize('rows,status,queued,pending', [
    ([], 'not_queued', False, False),
    ([{'status': 'PENDING'}], 'awaiting_configuration', True, True),
    ([{'status': 'UNKNOWN'}], 'unknown', True, False),
    ([{'status': 'BLOCKED'}], 'blocked', True, False),
    ([{'status': 'DELIVERED'}], 'delivered', True, False),
])
def test_status_reports_actual_queue_state(monkeypatch, rows, status, queued, pending):
    monkeypatch.setattr(email_module, 'email_config_summary', lambda: {'configured': False})
    result = delivery.order_status(lambda *args, **kwargs: rows, 17)
    assert result['status'] == status
    assert result['queued'] is queued
    assert result['pending_retry'] is pending
    assert result['ok'] is (status == 'delivered')


def test_unavailable_queue_does_not_claim_retry_is_scheduled():
    def unavailable(*args, **kwargs):
        raise TimeoutError('private provider content')
    result = delivery.order_status(unavailable, 17)
    assert result == {'ok': False, 'queued': None, 'pending_retry': False, 'status': 'unavailable'}


def test_acceptance_uses_immutable_snapshot_and_same_key(configured, monkeypatch):
    remote_calls, sends = [], []
    queue = [job(), None]
    def remote(path, **kwargs):
        remote_calls.append((path, kwargs['payload']))
        if path.endswith('architect_email_claim'):
            return queue.pop(0)
        return True
    def send(order, items, **kwargs):
        sends.append((copy.deepcopy(order), copy.deepcopy(items), kwargs))
        return {'ok': True, 'body': {'id': 'accepted-provider-id'}}
    monkeypatch.setattr(email_module, 'send_order_confirmation', send)
    result = delivery.process_batch(SimpleNamespace(supabase_request=remote))
    assert result['accepted'] == 1
    assert sends == [(job()['payload']['order'], job()['payload']['items'], {
        'idempotency_key': job()['idempotency_key'], 'recipients_override': [job()['recipient']]})]
    finish = remote_calls[1][1]
    assert finish['p_owner'] == remote_calls[0][1]['p_owner']
    assert finish['p_status'] == 'DELIVERED'
    assert finish['p_provider_id'] == 'accepted-provider-id'


@pytest.mark.parametrize('provider,outcome', [
    ({'ok': False, 'status': 429, 'delivery_outcome': 'rejected'}, 'RETRY'),
    ({'ok': False, 'status': 503, 'delivery_outcome': 'unknown'}, 'UNKNOWN'),
    ({'ok': False, 'status': 400, 'delivery_outcome': 'rejected'}, 'BLOCKED'),
    ({'ok': False, 'skipped': True}, 'RETRY'),
])
def test_only_known_safe_failures_are_retried(configured, monkeypatch, provider, outcome):
    finishes = []
    def remote(path, **kwargs):
        if path.endswith('architect_email_claim'):
            return job()
        finishes.append(kwargs['payload'])
        return True
    monkeypatch.setattr(email_module, 'send_order_confirmation', lambda *args, **kwargs: {
        **provider, 'error': 'secret-value', 'body': {'message': 'secret-value'}})
    delivery.process_batch(SimpleNamespace(supabase_request=remote), limit=1)
    assert finishes[0]['p_status'] == outcome
    assert 'secret-value' not in str(finishes)


def test_lost_acknowledgement_stops_batch_without_resending(configured, monkeypatch):
    sends = []
    def remote(path, **kwargs):
        if path.endswith('architect_email_claim'):
            return job()
        raise TimeoutError('response lost')
    monkeypatch.setattr(email_module, 'send_order_confirmation', lambda *args, **kwargs: sends.append(kwargs) or {'ok': True})
    with pytest.raises(TimeoutError):
        delivery.process_batch(SimpleNamespace(supabase_request=remote))
    assert len(sends) == 1


def test_email_renderer_does_not_add_implicit_admin_recipient(monkeypatch):
    sent = []
    monkeypatch.setattr(email_module, 'send_email', lambda *args, **kwargs: sent.append((args, kwargs)) or {'ok': True})
    monkeypatch.setattr(email_module, 'email_config_summary', lambda: {'admin_email': 'other@example.invalid'})
    snapshot = job()['payload']
    email_module.send_order_confirmation(snapshot['order'], snapshot['items'],
        recipients_override=[job()['recipient']], idempotency_key=job()['idempotency_key'])
    assert sent[0][0][0] == [job()['recipient']]
    assert sent[0][1]['idempotency_key'] == job()['idempotency_key']
