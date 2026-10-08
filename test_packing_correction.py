import copy
import hashlib
import json
import re
from pathlib import Path

import pytest
import app as b
import packing_correction as service
import packing_versions
import reconciliation_store
from test_packing_ui_reconciliation import packing_cloud
from test_multi_order_packing_agent import multi_order_flow


def version():
    db=b.conn()
    try: return service.form_version(db, 103)
    finally: db.close()


def pack(client, qty=3, token=None):
    return client.post('/orders/103/packing-list', data=dict(csrf_token='csrf', carrier='pending',
        packing_form_version=token if token is not None else version(),
        pack_qty_1001=str(qty), pack_qty_1002='0', pack_qty_1003='2'))


def document():
    db=b.conn()
    try: return packing_versions.saved_documents_for_order(db,103)[0]
    finally: db.close()


@pytest.fixture
def ready(packing_cloud, monkeypatch):
    client, cloud, calls=packing_cloud
    def pdf(order, items, meta, *args):
        path=Path(b.DATA_DIR)/'rendered.pdf'
        path.write_bytes(b'%PDF-1.4\n'+json.dumps(items,sort_keys=True).encode())
        return str(path)
    monkeypatch.setattr(b,'generate_invoice_packing_list_pdf',pdf)
    with b.app.test_request_context(): b._refresh_domain_route_context()
    assert pack(client).status_code==302
    sent=[]
    def send(*args,**kwargs):
        # The durable RUNNING receipt must exist before contacting the provider.
        receipts=cloud[103]['payload']['fulfillment_verifications']
        assert any(json.loads(r['payload']).get('status')=='running' for r in receipts)
        sent.append((args,kwargs))
        return {'ok':True,'body':{'id':'provider-1'}}
    monkeypatch.setattr(b,'send_email',send)
    return client,cloud,calls,sent


def send_url(): return '/orders/103/packing-correction/'+document()['file_hash']


def test_explicit_preview_and_double_click_send_once(ready):
    client,cloud,_,sent=ready
    url=send_url()
    page=client.get(url)
    assert page.status_code==200
    assert '5 szt.' in page.text.replace('<b>','').replace('</b>','')
    assert not sent
    assert client.post(url,data={'csrf_token':'csrf'}).status_code==200
    assert client.post(url,data={'csrf_token':'csrf'}).status_code==200
    assert len(sent)==1
    args,kw=sent[0]
    assert args[0]=='art@example.invalid'
    assert 'Poprawiona lista pakowa' in args[1]
    assert 'ZAM-2608271' in args[1] and 'ZAM-2609151' in args[1]
    assert kw['idempotency_key'].startswith('packing-correction/')
    assert kw['attachments'][0]['content'].startswith(b'%PDF')
    assert json.loads(cloud[103]['payload']['fulfillment_verifications'][0]['payload'])['status']=='accepted'


def test_unknown_delivery_is_not_retried_after_cache_restore(ready, monkeypatch):
    client,cloud,_,sent=ready
    def timeout(*args,**kw):
        sent.append(1)
        raise TimeoutError('lost acknowledgement')
    monkeypatch.setattr(b,'send_email',timeout)
    url=send_url()
    assert client.post(url,data={'csrf_token':'csrf'}).status_code==409
    db=b.conn()
    db.execute('DELETE FROM fulfillment_verifications')
    db.execute('DELETE FROM fulfillment_reconciliation_versions WHERE order_id=103')
    db.commit();db.close()
    reconciliation_store.restore(b,103)
    assert client.post(url,data={'csrf_token':'csrf'}).status_code==409
    assert len(sent)==1


def test_no_send_without_durable_claim(ready,monkeypatch):
    client,_,_,sent=ready
    def fail(*a,**k): raise TimeoutError('cloud down')
    monkeypatch.setattr(reconciliation_store,'publish',fail)
    assert client.post(send_url(),data={'csrf_token':'csrf'}).status_code==503
    assert not sent


def test_stale_form_does_not_replace_new_packing(ready):
    client,_,_,sent=ready
    old=version()
    assert pack(client,qty=2,token=old).status_code==302
    assert pack(client,qty=1,token=old).status_code==409
    assert document()['total_qty']==4
    assert not sent


def test_stale_mail_preview_cannot_send_previous_document(ready):
    client,_,_,sent=ready
    url=send_url()
    assert pack(client,qty=2).status_code==302
    assert client.post(url,data={'csrf_token':'csrf'}).status_code==409
    assert not sent


