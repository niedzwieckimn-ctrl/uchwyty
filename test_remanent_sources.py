"""Versioned source reconciliation and the real count lifecycle, with synthetic data."""
import json
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

import app as backend
import business_operations as operations
import remanent
import remanent_sources as sources
from test_business_operations import isolated, _owner


INSTALLATION = 'f5271c04-f67a-46d4-b7c3-b98669b70cdd'


def row(line_id='L1', sku='SKU-A', quantity=3, unit='9', **changes):
    value = None if unit is None else str(Decimal(unit)*quantity)
    result = dict(source_import_id='IMPORT-A', source_version=1, line_id=line_id,
        kind='purchase', sku=sku, quantity=quantity, document_no='DOC-A', supplier='Supplier',
        document_date='2026-01-02', received_date=None, inventory_year=2026,
        valuation_basis='goods_net', line_value_pln=value, unit_value_pln=unit)
    result.update(changes)
    return result


def encoded(*rows):
    return sources.canonical(dict(schema='annual_inventory_bridge', version=2,
        export_scope='complete_source_versions', source_installation_id=INSTALLATION,
        exported_at='2026-09-28T10:00:00Z', lines=list(rows))).encode('utf-8')


@pytest.fixture
def ledger(isolated):
    db=backend.conn()
    actor=_owner()
    add_product(db,1,'SKU-A')
    db.commit()
    sid=remanent.create_draft(db,actor.actor_id,2026)
    yield db,sid,actor
    db.close()


def add_product(db,pid,sku):
    db.execute('INSERT INTO products(id,sku,model,name,created_at) VALUES(?,?,?,?,?)',
        (pid,sku,'Synthetic',sku,backend.now_iso()))
    db.execute('INSERT INTO stock(product_id,qty) VALUES(?,5)',(pid,))


def receipt(db,package=1,items=None,day='2026-02-01'):
    db.execute("INSERT INTO china_packages(id,package_no,status,created_at) VALUES(?,?,'arrived',?)",
        (package,'PO-'+str(package),backend.now_iso()))
    db.execute('INSERT INTO china_stock_receipts(package_id,received_at,quantities_json) VALUES(?,?,?)',
        (package,day,json.dumps(items if items is not None else [{'product_id':1,'qty':3}])))
    db.commit()


def save(ledger,data,actions=None):
    db,sid,actor=ledger
    plan=sources.preview(db,sid,actor.actor_id,data)
    choices=[]
    for item in plan['rows']:
        choice=(actions or {}).get(item['source']['line_id'],{'action':'history','received_date':'2026-02-01'})
        choices.append(dict(line_key=item['key'],**choice))
    return sources.commit(db,sid,actor.actor_id,data,choices,plan['fingerprint'])


def link(*allocations):
    return {'action':'link','allocations':[dict(receipt_key=key,quantity=qty) for key,qty in allocations]}


def value(db,basis='goods_net'):
    return sources.valuation(db,2026,'2026-09-28',basis)


@pytest.mark.parametrize('payload',[[],None,1,'text'])
def test_parser_rejects_non_object_as_validation_error(payload):
    with pytest.raises(ValueError): sources.parse(json.dumps(payload).encode())


@pytest.mark.parametrize('changes',[
    {'document_date':None},{'document_date':20260101},{'document_date':'20260101'},
    {'document_date':'2026-02-30'},{'received_date':[]},{'received_date':''},
    {'received_date':False},{'line_value_pln':27.0},{'unit_value_pln':'NaN'},
    {'line_value_pln':'28'},{'quantity':True},{'kind':[]},{'source_version':2**64},
])
def test_parser_rejects_invalid_dates_types_and_conflicting_values(changes):
    with pytest.raises(ValueError): sources.parse(encoded(row(**changes)))


def test_parser_preserves_missing_zero_and_precise_repeating_price():
    data=encoded(row(unit=None),row('zero',unit='0'),row('third',unit='0.3333333333333333333333333333',line_value_pln='1.00'))
    parsed=sources.parse(data)
    assert parsed['lines'][0]['line_value_pln'] is None
    assert parsed['lines'][1]['line_value_pln']=='0'
    assert parsed['lines'][2]['line_value_pln']=='1.00'
    payload=json.loads(data);payload['source_installation_id']=INSTALLATION.upper()
    assert sources.parse(json.dumps(payload).encode())['source_installation_id']==INSTALLATION


