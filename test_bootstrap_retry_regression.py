from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import threading
from types import SimpleNamespace

import pytest
from werkzeug.exceptions import ServiceUnavailable

import app as backend
from test_business_freshness import _remote_rows


@pytest.fixture
def bootstrap(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, 'DB_PATH', str(tmp_path / 'bootstrap.db'))
    backend.init_db()
    monkeypatch.setattr(backend, 'SUPABASE_URL', 'https://example.invalid')
    monkeypatch.setattr(backend, 'SUPABASE_SERVICE_ROLE_KEY', 'isolated-key')
    monkeypatch.setattr(backend, '_run_post_pull_reconciliation', lambda: None)
    monkeypatch.setattr(backend, '_supabase_sync_state', {
        'running': False, 'pull_running': False, 'last_started_ts': 0,
        'last_pull_finished_ts': 0, 'initial_pull_attempted': False,
    })
    monkeypatch.setattr(backend, 'SUPABASE_BACKGROUND_PULL_INTERVAL_SEC', 60)
    rows = _remote_rows()
    calls = Counter()
    failed = {'china_documents'}
    def select(table, **kwargs):
        calls[table] += 1
        if table in failed:
            raise RuntimeError('isolated table read failure')
        return [dict(row) for row in rows.get(table, [])]
    monkeypatch.setattr(backend, 'supabase_select_rows', select)
    return rows, calls, failed


def request_bootstrap():
    with backend.app.test_request_context('/', method='GET'):
        return backend.maybe_pull_shared_from_supabase(required=True)


def test_partial_pull_is_not_repeated_by_four_waiting_requests(bootstrap):
    rows, calls, failed = bootstrap
    barrier = threading.Barrier(4)
    def visit(_):
        barrier.wait(timeout=5)
        return request_bootstrap()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(visit, range(4)))
    assert calls['products'] == 1
    assert calls['china_documents'] == 1
    assert sum(calls.values()) == len(backend.SUPABASE_PULL_TABLES)
    assert sum(isinstance(result, dict) and not result['ok'] for result in results) == 1
    assert results.count((False, 'throttled')) == 3
    assert backend._supabase_sync_state['initial_pull_attempted'] is True
    assert backend._local_supabase_data_present() is True
    assert backend._local_supabase_bootstrap_complete() is False


def test_empty_failed_pull_still_returns_503_without_success_marker(bootstrap):
    rows, calls, failed = bootstrap
    rows.clear()
    with pytest.raises(ServiceUnavailable, match='DATA_UNAVAILABLE'):
        request_bootstrap()
    assert backend._supabase_sync_state['initial_pull_attempted'] is False
    assert backend._local_supabase_bootstrap_complete() is False


@pytest.mark.parametrize('repair_remote', [False, True])
def test_retry_runs_in_background_and_only_full_success_is_durable(bootstrap, monkeypatch, repair_remote):
    rows, calls, failed = bootstrap
    first = request_bootstrap()
    assert not first['ok']
    assert backend._local_supabase_bootstrap_complete() is False
    queued = []
    class QueuedThread:
        def __init__(self, *, target, daemon):
            self.target = target
        def start(self):
            queued.append(self.target)
    monkeypatch.setattr(backend, 'threading', SimpleNamespace(Thread=QueuedThread))
    assert request_bootstrap() == (False, 'throttled')
    assert not queued
    assert calls['products'] == 1
    backend._supabase_sync_state['last_pull_finished_ts'] = 0
    if repair_remote:
        failed.clear()
    assert request_bootstrap() == (True, 'started')
    assert len(queued) == 1
    assert calls['products'] == 1  # Request returned without running network work.
    assert request_bootstrap() == (False, 'already_running')
    queued[0]()
    assert calls['products'] == 2
    assert backend._local_supabase_bootstrap_complete() is repair_remote
    assert backend._supabase_sync_state['pull_running'] is False
    assert backend._supabase_sync_state['last_pull_result']['ok'] is repair_remote
