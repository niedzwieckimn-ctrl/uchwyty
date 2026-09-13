import json
import uuid

import pytest

import app as backend
import agent_runtime as runtime
import business_operations as operations
import internal_approval as approvals
import internal_rbac as rbac
from test_business_operations import isolated, _actor, _owner


@pytest.fixture
def catalog(isolated):
    db=backend.conn(); now=backend.now_iso()
    for product_id,sku,model,qty in ((901,'UX-AVERY','Avery 160',24),(902,'UX-WINSOR','Winsor 128 BB',47)):
        db.execute('INSERT INTO products(id,sku,model,name,archived,created_at) VALUES(?,?,?,?,0,?)',
                   (product_id,sku,model,model,now))
        db.execute('INSERT INTO stock(product_id,qty) VALUES(?,?)',(product_id,qty))
    db.execute("INSERT INTO orders(id,order_no,customer_name,status,created_at,warehouse_issued) VALUES(903,'UX-ORDER','Test','confirmed',?,0)",(now,))
    db.execute("INSERT INTO order_items(order_id,product_id,sku,qty,created_at) VALUES(903,901,'UX-AVERY',5,?)",(now,))
    db.execute("INSERT INTO china_packages(id,package_no,status,created_at) VALUES(904,'PO UX','ordered',?)",(now,))
    db.execute("INSERT INTO china_items(package_id,product_id,sku,qty,created_at) VALUES(904,901,'UX-AVERY',7,?)",(now,))
    db.commit(); db.close()
    return 901,902


def run(owner,message,responses,conversation_id=''):
    return runtime.run_agent_turn(owner,message,runtime.FakeModelProvider(responses),conversation_id=conversation_id)


def start(owner,conversation_id=''):
    return run(owner,'Robimy remanent',[
        runtime.ProviderResponse(tool_calls=(runtime.ToolCall('start','inventory.count.session.start','{}'),),model='fake'),
        runtime.ProviderResponse(text='Remanent rozpoczęty. Podaj pierwszy produkt i policzoną ilość.',model='fake'),
    ],conversation_id)


def count(owner,conversation_id,product_id,quantity,key):
    args={'product_id':product_id,'counted_quantity':quantity,'expected_version':0,'idempotency_key':key}
    return run(owner,f'Policzono {quantity}',[
        runtime.ProviderResponse(tool_calls=(runtime.ToolCall('count','inventory.count.record',json.dumps(args)),),model='fake'),
        runtime.ProviderResponse(text=f'Policzono: {quantity}\nZgadza się.',model='fake'),
    ],conversation_id)


def test_a_b_c_active_session_is_automatic_and_reused(catalog):
    owner=_owner(); opened=start(owner)
    assert opened['status']=='SUCCESS' and 'session' not in opened['message'].lower()
    first=count(owner,opened['conversation_id'],catalog[0],24,'ux-count-1')
    second=count(owner,opened['conversation_id'],catalog[1],47,'ux-count-2')
    db=backend.conn()
    try:
        sessions=db.execute("SELECT * FROM internal_inventory_count_sessions WHERE created_by=? AND conversation_id=?",(owner.actor_id,opened['conversation_id'])).fetchall()
        items=db.execute('SELECT session_id,product_id FROM internal_inventory_count_items ORDER BY product_id').fetchall()
    finally: db.close()
    assert len(sessions)==1 and sessions[0]['status']=='OPEN'
    assert [row['session_id'] for row in items]==[sessions[0]['session_id'],sessions[0]['session_id']]
    assert first['speech_text'] and second['speech_text']


def test_d_completed_session_is_not_reused(catalog):
    owner=_owner(); opened=start(owner); count(owner,opened['conversation_id'],catalog[0],24,'ux-complete-count')
    completed=run(owner,'Zakończ remanent',[
        runtime.ProviderResponse(tool_calls=(runtime.ToolCall('complete','inventory.count.complete',json.dumps({'idempotency_key':'ux-complete'})),),model='fake'),
        runtime.ProviderResponse(text='Remanent zakończony.',model='fake'),
    ],opened['conversation_id'])
    assert completed['status']=='SUCCESS'
    restarted=start(owner,opened['conversation_id'])
    db=backend.conn()
    try:
        rows=db.execute('SELECT session_id,status FROM internal_inventory_count_sessions WHERE conversation_id=? ORDER BY created_at,session_id',(opened['conversation_id'],)).fetchall()
    finally: db.close()
    assert len(rows)==2 and {row['status'] for row in rows}=={'COMPLETED','OPEN'}
    assert restarted['conversation_id']==opened['conversation_id']


