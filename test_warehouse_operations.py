import uuid
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

import app as backend
import business_operations as operations
import internal_approval as approvals
import internal_rbac as rbac
import agent_runtime as runtime
from test_business_operations import isolated, _actor, _owner


def ai(role='AI_OWNER_ASSISTANT'):
    actor_id = rbac.AI_OWNER_ASSISTANT_ACTOR_ID if role == 'AI_OWNER_ASSISTANT' else _actor('AI_AGENT', role).actor_id
    return rbac.load_actor_context(actor_id, delegated_by_actor_id=rbac.BOOTSTRAP_OWNER_ACTOR_ID)


@pytest.fixture
def warehouse(isolated):
    db=backend.conn(); now=backend.now_iso()
    db.execute("INSERT INTO products(id,sku,model,name,archived,created_at) VALUES(801,'WH-801','WH','Test',0,?)",(now,))
    db.execute('INSERT INTO stock(product_id,qty) VALUES(801,10)')
    db.execute("INSERT INTO orders(id,order_no,customer_name,status,created_at,warehouse_issued) VALUES(802,'WH-802','Test','confirmed',?,0)",(now,))
    db.execute("INSERT INTO order_items(order_id,product_id,sku,qty,created_at) VALUES(802,801,'WH-801',4,?)",(now,))
    db.commit(); db.close()
    return 801,802


def op(name,payload,actor=None):
    return operations.execute_business_operation(actor or ai(),name,payload)


def record(product,counted,key='count-1',session=None,version=0,actor=None):
    return op('inventory.count.record',{'product_id':product,'count_session_id':session or str(uuid.uuid4()),
        'counted_quantity':counted,'expected_version':version,'idempotency_key':key},actor)


def test_expected_and_count_record_do_not_change_stock(warehouse):
    product,_=warehouse; session=str(uuid.uuid4())
    expected=op('inventory.count.get_expected',{'product_id':product})
    assert expected.status=='SUCCESS' and expected.data['expected_quantity']==10 and expected.data['version']==0
    result=record(product,8,session=session)
    assert result.status=='SUCCESS' and result.data['difference']==-2 and result.data['status']=='PENDING_ADJUSTMENT'
    db=backend.conn()
    try:
        assert db.execute('SELECT qty FROM stock WHERE product_id=?',(product,)).fetchone()['qty']==10
        assert db.execute('SELECT COUNT(*) n FROM stock_adjustments').fetchone()['n']==0
    finally: db.close()
    assert record(product,8,session=session)==result


def test_count_summary_and_unresolved_session_guard(warehouse):
    product,_=warehouse; session=str(uuid.uuid4()); record(product,8,session=session)
    summary=op('inventory.count.summary',{'count_session_id':session})
    assert summary.data['variance_count']==1 and summary.data['unresolved_count']==1
    complete=op('inventory.count.complete',{'count_session_id':session,'idempotency_key':'complete-blocked'})
    assert complete.status=='CONFLICT' and complete.error_code=='UNRESOLVED_DISCREPANCIES'


def test_adjust_requires_approval_is_atomic_and_idempotent(warehouse):
    product,_=warehouse; session=str(uuid.uuid4()); record(product,8,session=session)
    payload={'product_id':product,'count_session_id':session,'expected_version':0,'idempotency_key':'adjust-1'}
    pending=op('inventory.adjust',payload)
    assert pending.status=='PENDING_APPROVAL'
    db=backend.conn(); assert db.execute('SELECT qty FROM stock WHERE product_id=?',(product,)).fetchone()['qty']==10; db.close()
    approvals.approve_request(pending.approval_id,_owner())
    result=op('inventory.adjust',payload)
    assert result.status=='SUCCESS' and result.data['difference']==-2 and result.data['version']==1
    assert op('inventory.adjust',payload)==result
    db=backend.conn()
    try:
        assert db.execute('SELECT qty FROM stock WHERE product_id=?',(product,)).fetchone()['qty']==8
        assert db.execute("SELECT COUNT(*) n FROM stock_adjustments WHERE mode='inventory_count'").fetchone()['n']==1
        assert db.execute('SELECT status FROM internal_inventory_count_items WHERE session_id=?',(session,)).fetchone()['status']=='ADJUSTED'
    finally: db.close()
    done=op('inventory.count.complete',{'count_session_id':session,'idempotency_key':'complete-ok'})
    assert done.status=='SUCCESS' and done.data['status']=='COMPLETED'


