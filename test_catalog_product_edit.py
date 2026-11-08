import re
import app as backend
from test_product_creation import client, isolated, form, rows, _actor


def setup_edit(client):
    src = form(client)
    assert client.post('/products/new', data=src).status_code == 302
    pid = rows('products')[0]['id']
    path = f'/products/{pid}/edit'
    response = client.get(path)
    assert response.status_code == 200
    token = re.search('name="original" value="([^"]+)"', response.text)[1]
    return path, {**src, 'original': token}


def test_edit_handle_reprice_keys_and_protect_history(client):
    path, original = setup_edit(client)
    updated = {**original, 'sku':'FIX-99', 'name':'Winsor krótki', 'model':'Winsor', 'ean':'01234565'}
    response = client.post(path, data=updated, follow_redirects=True)
    assert 'Zapisano dane uchwytu FIX-99' in response.text
    assert rows('products')[0]['ean'] == '01234565'
    assert rows('stock')[0]['product_id'] == rows('products')[0]['id']
    assert [(r['model'],r['net_price']) for r in rows('pricing')] == [('FIX-99',100)]
    assert [(r['sku'],r['price_eur'],r['ean']) for r in rows('pricing_eur')] == [('FIX-99',33.72,'01234565')]
    assert client.post(path, data=updated).status_code == 302
    assert client.post(path, data={**original,'name':'stary formularz'}).status_code == 409
    assert rows('products')[0]['name'] == 'Winsor krótki'


def test_edit_legacy_and_access(client):
    path, data = setup_edit(client)
    assert client.post(path,data={**data,'ean':'12345'}).status_code == 400
    assert client.post(path,data={**data,'csrf_token':'wrong'}).status_code in (400,403)
    with client.session_transaction() as sess:
        sess['internal_actor_id'] = _actor(role='MAGAZYN').actor_id
    assert client.post(path,data=data).status_code == 403
    assert 'href="'+path+'"' not in client.get('/stock').text


def test_edit_euro_conflict_rolls_back(client):
    path, data = setup_edit(client)
    db = backend.conn()
    db.execute("UPDATE pricing_eur SET sku='TAKEN'")
    db.commit()
    db.close()
    assert client.post(path,data={**data,'sku':'TAKEN'}).status_code == 409
    assert rows('products')[0]['sku'] == data['sku']
    assert rows('pricing')[0]['model'] == data['sku']


def test_remote_edit_refreshes_existing_product_and_both_price_keys(client, monkeypatch):
    path, data = setup_edit(client)
    product = rows('products')[0]
    marked_price = rows('pricing')[0]
    euro = rows('pricing_eur')[0]
    update = {**data, 'sku':'CLOUD-2', 'name':'Nowa nazwa'}
    monkeypatch.setattr(backend, 'supabase_enabled', lambda:True)
    def remote(route, method='GET', payload=None, **kwargs):
        if method == 'GET':
            assert route == '/rest/v1/products'
            return [product]
        assert route == '/rest/v1/rpc/edit_catalog_product_v1'
        assert payload['p_expected']['sku'] == data['sku']
        return {'changed':True,'id':product['id'],'products':{**product,**payload['p_new']},
                'pricing':[{**marked_price,'model':update['sku']}],
                'pricing_eur':[{**euro,'sku':update['sku']}]}
    monkeypatch.setattr(backend, 'supabase_request', remote)
    assert client.get(path).status_code == 200
    assert client.post(path, data=update).status_code == 302
    assert [(r['id'],r['sku']) for r in rows('products')] == [(product['id'],'CLOUD-2')]
    assert [(r['model'],r['gross_price']) for r in rows('pricing')] == [('CLOUD-2',123)]
    assert [(r['sku'],r['price_eur']) for r in rows('pricing_eur')] == [('CLOUD-2',33.72)]


def test_stock_news_filter_and_turn_off(client, monkeypatch):
    _, data = setup_edit(client)
    pid = rows('products')[0]['id']
    marked = {pid}
    monkeypatch.setattr(backend, 'supabase_enabled', lambda:True)
    def remote(path, **kw):
        if path.endswith('/catalog_new_arrivals'):
            return [dict(product_id=i) for i in marked]
        assert path.endswith('warehouse_set_new_arrival') and kw['payload']['p_enabled'] is False
        marked.remove(kw['payload']['p_product_id'])
        return False
    monkeypatch.setattr(backend, 'supabase_request', remote)
    page = client.get('/stock?filter=new').text
    assert data['sku'] in page and 'Wyłącz nowość' in page and 'Wyłącz filtr nowości' in page
    response = client.post(f'/products/{pid}/new-arrival',data={**data,'source':'stock','enabled':'0'}, follow_redirects=True)
    assert response.request.path == '/stock'
    assert 'Brak produktów oznaczonych jako nowości.' in response.text
    assert data['sku'] in client.get('/stock').text


def test_stock_news_failure(client, monkeypatch):
    monkeypatch.setattr(backend,'supabase_enabled',lambda:True)
    def fail(*args,**kwargs):
        raise TimeoutError()
    monkeypatch.setattr(backend,'supabase_request',fail)
    page = client.get('/stock?filter=new').text
    assert 'Nie udało się pobrać nowości.' in page
    assert 'Brak produktów oznaczonych jako nowości.' not in page