def test_e_f_active_session_never_crosses_conversation_or_actor(catalog):
    owner=_owner(); first=start(owner); second=start(owner)
    ai_first=rbac.load_actor_context(rbac.AI_OWNER_ASSISTANT_ACTOR_ID,delegated_by_actor_id=owner.actor_id)
    assert operations.active_inventory_count_session(ai_first,owner,first['conversation_id'])
    assert operations.active_inventory_count_session(ai_first,owner,second['conversation_id'])
    assert operations.active_inventory_count_session(ai_first,owner,first['conversation_id']) != operations.active_inventory_count_session(ai_first,owner,second['conversation_id'])
    other=_actor('HUMAN','OWNER'); other_run=start(other)
    ai_other=rbac.load_actor_context(rbac.AI_OWNER_ASSISTANT_ACTOR_ID,delegated_by_actor_id=other.actor_id)
    assert operations.active_inventory_count_session(ai_other,other,other_run['conversation_id'])
    with pytest.raises(operations.ControlledOperationError):
        operations.active_inventory_count_session(ai_first,owner,other_run['conversation_id'])


def test_g_session_identifiers_are_hidden_from_model_tool_inputs(catalog):
    descriptors={item['name']:item for item in runtime._tool_descriptors(
        rbac.load_actor_context(rbac.AI_OWNER_ASSISTANT_ACTOR_ID,delegated_by_actor_id=_owner().actor_id),_owner())}
    for name in runtime.COUNT_SESSION_BOUND:
        assert 'count_session_id' not in descriptors[name]['parameters']['properties']
        assert 'conversation_id' not in descriptors[name]['parameters']['properties']
    assert descriptors['inventory.count.session.start']['parameters']['properties']=={}


def test_h_n_plain_message_and_voice_ready_contract(catalog):
    result=run(_owner(),'Pokaż stan',[
        runtime.ProviderResponse(text='**Avery 160**\n| Magazyn | 24 |\n| --- | --- |\nDostępne dla klientów: 19',model='fake')
    ])
    assert result['status']=='SUCCESS'
    assert all(marker not in result['message'] for marker in ('**','`','| --- |','|'))
    assert result['speech_text'] and all(marker not in result['speech_text'] for marker in ('**','`','|','http://','https://'))
    assert {'message','speech_text','artifacts','approvals'} <= set(result)


def test_product_summary_reuses_existing_availability_and_image_is_opt_in(catalog,tmp_path):
    product_id,_=catalog
    image_dir=tmp_path/'product_images'; image_dir.mkdir(); image=image_dir/'a.png'; image.write_bytes(b'png')
    db=backend.conn(); now=backend.now_iso()
    image_id=db.execute('INSERT INTO product_images(stored_path,filename,created_at) VALUES(?,?,?)',(str(image),'a.png',now)).lastrowid
    db.execute('INSERT INTO product_image_assignments(product_id,image_id,created_at) VALUES(?,?,?)',(product_id,image_id,now)); db.commit(); db.close()
    ordinary=operations.execute_business_operation(_owner(),'inventory.product.get',{'product_id':product_id})
    assert ordinary.data['stock']==24 and ordinary.data['ordered_quantity']==5
    assert ordinary.data['incoming_quantity']==7 and ordinary.data['available_for_customers']==19
    assert not any(a['type']=='product_image' for a in backend.build_business_artifacts('inventory.product.get',ordinary.data))
    requested=operations.execute_business_operation(_owner(),'inventory.product.get',{'product_id':product_id,'include_image':True})
    assert any(a['type']=='product_image' for a in backend.build_business_artifacts('inventory.product.get',requested.data))


def test_approval_ui_keeps_technical_outcome_out_of_chat_fallback(catalog):
    html=backend.app.test_client().get('/ai-assistant').get_data(as_text=True)
    # Route may redirect without login; static source remains the authoritative UI check.
    source=(__import__('pathlib').Path(__file__).parent/'templates'/'ai_assistant.html').read_text(encoding='utf-8')
    assert 'JSON.stringify(result.execution_outcome' not in source
    assert "response.ok ? 'Decyzja została zapisana.'" in source


