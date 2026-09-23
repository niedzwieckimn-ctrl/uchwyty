"""Focused checks for the ZIP46 integration, using an isolated SQLite database."""
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import app as backend
import search_analytics
from seventeentrack_module import parse_tracking_payload
from test_business_operations import isolated, _owner


def signed_in(client):
    with client.session_transaction() as session:
        session['admin_authenticated'] = True
        session['internal_actor_id'] = _owner().actor_id
        session['csrf_token'] = 'integration-token'
    return {'csrf_token': 'integration-token'}


def test_catalog_name_code_sku_variants_and_real_ambiguity():
    products = [
        {'id': 1, 'sku': 'CH032-AB-N29', 'model': 'CH032', 'name': 'Leo'},
        {'id': 2, 'sku': 'CH032-BB-N35', 'model': 'CH032', 'name': 'Leo'},
        {'id': 3, 'sku': 'CH034-AB-N29', 'model': 'CH034', 'name': 'Victor'},
        {'id': 4, 'sku': 'CH035-AB-N29', 'model': 'CH035', 'name': 'Victor'},
    ]
    catalog = search_analytics.Catalog(products)
    for phrase in ('Leo', 'CH032', 'CH032-AB-N29'):
        resolved = catalog.resolve(phrase)
        assert resolved['model_name'] == 'Leo'
        assert resolved['model_id'] == search_analytics.family_id('CH032')
    assert catalog.resolve('Victor')['resolution'] == 'ambiguous'
    assert catalog.resolve('Victor')['model_id'] is None
    legacy_rule = {'phrase': 'leonek', 'model_id': search_analytics.family_id('Leo')}
    assert search_analytics.Catalog(products, [legacy_rule]).resolve('leonek')['model_id'] == search_analytics.family_id('CH032')
    ambiguous_rule = {'phrase': 'v', 'model_id': search_analytics.family_id('Victor')}
    assert search_analytics.Catalog(products, [ambiguous_rule]).resolve('v')['resolution'] == 'ambiguous'


def test_historical_events_reproject_once_before_pagination(isolated):
    db = backend.conn()
    now = datetime.now(timezone.utc)
    db.execute("INSERT INTO products(id,sku,model,name,archived,created_at) VALUES(41,'CH032-AB-N29','CH032','Leo',0,?)",
               (backend.now_iso(),))
    db.execute("INSERT INTO products(id,sku,model,name,archived,created_at) VALUES(42,'CH032-BB-N35','CH032','Leo',0,?)",
               (backend.now_iso(),))
    for index, phrase in enumerate(('ch0', 'ch03', 'ch032')):
        event = {'id': f'old-{index}', 'customer_id': 'customer-1', 'customer_name': 'Klient',
                 'query': phrase, 'edit_id': 'one-edit', 'sequence': index,
                 'results_count': index + 1, 'resolution': 'unresolved',
                 'model_id': None, 'model_name': '', 'selected_sku': '', 'skus': [],
                 'created_at': (now + timedelta(seconds=index)).isoformat()}
        db.execute("INSERT INTO search_analytics_records VALUES(?,?,?,?)",
                   (event['id'], 'event', event['created_at'], json.dumps(event)))
    db.commit()
    before = db.execute("SELECT COUNT(*) FROM search_analytics_records WHERE kind='event'").fetchone()[0]
    db.close()
    snapshot = search_analytics.analytics_snapshot(backend, days=30, now=now + timedelta(minutes=6))
    assert snapshot['models'][0]['name'] == 'Leo'
    assert snapshot['models'][0]['count'] == 1
    assert len(snapshot['filtered']) == 1
    db = backend.conn()
    assert db.execute("SELECT COUNT(*) FROM search_analytics_records WHERE kind='event'").fetchone()[0] == before
    db.close()


def test_17track_latest_across_carriers_and_timezone():
    payload = {'number': '12345', 'track_info': {'latest_status': {'status': 'InTransit'},
        'tracking': {'providers': [
            {'provider': {'name': 'First'}, 'events': [
                {'description': 'Label created', 'time_iso': '2026-09-16T10:00:00+08:00'},
                {'description': 'Arrival', 'location': 'Warszawa',
                 'time_iso': '2026-09-18T12:00:00+02:00'}]},
            {'provider': {'name': 'Second'}, 'events': [
                {'description': 'Picked up', 'location': 'Berlin',
                 'time_iso': '2026-09-18T11:30:00+00:00'},
                {'description': 'Old scan', 'time_iso': '2026-09-15T12:00:00+00:00'}]}
        ]}}}
    result = parse_tracking_payload(payload)
    assert result['last_event'] == 'Picked up — Berlin'
    assert result['last_update'] == '2026-09-18T11:30:00+00:00'
    assert result['carrier'] == 'Second'
    assert parse_tracking_payload({'track_info': {'latest_status': {'status': 'InTransit'}}})['last_update'] == ''


