"""Offline only. Fake upstream + temporary SQLite; no application startup."""
import ast
import copy
import hashlib
import hmac
import json
from pathlib import Path
import socket
import sqlite3
from unittest.mock import Mock
import pytest
from flask import Flask
import requests
from orderchamp_client import OrderchampError
from orderchamp_incremental_client import (ADJUST, ORDERS_DELTA, ORDER_LINES, VARIANTS,
    IncrementalClient, connection_page)
from orderchamp_sync_engine import SyncEngine, normalize_order
from orderchamp_sync_http import SupabaseStore, register_routes, mirror_import_rows
from orderchamp_invoice import reviewed_form
from stock_availability import read_stock_availability


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def blocked(*a,**kw): raise AssertionError('network forbidden')
    monkeypatch.setattr(socket.socket,'connect',blocked)
    monkeypatch.setattr(requests.sessions.Session,'request',blocked)
    monkeypatch.delenv('ORDERCHAMP_API_TOKEN',raising=False)
    monkeypatch.setenv('ORDERCHAMP_SYNC_V2_ENABLED','0')


class Store:
    def __init__(self,rows):
        self.rows=copy.deepcopy(rows);self.data={};self.lease=None;self.enabled=True
        self.events=[];self.imported={};self.import_calls=0
    def control(self,action,token=None,data=None):
        self.events.append(action)
        if action=='claim':
            if self.lease or not self.enabled or self.data.get('review_required'): return None
            if self.data.get('intent'):
                self.data['review_required']=True;return None
            self.lease=token;return copy.deepcopy(self.data)
        if action in ('save','release','fail'):
            assert token==self.lease
            self.data=copy.deepcopy(data)
            if action!='save':self.lease=None
            return {'ok':True}
        if action=='status':return {'enabled':self.enabled,'report':self.data.get('report')}
        if action=='pause':self.enabled=False
        return {'ok':True}
    def availability(self):
        self.events.append('availability');return {'read_at':'2026-09-30T00:00:00+00:00','rows':copy.deepcopy(self.rows)}
    def import_orders(self,token,orders):
        assert token==self.lease
        self.events.append('import');self.import_calls+=1
        affected=[]
        for order in orders:
            old=self.imported.get(order['id'])
            if old and old['updated_at']>=order['updated_at']:continue
            self.imported[order['id']]=copy.deepcopy(order)
            for row in self.rows:
                before=sum(x['qty'] for x in old['items'] if x['sku']==row['sku']) if old and not old['cancelled'] else 0
                after=sum(x['qty'] for x in order['items'] if x['sku']==row['sku']) if not order['cancelled'] else 0
                row['oc_ordered']+=after-before;row['available_qty']-=after-before
                row['oc_unshipped']=sum(x['unshipped_qty'] for x in order['items'] if x['sku']==row['sku']) if not order['cancelled'] else 0
            affected.append(order['id'])
        return {'order_ids':affected}


def local(sku='A',qty=24):return {'id':1,'sku':sku,'available_qty':qty,'oc_ordered':0,'oc_unshipped':0}
def variant(sku,qty=0,reserved=0):
    return {'id':'v-'+sku,'sku':sku,'inventoryPolicy':'DENY','inventoryQuantity':qty,
        'inventoryLevels':{'nodes':[{'id':'l-'+sku,'quantity':qty,'availableQuantity':qty-reserved,
            'location':{'id':'main','isPrimary':True},'updatedAt':'now'}],'pageInfo':{'hasNextPage':False}}}
