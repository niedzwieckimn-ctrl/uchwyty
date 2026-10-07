import gzip
import io
import json
import logging
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

import app as b
import inpost_tracking
import supabase_read_cache as cache
from test_business_freshness import _remote_rows


@pytest.fixture
def warm(tmp_path, monkeypatch):
    monkeypatch.setattr(b, 'DB_PATH', str(tmp_path / 'cache.db'))
    b.init_db()
    monkeypatch.setattr(b, 'SUPABASE_URL', 'https://example.invalid')
    monkeypatch.setattr(b, 'SUPABASE_SERVICE_ROLE_KEY', 'isolated-secret')
    monkeypatch.setattr(b, '_supabase_sync_state', {'initial_pull_attempted': True})
    monkeypatch.setattr(b, 'SUPABASE_BACKGROUND_PULL_INTERVAL_SEC', 30)
    monkeypatch.setattr(b.invoice_payment_sync, 'flush_pending', lambda *a, **kw: None)
    monkeypatch.setattr(inpost_tracking, 'flush_pending', lambda *a, **kw: None)
    reconciled = []
    monkeypatch.setattr(b, '_run_post_pull_reconciliation', lambda: reconciled.append(True))
    queued = []
    class QueuedThread:
        def __init__(self, *, target, daemon):
            self.target = target
        def start(self):
            queued.append(self.target)
    monkeypatch.setattr(cache, 'threading', SimpleNamespace(Thread=QueuedThread))
    rows = _remote_rows()
    for table, col in b.SUPABASE_PULL_TABLES:
        b.sqlite_upsert_rows(table, rows.get(table, []), col)
    b._mark_local_supabase_bootstrap_complete()
    calls = []
    failed = set()
    def select(table, order_by='id', **kwargs):
        calls.append((table, kwargs.get('extra_params', {})))
        if table in failed:
            raise TimeoutError('fake offline cloud')
        result = [dict(row) for row in rows.get(table, [])]
        filters = kwargs.get('extra_params', {})
        if filters.get('id', '').startswith('eq.'):
            result = [row for row in result if str(row.get('id')) == filters['id'][3:]]
        return result
    monkeypatch.setattr(b, 'supabase_select_rows', select)
    return SimpleNamespace(queued=queued, rows=rows, calls=calls,
                           failed=failed, reconciled=reconciled)


def visit(path, method='GET', force=False):
    with b.app.test_request_context(path, method=method):
        return b.maybe_pull_shared_from_supabase(force=force, required=True)


def drain(warm):
    while warm.queued:
        warm.queued.pop(0)()


def test_invoice_get_returns_before_io_and_reads_only_four_tables(warm):
    assert visit('/invoices') == (True, 'started')
    assert not warm.calls  # No network on the page response path.
    drain(warm)
    assert {table for table, _ in warm.calls} == cache.FINANCE
    assert len(warm.calls) == 4  # Previous path fetched all 20 tables.
    assert not warm.reconciled  # A partial refresh must not rewrite global statuses.
    assert visit('/api/client_invoices') == (False, 'throttled')
    assert len(warm.calls) == 4


def test_label_reads_one_order_and_never_marks_whole_orders_table_fresh(warm):
    assert visit('/orders/1/inpost/label?bundle=1', force=True) == (True, 'started')
    drain(warm)
    assert warm.calls == [('orders', {'id': 'eq.1'})]
    assert not warm.reconciled
    warm.calls.clear()
    assert visit('/invoices') == (True, 'started')
    drain(warm)
    assert ('orders', {}) in warm.calls


def test_pending_page_scopes_are_queued_and_overlapping_reads_are_deduplicated(warm):
    assert visit('/invoices') == (True, 'started')
    assert visit('/orders/1/inpost/label') == (False, 'already_running')
    assert visit('/stock') == (False, 'already_running')
    assert len(warm.queued) == 1
    drain(warm)
    names = [table for table, _ in warm.calls]
    assert names.count('orders') == 1
    assert names.count('invoices') == 1
    assert 'stock' in names and 'ksef_documents' in names
    assert 'china_documents' not in names
    assert not b._supabase_sync_state['read_cache']['running']