def _add_counted_product(product_id=905, model='Cerne 128 BB', quantity=1):
    db=backend.conn(); now=backend.now_iso()
    db.execute('INSERT INTO products(id,sku,model,name,archived,created_at) VALUES(?,?,?,?,0,?)',
               (product_id,f'UX-{product_id}',model,model,now))
    db.execute('INSERT INTO stock(product_id,qty) VALUES(?,?)',(product_id,quantity))
    db.commit(); db.close()
    return product_id


def _record_turn(owner, conversation_id, product_id, counted, expected_version, message):
    args={'product_id':product_id,'counted_quantity':counted,'expected_version':expected_version,
          'idempotency_key':f'count-{product_id}-{counted}'}
    return run(owner,message,[
        runtime.ProviderResponse(tool_calls=(
            runtime.ToolCall('product','inventory.product.get',json.dumps({'product_id':product_id})),
            runtime.ToolCall('expected','inventory.count.get_expected',json.dumps({'product_id':product_id})),
            runtime.ToolCall('record','inventory.count.record',json.dumps(args)),
        ),model='fake'),
        runtime.ProviderResponse(text=(
            f'Cerne 128 BB.\nSystem: 1\nPoliczono: {counted}\nRóżnica: +{counted-1}.\n'
            f'Skorygować stan do {counted}?'
        ) if counted != 1 else 'Cerne 128 BB.\nPoliczono: 1.\nZgadza się.',model='fake'),
    ],conversation_id)


def _prepare_adjustment(owner, conversation_id, product_id, expected_version=0):
    args={'product_id':product_id,'expected_version':expected_version,
          'idempotency_key':f'adjust-{product_id}'}
    return run(owner,'Tak, przygotuj korektę',[
        runtime.ProviderResponse(tool_calls=(runtime.ToolCall(
            'adjust','inventory.adjust',json.dumps(args)),),model='fake'),
        runtime.ProviderResponse(text='Korekta czeka na zatwierdzenie przez człowieka.',model='fake'),
    ],conversation_id)


def _approval_client(isolated):
    with isolated.session_transaction() as session:
        session['admin_authenticated']=True
        rbac.bind_bootstrap_owner_session(session)
    return isolated


def test_remanent_no_difference_has_one_clean_card_and_no_approval(catalog):
    product_id=_add_counted_product(quantity=1); owner=_owner(); opened=start(owner)
    result=_record_turn(owner,opened['conversation_id'],product_id,1,0,'Cerne 128 BB mam 1 sztukę')

    assert result['approvals']==[]
    assert [item['type'] for item in result['artifacts']]==['inventory_count_card']
    card=result['artifacts'][0]
    assert card=={'type':'inventory_count_card','display_name':'Cerne 128 BB',
                  'expected_quantity':1,'counted_quantity':1,'difference':0,'matches':True}
    db=backend.conn()
    try: assert db.execute('SELECT qty FROM stock WHERE product_id=?',(product_id,)).fetchone()['qty']==1
    finally: db.close()


def test_remanent_difference_prepare_and_human_approve_changes_stock(catalog, isolated, monkeypatch):
    product_id=_add_counted_product(); owner=_owner(); opened=start(owner)
    counted=_record_turn(owner,opened['conversation_id'],product_id,3,0,'Cerne 128 BB na półce 3 sztuki')

    assert counted['message'].endswith('Skorygować stan do 3?')
    assert [item['type'] for item in counted['artifacts']]==['inventory_count_card']
    assert counted['artifacts'][0]['difference']==2
    pending=_prepare_adjustment(owner,opened['conversation_id'],product_id)
    approval=pending['approvals'][0]
    assert approval['operation']=='inventory.adjust'
    assert approval['product_name']=='Cerne 128 BB'
    assert (approval['from_quantity'],approval['to_quantity'])==(1,3)
    db=backend.conn(); assert db.execute('SELECT qty FROM stock WHERE product_id=?',(product_id,)).fetchone()['qty']==1; db.close()

    monkeypatch.setattr(backend,'AGENT_MODEL_PROVIDER',runtime.FakeModelProvider([
        runtime.ProviderResponse(text='Stan Cerne 128 BB skorygowany do 3 sztuk.',model='fake')]))
    response=_approval_client(isolated).post(
        f"/api/internal/ai/approvals/{approval['approval_id']}/approve",
        json={'conversation_id':opened['conversation_id']})

    body=response.get_json()
    assert response.status_code==200 and body['status']=='SUCCESS'
    assert body['message']=='Stan Cerne 128 BB skorygowany do 3 sztuk.'
    db=backend.conn()
    try:
        assert db.execute('SELECT qty FROM stock WHERE product_id=?',(product_id,)).fetchone()['qty']==3
        assert db.execute("SELECT COUNT(*) FROM internal_audit_log WHERE operation='inventory.adjust' AND result='SUCCESS'").fetchone()[0]==1
    finally: db.close()


