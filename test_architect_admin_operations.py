from types import SimpleNamespace
import pytest
import app as b
import routes.architects as a
from test_auth_resilience import admin_client
from architect_sample_shipping import linked_sample,require_complete_pack,validate_sample_items

OWNER='11111111-1111-4111-8111-111111111111'
SAMPLE='22222222-2222-4222-8222-222222222222'

def login(client,monkeypatch):
    monkeypatch.setattr(b,'_rate_hits',{})
    client.post('/login',data={'username':'admin','password':'correct-test-only'})
    with client.session_transaction() as state: return state['csrf_token']

def fixture_remote(path,**kwargs):
    if path.endswith('/architect_profiles'):
        return [dict(id=OWNER,first_name='Anna',last_name='Kowalska',studio_name='Studio & Dom',
                     email='qa@example.invalid',instagram='https://www.instagram.com/test/',
                     created_at='2026-10-30T17:16:53.014126+00:00',status='approved',sample_entitlements=1,removed_at=None)]
    if path.endswith('/architect_sample_orders'):
        return [dict(id=SAMPLE,architect_id=OWNER,status='requested',tracking_no=None,fulfillment_order_id=None,
                     created_at='2026-10-30T17:16:53.014126+00:00',shipping_address='Testowa 1, 00-001 Warszawa')]
    if path.endswith('/architect_sample_items'):
        return [dict(sample_order_id=SAMPLE,product_id=1,qty=1,cancelled_at=None)]
    if path.endswith('/client_stock_catalog_v'): return [dict(product_id=1,sku='TEST-SKU',model='Model',name='Uchwyt')]
    if path.endswith('/architect_codes'):
        return [dict(id='code',architect_id=OWNER,distributor_id=3,code='KOWALSKA-1234',status='redeemed',expires_at='2027-01-30')]
    return []

def test_clear_labels_and_sample_actions(admin_client,monkeypatch):
    login(admin_client,monkeypatch)
    monkeypatch.setattr(a,'supabase_request',fixture_remote)
    html=admin_client.get('/architects?view=settings').get_data(as_text=True)
    assert '30.10.2026' in html and '014126' not in html
    assert 'Anna Kowalska' in html and 'TEST-SKU' in html
    assert 'Anuluj próbkę' in html and 'Trwale usuń z programu' in html
    assert 'Przygotuj paczkę w magazynie' in html
    assert 'Studio &amp; Dom' in html
    assert '>requested<' not in html and '>approved<' not in html
    assert 'Zobacz Instagram' in html

def test_codes_tile_opens_real_filtered_list(admin_client,monkeypatch):
    login(admin_client,monkeypatch)
    monkeypatch.setattr(a,'supabase_request',fixture_remote)
    response=admin_client.get('/architects?view=codes&code_status=redeemed')
    assert response.status_code==200
    assert 'KOWALSKA-1234' in response.get_data(as_text=True)
    assert 'KOWALSKA-1234' not in admin_client.get('/architects?view=codes&code_status=active').get_data(as_text=True)

def test_archived_owner_hidden_but_history_names_remain(admin_client,monkeypatch):
    login(admin_client,monkeypatch)
    def remote(path,**kwargs):
        data=fixture_remote(path,**kwargs)
        if path.endswith('/architect_profiles'): data[0].update(removed_at='2026-10-31',status='suspended')
        return data
    monkeypatch.setattr(a,'supabase_request',remote)
    response=admin_client.get('/architects?view=settings')
    html=response.get_data(as_text=True)
    assert 'Anna Kowalska' not in html.split('id="architect-samples"')[0]
    assert 'Anna' in html.split('id="architect-samples"')[1]
    archive=admin_client.get('/architects?view=settings&show_removed=1').get_data(as_text=True)
    assert 'Konto trwale zamknięte' in archive and 'Trwale usuń z programu' not in archive

@pytest.mark.parametrize('action,extra,rpc',[('remove_architect',dict(architect_id=OWNER,reason='Konto zamknięte na prośbę',confirmation='USUŃ'),'architect_remove_from_program'),
    ('cancel_sample_item',dict(sample_id=SAMPLE,product_id='1'),'architect_cancel_sample_item')])
def test_admin_operations_use_server_rpc(admin_client,monkeypatch,action,extra,rpc):
    token=login(admin_client,monkeypatch)
    calls=[]
    monkeypatch.setattr(a,'supabase_request',lambda path,**kwargs:calls.append((path,kwargs)))
    response=admin_client.post('/architects',data=dict(csrf_token=token,action=action,**extra))
    assert response.status_code==302
    assert calls[0][0].endswith('/'+rpc)
    assert calls[0][1]['payload']['p_actor']=='admin'

