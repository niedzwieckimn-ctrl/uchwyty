"""Real auth boundaries with isolated credentials and a controlled transport."""
import json
import threading
import urllib.error
from concurrent.futures import ThreadPoolExecutor

import pytest
import app as b


class Response:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps({'id': 'test-user', 'email': 'qa@example.invalid',
                           'email_confirmed_at': '2026-01-01T00:00:00Z'}).encode()


@pytest.fixture(autouse=True)
def auth_state(monkeypatch):
    monkeypatch.setattr(b, '_client_auth_cache', {})
    monkeypatch.setattr(b, '_rate_hits', {})
    monkeypatch.setattr(b, 'SUPABASE_URL', 'https://auth.example.invalid')
    monkeypatch.setattr(b, 'SUPABASE_ANON_KEY', 'test-public-key')
    monkeypatch.setattr(b, 'SUPABASE_SERVICE_ROLE_KEY', '')


def authenticate(token):
    with b.app.test_request_context(headers={'Authorization': 'Bearer ' + token}):
        return b._authenticated_client_user()


def test_slow_user_does_not_block_another_user(monkeypatch):
    first_entered, release_first, second_entered = (threading.Event() for _ in range(3))
    def transport(request, **kwargs):
        if request.get_header('Authorization') == 'Bearer user-a':
            first_entered.set()
            assert release_first.wait(5)
        else:
            second_entered.set()
        return Response()
    monkeypatch.setattr(b.urllib.request, 'urlopen', transport)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(authenticate, 'user-a')
        assert first_entered.wait(2)
        second = pool.submit(authenticate, 'user-b')
        try:
            assert second_entered.wait(1), 'One user holds a global lock during the Auth network call'
        finally:
            release_first.set()
        assert first.result()['id'] == second.result()['id'] == 'test-user'


def test_parallel_requests_for_same_session_share_one_verification(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def transport(request, **kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return Response()
    monkeypatch.setattr(b.urllib.request, 'urlopen', transport)
    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs = [pool.submit(authenticate, 'same-session') for _ in range(8)]
        assert entered.wait(2)
        release.set()
        assert all(job.result()['id'] == 'test-user' for job in jobs)
    assert len(calls) == 1


@pytest.mark.parametrize('path', ['/api/architect/me', '/api/client/profile'])
@pytest.mark.parametrize('failure', [503, 429, 'timeout'])
def test_auth_outage_is_not_reported_as_invalid_session(monkeypatch, path, failure):
    def transport(request, **kwargs):
        if failure == 'timeout':
            raise TimeoutError('test timeout')
        raise urllib.error.HTTPError(request.full_url, failure, 'test outage', {}, None)
    monkeypatch.setattr(b.urllib.request, 'urlopen', transport)
    response = b.app.test_client().get(path, headers={'Authorization': 'Bearer outage-session'})
    assert response.status_code == 503
    assert response.get_json()['ok'] is False


@pytest.mark.parametrize('status', [401, 403])
def test_rejected_token_stays_unauthorized(monkeypatch, status):
    def transport(request, **kwargs):
        raise urllib.error.HTTPError(request.full_url, status, 'rejected', {}, None)
    monkeypatch.setattr(b.urllib.request, 'urlopen', transport)
    response = b.app.test_client().get('/api/client/profile', headers={'Authorization': 'Bearer invalid'})
    assert response.status_code == 401


def test_admin_password_supports_unicode_and_exact_whitespace(monkeypatch):
    monkeypatch.setattr(b, 'ADMIN_PASSWORD_HASH', '')
    monkeypatch.setattr(b, 'ADMIN_PASSWORD', '  żółć-test-only  ')
    assert b._admin_password_ok('  żółć-test-only  ')
    assert not b._admin_password_ok('żółć-test-only')
    assert not b._admin_password_ok('wrong')


@pytest.fixture
def admin_client(tmp_path, monkeypatch):
    monkeypatch.setattr(b, 'DB_PATH', str(tmp_path / 'admin-auth.db'))
    monkeypatch.setattr(b, 'supabase_enabled', lambda: False)
    monkeypatch.setitem(b.app.config, 'SECRET_KEY', 'test-only-session-key')
    monkeypatch.setattr(b, 'ADMIN_USERNAME', 'admin')
    monkeypatch.setattr(b, 'ADMIN_PASSWORD', 'correct-test-only')
    monkeypatch.setattr(b, 'ADMIN_PASSWORD_HASH', '')
    b.init_db()
    return b.app.test_client()


def test_admin_login_sets_session_and_reaches_protected_health(admin_client):
    response = admin_client.post('/login?next=/api/internal/external-executions/health',
                                data={'username': 'admin', 'password': 'correct-test-only'})
    assert response.status_code == 302
    assert 'HttpOnly' in response.headers['Set-Cookie']
    assert 'Secure' in response.headers['Set-Cookie']
    health = admin_client.get(response.headers['Location'])
    assert health.status_code == 200
    assert health.get_json()['ok'] is True


def test_failed_session_binding_never_authenticates_admin(admin_client, monkeypatch):
    def broken_binding(session):
        raise RuntimeError('test database unavailable')
    monkeypatch.setattr(b, 'bind_bootstrap_owner_session', broken_binding)
    response = admin_client.post('/login', data={'username': 'admin', 'password': 'correct-test-only'})
    assert response.status_code == 503
    with admin_client.session_transaction() as state:
        assert not state.get('admin_authenticated')
    assert admin_client.get('/api/internal/external-executions/health').status_code == 401


def test_admin_rejection_logs_only_diagnostic_flags(admin_client, caplog):
    response = admin_client.post('/login', data={'username': 'admin', 'password': 'wrong-test-only'})
    assert response.status_code == 200
    assert 'Nieprawidłowy login lub hasło' in response.get_data(as_text=True)
    assert 'username_match=True' in caplog.text
    assert 'wrong-test-only' not in caplog.text
    assert 'correct-test-only' not in caplog.text
    assert admin_client.get('/api/internal/external-executions/health').status_code == 401
