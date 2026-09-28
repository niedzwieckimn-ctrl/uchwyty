"""Actual Flask upload/decision forms over synthetic receipt and source data."""
import io
import json
import uuid
from html.parser import HTMLParser

import pytest

import app as backend
import remanent
import remanent_sources as sources
from test_business_operations import isolated, _owner, _actor


class Forms(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.inputs = {}
        self.options = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'input' and attrs.get('name'):
            self.inputs[attrs['name']] = attrs.get('value', '')
        if tag == 'option':
            self.options.append(attrs)


def source(sku, line_id, quantity, **changes):
    return dict(source_import_id='IMP-1', source_version=1, line_id=line_id,
        sku=sku, document_no='DOK-1', supplier='Dostawca', valuation_basis='goods_net',
        kind='purchase', quantity=quantity, inventory_year=2026, document_date='2025-12-31',
        received_date=None, line_value_pln=str(quantity*9), unit_value_pln='9', **changes)


def encoded(lines):
    return json.dumps({'schema':'annual_inventory_bridge', 'version':2,
        'export_scope':'complete_source_versions', 'source_installation_id':'f5271c04-f67a-46d4-b7c3-b98669b70cdd',
        'lines':lines}, ensure_ascii=False).encode('utf-8')


def post_file(client, path, data, fields=None):
    return client.post(path, data={**(fields or {}), 'csrf_token':'source-csrf',
        'file':(io.BytesIO(data),'costs.json')}, content_type='multipart/form-data')


@pytest.fixture
def review(isolated):
    client = isolated
    with client.session_transaction() as sess:
        sess['admin_authenticated'] = True
        sess['internal_actor_id'] = _owner().actor_id
        sess['csrf_token'] = 'source-csrf'
    db = backend.conn()
    sources.initialize(db)
    for pid in range(1,6):
        db.execute('INSERT INTO products(id,sku,model,name,created_at) VALUES(?,?,?,?,?)',
                   (pid,f'SOURCE-{pid}','Synthetic',f'Product {pid}',backend.now_iso()))
        db.execute('INSERT INTO stock(product_id,qty) VALUES(?,10)',(pid,))
    for receipt, pid, qty in [(1,1,2),(2,1,3),(3,3,2),(4,4,1),(5,5,4)]:
        db.execute("INSERT INTO china_packages(id,package_no,status,created_at) VALUES(?,?,'arrived',?)",
                   (receipt,f'PO-{receipt}',backend.now_iso()))
        db.execute('INSERT INTO china_stock_receipts(package_id,received_at,quantities_json) VALUES(?,?,?)',
                   (receipt,'2026-01-02',json.dumps([{'product_id':pid,'qty':qty}])))
    db.commit()
    sid = remanent.create_draft(db,_owner().actor_id,2026)
    rows = [source('SOURCE-1','partial',5), source('SOURCE-2','history',4),
            dict(source('SOURCE-1','opening',0),kind='opening'),
            source('UNKNOWN','unknown',1), source('SOURCE-3','conflict',3),
            source('SOURCE-4','suggested',1)]
    data = encoded(rows)
    plan = sources.preview(db,sid,_owner().actor_id,data)
    db.close()
    path = '/remanent/'+sid+'/sources'
    return client,sid,path,data,plan


def choices(plan, actions=None):
    actions = actions or {}
    return {'fingerprint':plan['fingerprint'], **{'action_'+row['key']:actions.get(row['source']['line_id'],'exclude')
                                                for row in plan['rows']}}


def test_upload_preview_renders_statuses_dates_costs_and_no_selected_action(review):
    client,sid,path,data,plan = review
    assert client.get(path).status_code == 200
    assert 'Zapisano. Fizyczny stan' not in client.get(path+'?saved=yes').get_data(as_text=True)
    response = post_file(client,path+'/preview',data)
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    form = Forms(html)
    assert form.inputs['csrf_token'] == 'source-csrf'
    assert form.inputs['fingerprint'] == plan['fingerprint']
    for text in ['Kilka możliwych przyjęć','Tylko w Rocznym Rozliczeniu','Stan początkowy',
                 'Nieznane lub niejednoznaczne SKU','Sprzeczne ilości','Sugestia dopasowania',
                 '2025-12-31','2026-01-02','SOURCE-5','IMP-1','partial']:
        assert text in html
    assert not any('selected' in option for option in form.options)


def test_explicit_partial_receipts_history_opening_save_once_and_never_change_stock(review):
    client,sid,path,data,plan = review
    fields = choices(plan,{'partial':'link','history':'history','opening':'opening'})
    indexed = {row['source']['line_id']:row['key'] for row in plan['rows']}
    partial = indexed['partial']
    fields.update({'receipt_'+partial+'_0':'yes', 'quantity_'+partial+'_0':'2',
                   'receipt_'+partial+'_1':'yes', 'quantity_'+partial+'_1':'3',
                   'received_'+indexed['history']:'2026-01-03',
                   'history_confirm_'+indexed['history']:'yes',
                   'opening_confirm_'+indexed['opening']:'yes'})
    assert post_file(client,path+'/commit',data,fields).status_code == 302
    assert post_file(client,path+'/commit',data,fields).status_code == 302
    db = backend.conn()
    try:
        assert [row[0] for row in db.execute('SELECT qty FROM stock ORDER BY product_id')] == [10]*5
        assert db.execute('SELECT COUNT(*) FROM remanent_source_commits').fetchone()[0] == 1
        saved = {row['line_id']:json.loads(row['decision_json']) for row in db.execute('SELECT * FROM remanent_source_lines')}
        assert [(item['receipt_key'],item['quantity']) for item in saved['partial']['allocations']] == [('1:product:1',2),('2:product:1',3)]
        assert saved['history']['received_date'] == '2026-01-03'
        assert saved['opening']['action'] == 'opening'
    finally:
        db.close()
    response = post_file(client,path+'/preview',data)
    assert response.status_code == 200 and 'Istniejące powiązanie' in response.get_data(as_text=True)


@pytest.mark.parametrize('case',['missing_choice','history_date','history_confirmation','opening_confirmation'])
def test_incomplete_decisions_cannot_be_saved(review,case):
    client,sid,path,data,plan = review
    fields = choices(plan)
    indexed = {row['source']['line_id']:row['key'] for row in plan['rows']}
    if case == 'missing_choice':
        del fields['action_'+indexed['unknown']]
    elif case.startswith('history'):
        fields['action_'+indexed['history']] = 'history'
        if case == 'history_date': fields['history_confirm_'+indexed['history']] = 'yes'
        else: fields['received_'+indexed['history']] = '2026-01-03'
    else:
        fields['action_'+indexed['opening']] = 'opening'
    assert post_file(client,path+'/commit',data,fields).status_code == 409
    db = backend.conn()
    assert db.execute('SELECT COUNT(*) FROM remanent_source_commits').fetchone()[0] == 0
    db.close()


def test_changed_file_or_receipt_invalidates_preview(review):
    client,sid,path,data,plan = review
    changed = data.replace(b'DOK-1',b'DOK-2')
    assert post_file(client,path+'/commit',changed,choices(plan)).status_code == 409
    db = backend.conn()
    db.execute("UPDATE china_stock_receipts SET received_at='2026-01-04' WHERE package_id=1")
    db.commit(); db.close()
    assert post_file(client,path+'/commit',data,choices(plan)).status_code == 409


def test_csrf_owner_and_role_guards_run_before_source_write(review):
    client,sid,path,data,plan = review
    response = client.post(path+'/preview',data={'file':(io.BytesIO(data),'costs.json')})
    assert response.status_code == 403
    foreign = _actor()
    with client.session_transaction() as sess:
        sess['internal_actor_id'] = foreign.actor_id
    assert client.get(path).status_code == 404
    assert post_file(client,path+'/commit',data,choices(plan)).status_code == 404
    assert client.get(path+'/result.json').status_code == 404
    warehouse = _actor(role='MAGAZYN')
    with client.session_transaction() as sess:
        sess['internal_actor_id'] = warehouse.actor_id
    assert client.get(path).status_code == 403


@pytest.mark.parametrize('content',[b'[]',b'null',b'{',b'x'*(5*1024*1024+1)],
                         ids=['array','null','invalid-json','over-5mb'])
def test_invalid_or_large_file_returns_form_error(review,content):
    client,sid,path,data,plan = review
    assert post_file(client,path+'/preview',content).status_code == 400


def test_explicit_valuation_choice_is_saved_and_cannot_change_after_start(review):
    client,sid,path,data,plan = review
    fields = {'csrf_token':'source-csrf','method':'periodic_weighted_average','basis':'goods_transport'}
    assert client.post(path+'/method',data=fields).status_code == 409
    fields['confirmed'] = 'yes'
    fields['confirm_manual_basis'] = 'yes'
    assert client.post(path+'/method',data=fields).status_code == 302
    db = backend.conn()
    assert tuple(db.execute('SELECT method,basis FROM remanent_valuation_settings WHERE session_id=?',(sid,)).fetchone()) == ('periodic_weighted_average','goods_transport')
    assert db.execute('SELECT manual_basis_confirmed FROM remanent_valuation_settings WHERE session_id=?',(sid,)).fetchone()[0] == 1
    db.execute("UPDATE internal_inventory_count_sessions SET phase='IN_PROGRESS' WHERE session_id=?",(sid,))
    db.commit(); db.close()
    fields['basis'] = 'landed_cost'
    assert client.post(path+'/method',data=fields).status_code == 409
    assert 'Pokaż podgląd' not in client.get(path).get_data(as_text=True)


def test_more_than_1000_explicit_line_choices_survive_multipart_csrf_parser(review):
    client,sid,path,_data,_plan = review
    data = encoded([source('SOURCE-1',str(index),1) for index in range(1001)])
    response = post_file(client,path+'/preview',data)
    assert response.status_code == 200
    db = backend.conn()
    plan = sources.preview(db,sid,_owner().actor_id,data)
    db.close()
    assert post_file(client,path+'/commit',data,choices(plan)).status_code == 302


def test_export_route_keeps_domain_gate_and_download_headers(review,monkeypatch):
    client,sid,path,data,plan = review
    assert client.get(path+'/result.json').status_code == 409
    def completed_export(db, selected, actor):
        assert selected == sid and actor == _owner().actor_id
        return b'{"schema":"warehouse_inventory_result","version":1}'
    monkeypatch.setattr(sources,'export_result',completed_export)
    response = client.get(path+'/result.json')
    assert response.status_code == 200
    assert response.mimetype == 'application/json'
    assert 'attachment;' in response.headers['Content-Disposition']


def test_source_strings_are_escaped_and_missing_cost_remains_visible(review):
    client,sid,path,data,plan = review
    row = source('SOURCE-1','unsafe',1)
    row.update(document_no='<script>alert(1)</script>',line_value_pln=None,unit_value_pln=None)
    response = post_file(client,path+'/preview',encoded([row]))
    html = response.get_data(as_text=True)
    assert '<script>alert(1)</script>' not in html
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in html and 'BRAK / BRAK' in html
