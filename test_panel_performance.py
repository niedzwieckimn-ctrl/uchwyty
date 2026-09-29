from datetime import date
import sqlite3
import threading

import pytest
from flask import Flask, session
from panel_performance import SQLiteReadCache, SignedURLCache, render_cached_template_string, _compiled_template
from invoice_list_read import read_invoice_list
import app as backend
from test_business_operations import isolated
from test_business_freshness import _remote_rows


def test_compiled_templates_do_not_share_session_or_context():
    app = Flask(__name__)
    app.secret_key = 'test'
    source = '{{ value }} / {{ session.name }}'
    before = _compiled_template.cache_info()
    with app.test_request_context('/'):
        session['name'] = 'first'
        assert render_cached_template_string(source, value='<one>') == '&lt;one&gt; / first'
    with app.test_request_context('/'):
        session['name'] = 'second'
        assert render_cached_template_string(source, value='two') == 'two / second'
    after = _compiled_template.cache_info()
    assert after.misses == before.misses + 1
    assert after.hits == before.hits + 1


@pytest.fixture
def cached_db(tmp_path):
    path = tmp_path / 'cache.db'
    def connect():
        return sqlite3.connect(path)
    with connect() as db:
        db.execute('CREATE TABLE values_table(value INTEGER)')
        db.execute('INSERT INTO values_table VALUES(1)')
    cache = SQLiteReadCache()
    calls = []
    def build(db):
        calls.append(1)
        return [{'value': db.execute('SELECT value FROM values_table').fetchone()[0]}]
    yield cache, connect, build, calls
    cache.clear()


def test_read_cache_commit_rollback_key_and_copy(cached_db):
    cache, connect, build, calls = cached_db
    first = cache.get(connect, ('day1', 60), build)
    first[0]['value'] = 999
    assert cache.get(connect, ('day1', 60), build) == [{'value': 1}]
    assert len(calls) == 1
    db = connect()
    db.execute('UPDATE values_table SET value=2')
    db.rollback()
    assert cache.get(connect, ('day1', 60), build)[0]['value'] == 1
    assert len(calls) == 1
    db.execute('UPDATE values_table SET value=3')
    db.commit()
    db.close()
    assert cache.get(connect, ('day1', 60), build)[0]['value'] == 3
    cache.get(connect, ('day2', 60), build)
    cache.get(connect, ('day2', 90), build)
    assert len(calls) == 4


def test_read_cache_does_not_store_result_across_concurrent_write(cached_db):
    cache, connect, build, calls = cached_db
    def changing_build(connection):
        result = build(connection)
        with connect() as db:
            db.execute('UPDATE values_table SET value=8')
        return result
    assert cache.get(connect, 'test', changing_build)[0]['value'] == 1
    assert cache.get(connect, 'test', build)[0]['value'] == 8
    assert len(calls) == 2


def test_read_cache_single_flight(cached_db):
    cache, connect, build, calls = cached_db
    barrier = threading.Barrier(4)
    results = []
    def worker():
        barrier.wait()
        results.append(cache.get(connect, 'shared', build))
    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads: thread.start()
    for thread in threads: thread.join(timeout=5)
    assert results == [[{'value': 1}]] * 4
    assert len(calls) == 1


