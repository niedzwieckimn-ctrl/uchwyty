import app as b
import packing_versions
from test_packing_previous_shipment import old_shipment
from test_packing_correction import ready, document, pack
from test_packing_ui_reconciliation import packing_cloud
from test_multi_order_packing_agent import multi_order_flow


def token():
    c = b.conn()
    try: return packing_versions.shipment_form_version(c, packing_versions.current_for_order(c,103))
    finally: c.close()


def submit(client, tracking, version):
    return client.post('/orders/103/shipped', data=dict(csrf_token='csrf',carrier='dpd',tracking_no=tracking,
                                                     shipment_form_version=version))


def test_next_parcel_tracking_preserves_previous_shipment_and_stock(ready, monkeypatch):
    client, _, _, sent = ready
    old_shipment()
    monkeypatch.setattr(b, 'supabase_enabled', lambda: False)
    current = document()
    response = submit(client, 'CURRENT-PARCEL', token())
    assert response.status_code == 302, response.text
    c = b.conn()
    assert c.execute("SELECT tracking FROM packing_shipments WHERE shipment_key='inpost:older'").fetchone()[0] == 'OLDER-TRACK'
    assert c.execute("SELECT final_batch_id FROM packing_shipments WHERE shipment_key='dpd:CURRENT-PARCEL'").fetchone()[0] == current['batch_id']
    assert c.execute('SELECT tracking_no,inpost_shipment_id FROM orders WHERE id=101').fetchone()[:] == ('CURRENT-PARCEL','')
    assert [r[0] for r in c.execute('SELECT qty FROM stock ORDER BY product_id')] == [3,0,2]
    c.close()
    assert not sent


def test_stale_tracking_form_cannot_ship_repacked_list(ready, monkeypatch):
    client, _, _, sent = ready
    version = token()
    assert pack(client,qty=1).status_code == 302
    monkeypatch.setattr(b, 'supabase_enabled', lambda: False)
    response = submit(client, 'STALE', version)
    assert response.status_code == 409
    c = b.conn()
    assert c.execute('SELECT COUNT(*) FROM packing_shipments').fetchone()[0] == 0
    c.close()
    assert not sent


def test_fix_last_tracking_keeps_original_and_rejects_stale_tab(ready, monkeypatch):
    client, _, _, sent = ready
    monkeypatch.setattr(b, 'supabase_enabled', lambda: False)
    first = token()
    assert submit(client, 'TYPO', first).status_code == 302
    assert submit(client, 'CORRECT', first).status_code == 409
    assert submit(client, 'CORRECT', token()).status_code == 302
    c = b.conn()
    assert c.execute('SELECT COUNT(DISTINCT final_batch_id) FROM packing_shipments').fetchone()[0] == 1
    assert c.execute('SELECT COUNT(*) FROM packing_shipments').fetchone()[0] == 2
    assert c.execute('SELECT tracking_no FROM orders WHERE id=103').fetchone()[0] == 'CORRECT'
    c.close()
    assert not sent


def test_order_view_shows_one_current_list_and_collapsed_history(ready, monkeypatch):
    client, _, _, _ = ready
    assert pack(client,qty=1).status_code == 302
    monkeypatch.setattr(b,'supabase_enabled',lambda:False)
    page = client.get('/orders/103')
    assert page.status_code == 200, page.text
    assert page.text.count('data-current-packing') == 1
    assert '<details class="packing-history">' in page.text
    assert 'Historia list (1)' in page.text
    assert 'name="shipment_form_version"' in page.text


def test_order_view_restores_manually_shipped_parcel_after_cold_cache(ready):
    client, cloud, _, _ = ready
    c = b.conn()
    c.execute('DELETE FROM fulfillment_documents')
    c.execute('DELETE FROM fulfillment_reconciliation_versions')
    c.commit(); c.close()
    # No InPost identifier exists; opening the order must hydrate cloud evidence.
    page = client.get('/orders/103?manual_shipment=1')
    assert page.status_code == 200, page.text
    assert 'data-current-packing' in page.text
    assert 'Najpierw zapisz listę pakową.' not in page.text
    assert 'disabled>Zapisz numer przesyłki' not in page.text