def test_receipt_order_and_duplicate_product_rows_cannot_transfer_costs(ledger):
    db,_,_=ledger
    add_product(db,2,'SKU-B')
    receipt(db,items=[{'product_id':1,'qty':1},{'product_id':2,'qty':3},{'product_id':1,'qty':2}])
    data=encoded(row(),row('B','SKU-B',unit='100'))
    save(ledger,data,{'L1':link(('1:product:1',3)),'B':link(('1:product:2',3))})
    before=value(db)
    assert before[1]['purchase_value']==27 and before[2]['purchase_value']==300
    db.execute('UPDATE china_stock_receipts SET quantities_json=? WHERE package_id=1',
        (json.dumps([{'product_id':2,'qty':3},{'product_id':1,'qty':3}]),))
    db.commit()
    after=value(db)
    assert after==before
    assert not after[1]['problems'] and not after[2]['problems']


@pytest.mark.parametrize('change', ['date','quantity','product','deleted'])
def test_changed_receipt_evidence_requires_new_decision(ledger,change):
    db,_,_=ledger
    add_product(db,2,'SKU-B')
    receipt(db)
    save(ledger,encoded(row()),{'L1':link(('1:product:1',3))})
    if change=='date': db.execute("UPDATE china_stock_receipts SET received_at='2026-03-01'")
    if change=='quantity': db.execute('UPDATE china_stock_receipts SET quantities_json=?',(json.dumps([{'product_id':1,'qty':4}]),))
    if change=='product': db.execute('UPDATE china_stock_receipts SET quantities_json=?',(json.dumps([{'product_id':2,'qty':3}]),))
    if change=='deleted': db.execute('DELETE FROM china_stock_receipts')
    db.commit()
    assert any('uzgodnij' in text for text in value(db)[1]['problems'])
    assert value(db)[1]['purchase_value']==0
    if change=='date':
        save(ledger,encoded(row()),{'L1':link(('1:product:1',3))})
        assert not value(db)[1]['problems'] and value(db)[1]['purchase_value']==27


def test_unverified_legacy_decision_blocks_instead_of_guessing_receipt_identity(ledger):
    db,_,actor=ledger
    receipt(db)
    save(ledger,encoded(row()),{'L1':link(('1:product:1',3))})
    legacy={'action':'link','allocations':[{'receipt_key':'1:0','quantity':3}],'received_date':None}
    db.execute('''INSERT INTO remanent_source_decisions(installation_id,import_id,version,line_id,decision_json,created_by,created_at)
        VALUES(?,?,1,'L1',?,?,?)''',(INSTALLATION,'IMPORT-A',sources.canonical(legacy),actor.actor_id,backend.now_iso()))
    db.commit()
    assert value(db)[1]['problems'] and value(db)[1]['purchase_value']==0


def test_unknown_excluded_sku_can_be_resolved_without_changing_immutable_payload(ledger):
    db,_,_=ledger
    data=encoded(row(sku='NEW-SKU'))
    save(ledger,data,{'L1':{'action':'exclude'}})
    assert not value(db)
    original=db.execute('SELECT product_id FROM remanent_source_lines').fetchone()[0]
    assert original==0
    add_product(db,2,'NEW-SKU');db.commit()
    save(ledger,data)
    assert sources.active_rows(db)[0]['product_id']==2
    assert value(db)[2]['history_quantity']==3 and value(db)[2]['purchase_value']==27
    assert db.execute('SELECT product_id FROM remanent_source_lines').fetchone()[0]==0
    assert db.execute('SELECT COUNT(*) FROM remanent_source_decisions').fetchone()[0]==2


def test_catalog_change_invalidates_saved_mapping_and_preview(ledger):
    db,sid,actor=ledger
    data=encoded(row())
    plan=sources.preview(db,sid,actor.actor_id,data)
    db.execute("UPDATE products SET sku='RENAMED' WHERE id=1");db.commit()
    with pytest.raises(ValueError,match='zmieniły'):
        sources.commit(db,sid,actor.actor_id,data,[{'line_key':plan['rows'][0]['key'],'action':'exclude'}],plan['fingerprint'])
    db.execute("UPDATE products SET sku='SKU-A' WHERE id=1");db.commit()
    save(ledger,data)
    db.execute("UPDATE products SET sku='RENAMED' WHERE id=1");db.commit()
    assert value(db)[None]['problems']


def test_cost_bases_share_physical_quantity_and_missing_basis_blocks(ledger):
    db,_,_=ledger
    save(ledger,encoded(row()))
    save(ledger,encoded(row(unit='12',valuation_basis='landed_cost')))
    assert len(sources.active_rows(db))==2
    assert value(db)[1]['history_quantity']==3 and value(db)[1]['purchase_value']==27
    assert value(db,'landed_cost')[1]['history_quantity']==3
    assert value(db,'landed_cost')[1]['purchase_value']==36
    assert value(db,'goods_transport')[1]['problems']