def test_delete_requires_confirmation_and_csrf(admin_client,monkeypatch):
    token=login(admin_client,monkeypatch)
    calls=[]
    monkeypatch.setattr(a,'supabase_request',lambda *args,**kwargs:calls.append(1))
    assert admin_client.post('/architects',data=dict(csrf_token=token,action='remove_architect',architect_id=OWNER,reason='Powód dostatecznie długi')).status_code==400
    assert not calls
    assert admin_client.post('/architects',data=dict(action='remove_architect',architect_id=OWNER,confirmation='USUŃ',reason='Powód dostatecznie długi')).status_code in (400,403)
    assert not calls

def test_prepare_shipping_redirects_only_after_rpc_and_sync(admin_client,monkeypatch):
    token=login(admin_client,monkeypatch)
    calls=[]
    monkeypatch.setattr(a,'supabase_request',lambda path,**kwargs:calls.append(path) or 42)
    monkeypatch.setattr(a,'maybe_pull_shared_from_supabase',lambda **kwargs:calls.append('sync'))
    response=admin_client.post('/architects',data=dict(csrf_token=token,action='prepare_sample_shipping',sample_id=SAMPLE))
    assert response.status_code==302 and response.location.endswith('/orders/42/packing-list')
    assert calls==['/rest/v1/rpc/architect_prepare_sample_shipping','sync']

def test_normal_orders_do_not_call_sample_service():
    def fail(*args,**kwargs): raise AssertionError('Unexpected service call')
    assert linked_sample(SimpleNamespace(supabase_request=fail),1,{'idempotency_key':'normal'}) is None

def test_sample_ownership_must_be_verified_and_already_issued():
    backend=SimpleNamespace(supabase_request=lambda *args,**kwargs:[dict(id=SAMPLE,status='preparing')])
    assert linked_sample(backend,42,dict(idempotency_key='architect-sample:'+SAMPLE,warehouse_issued=1))
    with pytest.raises(ValueError): linked_sample(backend,42,dict(idempotency_key='architect-sample:wrong',warehouse_issued=1))
    with pytest.raises(ValueError): linked_sample(backend,42,dict(idempotency_key='architect-sample:'+SAMPLE,warehouse_issued=0))

def test_partial_or_altered_sample_pack_is_rejected():
    expected=[dict(id=3,product_id=1,qty=1),dict(id=4,product_id=2,qty=1)]
    require_complete_pack(expected,expected)
    with pytest.raises(ValueError): require_complete_pack(expected,expected[:1])
    backend=SimpleNamespace(supabase_request=lambda *args,**kwargs:[dict(product_id=1,qty=1),dict(product_id=2,qty=1)])
    validate_sample_items(backend,SAMPLE,expected)
    with pytest.raises(ValueError): validate_sample_items(backend,SAMPLE,[dict(product_id=9,qty=1)])

@pytest.fixture
def sample_warehouse(admin_client,monkeypatch):
    import routes.shipping as shipping
    login(admin_client,monkeypatch)
    monkeypatch.setattr(shipping,'maybe_pull_shared_from_supabase',lambda **kwargs:None)
    monkeypatch.setattr(b,'maybe_pull_shared_from_supabase',lambda **kwargs:None)
    def remote(path,**kwargs):
        if path.endswith('/architect_sample_orders'): return [dict(id=SAMPLE,status='preparing',fulfillment_order_id=42)]
        if path.endswith('/architect_sample_items'): return [dict(product_id=501,qty=1)]
        return []
    monkeypatch.setattr(shipping,'supabase_request',remote)
    monkeypatch.setattr(b,'supabase_request',remote)
    monkeypatch.setattr(shipping,'supabase_enabled',lambda:False)
    db=b.conn()
    db.execute("INSERT INTO products(id,sku,model,name,created_at) VALUES(501,'QA-SAMPLE','Testowy','Próbka','2026-10-31')")
    db.execute("INSERT INTO stock(product_id,qty) VALUES(501,0)")
    db.execute("INSERT INTO orders(id,order_no,customer_name,customer_email,customer_address,status,created_at,idempotency_key,warehouse_issued) VALUES(42,'PROBKI-42','Testowa Osoba','qa@example.invalid','Testowa 1 00-001 Warszawa','confirmed','2026-10-31',?,1)",('architect-sample:'+SAMPLE,))
    db.execute("INSERT INTO orders(id,order_no,customer_name,customer_email,status,created_at) VALUES(43,'NORMAL-43','Testowa Osoba','qa@example.invalid','confirmed','2026-10-31')")
    db.execute("INSERT INTO order_items(id,order_id,product_id,sku,qty,created_at) VALUES(420,42,501,'QA-SAMPLE',1,'2026-10-31')")
    db.execute("INSERT INTO order_items(id,order_id,product_id,sku,qty,created_at) VALUES(430,43,501,'QA-SAMPLE',1,'2026-10-31')")
    db.commit();db.close()
    return admin_client