def order(qty=3,cancelled=False,updated='2026-09-30T00:00:00Z'):
    return {'id':'o1','number':'OC1','companyName':'Kupujący','companyPhone':'','email':'buyer@example.test',
      'vatNumber':'PL123','currency':'PLN','source':'marketplace','createdAt':'2026-09-29T23:00:00Z','updatedAt':updated,
      'isTest':False,'isConfirmed':True,'isFulfilled':False,'isCancelled':cancelled,
      'billingAddress':{'name':'Kupujący','street':'Testowa','houseNumber':'1','postalCode':'00-001','city':'Test','country':'PL'},
      'shippingAddress':{'name':'Kupujący','street':'Testowa','houseNumber':'1','postalCode':'00-001','city':'Test','country':'PL'},
      'subtotalPrice':'30.00','taxPrice':'6.90','totalPrice':'36.90',
      'lines':[{'id':'line1','sku':'A','quantity':qty,'unshippedQuantity':qty,'unitPrice':'10.00',
        'subtotalPrice':'30.00','taxPrice':'6.90','totalPrice':'36.90'}]}


class Client:
    def __init__(self,variants,orders=(),on_adjust=None):
        self.variants=variants;self.orders=list(orders);self.writes=[];self.request_count=0;self.on_adjust=on_adjust
    def orders_page(self,since=None,after=None):self.request_count+=1;return copy.deepcopy(self.orders),None
    def variants_page(self,after=None,skus=None):
        self.request_count+=1;offset=int(after or 0)
        selected=[v for v in self.variants if skus is None or v['sku'] in skus]
        return selected[offset:offset+50],str(offset+50) if offset+50<len(selected) else None
    def adjust_batch(self,rows,mid):
        self.request_count+=1;self.writes.extend(copy.deepcopy(rows))
        if self.on_adjust:self.on_adjust(rows)
        for row in rows:
            v=next(v for v in self.variants if v['sku']==row['sku'])
            v['inventoryQuantity']+=row['delta']
            v['inventoryLevels']['nodes'][0]['quantity']+=row['delta']
            v['inventoryLevels']['nodes'][0]['availableQuantity']+=row['delta']
    def close(self):pass


def run(store,client):return SyncEngine(store,lambda:client,clock=lambda:'2026-09-30T00:00:00+00:00').run()


def test_156_sku_one_database_snapshot_eight_remote_batches():
    rows=[local(str(i)) for i in range(156)];store=Store(rows);client=Client([variant(str(i)) for i in range(156)])
    result=run(store,client)
    assert 'error' not in result and len(client.writes)==156
    assert store.events.count('availability')==1
    assert client.request_count==2+4+8
    assert all(r['delta']==24 for r in client.writes)
    assert store.data['report']['changed_sku']==156


def test_operator_can_write_one_sku_before_enabling_the_rest():
    store=Store([local('A'),local('B')]);store.data={'only_sku':'A'}
    client=Client([variant('A'),variant('B')]);run(store,client)
    assert [r['sku'] for r in client.writes]==['A']
    assert client.variants[1]['inventoryQuantity']==0
    assert store.data['report']['only_sku']=='A'


def test_unchanged_cycle_only_one_remote_orders_request_and_one_snapshot():
    store=Store([local()]);client=Client([variant('A',24)]);run(store,client)
    client.request_count=0;run(store,client)
    assert not client.writes and client.request_count==1
    assert store.import_calls==0


def test_missing_variant_is_not_refetched_each_minute():
    store=Store([local()]);client=Client([]);run(store,client)
    client.request_count=0;run(store,client)
    assert client.request_count==1
    assert store.data['report']['skipped'][0]['reason']=='REMOTE_SKU_NOT_FOUND'


def test_periodic_compare_repairs_drift_without_set_or_duplicate_reservation():
    store=Store([local()]);client=Client([variant('A',24)]);run(store,client)
    client.variants[0]=variant('A',20)
    result=SyncEngine(store,lambda:client,clock=lambda:'2026-09-30T00:16:00+00:00').run()
    assert 'error' not in result and client.writes[-1]['delta']==4
    assert client.variants[0]['inventoryLevels']['nodes'][0]['availableQuantity']==24