def test_cost_basis_requires_same_complete_line_set(ledger):
    save(ledger,encoded(row(),row('L2')))
    with pytest.raises(ValueError,match='komplet'):
        save(ledger,encoded(row(valuation_basis='landed_cost')))


def test_latest_version_replaces_quantity_and_retains_history(ledger):
    db,_,_=ledger
    save(ledger,encoded(row(),row('L2')))
    assert value(db)[1]['history_quantity']==6  # identical lines have different IDs
    save(ledger,encoded(row(quantity=4,unit='10',source_version=2)))
    assert value(db)[1]['history_quantity']==4 and value(db)[1]['purchase_value']==40
    assert db.execute('SELECT COUNT(*) FROM remanent_source_lines').fetchone()[0]==3
    assert {r['version'] for r in sources.active_rows(db)}=={2}
    with pytest.raises(ValueError,match='Starsza'):
        save(ledger,encoded(row(),row('L2')))
    with pytest.raises(ValueError,match='inną treść'):
        save(ledger,encoded(row(quantity=5,unit='10',source_version=2)))
    assert value(db)[1]['history_quantity']==4


def test_repeated_upload_noop_and_no_physical_stock_write(ledger):
    db,sid,actor=ledger
    data=encoded(row()); plan=sources.preview(db,sid,actor.actor_id,data)
    decisions=[{'line_key':plan['rows'][0]['key'],'action':'history','received_date':'2026-02-01'}]
    first=sources.commit(db,sid,actor.actor_id,data,decisions,plan['fingerprint'])
    again=sources.commit(db,sid,actor.actor_id,data,decisions,plan['fingerprint'])
    assert not first['already_imported'] and again['already_imported']
    assert db.execute('SELECT COUNT(*) FROM remanent_source_lines').fetchone()[0]==1
    assert db.execute('SELECT qty FROM stock').fetchone()[0]==5
    assert db.execute('SELECT COUNT(*) FROM china_stock_receipts').fetchone()[0]==0


def test_partial_receipt_links_are_prorated_and_overallocation_rejected(ledger):
    db,_,_=ledger
    receipt(db,1,[{'product_id':1,'qty':2}]);receipt(db,2,[{'product_id':1,'qty':3}])
    save(ledger,encoded(row(quantity=10)),{'L1':link(('1:product:1',2),('2:product:1',3))})
    assert value(db)[1]['receipt_quantity']==5 and value(db)[1]['history_quantity']==0
    assert value(db)[1]['purchase_value']==45 and not value(db)[1]['problems']
    with pytest.raises(ValueError,match='przekraczają'):
        save(ledger,encoded(row(source_import_id='OTHER')),{'L1':link(('2:product:1',3))})
    assert value(db)[1]['purchase_value']==45


def test_warehouse_only_excludes_receipts_linked_to_other_active_import(ledger):
    db,sid,actor=ledger
    receipt(db)
    save(ledger,encoded(row()),{'L1':link(('1:product:1',3))})
    plan=sources.preview(db,sid,actor.actor_id,encoded(row(source_import_id='OTHER')))
    assert not plan['warehouse_only']


def test_document_year_does_not_replace_physical_receipt_year(ledger):
    db,_,_=ledger
    receipt(db,day='2026-01-02')
    save(ledger,encoded(row(document_date='2025-12-30',inventory_year=2025)),{'L1':link(('1:product:1',3))})
    assert value(db)[1]['receipt_quantity']==3 and value(db)[1]['purchase_value']==27
    prior=sources.valuation(db,2025,'2025-12-31','goods_net')
    assert prior[1]['receipt_quantity']==0 and prior[1]['purchase_value']==0


def test_unpriced_other_receipt_never_borrows_same_sku_price(ledger):
    db,_,_=ledger
    receipt(db,items=[{'product_id':1,'qty':10}])
    save(ledger,encoded(row()))  # Explicit historical 3 units at 9, separate physical 10 unknown cost.
    result=value(db)[1]
    assert result['history_quantity']==3 and result['receipt_quantity']==10
    assert result['purchase_value']==27 and result['problems']