def test_document_upload_list_download_and_restart(isolated, tmp_path, monkeypatch):
    client = isolated
    token = signed_in(client)
    db = backend.conn()
    db.execute("INSERT INTO china_packages(id,package_no,status,created_at) VALUES(71,'PO-71','ordered',?)",
               (backend.now_iso(),))
    db.commit(); db.close()
    pdf = b'%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF'
    response = client.post('/china/71/documents', data={**token, 'document_type': 'invoice',
        'document': (io.BytesIO(pdf), 'invoice.pdf')}, content_type='multipart/form-data')
    assert response.status_code == 302
    db = backend.conn()
    row = db.execute('SELECT * FROM china_documents WHERE package_id=71').fetchone()
    assert row and row['document_type'] == 'invoice'
    document_id = row['id']
    db.close()
    assert b'invoice.pdf' in client.get('/china').data
    assert client.get(f'/china/documents/{document_id}').data == pdf
    backend.init_db()
    assert client.get(f'/china/documents/{document_id}').data == pdf
    db = backend.conn()
    db.execute('UPDATE china_documents SET stored_path=? WHERE id=?',
               (str(Path('old-instance') / Path(row['stored_path']).name), document_id))
    db.commit(); db.close()
    assert client.get(f'/china/documents/{document_id}').data == pdf
    bad = client.post('/china/71/documents', data={**token, 'document_type': 'order',
        'document': (io.BytesIO(b'bad'), 'bad.pdf')}, content_type='multipart/form-data')
    assert bad.status_code == 302 and 'document_error=' in bad.headers['Location']


def test_document_upload_requires_durable_local_storage_on_render(isolated, monkeypatch):
    client = isolated
    token = signed_in(client)
    monkeypatch.setenv('RENDER', '1')
    monkeypatch.delenv('APP_DATA_DIR', raising=False)
    monkeypatch.delenv('REMANENT_PERSISTENCE_READY', raising=False)
    response = client.post('/china/71/documents', data={**token, 'document_type': 'order',
        'document': (io.BytesIO(b'%PDF-1.4\n%%EOF'), 'order.pdf')},
        content_type='multipart/form-data')
    assert response.status_code == 302 and 'document_error=' in response.headers['Location']


def test_manual_prices_both_lists_preserve_order_snapshot(isolated):
    client = isolated
    token = signed_in(client)
    db = backend.conn()
    now = backend.now_iso()
    db.execute("INSERT INTO pricing(model,net_price,gross_price,created_at) VALUES('CH032-AB-N29',10,12,?)", (now,))
    db.execute("INSERT INTO pricing_eur(sku,ean,price_eur,uvp_eur,created_at,updated_at) VALUES('CH032-AB-N29','',4,8,?,?)", (now, now))
    db.execute("INSERT INTO products(id,sku,model,name,archived,created_at) VALUES(82,'CH032-AB-N29','CH032','Leo',0,?)", (now,))
    db.execute("INSERT INTO orders(id,order_no,customer_name,status,created_at) VALUES(81,'Z-81','Test','confirmed',?)", (now,))
    db.execute("INSERT INTO order_items(order_id,product_id,sku,qty,unit_net_price,unit_gross_price,created_at) VALUES(81,82,'CH032-AB-N29',1,10,12,?)", (now,))
    db.commit(); db.close()
    for price_list, first, second in (('pln', '13,37', '16,45'), ('eur', '4,27', '8,99')):
        response = client.post('/pricing/update', data={**token, 'list': price_list,
            'key': 'CH032-AB-N29', 'first': first, 'second': second})
        assert response.status_code == 302
        assert b'CH032-AB-N29' in client.get('/pricing?list=' + price_list).data
    db = backend.conn()
    assert tuple(db.execute("SELECT net_price,gross_price FROM pricing WHERE model='CH032-AB-N29'").fetchone()) == (13.37, 16.45)
    assert tuple(db.execute("SELECT price_eur,uvp_eur FROM pricing_eur WHERE sku='CH032-AB-N29'").fetchone()) == (4.27, 8.99)
    assert tuple(db.execute('SELECT unit_net_price,unit_gross_price FROM order_items WHERE order_id=81').fetchone()) == (10, 12)
    db.close()