def test_periodic_compare_invalidates_anchor_when_remote_scope_changes():
    store=Store([local()]);client=Client([variant('A',24)]);run(store,client)
    client.variants[0]['inventoryPolicy']='CONTINUE'
    SyncEngine(store,lambda:client,clock=lambda:'2026-09-30T00:16:00+00:00').run()
    store.rows[0]['available_qty']=30
    SyncEngine(store,lambda:client,clock=lambda:'2026-09-30T00:17:00+00:00').run()
    assert not client.writes
    assert store.data['report']['skipped'][0]['reason']=='REMOTE_STOCK_SCOPE_UNSAFE'


def test_sale_import_does_not_subtract_reservation_twice_and_duplicate_is_idempotent():
    store=Store([local()]);client=Client([variant('A',24)]);run(store,client)
    client.orders=[order()];level=client.variants[0]['inventoryLevels']['nodes'][0];level['availableQuantity']=21
    run(store,client);run(store,client)
    assert store.rows[0]['available_qty']==21 and store.rows[0]['oc_ordered']==3
    assert len(store.imported)==1 and not client.writes
    client.orders=[order(cancelled=True,updated='2026-09-30T00:01:00Z')];level['availableQuantity']=24
    run(store,client)
    assert store.rows[0]['available_qty']==24 and not client.writes


def test_sale_between_snapshot_and_adjust_is_preserved():
    store=Store([local()]);v=variant('A',20)
    def sale(_):v['inventoryLevels']['nodes'][0]['availableQuantity']-=3
    client=Client([v],on_adjust=sale);run(store,client)
    assert client.writes[0]['delta']==4
    assert v['inventoryLevels']['nodes'][0]['quantity']==24
    assert v['inventoryLevels']['nodes'][0]['availableQuantity']==21


def test_later_local_stock_delta_and_zero_are_exported_without_catalog_read():
    store=Store([local()]);client=Client([variant('A',24)]);run(store,client)
    store.rows[0]['available_qty']=0;client.request_count=0;run(store,client)
    assert client.writes[-1]['delta']==-24 and client.request_count==2
    store.rows[0]['available_qty']=5;run(store,client)
    assert client.writes[-1]['delta']==5


def test_initial_anchor_defers_sale_not_imported_yet():
    store=Store([local()]);client=Client([variant('A',24,3)]);run(store,client)
    assert not client.writes
    assert store.data['report']['skipped'][0]['reason']=='ORDERS_NOT_YET_RECONCILED'


def test_transport_unknown_is_durable_and_never_retried():
    store=Store([local()])
    def failure(rows):
        assert store.data.get('intent')
        raise OrderchampError('MUTATION_OUTCOME_UNKNOWN')
    client=Client([variant('A')],on_adjust=failure)
    assert run(store,client)['error']=='MUTATION_OUTCOME_UNKNOWN'
    assert store.data['review_required']
    assert run(store,client)=={'claimed':False} and len(client.writes)==1


def test_crash_after_durable_intent_is_not_replayed():
    store=Store([local()]);store.data={'intent':{'id':'pending'}};client=Client([variant('A')])
    assert run(store,client)=={'claimed':False} and not client.writes


def test_restart_discards_an_old_unexecuted_plan_and_reads_current_stock():
    store=Store([local('A',0)])
    store.data={'phase':'write','anchors':{'A':{'level_id':'l-A','basis':0}},
        'plan':[{'sku':'A','level_id':'l-A','delta':24,'basis':24}]}
    client=Client([variant('A',0)])
    assert 'error' not in run(store,client)
    assert not client.writes


def test_db_failure_stops_entire_batch_without_zeroing_remote():
    store=Store([local()]);store.availability=Mock(side_effect=OrderchampError('SYNC_DATABASE_UNAVAILABLE'))
    client=Client([variant('A',24)])
    assert run(store,client)['error']=='SYNC_DATABASE_UNAVAILABLE' and not client.writes


def test_missing_variant_not_created_and_unsafe_policy_skipped():
    v=variant('B');v['inventoryPolicy']='CONTINUE'
    store=Store([local('A'),local('B')]);client=Client([v]);run(store,client)
    assert not client.writes and len(store.data['report']['skipped'])==2


