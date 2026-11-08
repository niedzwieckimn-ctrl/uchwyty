import app as backend
from test_product_creation import client, isolated, form, _actor


def test_product_mark_and_remove_through_warehouse(client, monkeypatch):
    assert client.post('/products/new', data=form(client)).status_code == 302
    monkeypatch.setattr(backend, 'supabase_enabled', lambda: True)
    monkeypatch.setattr(backend, 'maybe_pull_shared_from_supabase', lambda *a, **k: None)
    marked = set()

    def remote(path, method='GET', params=None, payload=None, **kwargs):
        if path == '/rest/v1/catalog_new_arrivals':
            return [dict(product_id=i) for i in marked]
        assert path == '/rest/v1/rpc/warehouse_set_new_arrival' and method == 'POST'
        assert payload['p_sku'] == 'NEW-BB-128'
        if payload['p_enabled']:
            marked.add(payload['p_product_id'])
        else:
            marked.discard(payload['p_product_id'])
        return payload['p_enabled']

    monkeypatch.setattr(backend, 'supabase_request', remote)
    db = backend.conn()
    pid = db.execute('select id from products where sku=?',('NEW-BB-128',)).fetchone()[0]
    db.close()
    path = f'/products/{pid}/new-arrival'
    data = dict(enabled='1', sku='NEW-BB-128', csrf_token='create-test', q='NEW-BB-128')
    assert 'Oznacz jako nowość' in client.get('/products?q=NEW-BB-128').text
    assert client.post(path, data={**data,'csrf_token':'bad'}).status_code in (400,403)
    assert not marked
    assert 'Nowość · wyłącz' in client.post(path, data=data, follow_redirects=True).text
    assert marked == {pid}
    assert 'Oznacz jako nowość' in client.post(path, data={**data,'enabled':'0'}, follow_redirects=True).text
    with client.session_transaction() as state:
        state['internal_actor_id'] = _actor(role='MAGAZYN').actor_id
    assert client.post(path, data=data).status_code == 403
    assert not marked
