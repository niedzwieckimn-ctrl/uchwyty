import hashlib
from pathlib import Path

import pytest
import requests

import app as backend
import internal_rbac as rbac
import orderchamp_http as http
from orderchamp_client import OrderchampClient, OrderchampError
from test_internal_rbac import isolated, _create_actor, _legacy_admin_session
from test_orderchamp_stock import client_for, connected, found, missing, Response, TOKEN

CONNECTION = '/api/admin/orderchamp/test-connection'
DRY_RUN = '/api/admin/orderchamp/dry-run'
PROBE_ORDERS = '/api/admin/orderchamp/probe-orders'
PUSH = '/api/admin/orderchamp/push-one'
SEED = '/api/admin/orderchamp/seed-one'
HEADERS = {'X-CSRF-Token': 'test'}


@pytest.fixture
def owner(isolated, monkeypatch):
    _legacy_admin_session(isolated)
    monkeypatch.setenv('ORDERCHAMP_API_TOKEN', TOKEN)
    def blocked(*args, **kwargs):
        raise AssertionError('Network forbidden')
    monkeypatch.setattr(requests.Session, 'request', blocked)
    db = backend.conn()
    db.executemany('INSERT INTO products(id,sku,model,name,created_at) VALUES(?,?,?,?,?)',
        [(1, 'CH010-AB-N28', 'CH010', 'Synthetic', backend.now_iso()),
         (2, 'CH010-AB-N35', 'CH010', 'Synthetic', backend.now_iso())])
    db.executemany('INSERT INTO stock(product_id,qty) VALUES(?,?)', [(1,24),(2,0)])
    db.commit()
    db.close()
    return isolated


def use_client(monkeypatch, *responses):
    client, session, waits = client_for(*responses)
    monkeypatch.setattr(http, 'http_client', lambda budget: client)
    return client, session, waits


@pytest.mark.parametrize('path', [CONNECTION, DRY_RUN, PROBE_ORDERS])
def test_anonymous_and_customer_credentials_cannot_call_endpoint(isolated, monkeypatch, path):
    monkeypatch.setattr(http, 'http_client', lambda budget: pytest.fail('Auth must run first'))
    response = isolated.post(path, json={'sku':'CH010-AB-N28'},
        headers={'Authorization':'Bearer customer-token', 'X-Role':'OWNER', **HEADERS})
    assert response.status_code == 401


@pytest.mark.parametrize('role,actor_type', [('MAGAZYN','HUMAN'), ('KSIEGOWOSC','HUMAN'),
                                           ('AI_OWNER_ASSISTANT','AI_AGENT'), ('SYSTEM_DATA_SYNC','SYSTEM')])
def test_only_real_human_owner_role_is_allowed(owner, monkeypatch, role, actor_type):
    actor_id = _create_actor(actor_type, role)
    _legacy_admin_session(owner, actor_id)
    monkeypatch.setattr(http, 'http_client', lambda budget: pytest.fail('No Orderchamp calls allowed'))
    response = owner.post(CONNECTION + '?role=OWNER', json={}, headers={**HEADERS,'X-Role':'OWNER'})
    assert response.status_code == 403


def test_disabled_owner_is_denied(owner):
    db = backend.conn()
    db.execute("UPDATE internal_actors SET status='disabled' WHERE actor_id=?", (rbac.BOOTSTRAP_OWNER_ACTOR_ID,))
    db.commit(); db.close()
    assert owner.post(CONNECTION, json={}, headers=HEADERS).status_code == 401


@pytest.mark.parametrize('headers', [{}, {'X-CSRF-Token':'wrong'}, {'X-CSRF-Token':'ą'},
                                   {**HEADERS,'Origin':'https://other.example'}])
def test_existing_csrf_and_origin_protection(owner, monkeypatch, headers):
    monkeypatch.setattr(http, 'http_client', lambda budget: pytest.fail('No external call before CSRF'))
    assert owner.post(CONNECTION, json={}, headers=headers).status_code == 403