def test_order_prices_and_buyer_are_retained():
    result=normalize_order(order())
    assert result['items'][0]['unit_net']=='10.00' and result['items'][0]['unit_gross']=='12.30'
    assert result['company']=='Kupujący' and result['currency']=='PLN' and result['number']=='OC1'


@pytest.mark.parametrize('field,value',[('quantity',0),('quantity',True),('subtotalPrice','NaN'),('subtotalPrice',None)])
def test_invalid_order_never_imported(field,value):
    source=order();source['lines'][0][field]=value
    with pytest.raises(OrderchampError):normalize_order(source)


def test_invoice_requires_explicit_type_and_buyer_review():
    context={'currency':'EUR'}
    assert not reviewed_form({'currency':'EUR'},context)
    assert reviewed_form({'currency':'EUR','oc_billing_reviewed':'1','invoice_type':'domestic','buyer_name':'Firma','buyer_country':'PL'},context)


def test_saved_orderchamp_invoice_type_survives_edit_and_pdf_regeneration():
    from orderchamp_invoice import saved_tax_context
    fallback=Mock(return_value=('wdt','EUR','DE'))
    invoice={'invoice_type':'domestic','currency':'EUR','buyer_country':'PL'}
    assert saved_tax_context({'order_no':'OC-test'},invoice,fallback)==('domestic','EUR','PL')
    fallback.assert_not_called()
    assert saved_tax_context({'order_no':'ZAM-1'},invoice,fallback)==('wdt','EUR','DE')


def test_fractional_transaction_price_cannot_silently_round_on_invoice():
    from orderchamp_invoice import needs_price_review
    regular=normalize_order(order());assert not needs_price_review(regular)
    source=order();source['lines'][0]['subtotalPrice']='10.00'
    assert needs_price_review(normalize_order(source))


def test_queries_parse_and_no_set_mutation_in_new_client():
    from graphql import parse
    for query in (ORDERS_DELTA,ORDER_LINES,VARIANTS,ADJUST):parse(query)
    assert '"SET"' not in Path('orderchamp_incremental_client.py').read_text()


def test_schema_parses():
    from pglast import parse_sql
    for path in ('sql/orderchamp_sync_v2.sql','sql/orderchamp_sync_preflight_readonly.sql','sql/orderchamp_sync_wake_optional.sql'):
        parse_sql(Path(path).read_text(encoding='utf-8'))


def test_global_auto_sync_excludes_all_orderchamp_endpoints():
    tree=ast.parse(Path('app.py').read_text(encoding='utf-8'))
    method=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='auto_sync_after_write')
    method.decorator_list=[]
    fn=ast.Module(body=[method],type_ignores=[])
    trigger=Mock();scope={'CLIENT_API_PATHS':set(),'norm':lambda x:x or '',
        'trigger_background_supabase_sync':trigger}
    exec(compile(fn,'isolated-hook','exec'),scope)
    for path in ('/api/admin/orderchamp/control','/api/admin/orderchamp/dry-run','/webhooks/orderchamp','/api/internal/orderchamp/tick'):
        scope['request']=type('R',(),{'path':path,'method':'POST','headers':{}})()
        response=type('Response',(),{'status_code':200,'headers':{}})()
        scope['auto_sync_after_write'](response)
    trigger.assert_not_called()
    scope['request'].path='/orders/create';scope['auto_sync_after_write'](response);trigger.assert_called_once()


def webapp(monkeypatch,store):
    import orderchamp_http as guards
    import orderchamp_sync_http as http
    app=Flask(__name__,template_folder='templates');app.secret_key='synthetic-session-secret'
    monkeypatch.setattr(http.Worker,'start',lambda self:None)
    monkeypatch.setattr(http.Worker,'wake',lambda self:None)
    register_routes(app,{},store=store,client_factory=lambda:pytest.fail('No upstream call on request'))
    return app,guards


