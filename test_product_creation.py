from io import BytesIO
import re
import uuid

import pytest
import app as backend
import product_creation as creation
from test_business_operations import isolated, _owner, _actor


@pytest.fixture
def client(isolated, monkeypatch):
    monkeypatch.setattr(backend, 'supabase_enabled', lambda: False)
    with isolated.session_transaction() as state:
        state['admin_authenticated'] = True
        state['internal_actor_id'] = _owner().actor_id
        state['csrf_token'] = 'create-test'
    return isolated


def form(client, **changes):
    response = client.get('/products/new')
    assert response.status_code == 200
    token = re.search(rb'name="request_id" value="([^"]+)"', response.data)[1].decode()
    return dict(sku='NEW-BB-128', model='New model', name='Uchwyt', ean='5907998300141',
                net_price='100', gross_price='', uvp_eur='', request_id=token,
                csrf_token='create-test', **changes)


def rows(table):
    db = backend.conn()
    try:
        return [dict(row) for row in db.execute('SELECT * FROM '+table)]
    finally:
        db.close()


def test_create_prices_and_zero_stock_once(client):
    data = form(client)
    for _ in range(2):
        response = client.post('/products/new', data=data)
        assert response.status_code == 302, response.data.decode()
    assert len(rows('products')) == 1
    assert rows('stock')[0]['qty'] == 0
    assert rows('pricing')[0]['net_price'] == 100
    assert rows('pricing')[0]['gross_price'] == 123
    assert rows('pricing_eur')[0]['price_eur'] == 33.72
    assert rows('pricing_eur')[0]['uvp_eur'] == 48.89


@pytest.mark.parametrize('key,value', [('net_price','NaN'),('net_price','-1'),('net_price','Infinity'),('ean','5907998300142'),('sku','bad space')])
def test_validation_no_writes(client,key,value):
    data = form(client); data[key]=value
    assert client.post('/products/new',data=data).status_code == 400
    assert rows('products') == rows('pricing') == rows('stock') == []


def test_duplicate_and_changed_replay_preserve_original(client):
    data=form(client)
    assert client.post('/products/new',data=data).status_code == 302
    duplicate=form(client);duplicate['sku']=data['sku'].lower()
    assert client.post('/products/new',data=duplicate).status_code == 400
    data['net_price']='200'
    assert client.post('/products/new',data=data).status_code == 400
    assert rows('pricing')[0]['net_price'] == 100


def test_image_validation_before_product_commit(client):
    data=form(client);data['image']=(BytesIO(b'not an image'),'photo.jpg')
    assert client.post('/products/new',data=data).status_code == 400
    assert rows('products') == []


def test_image_attached_to_created_product(client):
    from PIL import Image
    image=BytesIO();Image.new('RGB',(4,4),'blue').save(image,format='PNG');image.seek(0)
    data=form(client);data['image']=(image,'photo.png')
    response=client.post('/products/new',data=data)
    assert response.status_code == 302, response.data.decode()
    assert rows('product_image_assignments')[0]['product_id'] == rows('products')[0]['id']
    assert len(rows('product_images')) == 1


def test_cloud_failure_does_not_create_local_only_product(client,monkeypatch):
    data=form(client)
    monkeypatch.setattr(backend,'supabase_enabled',lambda:True)
    seen=[]
    def fail(path,**kwargs):
        seen.append(kwargs['payload']);raise TimeoutError()
    monkeypatch.setattr(backend,'supabase_request',fail)
    for _ in range(2):
        assert client.post('/products/new',data=data).status_code == 503
    assert seen[0] == seen[1]
    assert rows('products') == rows('pricing') == []


def test_cloud_receipt_replay_never_resets_stock_or_prices(client,monkeypatch):
    data=form(client)
    assert client.post('/products/new',data=data).status_code == 302
    product=rows('products')[0]
    receipt={'product_id':product['id'],'products':product,'stock':rows('stock')[0],
             'pricing':rows('pricing')[0],'pricing_eur':rows('pricing_eur')[0]}
    db=backend.conn();db.execute('UPDATE stock SET qty=7');db.execute('UPDATE pricing SET net_price=120');db.commit();db.close()
    monkeypatch.setattr(backend,'supabase_enabled',lambda:True)
    monkeypatch.setattr(backend,'supabase_request',lambda *a,**kw:receipt)
    assert client.post('/products/new',data=data).status_code == 302
    assert rows('stock')[0]['qty'] == 7
    assert rows('pricing')[0]['net_price'] == 120


def test_sqlite_transaction_rolls_back_price_failure(client):
    data=form(client)
    db=backend.conn();db.execute("CREATE TRIGGER fail_price BEFORE INSERT ON pricing_eur BEGIN SELECT RAISE(ABORT,'fail'); END");db.commit();db.close()
    assert client.post('/products/new',data=data).status_code == 503
    assert rows('products') == rows('stock') == rows('pricing') == []


def test_csrf_expired_form_and_permission(client):
    data=form(client);data['csrf_token']='wrong'
    assert client.post('/products/new',data=data).status_code in (400,403)
    data['csrf_token']='create-test';data['request_id']=str(uuid.uuid4())
    assert client.post('/products/new',data=data).status_code == 400
    with client.session_transaction() as state:
        state['internal_actor_id']=_actor(role='MAGAZYN').actor_id
    assert client.get('/products/new').status_code == 403
    assert rows('products') == []


def test_shared_model_price_conflict_not_silently_ignored(client):
    data=form(client)
    db=backend.conn();db.execute("INSERT INTO pricing(model,net_price,gross_price,created_at) VALUES('New model',50,61.50,'test')");db.commit();db.close()
    assert client.post('/products/new',data=data).status_code == 400
    assert rows('products') == []
    assert rows('pricing')[0]['net_price'] == 50


def test_decimal_rounding_and_override():
    data=creation.validate({'sku':'X','model':'X','net_price':'0,23','gross_price':'0,29','uvp_eur':'1,25'})
    assert data['price_eur']=='0.08'
    assert data['uvp_eur']=='1.25'


def test_matching_shared_model_reuses_one_price(client):
    data=form(client)
    db=backend.conn();db.execute("INSERT INTO pricing(model,net_price,gross_price,created_at) VALUES('New model',100,123,'test')");db.commit();db.close()
    assert client.post('/products/new',data=data).status_code == 302
    assert len(rows('pricing')) == 1
    db=backend.conn()
    try:
        assert len(db.execute('SELECT p.id FROM products p JOIN pricing pr ON pr.model=p.model OR pr.model=p.sku').fetchall()) == 1
    finally:
        db.close()
