import json
from datetime import datetime
from zoneinfo import ZoneInfo

import app as backend
import business_operations as operations
import dashboard_read
import invoice_payment_sync
from test_business_read_operations import data
from test_business_operations import isolated, _owner, _product
from flask import template_rendered


def test_dashboard_and_agent_exclude_preparing_and_keep_currencies(data):
    db=backend.conn()
    db.execute("UPDATE invoice_meta SET paid=0 WHERE invoice_id=2")
    db.execute("UPDATE invoices SET payment_to='2026-09-01' WHERE id=2")
    db.execute("INSERT INTO invoices(id,order_id,invoice_no,issue_date,sell_date,payment_type,payment_to,total_gross,total_net,created_at,currency,publication_state) VALUES(3,1,'INCOMPLETE','2026-09-01','2026-09-01','transfer','2026-09-01',999,999,'2026-09-01','PLN','preparing')")
    db.commit();db.close()
    invoices=operations.execute_business_operation(data,'invoices.overdue',{'as_of':'2026-09-10'})
    assert invoices.status=='SUCCESS'
    assert {row['id'] for row in invoices.data['results']}=={1,2}
    record=operations.execute_business_operation(data,'invoices.get',{'id':3})
    assert record.status=='SUCCESS'
    assert record.data['record']['publication_state']=='preparing'
    assert record.data['record']['included_in_receivables'] is False
    def overdue(conn):
        return backend.overdue_invoice_rows(conn,current_time=datetime(2026,9,10,12,tzinfo=ZoneInfo('Europe/Warsaw')))
    result=dashboard_read.build_dashboard_read(backend.conn,overdue_invoice_rows=overdue,
        build_replenishment_analysis=lambda *a,**k:[],recommended_replenishments=lambda *a,**k:[],
        calculate_fulfillment_readiness=lambda *a,**k:[])
    assert result['overdue_count']==2
    assert result['overdue_by_currency']=={'PLN':246.0,'EUR':50.0}
    assert result['overdue_amount']==246.0


def test_dashboard_prices_do_not_duplicate_and_eur_uses_eur_catalog(data):
    db=backend.conn()
    db.execute("INSERT INTO pricing(model,net_price,created_at) VALUES('CH034',70,'2026-09-01')")
    db.execute("INSERT INTO pricing(model,net_price,created_at) VALUES('CH034-BB-128160',90,'2026-09-01')")
    db.execute("INSERT INTO pricing_eur(sku,price_eur,created_at,updated_at) VALUES('CH034-BB-128160',15,'2026-09-01','2026-09-01')")
    db.execute('UPDATE order_items SET unit_net_price=NULL WHERE order_id=2')
    db.commit();db.close()
    result=dashboard_read.build_dashboard_read(backend.conn,overdue_invoice_rows=lambda *a:[],
        build_replenishment_analysis=lambda *a,**k:[],recommended_replenishments=lambda *a,**k:[],
        calculate_fulfillment_readiness=lambda *a,**k:[])
    values={o['id']:o['total_net'] for o in result['recent_orders']}
    assert values=={1:200,2:15}


def test_payment_outbox_is_atomic_and_pull_cannot_undo_local_state(data,monkeypatch):
    monkeypatch.setattr(backend,'supabase_enabled',lambda:False)
    backend._set_invoice_payment_state(1,paid=1)
    db=backend.conn()
    assert db.execute('SELECT paid FROM invoice_meta WHERE invoice_id=1').fetchone()[0]==1
    assert db.execute('SELECT COUNT(*) FROM invoice_payment_sync_outbox').fetchone()[0]>=2
    db.close()
    backend.sqlite_upsert_rows('invoice_meta',[{'invoice_id':1,'paid':0,'pdf_path':'newer.pdf','updated_at':'2020-01-01'}],'invoice_id')
    backend.sqlite_delete_missing_rows('invoice_meta','invoice_id',[])
    backend.sqlite_delete_missing_rows('invoices','id',[])
    db=backend.conn()
    row=db.execute('SELECT * FROM invoice_meta WHERE invoice_id=1').fetchone()
    assert row['paid']==1 and row['pdf_path']=='newer.pdf'
    assert db.execute('SELECT 1 FROM invoices WHERE id=1').fetchone()
    db.close()


def test_invoice_read_without_order_stays_available(isolated):
    db=backend.conn()
    db.execute("INSERT INTO invoices(id,order_id,invoice_no,issue_date,sell_date,payment_type,buyer_name,total_net,total_gross,created_at,currency,publication_state) VALUES(1,999,'ARCHIVE','2026-09-01','2026-09-01','transfer','Archive',0,0,'2026-09-01','PLN','complete')")
    db.commit();db.close()
    result=operations.execute_business_operation(_owner(),'invoices.get',{'id':1})
    assert result.status=='SUCCESS',(result.error_code,result.safe_error_message)
    assert result.data['record']['payment_write_available'] is False


def test_invoice_screen_marks_incomplete_and_excludes_all_financial_totals(data,monkeypatch):
    monkeypatch.setattr(backend,'maybe_pull_shared_from_supabase',lambda **kw:None)
    monkeypatch.setattr(backend,'app_now',lambda:datetime(2026,9,10,12,tzinfo=ZoneInfo('Europe/Warsaw')))
    db=backend.conn()
    db.execute("UPDATE invoices SET publication_state='preparing' WHERE id=1")
    db.commit();db.close()
    captured=[]
    def collect(sender,template,context,**extra): captured.append(context)
    client=backend.app.test_client()
    with client.session_transaction() as session:
        session['admin_authenticated']=True
        session['internal_actor_id']=_owner().actor_id
        session['csrf_token']='finance-synthetic'
    with template_rendered.connected_to(collect,backend.app):
        response=client.get('/invoices')
    assert response.status_code==200
    assert 'Dokument niedokończony' in response.get_data(as_text=True)
    context=captured[-1]
    assert context['summary']['unpaid']==0 and context['summary']['paid']==1
    assert context['month_totals']==[{'currency':'EUR','net':50.0,'gross':50.0}]
    group=next(g for g in context['groups'] if g['customer_name']=='Firma Avery')
    assert group['currency_totals']['PLN']['total_gross']==0
    assert client.post('/invoices/1/paid',data={'csrf_token':'finance-synthetic'}).status_code==409
    db=backend.conn()
    assert db.execute('SELECT paid FROM invoice_meta WHERE invoice_id=1').fetchone()[0]==0
    db.close()