def test_http_owner_csrf_and_body_protection(monkeypatch):
    import internal_rbac as rbac
    store=Store([local()]);app,guards=webapp(monkeypatch,store)
    browser=app.test_client()
    assert browser.post('/api/admin/orderchamp/control',json={'action':'enable'}).status_code==401
    actor=type('Actor',(),{'actor_type':rbac.ACTOR_HUMAN,'roles':['OWNER'],
        'permission_decision':lambda self,p:rbac.ALLOW})()
    monkeypatch.setattr(guards,'current_actor_context',lambda:actor)
    with browser.session_transaction() as sess:sess.update(admin_authenticated=True,csrf_token='csrf')
    monkeypatch.setenv('ORDERCHAMP_SYNC_V2_ENABLED','1')
    assert browser.post('/api/admin/orderchamp/control',json={'action':'enable'}).status_code==403
    assert not store.events
    response=browser.post('/api/admin/orderchamp/control',json={'action':'queue'},headers={'X-CSRF-Token':'csrf'})
    assert response.status_code==202 and store.events==['queue']
    actor.roles=['MAGAZYN']
    assert browser.get('/api/admin/orderchamp/status').status_code==403
    assert browser.post('/api/admin/orderchamp/push-one',json={}).status_code==404


def test_webhook_hmac_account_and_replay_only_wake_job(monkeypatch):
    store=Store([local()]);app,_=webapp(monkeypatch,store);browser=app.test_client()
    monkeypatch.setenv('ORDERCHAMP_SYNC_V2_ENABLED','1')
    monkeypatch.setenv('ORDERCHAMP_WEBHOOK_SECRET','test-secret');monkeypatch.setenv('ORDERCHAMP_ACCOUNT_ID','a1')
    raw=b'{"data":{"order":{"id":"o1"}}}'
    headers={'X-Orderchamp-Account-Id':'a1','X-Orderchamp-Event':'ORDER_CREATED'}
    assert browser.post('/webhooks/orderchamp',data=raw,headers=headers).status_code==403
    headers['X-Orderchamp-Signature']=hmac.new(b'test-secret',raw,hashlib.sha256).hexdigest()
    assert browser.post('/webhooks/orderchamp',data=raw,headers=headers).status_code==200
    assert browser.post('/webhooks/orderchamp',data=raw,headers=headers).status_code==200
    assert store.events==['wake','wake'] and not store.imported
    headers['X-Orderchamp-Account-Id']='other'
    assert browser.post('/webhooks/orderchamp',data=raw,headers=headers).status_code==403


def test_machine_trigger_requires_separate_secret(monkeypatch):
    app,_=webapp(monkeypatch,Store([local()]));browser=app.test_client()
    monkeypatch.setenv('ORDERCHAMP_SYNC_V2_ENABLED','1')
    assert browser.post('/api/internal/orderchamp/tick').status_code==403
    monkeypatch.setenv('ORDERCHAMP_SYNC_TRIGGER_TOKEN','x'*32)
    assert browser.post('/api/internal/orderchamp/tick',headers={'Authorization':'Bearer '+'x'*32}).status_code==202


def test_bulk_adjust_is_one_request_and_ambiguous_response_is_not_retried():
    from test_orderchamp_stock import Session, Response
    session=Session([Response(status=502)])
    client=IncrementalClient('test-token',session=session,sleep=lambda _:None)
    with pytest.raises(OrderchampError,match='MUTATION_OUTCOME_UNKNOWN'):
        client.adjust_batch([{'level_id':'l1','delta':4}],'m1')
    assert len(session.calls)==1
    assert session.calls[0][1]['json']['variables']['input']['inventoryLevels'][0]['action']=='ADJUST'


def test_shortage_never_increases_a_previously_anchored_sku():
    store=Store([local('A',0)]);client=Client([variant('A',0)]);run(store,client)
    store.rows[0].update(oc_ordered=3,oc_unshipped=3,shortage_qty=3)
    run(store,client)
    assert not client.writes and store.data['report']['skipped'][0]['reason']=='OVERCOMMITTED_STOCK_REVIEW'