def test_connection_calls_existing_client(owner, monkeypatch):
    client, session, _ = use_client(monkeypatch, connected())
    original = client.test_connection
    called = []
    def observed():
        called.append(True)
        return original()
    monkeypatch.setattr(client, 'test_connection', observed)
    response = owner.post(CONNECTION, json={}, headers=HEADERS)
    assert response.status_code == 200
    assert response.get_json() == {'connected':True, 'products_read':True, 'write_checked':False}
    assert called == [True] and session.closed
    assert response.headers['Cache-Control'] == 'no-store'


def test_orders_probe_reads_count_without_customer_data_or_write(owner, monkeypatch):
    _, session, _ = use_client(monkeypatch, Response({'data': {'orders': {
        'totalCount': 1, 'nodes': [{'id': 'order-1', 'createdAt': '2026-09-29T08:00:00Z',
                                    'updatedAt': '2026-09-29T08:01:00Z', 'status': 'AWAITING_FULFILMENT',
                                    'isConfirmed': True, 'isCancelled': False}]}}}))
    response = owner.post(PROBE_ORDERS, json={}, headers=HEADERS)
    assert response.status_code == 200
    assert response.get_json()['all_order_count'] == 1
    assert response.get_json()['writes_enabled'] is False
    assert len(session.calls) == 1 and session.calls[0][1]['json']['query'].startswith('query ')
    assert 'customer' not in str(response.get_json()).lower()
    assert session.closed


def test_stock_push_requires_owner_csrf_and_backend_preconditions(owner, monkeypatch):
    calls = []
    client, session, _ = use_client(monkeypatch)
    monkeypatch.setattr(http, 'push_one_stock', lambda supplied_client, path, **kwargs:
                        calls.append(kwargs) or {'ok': True, 'status': 'VERIFIED', 'wrote': True})
    body = {'sku': 'CH010-AB-N28', 'expected_local': 24,
            'expected_remote_updated_at': '2026-09-29T08:00:00Z'}
    assert owner.post(PUSH, json=body).status_code == 403
    assert not calls
    response = owner.post(PUSH, json=body, headers=HEADERS)
    assert response.status_code == 200
    assert response.get_json()['status'] == 'VERIFIED'
    assert calls == [{'sku': body['sku'], 'expected_local': 24,
                      'expected_remote_updated_at': body['expected_remote_updated_at']}]
    assert session.closed


def test_seed_one_requires_owner_csrf_and_explicit_single_sku(owner, monkeypatch):
    calls = []
    _, session, _ = use_client(monkeypatch)
    monkeypatch.setattr(http, 'push_one_stock', lambda supplied_client, path, **kwargs:
                        calls.append(kwargs) or {'ok': True, 'status': 'VERIFIED', 'wrote': True})
    body = {'sku': 'CH010-AB-N28', 'expected_local': 24,
            'expected_remote_updated_at': '2026-09-29T08:00:00Z'}
    assert owner.post(SEED, json=body).status_code == 403
    assert owner.post(SEED, json={**body, 'all': True}, headers=HEADERS).status_code == 409
    assert not calls
    response = owner.post(SEED, json=body, headers=HEADERS)
    assert response.status_code == 200
    assert calls == [{**body, 'initial_seed': True}]
    assert session.closed


def test_single_dry_run_reuses_service_no_writes_or_files(owner, monkeypatch):
    _, session, _ = use_client(monkeypatch, connected(), found('CH010-AB-N28'))
    original = http.dry_run_stock_sync
    calls = []
    def observe(client, db, **kwargs):
        calls.append(kwargs)
        return original(client, db, **kwargs)
    monkeypatch.setattr(http, 'dry_run_stock_sync', observe)
    path = Path(backend.DB_PATH)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    before_files = set(path.parent.iterdir())
    response = owner.post(DRY_RUN, json={'sku':'CH010-AB-N28'}, headers=HEADERS)
    data = response.get_json()
    assert response.status_code == 200
    assert calls == [{'sku':'CH010-AB-N28'}]
    assert data['rows'][0]['local_available'] == data['rows'][0]['would_send'] == 24
    assert data['rows'][0]['remote']['id'] == 'variant-CH010-AB-N28'
    assert data['writes_enabled'] is False and data['summary']['synchronized'] == 0
    assert 'local_database' not in data
    assert all(call[1]['json']['query'].startswith('query ') for call in session.calls)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert set(path.parent.iterdir()) == before_files