def test_remanent_reject_keeps_stock_and_count_observation(catalog, isolated, monkeypatch):
    product_id=_add_counted_product(); owner=_owner(); opened=start(owner)
    _record_turn(owner,opened['conversation_id'],product_id,3,0,'Cerne 128 BB mam 3')
    pending=_prepare_adjustment(owner,opened['conversation_id'],product_id); approval=pending['approvals'][0]
    monkeypatch.setattr(backend,'AGENT_MODEL_PROVIDER',runtime.FakeModelProvider([
        runtime.ProviderResponse(text='Korekta odrzucona. Stan pozostał bez zmian.',model='fake')]))

    response=_approval_client(isolated).post(
        f"/api/internal/ai/approvals/{approval['approval_id']}/reject",
        json={'conversation_id':opened['conversation_id']})

    assert response.get_json()['message']=='Korekta odrzucona. Stan pozostał bez zmian.'
    db=backend.conn()
    try:
        assert db.execute('SELECT qty FROM stock WHERE product_id=?',(product_id,)).fetchone()['qty']==1
        assert db.execute('SELECT status FROM internal_inventory_count_items WHERE product_id=?',(product_id,)).fetchone()['status']=='PENDING_ADJUSTMENT'
    finally: db.close()
    assert approvals.get_request_snapshot(approval['approval_id'])['status']=='REJECTED'


def test_remanent_conflict_does_not_apply_stale_count(catalog, isolated, monkeypatch):
    product_id=_add_counted_product(); owner=_owner(); opened=start(owner)
    _record_turn(owner,opened['conversation_id'],product_id,3,0,'Cerne 128 BB mam 3')
    pending=_prepare_adjustment(owner,opened['conversation_id'],product_id); approval=pending['approvals'][0]
    db=backend.conn(); db.execute('UPDATE stock SET qty=2 WHERE product_id=?',(product_id,)); db.commit(); db.close()
    monkeypatch.setattr(backend,'AGENT_MODEL_PROVIDER',runtime.FakeModelProvider([
        runtime.ProviderResponse(text='Stan zmienił się od czasu liczenia. Trzeba policzyć produkt ponownie.',model='fake')]))

    response=_approval_client(isolated).post(
        f"/api/internal/ai/approvals/{approval['approval_id']}/approve",
        json={'conversation_id':opened['conversation_id']})

    body=response.get_json()
    assert body['status']=='CONFLICT'
    assert body['message']=='Stan zmienił się od czasu liczenia. Trzeba policzyć produkt ponownie.'
    db=backend.conn()
    try: assert db.execute('SELECT qty FROM stock WHERE product_id=?',(product_id,)).fetchone()['qty']==2
    finally: db.close()


def test_ai_cannot_approve_its_inventory_adjustment(catalog):
    product_id=_add_counted_product(); owner=_owner(); opened=start(owner)
    _record_turn(owner,opened['conversation_id'],product_id,3,0,'Cerne 128 BB mam 3')
    pending=_prepare_adjustment(owner,opened['conversation_id'],product_id)
    ai=rbac.load_actor_context(rbac.AI_OWNER_ASSISTANT_ACTOR_ID,delegated_by_actor_id=owner.actor_id)

    with pytest.raises(approvals.ApprovalDenied):
        approvals.approve_request(pending['approvals'][0]['approval_id'],ai)