def test_signed_urls_cache_expiry_scope_partial_failure(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr('panel_performance.time.monotonic', lambda: clock[0])
    cache = SignedURLCache()
    calls = []
    def fetch(paths):
        calls.append(paths)
        return {path: 'signed:' + path for path in paths if path != 'missing'}
    assert cache.get_many('bucket1', ['a','a','missing'], 100, fetch) == {'a':'signed:a'}
    assert cache.get_many('bucket1', ['a'], 100, fetch) == {'a':'signed:a'}
    assert len(calls) == 1
    cache.get_many('bucket1', ['missing'], 100, fetch)
    cache.get_many('bucket2', ['a'], 100, fetch)
    clock[0] = 191
    cache.get_many('bucket1', ['a'], 100, fetch)
    assert calls == [['a','missing'], ['missing'], ['a'], ['a']]


def seed_invoice_list():
    data = _remote_rows()
    for table in ('customers','products','orders'):
        backend.sqlite_upsert_rows(table, data[table], 'id')
    with backend.conn() as db:
        for number in range(1, 131):
            values = dict(data['invoices'][0], id=number, invoice_no=f'FV/{number:04}',
                buyer_name=['Żółć Sp. z o.o.','Straße','Alfa'][number % 3],
                buyer_tax_no=str(number % 3),
                issue_date='2026-09-01' if number % 2 else '2026-08-01',
                payment_to='2026-09-01' if number % 4 else '2026-10-20',
                publication_state='complete' if number % 7 else 'preparing',
                currency=['PLN','EUR','USD'][number % 3],
                invoice_type=['domestic','export',None][number % 3])
            db.execute(f"INSERT INTO invoices({','.join(values)}) VALUES({','.join('?' for _ in values)})", list(values.values()))
            db.execute('''INSERT INTO invoice_meta(invoice_id,pdf_path,invoice_items_json,paid,sent_to_client,updated_at)
                        VALUES(?,?,?,?,?,?)''', (number, f'supabase://invoice-pdfs/{number}.pdf',
                        '[{"qty":1}]', int(number%5 == 0), int(number%2 == 0), '2026-09-01'))
            if number % 3:
                db.execute('INSERT INTO ksef_documents(invoice_id,status,updated_at) VALUES(?,?,?)',
                    (number,'sent' if number%2 else 'ready','2026-09-01'))


def test_invoice_sql_filters_summary_pagination_and_no_payload(isolated):
    seed_invoice_list()
    db = backend.conn()
    try:
        def read(args):
            return read_invoice_list(db, args, date(2026,9,29), norm=backend.norm,
                normalize_currency=backend.normalize_order_currency, resolve_type=backend.resolve_invoice_type, to_int=backend.to_int)
        result = read({})
        assert result['summary']['all'] == result['total_filtered'] == 130
        assert len(result['rows']) == 50 and result['page_count'] == 3
        assert all('invoice_items_json' not in row and row['pdf_ok'] == 1 for row in result['rows'])
        pages = [read({'page':str(page)})['rows'] for page in range(1,4)]
        ids = [row['id'] for page in pages for row in page]
        assert len(ids) == len(set(ids)) == 130
        assert read({'q':'STRASSE'})['total_filtered'] == 44
        assert read({'q':'żÓŁĆ'})['total_filtered'] == 43
        assert read({'month':'2026-08'})['total_filtered'] == 65
        assert read({'period':'30d'})['total_filtered'] == 65
        assert read({'payment':'open'})['total_filtered'] == 89
        assert read({'sent':'sent'})['total_filtered'] == 65
        assert read({'document_type':'export'})['total_filtered'] == 44
        assert read({'currency':'USD'})['total_filtered'] == 43
        assert read({'q':"' OR 1=1 --"})['total_filtered'] == 0
        customer = read({'view':'customers'})
        assert len(customer['rows']) == 50
        assert sum(t['count'] for totals in customer['group_totals'].values() for t in totals) == 130
        assert read({'page':'999'})['page'] == 3
    finally:
        db.close()


def test_invoice_list_and_stock_html_do_no_storage_io(isolated, monkeypatch):
    seed_invoice_list()
    with backend.conn() as db:
        db.execute("INSERT INTO stock(product_id,qty) VALUES(1,10)")
        db.execute("INSERT INTO product_images(id,filename,stored_path,created_at) VALUES(1,'a.png','supabase://images/a.png','2026-09-01')")
        db.execute('INSERT INTO product_image_assignments(product_id,image_id,created_at) VALUES(1,1,?)', ('2026-09-01',))
    def no_storage(*_args, **_kwargs):
        pytest.fail('Storage must not block panel HTML')
    monkeypatch.setattr(backend, 'supabase_storage_download_bytes', no_storage)
    monkeypatch.setattr(backend, 'supabase_storage_create_signed_urls', no_storage)
    with isolated.session_transaction() as state:
        state['admin_authenticated'] = True
        state['csrf_token'] = 'csrf-first'
    for path in ('/invoices','/invoices?view=customers','/stock'):
        response = isolated.get(path)
        assert response.status_code == 200
        assert 'Server-Timing' in response.headers
        assert b'csrf-first' in response.data
    assert b'data-thumbnail-id="1"' in isolated.get('/stock').data
    with isolated.session_transaction() as state:
        state['csrf_token'] = 'csrf-second'
    response = isolated.get('/invoices')
    assert b'csrf-second' in response.data and b'csrf-first' not in response.data
    assert backend.SUPABASE_URL in response.headers['Content-Security-Policy'].split('img-src')[1].split(';')[0]
    monkeypatch.setattr(backend, 'supabase_storage_create_signed_urls', lambda bucket, paths, **kwargs:
                        {path:'https://example.test/'+path for path in paths})
    response = isolated.get('/api/stock/thumbnail-urls?ids=1')
    assert response.status_code == 200 and response.json['urls']['1'].startswith('https://example.test/')
    assert isolated.get('/api/stock/thumbnail-urls?ids=' + ','.join('1' for _ in range(101))).status_code == 400
    assert backend.app.test_client().get('/api/stock/thumbnail-urls?ids=1').status_code == 401


def test_sync_unchanged_rows_do_not_write_and_changes_still_apply(isolated):
    row = _remote_rows()['products'][0]
    backend.sqlite_upsert_rows('products', [row], 'id')
    observer = backend.conn()
    try:
        version = observer.execute('PRAGMA data_version').fetchone()[0]
        backend.sqlite_upsert_rows('products', [row], 'id')
        assert observer.execute('PRAGMA data_version').fetchone()[0] == version
        backend.sqlite_upsert_rows('products', [dict(row, name='updated')], 'id')
        assert observer.execute('PRAGMA data_version').fetchone()[0] != version
        assert observer.execute('SELECT name FROM products').fetchone()[0] == 'updated'
    finally:
        observer.close()


def test_full_pull_overlaps_reads_but_preserves_write_order(isolated, monkeypatch):
    tables = [('products','id'),('stock','product_id'),('orders','id'),('order_items','id')]
    monkeypatch.setattr(backend, 'SUPABASE_SERVICE_ROLE_KEY', 'fake')
    monkeypatch.setattr(backend, 'SUPABASE_PULL_TABLES', tables)
    monkeypatch.setattr(backend.invoice_payment_sync, 'flush_pending', lambda *_args: None)
    import inpost_tracking
    monkeypatch.setattr(inpost_tracking, 'flush_pending', lambda *_args: None)
    monkeypatch.setattr(backend, 'normalize_temp_order_numbers', lambda: None)
    monkeypatch.setattr(backend, 'link_orders_to_customers_by_email', lambda **kwargs: None)
    barrier = threading.Barrier(4)
    threads, writes = set(), []
    def read(table, **kwargs):
        threads.add(threading.get_ident())
        barrier.wait(timeout=3)
        return []
    monkeypatch.setattr(backend, 'supabase_select_rows', read)
    monkeypatch.setattr(backend, 'sqlite_upsert_rows', lambda table, rows, col: writes.append(table))
    result = backend.pull_shared_tables_from_supabase(force=True, delete_missing=False)
    assert result['ok'] and len(threads) == 4
    assert writes == [table for table,col in tables]