def test_failure_preserves_local_data_and_backs_off_only_failed_scope(warm, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(cache, 'time', SimpleNamespace(time=lambda: clock[0],
                                                     perf_counter=__import__('time').perf_counter))
    warm.failed.add('orders')
    visit('/orders/1/inpost/label')
    drain(warm)
    assert visit('/orders/1/inpost/label') == (False, 'throttled')
    assert len(warm.calls) == 1
    db = b.conn()
    assert db.execute('SELECT status FROM orders WHERE id=1').fetchone()[0] == 'confirmed'
    db.close()
    assert visit('/pricing') == (True, 'started')  # Unrelated scope is not blocked.
    drain(warm)
    warm.failed.clear()
    clock[0] += 61
    visit('/orders/1/inpost/label')
    drain(warm)
    assert sum(table == 'orders' for table, _ in warm.calls) == 2


def test_successful_full_pull_primes_cache_without_second_download(warm):
    result = b.pull_shared_tables_from_supabase(force=True, delete_missing=False)
    assert result['ok'] and len(warm.calls) == 20
    assert visit('/invoices') == (False, 'throttled')
    assert visit('/orders/1/inpost/label') == (False, 'throttled')
    assert not warm.queued


def test_failed_flow_table_cannot_trigger_status_reconciliation_from_partial_data(warm):
    warm.failed.add('invoice_allocations')
    visit('/stock')
    drain(warm)
    assert not warm.reconciled
    # Simulate successful refresh of another scope while allocations back off.
    state = b._supabase_sync_state['read_cache']
    state['finished'].pop(cache._key('orders', {}))
    visit('/stock')
    drain(warm)
    assert not warm.reconciled


def test_read_plan_covers_existing_get_callers():
    paths = ['/', '/company', '/customers', '/pricing', '/products', '/stock',
             '/payments/overdue', '/invoices', '/api/client_invoices', '/ksef',
             '/api/invoices/1/download', '/api/order_lookup', '/orders',
             '/orders/new', '/orders/stock-issue-audit', '/orders/1',
             '/orders/1/invoice', '/orders/1/inpost', '/orders/1/inpost/label',
             '/orders/1/packing-list', '/orders/1/proforma', '/orders/by-code/abc',
             '/api/client/orders/1/pdf', '/api/client/orders/1/pdf-retail']
    for path in paths:
        tables, _ = cache.plan(SimpleNamespace(path=path))
        assert tables is not None, path
        assert tables <= {table for table, _ in b.SUPABASE_PULL_TABLES}, path


def test_post_forced_refresh_is_not_cached_or_scoped(warm):
    b.pull_shared_tables_from_supabase(force=True, delete_missing=False)
    warm.calls.clear()
    result = visit('/orders/1/inpost', method='POST', force=True)
    assert result['ok']
    assert {table for table, _ in warm.calls} == {table for table, _ in b.SUPABASE_PULL_TABLES}
    assert not warm.queued


def test_scoped_snapshot_never_deletes_unselected_orders(warm):
    b.sqlite_upsert_rows('orders', [dict(warm.rows['orders'][0], id=2, order_no='ZAM-2')], 'id')
    result = b.pull_shared_tables_from_supabase(
        force=True, delete_missing=True, tables=[('orders', 'id')],
        filters={'orders': {'id': 'eq.1'}})
    assert result['ok']
    db = b.conn()
    assert db.execute('SELECT COUNT(*) FROM orders').fetchone()[0] == 2
    db.close()


def test_database_change_invalidates_warm_scope(warm, monkeypatch, tmp_path):
    visit('/invoices')
    drain(warm)
    previous = b._supabase_sync_state['read_cache']
    monkeypatch.setattr(b, 'DB_PATH', str(tmp_path / 'second.db'))
    b.init_db()
    with b.app.test_request_context('/invoices'):
        assert cache.schedule(b, b.request) == (True, 'started')
    drain(warm)
    assert b._supabase_sync_state['read_cache'] is not previous
    assert sum(table == 'invoices' for table, _ in warm.calls) == 2


@pytest.mark.parametrize('compressed', [False, True])
def test_transport_gzip_and_plain_json_have_identical_results_and_safe_byte_logs(warm, monkeypatch, caplog, compressed):
    payload = [{'id': 1, 'document': 'repeated invoice text ' * 2000}]
    decoded = json.dumps(payload).encode()
    wire = gzip.compress(decoded) if compressed else decoded
    class Response(io.BytesIO):
        status = 200
        headers = {'Content-Type': 'application/json', 'Content-Encoding': 'gzip' if compressed else ''}
    def open_response(req, **kwargs):
        assert req.get_header('Accept-encoding') == 'gzip'
        return Response(wire)
    monkeypatch.setattr(b.urllib.request, 'urlopen', open_response)
    monkeypatch.setattr(b, 'PERF_LOG_ENABLED', True)
    with caplog.at_level(logging.INFO):
        assert b.supabase_request('/rest/v1/invoices', params={'email': 'eq.private@example.invalid'}) == payload
    log = '\n'.join(record.message for record in caplog.records if 'SUPABASE_TRANSFER' in record.message)
    assert 'response_bytes=' + str(len(wire)) in log
    assert 'decoded_bytes=' + str(len(decoded)) in log
    assert 'private@example.invalid' not in log and 'isolated-secret' not in log
    assert 'repeated invoice text' not in log
    if compressed:
        assert len(wire) < len(decoded) / 10


def test_transport_does_not_mask_http_errors(warm, monkeypatch):
    def fail(*args, **kwargs):
        raise HTTPError('https://example.invalid', 402, 'quota', {}, None)
    monkeypatch.setattr(b.urllib.request, 'urlopen', fail)
    with pytest.raises(HTTPError) as exc:
        b.supabase_request('/rest/v1/orders')
    assert exc.value.code == 402