def test_different_purchase_prices_produce_weighted_average(ledger):
    db,sid,actor=ledger
    save(ledger,encoded(row(),row('L2',quantity=1,unit='12'),
        row('opening',quantity=0,unit='0',kind='opening',source_import_id='opening-2026-SKU-A')),
        {'opening':{'action':'opening'}})
    sources.select_method(db,sid,actor.actor_id,sources.METHOD,'goods_net')
    remanent.confirm_purchase_coverage(db,sid,actor.actor_id,'2026-09-28');db.commit()
    remanent.start_count(db,sid,actor.actor_id,'2026-09-28')
    item=remanent.detail(db,sid,actor.actor_id)[1][0]
    assert item['document_stock']==4 and Decimal(item['unit_value_pln'])==Decimal('9.75')


def test_concurrent_same_preview_never_duplicates_source(ledger):
    db,sid,actor=ledger
    data=encoded(row());plan=sources.preview(db,sid,actor.actor_id,data)
    decisions=[{'line_key':plan['rows'][0]['key'],'action':'history','received_date':'2026-02-01'}]
    def approve():
        local=backend.conn()
        try: return sources.commit(local,sid,actor.actor_id,data,decisions,plan['fingerprint'])
        finally: local.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:approve(),range(2)))
    assert sorted(r['already_imported'] for r in results)==[False,True]
    assert db.execute('SELECT COUNT(*) FROM remanent_source_lines').fetchone()[0]==1
    assert value(db)[1]['history_quantity']==3


def test_zero_opening_known_vs_missing_cost_and_conflicting_opening(ledger):
    db,_,_=ledger
    opening=row('opening',quantity=0,unit='0',kind='opening',source_import_id='opening-2026-SKU-A')
    save(ledger,encoded(opening),{'opening':{'action':'opening'}})
    assert value(db)[1]['opening_known'] and value(db)[1]['opening_value']==0 and not value(db)[1]['problems']
    save(ledger,encoded(row('open2',quantity=2,unit=None,kind='opening',source_import_id='other-opening')),
        {'open2':{'action':'opening'}})
    assert len(value(db)[1]['problems'])==2


def test_manual_values_require_explicit_basis_and_frozen_settings_block_sql_changes(ledger):
    db,sid,actor=ledger
    remanent.add_entry(db,sid,actor.actor_id,product_id=1,kind='opening',period_start='2026-01-01',
        period_end='2026-01-01',quantity=2,unit_value_pln='5',source='Manual')
    db.commit()
    with pytest.raises(ValueError,match='Potwierdź'):
        sources.select_method(db,sid,actor.actor_id,sources.METHOD,'goods_net')
    sources.select_method(db,sid,actor.actor_id,sources.METHOD,'goods_net',confirm_manual_basis=True)
    db.commit()
    assert db.execute('SELECT manual_basis_confirmed FROM remanent_valuation_settings').fetchone()[0]==1
    db.execute("UPDATE internal_inventory_count_sessions SET phase='IN_PROGRESS' WHERE session_id=?",(sid,));db.commit()
    for sql in ["UPDATE remanent_valuation_settings SET basis='landed_cost'",'DELETE FROM remanent_valuation_settings']:
        with pytest.raises(sqlite3.DatabaseError,match='VALUATION_SETTINGS_FROZEN'): db.execute(sql)
        db.rollback()


def test_end_to_end_weighted_snapshot_count_close_export_and_restart(ledger):
    db,sid,actor=ledger
    receipt(db)
    data=encoded(row(unit='10'),row('opening',quantity=2,unit='5',kind='opening',source_import_id='opening-2026-SKU-A'))
    save(ledger,data,{'L1':link(('1:product:1',3)),'opening':{'action':'opening'}})
    sources.select_method(db,sid,actor.actor_id,sources.METHOD,'goods_net')
    remanent.confirm_purchase_coverage(db,sid,actor.actor_id,'2026-09-28');db.commit()
    remanent.start_count(db,sid,actor.actor_id,'2026-09-28')
    _,items,_=remanent.detail(db,sid,actor.actor_id)
    assert items[0]['document_stock']==5 and Decimal(items[0]['unit_value_pln'])==8
    expected=operations.execute_business_operation(actor,'inventory.count.get_expected',{'product_id':1})
    recorded=operations.execute_business_operation(actor,'inventory.count.record',dict(product_id=1,count_session_id=sid,
        counted_quantity=5,expected_version=expected.data['version'],idempotency_key=str(uuid.uuid4())))
    assert recorded.status=='SUCCESS',(recorded.error_code,recorded.safe_error_message)
    remanent.close_count(db,sid,actor.actor_id)
    original=sources.export_result(db,sid,actor.actor_id)
    result=json.loads(original)
    assert result['items'][0]['counted_quantity']==5 and result['items'][0]['line_value_pln']=='40.00'
    assert len(result['items'][0]['source_versions'])==2
    assert result['valuation_method']==sources.METHOD and result['valuation_basis']=='goods_net'
    assert db.execute('SELECT qty FROM stock').fetchone()[0]==5
    backend.init_db()
    db.execute('UPDATE stock SET qty=99');db.commit()
    assert sources.export_result(db,sid,actor.actor_id)==original
    with pytest.raises(sqlite3.DatabaseError,match='VALUATION_SETTINGS_FROZEN'):
        db.execute("UPDATE remanent_valuation_settings SET basis='landed_cost'")
    db.rollback()


