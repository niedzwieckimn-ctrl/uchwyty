"""Real packing form/SQLite with an in-memory CAS store; no remote services."""
import copy
import json
import re

import pytest

import app as backend
import reconciliation_store
import packing_versions
from test_multi_order_packing_agent import multi_order_flow


@pytest.fixture
def packing_cloud(multi_order_flow, monkeypatch):
    cloud, calls = {}, []
    def remote(path, method='GET', payload=None, **kwargs):
        calls.append((method, path, copy.deepcopy(payload or kwargs)))
        if path == '/rest/v1/fulfillment_reconciliation' and method == 'GET':
            if kwargs['params'].get('select') == 'order_id,revision':
                ids = [int(i) for i in kwargs['params']['order_id'][4:-1].split(',')]
                return [{'order_id': oid, 'revision': cloud[oid]['revision']} for oid in ids if oid in cloud]
            oid = int(kwargs['params']['order_id'].split('.')[-1])
            return [copy.deepcopy(cloud[oid])] if oid in cloud else []
        assert path == '/rest/v1/rpc/save_fulfillment_reconciliation', path
        oid = payload['p_order_id']
        revision = cloud.get(oid, {}).get('revision', 0)
        if revision != payload['p_expected_revision']:
            return {'saved': False, 'revision': revision}
        cloud[oid] = {'revision': revision + 1, 'payload': copy.deepcopy(payload['p_payload'])}
        return {'saved': True, 'revision': revision + 1}
    monkeypatch.setattr(backend, 'supabase_enabled', lambda: True)
    monkeypatch.setattr(backend, 'supabase_request', remote)
    monkeypatch.setattr(backend, 'sync_local_rows_to_supabase', lambda *a, **kw: None)
    monkeypatch.setattr(backend, '_send_orders_packed_email', lambda *a, **kw: {'ok': True})
    client = backend.app.test_client()
    with client.session_transaction() as session:
        session['admin_authenticated'] = True
        session['csrf_token'] = 'csrf'
    return client, cloud, calls


def post(client, root=103):
    from packing_correction import form_version
    db = backend.conn()
    try:
        token = form_version(db, root)
    finally:
        db.close()
    return client.post(f'/orders/{root}/packing-list', data={
        'packing_form_version': token,
        'csrf_token': 'csrf', 'carrier': 'pending',
        'pack_qty_1001': '3', 'pack_qty_1002': '0', 'pack_qty_1003': '2'})


def pending(oid=101):
    reconciliation_store.stage(backend, oid)


def test_pending_secondary_member_is_reconciled_before_combined_packing(packing_cloud):
    client, cloud, calls = packing_cloud
    pending()
    response = post(client)
    assert response.status_code == 302, response.get_data(as_text=True)
    assert '/orders/103/invoice' in response.location
    db = backend.conn()
    try:
        assert not db.execute('SELECT 1 FROM fulfillment_reconciliation_pending').fetchone()
        assert db.execute('SELECT COUNT(*) FROM packing_batches').fetchone()[0] == 1
        assert list(map(tuple, db.execute('SELECT order_id,qty FROM packing_allocations ORDER BY order_id'))) == [(101, 3), (103, 2)]
        assert [r[0] for r in db.execute('SELECT qty FROM stock ORDER BY product_id')] == [3, 0, 2]
    finally:
        db.close()
    assert set(cloud) == {101, 103}
    assert sum(method == 'POST' for method, _, _ in calls) == 3
    revision_reads = [data['params']['order_id'] for method, _, data in calls
                      if method == 'GET' and data['params']['select'] == 'order_id,revision']
    assert revision_reads == ['in.(103)']
    full_reads = [data['params']['order_id'] for method, _, data in calls
                  if method == 'GET' and data['params']['select'] == 'revision,payload']
    # One recovery read, then a fresh equality read before each of three CAS writes.
    assert full_reads == ['eq.101', 'eq.101', 'eq.101', 'eq.103']
    for index, (method, _, data) in enumerate(calls):
        if method == 'POST':
            assert calls[index - 1] == ('GET', '/rest/v1/fulfillment_reconciliation', {
                'params': {'order_id': 'eq.' + str(data['p_order_id']), 'select': 'revision,payload'}})


def test_ack_lost_after_remote_save_is_not_written_again(packing_cloud):
    client, cloud, calls = packing_cloud
    revision, payload = reconciliation_store.stage(backend, 101)
    cloud[101] = {'revision': revision + 1, 'payload': payload}
    assert post(client).status_code == 302
    assert sum(method == 'POST' for method, _, _ in calls) == 2
    assert cloud[101]['revision'] == 2


def test_real_revision_conflict_preserves_pending_and_form(packing_cloud):
    client, cloud, calls = packing_cloud
    revision, payload = reconciliation_store.stage(backend, 101)
    cloud[101] = {'revision': revision + 1, 'payload': {'other_writer': True}}
    response = post(client)
    assert response.status_code == 409
    html = response.get_data(as_text=True)
    assert 'Wybrane ilości pozostają' in html
    assert re.search(r'name="pack_qty_1001" value="3"', html)
    db = backend.conn()
    try:
        assert db.execute('SELECT COUNT(*) FROM packing_batches').fetchone()[0] == 0
        saved = db.execute('SELECT payload FROM fulfillment_reconciliation_pending WHERE order_id=101').fetchone()
        assert json.loads(saved[0]) == payload
        assert db.execute('SELECT status FROM orders WHERE id=101').fetchone()[0] == 'partially_shipped'
        assert db.execute('SELECT qty FROM stock WHERE product_id=1').fetchone()[0] == 3
    finally:
        db.close()
    assert cloud[101]['payload'] == {'other_writer': True}