def test_adjust_revalidates_stock_after_approval(warehouse):
    product,_=warehouse; session=str(uuid.uuid4()); record(product,8,session=session)
    payload={'product_id':product,'count_session_id':session,'expected_version':0,'idempotency_key':'adjust-stale'}
    pending=op('inventory.adjust',payload); approvals.approve_request(pending.approval_id,_owner())
    db=backend.conn(); db.execute('UPDATE stock SET qty=9 WHERE product_id=?',(product,)); db.commit(); db.close()
    result=op('inventory.adjust',payload)
    assert result.status=='CONFLICT' and result.error_code=='ENTITY_VERSION_CONFLICT'
    db=backend.conn()
    try:
        assert db.execute('SELECT qty FROM stock WHERE product_id=?',(product,)).fetchone()['qty']==9
        assert db.execute('SELECT status FROM internal_inventory_count_items WHERE session_id=?',(session,)).fetchone()['status']=='PENDING_ADJUSTMENT'
    finally: db.close()


def test_adjust_handler_failure_rolls_back_write_and_approval(warehouse,monkeypatch):
    product,_=warehouse; session=str(uuid.uuid4()); record(product,8,session=session)
    payload={'product_id':product,'count_session_id':session,'expected_version':0,'idempotency_key':'adjust-fail'}
    pending=op('inventory.adjust',payload); approvals.approve_request(pending.approval_id,_owner())
    def failing(data,actor,correlation,db):
        db.execute('UPDATE stock SET qty=1 WHERE product_id=?',(product,))
        raise RuntimeError('injected')
    monkeypatch.setitem(operations._HANDLERS,'inventory.adjust',failing)
    result=op('inventory.adjust',payload)
    assert result.status=='FAILED'
    db=backend.conn()
    try: assert db.execute('SELECT qty FROM stock WHERE product_id=?',(product,)).fetchone()['qty']==10
    finally: db.close()
    assert approvals.get_request_snapshot(pending.approval_id)['status']=='APPROVED'


def test_packing_check_uses_shared_readiness_and_shortage_report_is_observational(warehouse):
    product,order=warehouse
    check=op('orders.packing.check',{'order_id':order})
    assert check.status=='SUCCESS' and check.data['ready'] is True and check.data['total_units']==4
    payload={'order_id':order,'product_id':product,'missing_quantity':2,'idempotency_key':'short-1','note':'Potwierdzone przy stole'}
    report=op('orders.packing.shortage.report',payload)
    assert report.status=='SUCCESS' and op('orders.packing.shortage.report',payload)==report
    db=backend.conn()
    try:
        assert db.execute('SELECT qty FROM stock WHERE product_id=?',(product,)).fetchone()['qty']==10
        assert db.execute('SELECT status FROM orders WHERE id=?',(order,)).fetchone()['status']=='confirmed'
    finally: db.close()


def test_packing_confirm_requires_physical_confirmation_and_approval_without_stock_issue(warehouse):
    product,order=warehouse
    denied=op('orders.packing.confirm',{'order_id':order,'expected_version':0,'idempotency_key':'pack-no','human_confirmed':False})
    assert denied.status=='DENIED' and denied.error_code=='PHYSICAL_CONFIRMATION_REQUIRED'
    payload={'order_id':order,'expected_version':0,'idempotency_key':'pack-yes','human_confirmed':True}
    pending=op('orders.packing.confirm',payload); assert pending.status=='PENDING_APPROVAL'
    approvals.approve_request(pending.approval_id,_owner())
    result=op('orders.packing.confirm',payload)
    assert result.status=='SUCCESS' and op('orders.packing.confirm',payload)==result
    db=backend.conn()
    try:
        order_row=db.execute('SELECT status,packed_at,warehouse_issued FROM orders WHERE id=?',(order,)).fetchone()
        assert order_row['status']=='packed' and order_row['packed_at'] and order_row['warehouse_issued']==0
        assert db.execute('SELECT qty FROM stock WHERE product_id=?',(product,)).fetchone()['qty']==10
    finally: db.close()