@pytest.mark.parametrize('sku', ['', None, [], ' A ', 'X'*257])
def test_invalid_sku_is_controlled_error(owner, sku):
    response = owner.post(DRY_RUN, json={'sku':sku}, headers=HEADERS)
    assert response.status_code == 400 and response.get_json()['error_code'] == 'INVALID_SKU'


def test_unknown_local_sku_is_404(owner, monkeypatch):
    _, session, _ = use_client(monkeypatch)
    response = owner.post(DRY_RUN, json={'sku':'UNKNOWN'}, headers=HEADERS)
    assert response.status_code == 404
    assert response.get_json()['error_code'] == 'LOCAL_SKU_NOT_FOUND'
    assert session.calls == []


def test_missing_remote_sku_has_report_and_controlled_404(owner, monkeypatch):
    use_client(monkeypatch, connected(), missing())
    response = owner.post(DRY_RUN, json={'sku':'CH010-AB-N28'}, headers=HEADERS)
    assert response.status_code == 404
    row = response.get_json()['rows'][0]
    assert row['status'] == 'MISSING' and 'REMOTE_SKU_NOT_FOUND' in row['warnings']


@pytest.mark.parametrize('failure', [OrderchampError(TOKEN), RuntimeError('Authorization: Bearer '+TOKEN)])
def test_exception_text_never_exposed(owner, monkeypatch, caplog, failure):
    def fail(budget):
        raise failure
    monkeypatch.setattr(http, 'http_client', fail)
    response = owner.post(CONNECTION, json={}, headers=HEADERS)
    assert response.status_code in (500,502)
    assert TOKEN not in response.get_data(as_text=True) + caplog.text
    assert 'Authorization' not in response.get_data(as_text=True)


def test_upstream_secret_in_error_is_not_returned(owner, monkeypatch):
    use_client(monkeypatch, Response({'errors':[{'message':TOKEN}]}))
    response = owner.post(CONNECTION, json={}, headers=HEADERS)
    assert response.status_code == 502 and response.get_json()['error_code'] == 'GRAPHQL_ERROR'
    assert TOKEN not in response.get_data(as_text=True)


def test_json_string_escaping_cannot_bypass_redaction(owner, monkeypatch):
    token = 'synthetic-"quote\\token'
    monkeypatch.setenv('ORDERCHAMP_API_TOKEN', token)
    client, _, _ = use_client(monkeypatch)
    monkeypatch.setattr(client, 'test_connection', lambda: {'value': token, token: 'secret'})
    response = owner.post(CONNECTION, json={}, headers=HEADERS)
    assert response.get_json() == {'value':'[REDACTED]', '[REDACTED]':'secret'}


def test_pagination_calls_same_service_once_per_request(owner, monkeypatch):
    _, first_session, _ = use_client(monkeypatch, connected(), found('CH010-AB-N28'))
    first = owner.post(DRY_RUN, json={}, headers=HEADERS)
    page = first.get_json()['pagination']
    assert first.status_code == 200 and len(first_session.calls) == 2
    assert page['total_sku'] == 2 and page['next_offset'] == 1
    _, second_session, _ = use_client(monkeypatch, connected(), found('CH010-AB-N35', quantity=0, available=0))
    second = owner.post(DRY_RUN, json={'offset':page['next_offset'], 'limit':1,
        'catalog_version':page['catalog_version']}, headers=HEADERS)
    assert second.status_code == 200 and len(second_session.calls) == 2
    assert second.get_json()['rows'][0]['sku'] == 'CH010-AB-N35'
    assert second.get_json()['pagination']['has_more'] is False