def test_preflight_timeout_does_not_publish_new_list(packing_cloud, monkeypatch):
    client, _, _ = packing_cloud
    pending()
    def timeout(*a, **kw):
        raise TimeoutError('remote unavailable')
    monkeypatch.setattr(backend, 'supabase_request', timeout)
    response = post(client)
    assert response.status_code == 503
    assert 'Nowa lista nie została zapisana' in response.get_data(as_text=True)
    db = backend.conn()
    try:
        assert db.execute('SELECT COUNT(*) FROM packing_batches').fetchone()[0] == 0
        assert db.execute('SELECT 1 FROM fulfillment_reconciliation_pending WHERE order_id=101').fetchone()
    finally:
        db.close()


def test_publish_timeout_then_retry_keeps_one_batch_and_documents(packing_cloud, monkeypatch):
    client, cloud, calls = packing_cloud
    original = backend.supabase_request
    fail = True
    def interrupt(path, method='GET', **kwargs):
        if fail and method == 'POST':
            raise TimeoutError('save response unavailable')
        return original(path, method=method, **kwargs)
    monkeypatch.setattr(backend, 'supabase_request', interrupt)
    response = post(client)
    assert response.status_code == 503
    assert 'Lista jest zapisana lokalnie' in response.get_data(as_text=True)
    db = backend.conn()
    try:
        original_docs = list(map(tuple, db.execute('SELECT * FROM fulfillment_document_history ORDER BY order_id')))
        assert db.execute('SELECT COUNT(*) FROM packing_batches').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM invoices').fetchone()[0] == 1  # seed only
    finally:
        db.close()
    fail = False
    response = post(client)
    assert response.status_code == 302, response.get_data(as_text=True)
    db = backend.conn()
    try:
        assert db.execute('SELECT COUNT(*) FROM packing_batches').fetchone()[0] == 1
        assert list(map(tuple, db.execute('SELECT * FROM fulfillment_document_history ORDER BY order_id'))) == original_docs
        assert not db.execute('SELECT 1 FROM fulfillment_reconciliation_pending').fetchone()
    finally:
        db.close()
    assert set(cloud) == {101, 103}


def test_removed_member_pending_is_finished_too(packing_cloud):
    client, cloud, calls = packing_cloud
    assert post(client).status_code == 302
    pending(101)
    from packing_correction import form_version
    db = backend.conn()
    token = form_version(db, 103)
    db.close()
    response = client.post('/orders/103/packing-list', data={
        'packing_form_version': token,
        'csrf_token': 'csrf', 'carrier': 'pending', 'pack_qty_1001': '0', 'pack_qty_1003': '2'})
    assert response.status_code == 302, response.get_data(as_text=True)
    db = backend.conn()
    try:
        assert db.execute('SELECT COUNT(*) FROM packing_batches').fetchone()[0] == 2
        assert not db.execute("SELECT 1 FROM fulfillment_documents WHERE order_id=101 AND kind='packing_list'").fetchone()
        assert db.execute('SELECT 1 FROM fulfillment_document_history WHERE order_id=101').fetchone()
        assert not db.execute('SELECT 1 FROM fulfillment_reconciliation_pending').fetchone()
    finally:
        db.close()
    assert not cloud[101]['payload']['fulfillment_documents']


def test_get_does_not_attempt_to_reconcile_pending(packing_cloud):
    client, cloud, calls = packing_cloud
    pending()
    assert client.get('/orders/103/packing-list').status_code == 200
    assert not calls
    assert not cloud


def test_late_pending_race_rolls_back_entire_new_batch(packing_cloud, monkeypatch):
    client, cloud, calls = packing_cloud
    original = packing_versions.prepare_write_evidence
    def racing(b, ids):
        original(b, ids)
        pending(101)
    monkeypatch.setattr(packing_versions, 'prepare_write_evidence', racing)
    response = post(client)
    assert response.status_code == 409
    db = backend.conn()
    try:
        assert db.execute('SELECT COUNT(*) FROM packing_batches').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM fulfillment_documents').fetchone()[0] == 0
        assert db.execute('SELECT 1 FROM fulfillment_reconciliation_pending WHERE order_id=101').fetchone()
        assert db.execute('SELECT status FROM orders WHERE id=103').fetchone()[0] == 'packed'
    finally:
        db.close()
    assert not cloud


def test_clean_packing_batches_preflight_revisions_then_checks_payload_before_each_cas(packing_cloud):
    client, cloud, calls = packing_cloud
    assert post(client).status_code == 302
    assert calls[0] == ('GET', '/rest/v1/fulfillment_reconciliation', {
        'params': {'order_id': 'in.(101,103)', 'select': 'order_id,revision'}})
    assert [data['params'] for method, _, data in calls if method == 'GET'][1:] == [
        {'order_id': 'eq.101', 'select': 'revision,payload'},
        {'order_id': 'eq.103', 'select': 'revision,payload'}]
    assert len(calls) == 5
    for index, (method, _, data) in enumerate(calls):
        if method == 'POST':
            assert calls[index - 1] == ('GET', '/rest/v1/fulfillment_reconciliation', {
                'params': {'order_id': 'eq.' + str(data['p_order_id']), 'select': 'revision,payload'}})
    assert set(cloud) == {101, 103}
    assert all(record['revision'] == 1 for record in cloud.values())