def test_parallel_packing_confirmation_executes_once_and_has_business_audit(warehouse):
    _,order=warehouse
    payload={'order_id':order,'expected_version':0,'idempotency_key':'pack-race','human_confirmed':True}
    pending=op('orders.packing.confirm',payload); approvals.approve_request(pending.approval_id,_owner())
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:op('orders.packing.confirm',payload),range(2)))
    assert all(result.status=='SUCCESS' for result in results)
    assert len({result.execution_id for result in results})==1
    db=backend.conn()
    try:
        rows=db.execute("SELECT actor_type,after_state FROM internal_audit_log WHERE operation='orders.packing.confirm' AND result='SUCCESS'").fetchall()
        assert len(rows)==1 and rows[0]['actor_type']=='AI_AGENT'
        assert json.loads(rows[0]['after_state'])['basis']=='human_physical_confirmation'
    finally: db.close()


def test_packing_confirm_blocks_incomplete_order(warehouse):
    product,order=warehouse
    db=backend.conn(); db.execute('UPDATE stock SET qty=2 WHERE product_id=?',(product,)); db.commit(); db.close()
    version=op('orders.packing.check',{'order_id':order}).data['version']
    result=op('orders.packing.confirm',{'order_id':order,'expected_version':version,'idempotency_key':'pack-short','human_confirmed':True})
    assert result.status=='CONFLICT' and result.error_code=='ORDER_NOT_READY'


def test_http_approval_executes_packing_confirmation(warehouse, isolated):
    product,order=warehouse
    payload={'order_id':order,'expected_version':0,'idempotency_key':'pack-http','human_confirmed':True}
    pending=op('orders.packing.confirm',payload)
    with isolated.session_transaction() as session:
        session['admin_authenticated']=True
        rbac.bind_bootstrap_owner_session(session)
    response=isolated.post(f'/api/internal/ai/approvals/{pending.approval_id}/approve',json={})
    assert response.status_code==200 and response.get_json()['status']=='SUCCESS'
    db=backend.conn()
    try:
        assert db.execute('SELECT status FROM orders WHERE id=?',(order,)).fetchone()['status']=='packed'
        assert db.execute('SELECT qty FROM stock WHERE product_id=?',(product,)).fetchone()['qty']==10
    finally: db.close()


def test_ai_without_warehouse_permission_is_denied(warehouse):
    product,_=warehouse
    denied=record(product,10,actor=_actor('AI_AGENT','AI_FINANCE'))
    assert denied.status=='DENIED' and denied.error_code=='PERMISSION_DENIED'


def test_runtime_exposes_warehouse_tools_and_pending_card(warehouse):
    _,order=warehouse
    names={item['name'] for item in runtime._tool_descriptors(ai(),_owner())}
    assert {'inventory.count.record','inventory.adjust','orders.packing.check',
            'orders.packing.shortage.report','orders.packing.confirm'} <= names
    args={'order_id':order,'expected_version':0,'idempotency_key':'pack-runtime','human_confirmed':True}
    provider=runtime.FakeModelProvider([
        runtime.ProviderResponse(tool_calls=(runtime.ToolCall('pack','orders.packing.confirm',json.dumps(args)),),model='fake'),
        runtime.ProviderResponse(text='Pakowanie czeka na zatwierdzenie.',model='fake'),
    ])
    result=runtime.run_agent_turn(_owner(),'Fizycznie spakowałem zamówienie 802.',provider)
    assert result['status']=='SUCCESS'
    assert result['pending_approvals'][0]['operation']=='orders.packing.confirm'


def test_warehouse_artifact_cards(warehouse):
    product,order=warehouse
    count=op('inventory.count.get_expected',{'product_id':product})
    packing=op('orders.packing.check',{'order_id':order})
    assert backend.build_business_artifacts('inventory.count.get_expected',count.data)[0]['type']=='inventory_count_card'
    assert backend.build_business_artifacts('orders.packing.check',packing.data)[0]['type']=='packing_check_card'