def test_catalog_change_detected_instead_of_silent_skip(owner, monkeypatch):
    use_client(monkeypatch, connected(), found('CH010-AB-N28'))
    page = owner.post(DRY_RUN, json={}, headers=HEADERS).get_json()['pagination']
    db = backend.conn()
    db.execute('UPDATE products SET archived=1 WHERE id=1'); db.commit(); db.close()
    response = owner.post(DRY_RUN, json={'offset':1, 'catalog_version':page['catalog_version']}, headers=HEADERS)
    assert response.status_code == 409 and response.get_json()['error_code'] == 'CATALOG_CHANGED_RESTART'


@pytest.mark.parametrize('body', [{'limit':25}, {'limit':True}, {'offset':True}, {'offset':-1},
    {'offset':1}, {'catalog_version':'bad'}, {'sku':'A','offset':0}, {'write':True}, {'db':'elsewhere.db'}, []])
def test_batch_cannot_be_unbounded_or_choose_database(owner, body):
    assert owner.post(DRY_RUN, json=body, headers=HEADERS).status_code == 400


def test_deadline_stops_retry_without_sleeping_past_http_budget():
    clock = [0.0]
    waits = []
    budget = http.HttpBudget(12, clock=lambda: clock[0], sleeper=waits.append)
    budget.sleep(1)
    clock[0] = 11.9
    with pytest.raises(OrderchampError, match='HTTP_TIME_BUDGET_EXCEEDED'):
        budget.sleep(2)
    assert waits == [1]


def test_http_uses_short_transport_timeouts_and_same_queries(monkeypatch):
    clock = [0.0]
    seen = []
    budget = http.HttpBudget(12, clock=lambda: clock[0], sleeper=lambda _: None)
    transport = http.BudgetSession(budget)
    def fake_post(self, url, **kwargs):
        seen.append(kwargs)
        return connected()
    monkeypatch.setattr(requests.Session, 'post', fake_post)
    client = OrderchampClient(TOKEN, session=transport, sleep=budget.sleep, monotonic=budget.clock)
    assert client.test_connection()['connected']
    assert seen[0]['timeout'] == (2,3)
    assert seen[0]['json']['query'].startswith('query ')


def test_http_timeout_returns_json_and_releases_busy_lock(owner, monkeypatch):
    def too_slow(budget):
        raise OrderchampError('HTTP_TIME_BUDGET_EXCEEDED')
    monkeypatch.setattr(http, 'http_client', too_slow)
    assert owner.post(CONNECTION, json={}, headers=HEADERS).status_code == 504
    use_client(monkeypatch, connected())
    assert owner.post(CONNECTION, json={}, headers=HEADERS).status_code == 200


def test_admin_page_has_controls_and_csrf_not_api_token(owner):
    response = owner.get('/admin/orderchamp')
    text = response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'Test połączenia' in text and 'CH010-AB-N28' in text
    assert 'name="csrf-token" content="test"' in text
    assert TOKEN not in text


def test_get_does_not_execute_post_endpoints(owner, monkeypatch):
    monkeypatch.setattr(http, 'http_client', lambda budget: pytest.fail('GET must not execute diagnostics'))
    assert owner.get(CONNECTION).status_code == 405
    assert owner.get(DRY_RUN).status_code == 405


def test_http_long_retry_after_returns_immediately_with_budget_error(owner, monkeypatch):
    calls = []
    def throttled(self, url, **kwargs):
        calls.append(kwargs)
        return Response(status=429, headers={'Retry-After':'60'})
    monkeypatch.setattr(requests.Session, 'post', throttled)
    response = owner.post(CONNECTION, json={}, headers=HEADERS)
    assert response.status_code == 504
    assert response.get_json()['error_code'] == 'HTTP_TIME_BUDGET_EXCEEDED'
    assert len(calls) == 1
