"""Recovery is limited to one existing staging account and a CSRF-protected owner."""
import pytest
import app as b
from test_auth_resilience import admin_client
from test_internal_rbac import _create_actor

PATH = '/admin/staging/distributor-password'
UID = '0062dcbf-717e-40a7-a820-3f79aefaa33f'
EMAIL = 'dystrybutor.staging@example.com'


@pytest.fixture
def recovery(admin_client, monkeypatch):
    monkeypatch.setattr(b, 'SUPABASE_URL', 'https://irhrzdsjnwjyqfvhiclk.supabase.co')
    monkeypatch.setattr(b, '_rate_hits', {})
    admin_client.post('/login', data={'username':'admin','password':'correct-test-only'})
    with admin_client.session_transaction() as state:
        token = state['csrf_token']
    calls = []
    def provider(path, **kwargs):
        calls.append((path, kwargs))
        return {'id':UID, 'email':EMAIL}
    monkeypatch.setattr(b, 'supabase_request', provider)
    return admin_client, token, calls


def post(recovery, **overrides):
    client, token, _ = recovery
    data = dict(csrf_token=token, password='isolated-test-password', confirmation='isolated-test-password')
    data.update(overrides)
    return client.post(PATH, data=data)


def test_recovery_updates_only_fixed_existing_account(recovery, caplog):
    client, _, calls = recovery
    assert 'name="csrf_token"' in client.get(PATH).get_data(as_text=True)
    result = post(recovery, user_id='other-user', email='other@example.invalid')
    assert result.status_code == 200
    assert 'Hasło zostało zmienione' in result.get_data(as_text=True)
    assert result.headers['Cache-Control'] == 'no-store'
    assert calls == [('/auth/v1/admin/users/'+UID, {'timeout':10}),
                     ('/auth/v1/admin/users/'+UID, {'method':'PUT','payload':{'password':'isolated-test-password'},'timeout':10})]
    assert 'isolated-test-password' not in result.get_data(as_text=True) + caplog.text


@pytest.mark.parametrize('values', [dict(csrf_token='invalid'), dict(password='short'), dict(confirmation='different')])
def test_invalid_forms_do_not_reach_provider(recovery, values):
    assert post(recovery, **values).status_code in (400,403)
    assert not recovery[2]


def test_production_is_disabled(recovery, monkeypatch):
    monkeypatch.setattr(b, 'SUPABASE_URL', 'https://production.example.invalid')
    assert post(recovery).status_code == 404
    assert not recovery[2]


def test_account_identity_mismatch_is_not_modified(recovery, monkeypatch):
    monkeypatch.setattr(b, 'supabase_request', lambda *args, **kwargs: {'id':UID,'email':'other@example.invalid'})
    assert post(recovery).status_code == 409


def test_provider_failure_does_not_leak_credentials(recovery, monkeypatch, caplog):
    def broken(*args, **kwargs):
        raise RuntimeError('isolated-test-password service-role-secret')
    monkeypatch.setattr(b, 'supabase_request', broken)
    result = post(recovery)
    assert result.status_code == 503
    for secret in ('isolated-test-password', 'service-role-secret'):
        assert secret not in result.get_data(as_text=True) + caplog.text


def test_distributor_bearer_does_not_open_admin_form(recovery):
    result = b.app.test_client().get(PATH, headers={'Authorization':'Bearer distributor-token'})
    assert result.status_code == 302
    assert '/login' in result.headers['Location']


def test_non_owner_cannot_change_password(recovery):
    client, _, calls = recovery
    actor = _create_actor('HUMAN', 'MAGAZYN')
    with client.session_transaction() as state:
        state['internal_actor_id'] = actor
    assert post(recovery).status_code == 403
    assert not calls


def test_json_cannot_bypass_form_csrf(recovery):
    client, _, calls = recovery
    assert client.post(PATH, json={'password':'test'}).status_code == 415
    assert not calls
