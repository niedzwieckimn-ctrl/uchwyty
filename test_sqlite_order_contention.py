"""Real SQLite transactions and separate threads; no production services."""
import sqlite3
import threading
from pathlib import Path

import pytest

import app as backend


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, 'DB_PATH', str(tmp_path / 'contention.db'))
    monkeypatch.setattr(backend, 'DATA_DIR', str(tmp_path))
    monkeypatch.setattr(backend, 'supabase_enabled', lambda: False)
    # A failing regression must not poison the other tests' global lock.
    monkeypatch.setattr(backend, '_sqlite_write_lock', threading.RLock())
    backend.init_db()
    db = backend.conn()
    db.execute('INSERT INTO stock(product_id,qty) VALUES(99,8)')
    db.commit()
    db.close()


@pytest.mark.parametrize('operation', [
    'upsert', 'upsert_system_exit', 'delete', 'delete_selected',
    'context_success', 'context_error',
])
def test_failed_sync_or_context_exit_does_not_leak_lock(isolated_db, monkeypatch, operation):
    if operation.startswith('delete'):
        db = backend.conn()
        db.execute("CREATE TRIGGER reject_delete BEFORE DELETE ON stock BEGIN SELECT RAISE(ABORT,'test delete failure'); END")
        db.close()
    observed = {}
    finished = threading.Event()
    cleanup = threading.Event()
    connections = []
    original_conn = backend.conn

    def tracked_conn():
        db = original_conn()
        if threading.current_thread().name == 'sync-regression':
            connections.append(db)
        return db

    monkeypatch.setattr(backend, 'conn', tracked_conn)

    def run():
        try:
            if operation.startswith('upsert'):
                class InterruptedRow(dict):
                    def get(self, key, default=None):
                        if key == 'qty':
                            raise SystemExit('sync interrupted')
                        return super().get(key, default)

                invalid = {'product_id': 100, 'qty': None}
                if operation == 'upsert_system_exit':
                    invalid = InterruptedRow(invalid)
                backend.sqlite_upsert_rows('stock', [
                    {'product_id': 99, 'qty': 6},
                    invalid,
                ], 'product_id')
            elif operation.startswith('delete'):
                backend.sqlite_delete_missing_rows(
                    'stock', 'product_id', [100] if operation == 'delete_selected' else []
                )
            else:
                with backend.conn() as db:
                    db.execute('UPDATE stock SET qty=6 WHERE product_id=99')
                    if operation == 'context_error':
                        raise ValueError('test context failure')
        except (sqlite3.IntegrityError, ValueError, SystemExit) as exc:
            observed['error'] = type(exc).__name__
        finally:
            observed['held'] = any(db._write_lock_held for db in connections)
            finished.set()
            cleanup.wait(10)
            # Release pre-fix leaks on the owning thread, even if the test fails.
            for db in connections:
                db.close()

    worker = threading.Thread(target=run, name='sync-regression', daemon=True)
    worker.start()
    try:
        assert finished.wait(3), 'sync did not return'
        acquired = backend._sqlite_write_lock.acquire(timeout=0.2)
        if acquired:
            backend._sqlite_write_lock.release()
        assert acquired, f'global RLock leaked by sync-regression: {observed}'
        db = original_conn()
        try:
            qty = db.execute('SELECT qty FROM stock WHERE product_id=99').fetchone()[0]
        finally:
            db.close()
        assert qty == (6 if operation == 'context_success' else 8)
    finally:
        cleanup.set()
        worker.join(3)
    assert not worker.is_alive()


def test_failed_commit_keeps_protection_until_rollback(isolated_db):
    db = backend.conn()
    try:
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('CREATE TABLE parent_test(id INTEGER PRIMARY KEY)')
        db.execute('CREATE TABLE child_test(parent_id REFERENCES parent_test(id) DEFERRABLE INITIALLY DEFERRED)')
        db.execute('INSERT INTO child_test VALUES(999)')
        with pytest.raises(sqlite3.IntegrityError):
            db.commit()
        assert db.in_transaction
        results = []

        def probe():
            acquired = backend._sqlite_write_lock.acquire(timeout=0.2)
            results.append(acquired)
            if acquired:
                backend._sqlite_write_lock.release()

        worker = threading.Thread(target=probe, daemon=True)
        worker.start()
        worker.join(1)
        assert results == [False], 'failed commit must not expose an active transaction'
        db.rollback()
        worker = threading.Thread(target=probe, daemon=True)
        worker.start()
        worker.join(1)
        assert results == [False, True]
    finally:
        db.close()