def test_new_sale_during_first_catalogue_read_is_reconciled_before_write():
    store=Store([local()]);client=Client([variant('A',20)])
    client.orders_page=Mock(side_effect=[([],None),([order()],None)])
    assert run(store,client)['error']=='ORDERS_CHANGED_DURING_CATALOG'
    assert not client.writes and store.data['phase']=='orders'


def test_templates_and_modified_python_compile():
    import jinja2
    for path in ('app.py','inventory_analytics.py','orderchamp_sync_http.py','orderchamp_sync_engine.py','routes/invoices.py'):
        compile(Path(path).read_text(encoding='utf-8'),path,'exec')
    jinja2.Environment().parse(Path('templates/orderchamp_sync.html').read_text(encoding='utf-8'))
    tree=ast.parse(Path('routes/invoices.py').read_text(encoding='utf-8'))
    templates=[n.value.value for n in ast.walk(tree) if isinstance(n,ast.Assign) and isinstance(n.value,ast.Constant)
        and isinstance(n.value.value,str) and '<form' in n.value.value]
    assert templates
    for template in templates:jinja2.Environment().parse(template)


def test_scoped_sqlite_mirror_commits_all_rows_and_rolls_back_on_conflict():
    db=sqlite3.connect(':memory:')
    db.executescript('''
      CREATE TABLE customers(id INTEGER PRIMARY KEY,name TEXT);
      CREATE TABLE orders(id INTEGER PRIMARY KEY,customer_id INTEGER,status TEXT);
      CREATE TABLE order_items(id INTEGER PRIMARY KEY,order_id INTEGER,qty INTEGER);
      CREATE TABLE invoice_allocations(id INTEGER PRIMARY KEY,order_item_id INTEGER,qty INTEGER);
    ''')
    payload={'order_ids':[2],'customers':[{'id':1,'name':'Buyer'}],
      'orders':[{'id':2,'customer_id':1,'status':'new'}],
      'order_items':[{'id':3,'order_id':2,'qty':4}]}
    mirror_import_rows(db,payload);mirror_import_rows(db,payload)
    assert db.execute('SELECT count(*),sum(qty) FROM order_items').fetchone()==(1,4)
    db.execute('INSERT INTO invoice_allocations VALUES(1,3,1)');db.commit()
    changed=copy.deepcopy(payload);changed['orders'][0]['status']='cancelled'
    changed['order_items'][0]['id']=4
    with pytest.raises(OrderchampError,match='LOCAL_IMPORT_CONFLICT'):mirror_import_rows(db,changed)
    assert db.execute('SELECT status FROM orders').fetchone()[0]=='new'
    assert db.execute('SELECT id FROM order_items').fetchone()[0]==3
    db.close()


def test_scoped_mirror_preserves_existing_domain_guards():
    db=sqlite3.connect(':memory:')
    db.executescript('''CREATE TABLE customers(id INTEGER PRIMARY KEY,name TEXT);
      CREATE TABLE orders(id INTEGER PRIMARY KEY,status TEXT);
      CREATE TABLE order_items(id INTEGER PRIMARY KEY,order_id INTEGER,qty INTEGER);
      CREATE TABLE invoice_allocations(order_item_id INTEGER);
      INSERT INTO orders VALUES(2,'delivered');''')
    payload={'order_ids':[2],'customers':[], 'orders':[{'id':2,'status':'new'}],
      'order_items':[{'id':3,'order_id':2,'qty':4}]}
    def protect(db,rows):return [{**r,'status':db.execute('SELECT status FROM orders WHERE id=?',(r['id'],)).fetchone()[0]} for r in rows]
    mirror_import_rows(db,payload,protect)
    assert db.execute('SELECT status FROM orders').fetchone()[0]=='delivered'
    db.close()
