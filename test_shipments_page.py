import copy
from types import SimpleNamespace
import app as b
import shipments_page as page
from test_tracking_parcel_version import ready, packing_cloud, multi_order_flow, token, submit
from test_packing_correction import pack, document

def local_cards():
    rows,_=page.records(b)
    return page.cards_from_records(rows)

def test_edit_reuses_one_card_and_tracking_correction_reuses_shipment(ready,monkeypatch):
    client,_,_,sent=ready
    monkeypatch.setattr(b,'supabase_enabled',lambda:False)
    key=local_cards()[0]['key']
    proceed=client.get('/shipments/'+key+'/continue')
    assert proceed.status_code==302 and '/invoice' in proceed.location
    assert pack(client,qty=1).status_code==302
    assert len(local_cards())==1 and local_cards()[0]['key']==key
    assert local_cards()[0]['total']==3
    assert submit(client,'WRONG',token()).status_code==302
    assert submit(client,'RIGHT',token()).status_code==302
    cards=local_cards()
    assert len(cards)==1 and cards[0]['tracking']=='RIGHT' and cards[0]['history_count']==1
    assert client.get('/shipments/'+key+'/continue').status_code==409
    view=client.get('/shipments')
    assert view.status_code==200 and view.text.count('data-shipment=')==1
    assert 'Historia wysyłek (1)' in view.text and 'RIGHT' in view.text
    assert not sent

def test_listing_does_not_modify_stock_or_create_documents(ready,monkeypatch):
    client,_,_,_=ready
    monkeypatch.setattr(b,'supabase_enabled',lambda:False)
    db=b.conn(); before=list(map(tuple,db.execute('SELECT * FROM stock'))); batches=db.execute('SELECT COUNT(*) FROM packing_batches').fetchone()[0]; db.close()
    assert client.get('/shipments').status_code==200
    db=b.conn(); assert list(map(tuple,db.execute('SELECT * FROM stock')))==before
    assert db.execute('SELECT COUNT(*) FROM packing_batches').fetchone()[0]==batches; db.close()
    key=local_cards()[0]['key']
    pdf=client.get('/shipments/'+key+'/packing-list')
    assert pdf.status_code==302 and pdf.location.endswith(document()['file_hash'])
    with client.session_transaction() as s: s.pop('admin_authenticated',None)
    assert client.get('/shipments').status_code==302
    assert client.get('/shipments/'+key+'/packing-list').status_code==302

def test_remote_cold_and_warm_reads_omit_document_bytes(ready,monkeypatch):
    _,cloud,_,_=ready
    calls=[]
    def remote(path,params,**kw):
        calls.append(params)
        if params['select']=='order_id,revision':
            return [dict(order_id=oid,revision=r['revision']) for oid,r in cloud.items()]
        assert 'pdf' not in params['select'] and 'fulfillment_document' not in params['select']
        return [dict(order_id=oid,revision=r['revision'],**{k:copy.deepcopy(r['payload'].get(k,[])) for k in page.SECTIONS}) for oid,r in cloud.items()]
    monkeypatch.setattr(b,'supabase_request',remote)
    page._cache.clear()
    first,_=page.records(b); second,_=page.records(b)
    assert first==second and len(calls)==3
    assert len(page.cards_from_records(first))==1 # combined orders, one parcel
    assert calls[-1]['select']=='order_id,revision'

def test_unavailable_cloud_does_not_claim_empty_success(ready,monkeypatch):
    client,_,_,_=ready
    monkeypatch.setattr(page,'records',lambda *a: (_ for _ in ()).throw(TimeoutError()))
    view=client.get('/shipments')
    assert view.status_code==503 and 'Nie udało się odczytać' in view.text
    assert client.get('/shipments/example/continue').status_code==503

def test_partial_shipments_stay_separate_and_invoice_action_uses_real_route(ready,monkeypatch):
    from test_packing_previous_shipment import old_shipment
    client,_,_,_=ready
    monkeypatch.setattr(b,'supabase_enabled',lambda:False)
    key=local_cards()[0]['key']
    old_shipment()
    cards=local_cards()
    assert len(cards)==2 and sum(c['sent'] for c in cards)==1
    assert next(c for c in cards if c['key']==key)['tracking']==''
    db=b.conn()
    db.execute("UPDATE packing_batches SET invoice_id=301 WHERE packing_list_id=?",(key,))
    db.execute("UPDATE invoices SET publication_state='complete' WHERE id=301")
    db.commit(); db.close()
    response=client.get('/shipments/'+key+'/continue')
    assert response.status_code==302 and response.location.endswith('/packing-list')