@pytest.mark.parametrize('ending', ['commit', 'rollback', 'close', 'sql_error', 'system_exit'])
def test_transaction_cleanup_releases_lock_to_another_thread(isolated_db, ending):
    db = backend.conn()
    try:
        db.execute('UPDATE stock SET qty=6 WHERE product_id=99')
        if ending == 'sql_error':
            with pytest.raises(sqlite3.IntegrityError):
                db.cursor().execute('UPDATE stock SET qty=NULL WHERE product_id=99')
            db.rollback()
        elif ending == 'system_exit':
            with pytest.raises(SystemExit):
                with db:
                    raise SystemExit('worker interrupted')
        else:
            getattr(db, ending)()
        acquired = []

        def probe():
            free = backend._sqlite_write_lock.acquire(timeout=0.2)
            acquired.append(free)
            if free:
                backend._sqlite_write_lock.release()

        other = threading.Thread(target=probe, daemon=True)
        other.start()
        other.join(1)
        assert acquired == [True]
        check = backend.conn()
        try:
            assert check.execute('SELECT qty FROM stock WHERE product_id=99').fetchone()[0] == (
                6 if ending == 'commit' else 8
            )
        finally:
            check.close()
    finally:
        db.close()


def test_consecutive_packing_and_invoice_requests_release_lock(order_actions):
    requests = [
        ('packing-list', {'csrf_token': 'test', 'pack_qty_99': '7'}),
        ('packing-list', {'csrf_token': 'test', 'pack_qty_99': '7'}),
        ('invoice', _invoice_form()),
    ]
    for operation, form in requests:
        responses = []
        worker = threading.Thread(
            target=lambda: responses.append(order_actions.post('/orders/99/' + operation, data=form)),
            daemon=True,
        )
        worker.start()
        worker.join(5)
        assert not worker.is_alive(), f'{operation} timed out after previous action'
        assert responses and responses[0].status_code == 302
        free = backend._sqlite_write_lock.acquire(timeout=0.2)
        if free:
            backend._sqlite_write_lock.release()
        assert free, f'{operation} left the global write lock held'
    db = backend.conn()
    try:
        assert db.execute('SELECT COUNT(*) FROM invoices WHERE order_id=99').fetchone()[0] == 1
        assert not db.execute('SELECT * FROM fulfillment_locks').fetchall()
    finally:
        db.close()


@pytest.fixture
def order_actions(isolated_db, monkeypatch):
    now = backend.now_iso()
    db = backend.conn()
    db.execute("INSERT INTO products(id,sku,model,name,created_at) VALUES(99,'SKU-99','M99','Uchwyt',?)", (now,))
    db.execute("INSERT INTO orders(id,order_no,customer_name,customer_address,customer_email,status,created_at,currency) VALUES(99,'ZAM-99','Test','Testowa 1, 00-001 Warszawa','buyer@example.invalid','confirmed',?,'PLN')", (now,))
    db.execute("INSERT INTO order_items(id,order_id,product_id,sku,qty,unit_net_price,unit_gross_price,currency,created_at) VALUES(99,99,99,'SKU-99',7,10,12.3,'PLN',?)", (now,))
    db.execute("INSERT INTO company_profile(id,company_name,address,nip,updated_at) VALUES(1,'Sprzedawca','Testowa 1','1234567890',?)", (now,))
    db.commit()
    db.close()
    monkeypatch.setattr(backend.app, 'secret_key', 'contention-test')
    monkeypatch.setattr(backend, '_client_profile_for_email', lambda _email: {})
    monkeypatch.setattr(backend, 'SUPABASE_AUTO_SYNC_ON_WRITE', False)
    # No invoice/PDF/packing service is stubbed. Only outside I/O is forbidden.
    def no_remote(*args, **kwargs):
        raise AssertionError('unexpected external I/O')
    monkeypatch.setattr(backend, 'supabase_request', no_remote)
    monkeypatch.setattr(backend, '_send_orders_packed_email', lambda *a, **k: {'ok': True})
    client = backend.app.test_client()
    with client.session_transaction() as session:
        session['admin_authenticated'] = True
        session['csrf_token'] = 'test'
    return client


def _invoice_form():
    return {
        'csrf_token': 'test', 'invoice_no': 'FVAT 30/09/2026',
        'invoice_no_manual': '1', 'issue_date': '2026-09-15',
        'sell_date': '2026-09-15', 'payment_to': '2026-09-22',
        'payment_type': 'przelew', 'buyer_name': 'Test',
        'buyer_tax_no': '1234567890', 'buyer_address': 'Testowa 1\n00-001 Warszawa',
        'buyer_country': 'PL', 'buyer_email': 'buyer@example.invalid',
        'invoice_type': 'domestic', 'currency': 'PLN',
        'invoice_qty_99': '7', 'submit_action': 'invoice',
    }


