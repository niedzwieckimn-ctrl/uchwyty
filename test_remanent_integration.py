"""Regression checks for a frozen yearly stock count and the full invoice period."""
import io
import json
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import app as backend
import business_operations as operations
import internal_rbac as rbac
import remanent
from cash_flow_module import calculate_cash_flow_snapshot
from invoice_sales import sales_by_sku
from test_business_operations import isolated, _owner


@pytest.fixture
def inventory(isolated):
    db=backend.conn()
    now=backend.now_iso()
    db.execute("INSERT INTO products(id,sku,model,name,archived,created_at) VALUES(1,'SKU-1','Model','Uchwyt',0,?)",(now,))
    db.execute('INSERT INTO stock(product_id,qty) VALUES(1,72)')
    db.execute("INSERT INTO orders(id,order_no,customer_name,status,created_at,currency) VALUES(1,'Z-1','Firma','confirmed',?,'EUR')",(now,))
    for invoice_id,issue,quantity,kind in ((1,'2026-02-01',60,'wdt'),(2,'2026-09-01',27,'export')):
        db.execute('''INSERT INTO invoices(id,order_id,invoice_no,issue_date,sell_date,payment_type,
            buyer_name,total_net,total_gross,created_at,invoice_type,currency)
            VALUES(?,?,?, ?,?,'transfer','Firma',100,100,?,?, 'EUR')''',
            (invoice_id,1,f'FV-{invoice_id}',issue,issue,now,kind))
        db.execute('INSERT INTO invoice_meta(invoice_id,invoice_items_json,updated_at) VALUES(?,?,?)',
                   (invoice_id,json.dumps([{'sku':'SKU-1','qty':quantity,'currency':'EUR'}]),now))
    db.execute("INSERT INTO china_packages(id,package_no,status,created_at) VALUES(1,'PO-1','arrived',?)",(now,))
    db.execute('INSERT INTO china_stock_receipts(package_id,received_at,quantities_json) VALUES(1,?,?)',
               ('2026-05-01',json.dumps([{'product_id':1,'qty':90}])))
    db.execute("INSERT INTO company_profile(id,company_name,address,nip,updated_at) VALUES(1,'Przykład sp. z o.o.','Warszawa','1234567890',?)",(now,))
    db.commit();db.close()
    return _owner()


def draft(db,actor):
    sid=remanent.create_draft(db,actor.actor_id,2026)
    remanent.add_entry(db,sid,actor.actor_id,product_id=1,kind='historical_purchase_manual',
                       period_start='2026-01-12',period_end='2026-01-12',quantity=250,
                       unit_value_pln='5.00',source='Fakturownia')
    remanent.add_entry(db,sid,actor.actor_id,product_id=1,kind='historical_sales_manual',
                       period_start='2026-01-01',period_end='2026-01-31',quantity=180,
                       source='Fakturownia')
    remanent.confirm_purchase_coverage(db,sid,actor.actor_id,'2026-09-21')
    db.commit()
    return sid


def test_full_history_wdt_and_document_formula(inventory):
    db=backend.conn()
    try:
        sales,problems,days=sales_by_sku(db,'2026-01-01','2026-09-21')
        assert sales=={1:87} and not problems and days[1]==['2026-02-01','2026-09-01']
        sid=draft(db,inventory)
        remanent.start_count(db,sid,inventory.actor_id,'2026-09-21')
        _,rows,total=remanent.detail(db,sid,inventory.actor_id)
        assert rows[0]['opening_stock']==0
        assert rows[0]['purchases_total']==340
        assert rows[0]['sales_total']==267
        assert rows[0]['document_stock']==73
        assert total['document_value']=='365.00'
        assert rows[0]['system_stock_at_start']==72
    finally: db.close()


def test_cashflow_and_remanent_share_full_invoice_source_and_exclude_staged(inventory):
    db=backend.conn()
    try:
        db.execute('''INSERT INTO invoices(id,order_id,invoice_no,issue_date,sell_date,payment_type,
            buyer_name,total_net,total_gross,created_at,invoice_type,currency,publication_state)
            VALUES(3,1,'DRAFT','2026-03-01','2026-03-01','transfer','Firma',999,999,?,'wdt','EUR','staging')''',
            (backend.now_iso(),))
        db.execute('INSERT INTO invoice_meta(invoice_id,invoice_items_json,updated_at) VALUES(?,?,?)',
            (3,json.dumps([{'sku':'SKU-1','qty':999}]),backend.now_iso()))
        db.commit()
        sales,problems,_=sales_by_sku(db,'2026-01-01','2026-09-21')
        assert sales=={1:87} and not problems
        from invoice_sales import invoice_rows
        assert {row['id'] for row in invoice_rows(db,'2026-01-01','2026-09-21')}=={1,2}
        now=datetime(2026,9,21,12,tzinfo=ZoneInfo('Europe/Warsaw'))
        deps={'conn':backend.conn,'app_now':lambda:now,'to_float':backend.to_float}
        snap=calculate_cash_flow_snapshot(deps,current_time=now)
        assert {item['source_id'] for item in snap['sources'] if item['source_type']=='invoice'}=={1,2}
        assert next(row['units'] for row in snap['sales_chart'] if row['key']=='2026-02')==60
    finally: db.close()


