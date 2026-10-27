"""Focused regression checks for the architect/B2B role boundary."""

import os
import urllib.error
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('APP_DATA_DIR', str(Path(__file__).with_name('.architect-test-data')))

import app as backend
import routes.architects as architect_routes
import routes.orders as order_routes


def test_architect_session_cannot_read_b2b_orders():
    user = {'id':'0b000000-0000-0000-0000-000000000001',
            'email':'architect@example.com','name':'Architect'}
    calls=[]

    def remote(path, **kwargs):
        calls.append(path)
        if path == '/rest/v1/client_accounts': return []
        raise AssertionError(f'Unexpected B2B read: {path}')

    with patch.object(backend, '_authenticated_client_user', return_value=user), \
         patch.object(backend, 'supabase_enabled', return_value=True), \
         patch.object(backend, 'supabase_request', side_effect=remote):
        response=backend.app.test_client().get('/api/client/orders')
    assert response.status_code == 403
    assert calls == ['/rest/v1/client_accounts']


def test_distributor_orders_are_scoped_by_membership_not_email():
    user = {'id':'0b000000-0000-0000-0000-000000000002',
            'email':'employee@example.com','name':'Employee'}
    calls=[]

    def remote(path, **kwargs):
        calls.append((path,kwargs.get('params')))
        if path == '/rest/v1/client_accounts':
            return [{'customer_id':17,'is_active':True}]
        if path == '/rest/v1/orders':
            assert kwargs['params']['customer_id'] == 'eq.17'
            return [{'id':51,'customer_email':'company@example.com'}]
        raise AssertionError(f'Unexpected read: {path}')

    with patch.object(backend, '_authenticated_client_user', return_value=user), \
         patch.object(backend, 'supabase_enabled', return_value=True), \
         patch.object(backend, 'supabase_request', side_effect=remote), \
         patch.object(order_routes, 'supabase_enabled', return_value=True), \
         patch.object(order_routes, 'supabase_request', side_effect=remote):
        response=backend.app.test_client().get('/api/client/orders')
    assert response.status_code == 200
    assert response.get_json()['orders'][0]['id'] == 51
    assert len(calls) == 2


def test_admin_architect_page_renders_without_program_data():
    with patch.dict(backend.app.config,{'SECRET_KEY':'test-only-session-key'}):
        client=backend.app.test_client()
        with client.session_transaction() as session:
            session['admin_authenticated']=True
            session['csrf_token']='test-only-token'
        with patch.object(architect_routes,'supabase_request',return_value=[]):
            response=client.get('/architects')
    assert response.status_code == 200
    assert 'Treść katalogu architekta'.encode('utf-8') in response.data


def test_admin_architect_list_links_to_profile_and_shows_stats():
    architect_id = '0b000000-0000-0000-0000-000000000003'
    def remote(path, **_kwargs):
        if path == '/rest/v1/architect_profiles':
            return [{'id':architect_id,'first_name':'Anna','last_name':'Kowalska',
                     'studio_name':'Studio K','email':'a@example.com','instagram':'@studio',
                     'status':'approved','sample_entitlements':1,'created_at':'2026-10-27'}]
        return []
    with patch.dict(backend.app.config,{'SECRET_KEY':'test-only-session-key'}):
        client=backend.app.test_client()
        with client.session_transaction() as session:
            session['admin_authenticated']=True
            session['csrf_token']='test-only-token'
        with patch.object(architect_routes,'supabase_request',side_effect=remote):
            response=client.get('/architects?q=Studio')
    assert response.status_code == 200
    assert f'/architects/{architect_id}'.encode() in response.data
    assert 'Studio K'.encode() in response.data


def test_parallel_architect_registration_returns_existing_profile_after_conflict():
    architect_id = '0b000000-0000-0000-0000-000000000004'
    user = {
        'id': architect_id, 'email': 'architect@example.com', 'email_confirmed': True,
        'metadata': {'first_name': 'Test', 'last_name': 'Architekt',
                     'studio_name': 'Pracownia testowa', 'phone': '000000000',
                     'instagram': '@test', 'shipping_address': 'Testowa 1, Warszawa'},
    }
    saved = {'id': architect_id, 'status': 'pending_verification'}
    reads = []

    def remote(path, **kwargs):
        assert path == '/rest/v1/architect_profiles'
        if kwargs.get('method') == 'POST':
            raise urllib.error.HTTPError('https://example.invalid', 409, 'Conflict', {}, None)
        reads.append(True)
        return [] if len(reads) == 1 else [saved]

    with patch.object(backend, '_authenticated_client_user', return_value=user), \
         patch.object(architect_routes, 'supabase_request', side_effect=remote):
        response = backend.app.test_client().post('/api/architect/me', json={})
    assert response.status_code == 200
    assert response.get_json()['profile'] == saved
    assert len(reads) == 2