@pytest.mark.parametrize('missing',['opening','cost','coverage','method'])
def test_incomplete_sources_keep_draft_without_partial_snapshot(ledger,missing):
    db,sid,actor=ledger
    receipt(db)
    rows=[row(unit=None if missing=='cost' else '10')]
    actions={'L1':link(('1:product:1',3))}
    if missing!='opening':
        rows.append(row('opening',quantity=0,unit='0',kind='opening',source_import_id='opening-2026-SKU-A'))
        actions['opening']={'action':'opening'}
    save(ledger,encoded(*rows),actions)
    if missing!='method': sources.select_method(db,sid,actor.actor_id,sources.METHOD,'goods_net')
    if missing!='coverage': remanent.confirm_purchase_coverage(db,sid,actor.actor_id,'2026-09-28')
    db.commit()
    with pytest.raises(ValueError): remanent.start_count(db,sid,actor.actor_id,'2026-09-28')
    assert db.execute('SELECT phase FROM internal_inventory_count_sessions WHERE session_id=?',(sid,)).fetchone()[0]=='DRAFT'
    assert db.execute('SELECT COUNT(*) FROM internal_remanent_snapshots').fetchone()[0]==0


def test_found_quantity_has_no_invented_zero_price_when_sources_have_no_units(ledger):
    db,sid,actor=ledger
    opening=row('opening',quantity=0,unit='0',kind='opening',source_import_id='opening-2026-SKU-A')
    save(ledger,encoded(opening),{'opening':{'action':'opening'}})
    sources.select_method(db,sid,actor.actor_id,sources.METHOD,'goods_net')
    remanent.confirm_purchase_coverage(db,sid,actor.actor_id,'2026-09-28');db.commit()
    remanent.start_count(db,sid,actor.actor_id,'2026-09-28')
    item=remanent.detail(db,sid,actor.actor_id)[1][0]
    assert item['document_stock']==0 and item['unit_value_pln'] is None
    expected=operations.execute_business_operation(actor,'inventory.count.get_expected',{'product_id':1})
    recorded=operations.execute_business_operation(actor,'inventory.count.record',dict(product_id=1,count_session_id=sid,
        counted_quantity=5,expected_version=expected.data['version'],idempotency_key=str(uuid.uuid4())))
    assert recorded.status=='SUCCESS',(recorded.error_code,recorded.safe_error_message)
    with pytest.raises(ValueError,match='ceny jednostkowej'):
        remanent.close_count(db,sid,actor.actor_id)
    session,items,_=remanent.detail(db,sid,actor.actor_id)
    assert session['status']=='OPEN' and items[0]['counted_qty']==5
    assert items[0]['counted_stock_value'] is None
    assert db.execute('SELECT qty FROM stock WHERE product_id=1').fetchone()[0]==5


@pytest.mark.parametrize('receipt_json',['{}','[null]','[{"product_id":1}]','[{"product_id":1,"qty":"3"}]','not json'])
def test_corrupt_receipt_has_controlled_error_and_preserves_draft(ledger,receipt_json):
    db,sid,actor=ledger
    receipt(db)
    db.execute('UPDATE china_stock_receipts SET quantities_json=? WHERE package_id=1',(receipt_json,));db.commit()
    save(ledger,encoded(row('opening',quantity=0,unit='0',kind='opening',source_import_id='opening-2026-SKU-A')),
        {'opening':{'action':'opening'}})
    sources.select_method(db,sid,actor.actor_id,sources.METHOD,'goods_net')
    remanent.confirm_purchase_coverage(db,sid,actor.actor_id,'2026-09-28');db.commit()
    with pytest.raises(ValueError):
        remanent.start_count(db,sid,actor.actor_id,'2026-09-28')
    session,_,_=remanent.detail(db,sid,actor.actor_id)
    assert session['phase']=='DRAFT' and session['status']=='OPEN'
    assert db.execute('SELECT COUNT(*) FROM internal_remanent_snapshots').fetchone()[0]==0
    assert db.execute('SELECT quantities_json FROM china_stock_receipts').fetchone()[0]==receipt_json