def test_count_close_restart_immutability_and_both_pdfs(inventory):
    db=backend.conn()
    try:
        sid=draft(db,inventory)
        remanent.start_count(db,sid,inventory.actor_id,'2026-09-21')
        expected=operations.execute_business_operation(inventory,'inventory.count.get_expected',{'product_id':1})
        result=operations.execute_business_operation(inventory,'inventory.count.record',
             {'product_id':1,'count_session_id':sid,'counted_quantity':70,
              'expected_version':expected.data['version'],'idempotency_key':str(uuid.uuid4())})
        assert result.status=='SUCCESS' and result.data['difference_document_vs_count']==-3, (result.error_code, result.safe_error_message)
        assert db.execute('SELECT qty FROM stock WHERE product_id=1').fetchone()[0]==72
        session,rows,total=remanent.close_count(db,sid,inventory.actor_id)
        assert rows[0]['counted_qty']==70 and rows[0]['difference_value']=='-15.00'
        assert total['counted_value']=='350.00' and total['shortage_qty']==3
        original_company=json.loads(session['company_snapshot_json'])
        from remanent_pdf import render_pdf
        company=dict(db.execute('SELECT * FROM company_profile WHERE id=1').fetchone())
        for kind in ('internal','sheet'):
            output=render_pdf(session,rows,total,kind,company)
            assert output.getvalue().startswith(b'%PDF')
        backend.init_db()  # reopening/migrating same database does not change a closed count
        db.execute('UPDATE stock SET qty=99 WHERE product_id=1')
        db.execute('UPDATE invoice_meta SET invoice_items_json=? WHERE invoice_id=2',
                   (json.dumps([{'sku':'SKU-1','qty':99}]),))
        db.execute("UPDATE company_profile SET company_name='Nowa nazwa' WHERE id=1")
        db.commit()
        _,after,summary=remanent.detail(db,sid,inventory.actor_id)
        assert after[0]['document_stock']==73 and after[0]['counted_qty']==70
        assert summary['counted_value']=='350.00'
        assert json.loads(remanent.detail(db,sid,inventory.actor_id)[0]['company_snapshot_json'])==original_company
        with pytest.raises(sqlite3.DatabaseError,match='CLOSED_REMANENT_IMMUTABLE'):
            db.execute('UPDATE internal_remanent_snapshots SET counted_final=1 WHERE session_id=?',(sid,))
        db.rollback()
        with pytest.raises(sqlite3.DatabaseError,match='CLOSED_REMANENT_IMMUTABLE'):
            db.execute('UPDATE internal_inventory_count_sessions SET company_snapshot_json=? WHERE session_id=?',
                       ('{}',sid))
        db.rollback()
    finally: db.close()


def test_missing_purchases_are_unknown_and_close_blocked(inventory):
    db=backend.conn()
    try:
        sid=remanent.create_draft(db,inventory.actor_id,2026)
        remanent.add_entry(db,sid,inventory.actor_id,product_id=1,kind='unit_value_manual',
                           period_start='2026-01-01',period_end='2026-01-01',quantity=0,
                           unit_value_pln='5',source='Księgowa')
        db.commit()
        remanent.start_count(db,sid,inventory.actor_id,'2026-09-21')
        _,rows,_=remanent.detail(db,sid,inventory.actor_id)
        assert rows[0]['purchases_total'] is None and rows[0]['document_stock'] is None
        with pytest.raises(ValueError,match='Niepotwierdzona kompletność zakupów'):
            remanent.close_count(db,sid,inventory.actor_id,confirm_uncounted=True)
    finally: db.close()


def test_explicit_zero_for_uncounted_is_recorded_but_not_claimed_as_counted(inventory):
    db=backend.conn()
    try:
        sid=draft(db,inventory)
        remanent.start_count(db,sid,inventory.actor_id,'2026-09-21')
        with pytest.raises(ValueError,match='Niepoliczone'):
            remanent.close_count(db,sid,inventory.actor_id)
        _s,rows,total=remanent.close_count(db,sid,inventory.actor_id,confirm_uncounted=True)
        assert rows[0]['counted_qty']==0 and rows[0]['assumed_zero']
        assert total['counted_count']==0 and total['uncounted_count']==1
        assert total['counted_value']=='0.00'
        with pytest.raises(sqlite3.DatabaseError,match='CLOSED_REMANENT_IMMUTABLE'):
            db.execute('UPDATE internal_remanent_snapshots SET assumed_zero=0 WHERE session_id=?',(sid,))
        db.rollback()
    finally: db.close()