@pytest.mark.parametrize('operation', ['packing-list', 'invoice'])
@pytest.mark.parametrize('failed_sync', [False, True])
def test_order_actions_complete_with_background_sync_active(
    order_actions, monkeypatch, operation, failed_sync,
):
    main_id = threading.get_ident()
    sync_threads = set()
    trigger_enabled = [True]
    applied = threading.Event()
    batch_active = threading.Event()
    finish_batch = threading.Event()
    ui_waiting = threading.Event()
    finish_sync = threading.Event()
    stopped = threading.Event()
    captured = {}
    connections = []
    original_conn = backend.conn
    original_pull = backend.pull_shared_tables_from_supabase
    original_execute = backend._SerializedWriteCursor.execute
    original_before_sql = backend._SerializedWriteConnection._before_sql
    monkeypatch.setattr(backend, '_supabase_sync_state', {
        'running': False, 'pull_running': False, 'last_pull_finished_ts': 0,
    })
    monkeypatch.setattr(backend, 'supabase_enabled', lambda: (
        threading.get_ident() in sync_threads
        or (threading.get_ident() == main_id and trigger_enabled[0])
    ))
    monkeypatch.setattr(backend, 'SUPABASE_PULL_TABLES', [('stock', 'product_id')])

    def tracked_conn():
        db = original_conn()
        if threading.get_ident() in sync_threads:
            connections.append(db)
        return db

    def select(_table, **kwargs):
        rows = [{'product_id': 99, 'qty': 8}]
        if failed_sync:
            rows.append({'product_id': 100, 'qty': None})
        return rows

    def execute(cursor, sql, parameters=()):
        result = original_execute(cursor, sql, parameters)
        if threading.get_ident() in sync_threads and sql.startswith('INSERT INTO stock'):
            batch_active.set()
            assert finish_batch.wait(10), 'test failed to release active sync transaction'
        return result

    def before_sql(db, sql):
        if threading.current_thread().name == 'order-ui' and sql.startswith('DELETE FROM fulfillment_locks'):
            ui_waiting.set()
        return original_before_sql(db, sql)

    def pull(**kwargs):
        captured['sync_thread'] = threading.current_thread()
        sync_threads.add(threading.get_ident())
        try:
            captured['result'] = original_pull(**kwargs)
            applied.set()
            assert finish_sync.wait(15), 'UI failed to release test barrier'
            return captured['result']
        finally:
            # Allows the pre-fix regression to terminate without hanging pytest.
            for db in connections:
                db.close()
            stopped.set()

    monkeypatch.setattr(backend, 'conn', tracked_conn)
    monkeypatch.setattr(backend._SerializedWriteCursor, 'execute', execute)
    monkeypatch.setattr(backend._SerializedWriteConnection, '_before_sql', before_sql)
    monkeypatch.setattr(backend, 'supabase_select_rows', select)
    monkeypatch.setattr(backend, 'pull_shared_tables_from_supabase', pull)
    monkeypatch.setattr(backend, '_run_post_pull_reconciliation', lambda: None)
    assert backend.trigger_background_supabase_pull('contention-test') == (True, 'started')
    trigger_enabled[0] = False
    request_done = threading.Event()

    def request_action():
        try:
            form = _invoice_form() if operation == 'invoice' else {
                'csrf_token': 'test', 'carrier': 'pending', 'pack_qty_99': '7',
            }
            captured['response'] = order_actions.post('/orders/99/' + operation, data=form)
        except BaseException as exc:
            captured['request_error'] = exc
        finally:
            request_done.set()

    ui = threading.Thread(target=request_action, name='order-ui', daemon=True)
    try:
        assert batch_active.wait(10), 'background sync failed to start transaction'
        assert backend._supabase_sync_state['pull_running'] is True
        ui.start()
        assert ui_waiting.wait(3), 'UI failed to reach fulfillment_locks'
        assert not request_done.is_set(), 'UI bypassed the active write lock'
        finish_batch.set()
        assert applied.wait(10), 'background sync failed to reach apply boundary'
        assert captured['result']['ok'] is (not failed_sync)
        if failed_sync:
            assert captured['result']['tables']['stock']['stage'] == 'upsert'
        assert request_done.wait(5), 'UI blocked by background sync at fulfillment_locks'
        assert 'request_error' not in captured, captured.get('request_error')
        assert captured['response'].status_code == 302, captured['response'].get_data(as_text=True)
    finally:
        finish_batch.set()
        finish_sync.set()
        stopped.wait(3)
        if 'sync_thread' in captured:
            captured['sync_thread'].join(3)
        if ui.ident is not None:
            ui.join(5)
    assert not ui.is_alive()
    assert not captured['sync_thread'].is_alive()
    db = original_conn()
    try:
        assert db.execute('SELECT 1 FROM fulfillment_locks WHERE order_id=99').fetchone() is None
        if operation == 'invoice':
            invoice = db.execute('SELECT id,publication_state FROM invoices WHERE order_id=99').fetchone()
            assert invoice and invoice['publication_state'] == 'complete'
            path = db.execute('SELECT pdf_path FROM invoice_meta WHERE invoice_id=?', (invoice['id'],)).fetchone()[0]
            assert backend.invoice_pdf_exists(path, 'FVAT 30/09/2026')[0]
            assert db.execute('SELECT qty FROM stock WHERE product_id=99').fetchone()[0] == 1
        else:
            assert db.execute('SELECT SUM(qty) FROM packing_allocations WHERE order_id=99').fetchone()[0] == 7
            assert list(Path(backend.DATA_DIR).rglob('*.pdf'))
    finally:
        db.close()
