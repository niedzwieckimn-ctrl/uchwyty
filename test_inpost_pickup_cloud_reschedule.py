import copy
import json
import sqlite3

import pytest

import inpost_pickups as pickups
from test_inpost_daily_pickups import at, pickup_data


class OldRpcBackend:
    """Installed September RPC accepts updates but ignores pickup_json."""
    def __init__(self):
        payload = dict(pickup_data(), _daily_pickup={
            'scheduled_date': '2026-10-09', 'parcel_count': 2, 'cutoff': '12:20'})
        self.row = dict(shipment_id='41', pickup_json=json.dumps(payload),
                        state='pending', attempted=0, dispatch_id=None,
                        claim_token=None, lease_until=0, next_check=0, error='')
        self.calls = []
        self.lose_lease = False
        self.wrong_receipt = False

    def conn(self):
        return sqlite3.connect(':memory:')

    def supabase_enabled(self):
        return True

    def now_iso(self):
        return '2026-10-09T12:21:00+02:00'

    def supabase_request(self, path, **kwargs):
        self.calls.append((path, copy.deepcopy(kwargs)))
        if path.endswith('/claim_inpost_pickup'):
            self.row['claim_token'] = kwargs['payload']['p_token']
            return copy.deepcopy(self.row)
        if path.endswith('/update_inpost_pickup'):
            values = kwargs['payload']['p_values']
            self.row.update({key: value for key, value in values.items() if key != 'pickup_json'})
            return True
        assert path == '/rest/v1/inpost_pickup_jobs'
        if kwargs.get('method') != 'PATCH':
            return [copy.deepcopy(self.row)]
        if self.lose_lease:
            self.row['claim_token'] = 'new-owner'
        expected = {'shipment_id': 'eq.41', 'claim_token': 'eq.' + self.row['claim_token'],
                    'attempted': 'eq.0', 'dispatch_id': 'is.null', 'state': 'eq.pending'}
        if any(kwargs['params'].get(key) != value for key, value in expected.items()):
            return []
        assert kwargs['prefer'] == 'return=representation'
        old = copy.deepcopy(self.row)
        self.row.update(kwargs['payload'])
        return [old if self.wrong_receipt else copy.deepcopy(self.row)]


def next_plan(backend):
    payload = pickups._job_payload(backend.row)
    payload['_daily_pickup']['scheduled_date'] = '2026-10-12'
    return payload


def test_reschedule_persists_plan_with_installed_rpc_that_ignores_payload(monkeypatch):
    b = OldRpcBackend()
    store = pickups.Store(b)
    assert pickups._reschedule_expired(store, store.rows(), at(12, 21))
    assert pickups._job_date(store.rows()[0]) == '2026-10-12'
    assert pickups._parcel_count(b.row) == 2
    assert b.row['attempted'] == 0 and b.row['dispatch_id'] is None
    assert '2026-10-09' in b.row['error'] and '2026-10-12' in b.row['error']
    assert b.row['claim_token'] is None
    assert not any(path.endswith('/update_inpost_pickup') for path, _ in b.calls)


def test_lost_lease_does_not_overwrite_another_worker_plan():
    b = OldRpcBackend()
    b.lose_lease = True
    with pytest.raises(RuntimeError, match='Nie potwierdzono zapisu'):
        pickups.Store(b).replace_payload(b.row.copy(), next_plan(b))
    assert pickups._job_date(b.row) == '2026-10-09'
    assert b.row['claim_token'] == 'new-owner'


def test_unverified_server_receipt_is_not_reported_as_success():
    b = OldRpcBackend()
    b.wrong_receipt = True
    with pytest.raises(RuntimeError, match='Nie potwierdzono zapisu'):
        pickups.Store(b).replace_payload(b.row.copy(), next_plan(b))


@pytest.mark.parametrize('state,attempted,dispatch', [('unknown', 1, None), ('sent', 1, '900'), ('unknown', 0, None)])
def test_newly_observed_uncertain_or_accepted_job_cannot_be_rescheduled(state, attempted, dispatch):
    b = OldRpcBackend()
    stale = b.row.copy()
    b.row.update(state=state, attempted=attempted, dispatch_id=dispatch)
    assert not pickups.Store(b).replace_payload(stale, next_plan(b))
    assert pickups._job_date(b.row) == '2026-10-09'
    assert b.row['state'] == state and b.row['attempted'] == attempted
    assert not any(kwargs.get('method') == 'PATCH' for _, kwargs in b.calls)


def test_waiting_pickup_after_deadline_is_visibly_a_problem():
    b = OldRpcBackend()
    state = pickups.dashboard_state(b, now=at(12, 21))
    assert state['today']['state'] == 'problem'
    assert 'Kurier nie jest zamówiony' in state['today']['error']
    assert not state['can_order']


def test_short_unattempted_retry_backoff_is_preserved(monkeypatch):
    b = OldRpcBackend()
    monkeypatch.setattr(pickups.time, 'time', lambda: 1000)
    store = pickups.Store(b)
    claimed = store.claim('41')
    store.update(claimed, state='pending', next_check=1010)
    assert b.row['next_check'] == 1010
    store.update(store.claim('41'), state='unknown', attempted=1)
    assert b.row['next_check'] == 1060


def test_reschedule_preserves_address_and_parcel_count_changed_before_claim():
    b = OldRpcBackend()
    stale = b.row.copy()
    proposed = next_plan(b)
    current = pickups._job_payload(b.row)
    current['street'] = 'Nowa 2'
    current['_daily_pickup']['parcel_count'] = 4
    b.row['pickup_json'] = json.dumps(current)
    assert pickups.Store(b).replace_payload(stale, proposed)
    saved = pickups._job_payload(b.row)
    assert saved['street'] == 'Nowa 2'
    assert saved['_daily_pickup']['parcel_count'] == 4
    assert saved['_daily_pickup']['scheduled_date'] == '2026-10-12'


def test_reschedule_does_not_overwrite_a_newly_changed_day():
    b = OldRpcBackend()
    stale = b.row.copy()
    proposed = next_plan(b)
    current = pickups._job_payload(b.row)
    current['_daily_pickup']['scheduled_date'] = '2026-10-13'
    b.row['pickup_json'] = json.dumps(current)
    assert not pickups.Store(b).replace_payload(stale, proposed)
    assert pickups._job_date(b.row) == '2026-10-13'