def test_csv_preview_duplicate_unknown_and_overlap(inventory):
    db=backend.conn()
    try:
        sid=remanent.create_draft(db,inventory.actor_id,2026)
        data=b'sku,quantity,date,unit_value_pln,document_no,line_value_pln\nSKU-1,3,2026-01-15,5.12,PO-1,15.36\n'
        preview=remanent.preview_csv(db,sid,inventory.actor_id,data,'historical_purchase_import','2026-01-01','2026-01-31')
        assert preview['matched']==1 and preview['quantity']==3 and preview['value_pln']=='15.36'
        remanent.import_csv(db,sid,inventory.actor_id,data,'historical_purchase_import',
                            '2026-01-01','2026-01-31',preview['sha256'])
        with pytest.raises(ValueError,match='już istnieje'):
            remanent.import_csv(db,sid,inventory.actor_id,data,'historical_purchase_import',
                                '2026-01-01','2026-01-31',preview['sha256'])
        unknown=remanent.preview_csv(db,sid,inventory.actor_id,data.replace(b'SKU-1',b'MISSING'),
            'historical_purchase_import','2026-01-01','2026-01-31')
        assert unknown['unmatched']==1 and unknown['problems']
        sale=b'sku,quantity,date\nSKU-1,3,2026-02-01\n'
        conflict=remanent.preview_csv(db,sid,inventory.actor_id,sale,'historical_sales_import','2026-01-01','2026-03-01')
        assert conflict['problems'] and 'pokrywa' in conflict['problems'][0]['error']
    finally: db.close()


def test_remanent_ui_requires_session_and_renders_snapshot(inventory,monkeypatch):
    monkeypatch.setattr(backend,'maybe_pull_shared_from_supabase',lambda **kwargs: None)
    client=backend.app.test_client()
    assert client.get('/login').status_code==200
    assert client.get('/remanent').status_code==302
    with client.session_transaction() as sess:
        sess['admin_authenticated']=True
        sess['internal_actor_id']=inventory.actor_id
        sess['csrf_token']='test'
    response=client.get('/remanent')
    assert response.status_code==200 and 'Nowy remanent' in response.get_data(as_text=True)
    response=client.post('/remanent/new',data={'year':'2026','csrf_token':'test'})
    assert response.status_code==302
    sid=response.headers['Location'].split('/')[-1]
    draft_page=client.get(response.headers['Location'])
    assert draft_page.status_code==200 and 'Import CSV' in draft_page.get_data(as_text=True)
    db=backend.conn()
    try:
        remanent.add_entry(db,sid,inventory.actor_id,product_id=1,kind='unit_value_manual',
            period_start='2026-01-01',period_end='2026-01-01',quantity=0,unit_value_pln='5',source='Test')
        remanent.confirm_purchase_coverage(db,sid,inventory.actor_id,'2026-09-21')
        db.commit()
    finally: db.close()
    response=client.post(f'/remanent/{sid}/start',data={'as_of_date':'2026-09-21','csrf_token':'test'})
    assert response.status_code==302, response.get_data(as_text=True)[:300]
    page=client.get(f'/remanent/{sid}')
    assert page.status_code==200 and 'Stan dokument.' in page.get_data(as_text=True)
    assert client.get(f'/remanent/{sid}/pdf/internal').status_code==409


def test_agent_fast_mode_reuses_the_one_active_remanent(inventory):
    import agent_runtime as runtime
    from test_warehouse_ux import start
    db=backend.conn()
    try:
        sid=draft(db,inventory)
        remanent.start_count(db,sid,inventory.actor_id,'2026-09-21')
    finally: db.close()
    opened=start(inventory)
    assert opened['status']=='SUCCESS'
    ai=rbac.load_actor_context(rbac.AI_OWNER_ASSISTANT_ACTOR_ID,delegated_by_actor_id=inventory.actor_id)
    assert operations.active_inventory_count_session(ai,inventory,opened['conversation_id'])==sid
    result=runtime.run_agent_turn(inventory,'SKU-1 70',runtime.FakeModelProvider([]),opened['conversation_id'])
    assert result['status']=='SUCCESS', result
    assert 'Stan dokumentowy: 73' in result['message']
    db=backend.conn()
    try:
        assert db.execute('SELECT COUNT(*) FROM internal_inventory_count_sessions WHERE inventory_year IS NOT NULL').fetchone()[0]==1
        assert db.execute('SELECT counted_quantity FROM internal_inventory_count_items WHERE session_id=?',(sid,)).fetchone()[0]==70
    finally: db.close()


def test_two_workers_cannot_create_two_open_counts(inventory):
    def create():
        db=backend.conn()
        try:
            try: return remanent.create_draft(db,inventory.actor_id,2026)
            except ValueError: return None
        finally: db.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _i:create(),range(2)))
    assert sum(bool(result) for result in results)==1