def test_warehouse_sample_form_keeps_reserved_stock_and_excludes_other_orders(sample_warehouse):
    response=sample_warehouse.get('/orders/42/packing-list?carrier=inpost')
    assert response.status_code==200
    assert 'QA-SAMPLE' in response.text and 'PROBKI-42' in response.text
    assert 'NORMAL-43' not in response.text
    assert 'name="pack_qty_420" value="1"' in response.text
    assert 'Dalej: wybierz kuriera' in response.text
    assert 'catalog_confirmed' in response.text

def test_sample_invoice_is_blocked_even_for_direct_link(sample_warehouse,monkeypatch):
    import routes.invoices as invoices
    import routes.shipping as shipping
    monkeypatch.setattr(invoices,'supabase_request',shipping.supabase_request)
    response=sample_warehouse.get('/orders/42/invoice')
    assert response.status_code==302 and response.location.endswith('/orders/42/packing-list')

def test_sample_packing_needs_full_quantity_and_printed_catalog(sample_warehouse):
    import packing_correction
    db=b.conn();version=packing_correction.form_version(db,42);db.close()
    for extra in [dict(pack_qty_420='0',catalog_confirmed='1'),dict(pack_qty_420='1')]:
        response=sample_warehouse.post('/orders/42/packing-list',data=dict(csrf_token='test',carrier='inpost',packing_form_version=version,**extra))
        # Real CSRF token rather than invoking a view without middleware.
        if response.status_code==403:
            with sample_warehouse.session_transaction() as state: token=state['csrf_token']
            response=sample_warehouse.post('/orders/42/packing-list',data=dict(csrf_token=token,carrier='inpost',packing_form_version=version,**extra))
        assert response.status_code==400
    db=b.conn()
    assert db.execute('SELECT qty FROM stock WHERE product_id=501').fetchone()[0]==0
    assert db.execute('SELECT COUNT(*) FROM packing_batches').fetchone()[0]==0
    db.close()

def test_sample_pack_publishes_once_and_opens_existing_carrier_screen(sample_warehouse,monkeypatch):
    import packing_correction, packing_versions
    import routes.shipping as shipping
    from pathlib import Path
    # A local PDF substitute isolates formatting from the warehouse transaction.
    path=Path(b.DB_PATH).with_suffix('.pdf');path.write_bytes(b'%PDF-1.4\n%%EOF')
    monkeypatch.setattr(shipping,'generate_invoice_packing_list_pdf',lambda *args,**kwargs:str(path))
    monkeypatch.setattr(shipping,'complete_orders_packed_side_effects',lambda *args,**kwargs:pytest.fail('B2B sales e-mail used for samples'))
    db=b.conn();version=packing_correction.form_version(db,42);db.close()
    with sample_warehouse.session_transaction() as state: token=state['csrf_token']
    result=sample_warehouse.post('/orders/42/packing-list',data=dict(csrf_token=token,carrier='inpost',packing_form_version=version,pack_qty_420='1',catalog_confirmed='1'))
    assert result.status_code==302, result.text
    assert 'after_invoice=1' in result.location and '/invoice?' not in result.location
    db=b.conn()
    assert db.execute('SELECT qty FROM stock WHERE product_id=501').fetchone()[0]==0
    assert db.execute('SELECT COUNT(*) FROM invoices').fetchone()[0]==0
    current=packing_versions.current_for_order(db,42)
    assert current['order_ids']==[42]
    db.close()
    assert 'InPost' in sample_warehouse.get(result.location).text
    # Existing shipment overview handles the very same LP.
    response=sample_warehouse.get('/shipments')
    assert response.status_code==200
    assert 'Nadaj pakiet próbek' in response.text and 'Wystaw fakturę' not in response.text