def test_old_form_without_revision_rejected(ready):
    client,_,_,_=ready
    assert client.post('/orders/103/packing-list',data={'csrf_token':'csrf','pack_qty_1001':'1'}).status_code==409


def test_csrf_and_auth_protect_correction(ready):
    client,_,_,sent=ready
    url=send_url()
    assert client.post(url).status_code==403
    with client.session_transaction() as s: s.pop('admin_authenticated',None)
    assert client.post(url,data={'csrf_token':'csrf'}).status_code in (302,401,403)
    assert not sent


def test_withdraw_retains_history_stock_and_survives_restore(ready):
    client,cloud,_,sent=ready
    doc=document()
    response=client.post('/orders/103/packing-withdraw/'+doc['file_hash'],data={'csrf_token':'csrf'})
    assert response.status_code==200,response.text
    assert not b.load_open_packing_selection(103)
    assert document()['withdrawn'] and not document()['is_current']
    db=b.conn()
    assert [r[0] for r in db.execute('SELECT qty FROM stock ORDER BY product_id')]==[3,0,2]
    assert db.execute('SELECT COUNT(*) FROM invoices').fetchone()[0]==1
    assert db.execute('SELECT COUNT(*) FROM packing_allocations').fetchone()[0]==2
    db.execute('DELETE FROM packing_lists')
    db.execute('DELETE FROM fulfillment_verifications')
    db.execute('DELETE FROM fulfillment_reconciliation_versions')
    db.commit();db.close()
    reconciliation_store.restore(b,103)
    assert not b.load_open_packing_selection(103)
    assert document()['withdrawn']
    assert not sent
    assert pack(client,qty=2).status_code==302
    assert document()['total_qty']==4 and document()['is_current']


@pytest.mark.parametrize('field',['invoice','pending_invoice','shipment'])
def test_cannot_withdraw_financial_or_shipped_list(ready,field):
    client,_,_,sent=ready
    doc=document()
    db=b.conn()
    if field=='invoice':
        db.execute('UPDATE packing_batches SET invoice_id=301 WHERE id=?',(doc['batch_id'],))
        db.execute('UPDATE packing_lists SET invoice_id=301 WHERE current_batch_id=?',(doc['batch_id'],))
    elif field=='pending_invoice':
        db.execute("UPDATE invoices SET publication_state='preparing' WHERE id=301")
    else:
        db.execute("UPDATE orders SET tracking_no='123' WHERE id=103")
    db.commit();db.close()
    response=client.post('/orders/103/packing-withdraw/'+doc['file_hash'],data={'csrf_token':'csrf'})
    assert response.status_code==409,response.text
    assert document()['is_current'] and not sent


def test_new_correction_after_new_version(ready):
    client,_,_,sent=ready
    assert client.post(send_url(),data={'csrf_token':'csrf'}).status_code==200
    assert pack(client,qty=2).status_code==302
    assert client.post(send_url(),data={'csrf_token':'csrf'}).status_code==200
    assert len(sent)==2
    assert sent[0][1]['idempotency_key']!=sent[1][1]['idempotency_key']


def test_final_receipt_ack_lost_does_not_repeat_mail(ready, monkeypatch):
    client, cloud, _, sent = ready
    original = reconciliation_store.publish
    def lost_ack(*args, **kwargs):
        if sent:
            raise TimeoutError('receipt response lost')
        return original(*args, **kwargs)
    monkeypatch.setattr(reconciliation_store, 'publish', lost_ack)
    url = send_url()
    assert client.post(url, data={'csrf_token':'csrf'}).status_code == 200
    assert client.post(url, data={'csrf_token':'csrf'}).status_code == 200
    monkeypatch.setattr(reconciliation_store, 'publish', original)
    assert client.post(url, data={'csrf_token':'csrf'}).status_code == 200
    assert len(sent) == 1


def test_correction_get_uses_bounded_read_scope():
    import supabase_read_cache as cache
    for action in ('packing-correction', 'packing-withdraw', 'packing-documents'):
        with b.app.test_request_context('/orders/103/' + action + '/' + 'a'*64):
            from flask import request
            tables, filters = cache.plan(request)
            assert tables == cache.FLOW | {'customers', 'company_profile'}
            assert filters == {}
