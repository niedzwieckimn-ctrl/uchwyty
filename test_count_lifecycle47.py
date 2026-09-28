"""Session gates, real count writes and pagination past the old 500-row cutoff."""
from datetime import datetime, timedelta, timezone
import uuid

import pytest

import agent_conversation
import app as backend
import business_operations as operations
import internal_approval
import internal_rbac as rbac
import inventory_count_lifecycle as lifecycle
import remanent
import remanent_sources
from test_business_operations import isolated, _owner, _actor


def ai(owner=None):
    return rbac.load_actor_context(rbac.AI_OWNER_ASSISTANT_ACTOR_ID,
        delegated_by_actor_id=(owner or _owner()).actor_id)


def conversation(owner=None):
    human = owner or _owner()
    return agent_conversation.open_conversation(human, ai(human))[0]


def operation(name, payload, owner=None):
    return operations.execute_business_operation(ai(owner),name,payload)


def start(cid=None, selected=None, owner=None):
    cid = cid or conversation(owner)
    payload = {'conversation_id':cid,'idempotency_key':str(uuid.uuid4())}
    if selected: payload['count_session_id'] = selected
    result = operation('inventory.count.session.start',payload,owner)
    return cid,result


def count(sid, quantity, product=801):
    expected = operation('inventory.count.get_expected',{'product_id':product})
    assert expected.status == 'SUCCESS'
    return operation('inventory.count.record',{'count_session_id':sid,'product_id':product,
        'counted_quantity':quantity,'expected_version':expected.data['version'],
        'idempotency_key':str(uuid.uuid4())})


@pytest.fixture
def stock(isolated):
    db = backend.conn()
    db.execute("INSERT INTO products(id,sku,model,name,created_at) VALUES(801,'LIFE-801','Sam','Sam BB 96',?)",(backend.now_iso(),))
    db.execute('INSERT INTO stock(product_id,qty) VALUES(801,10)')
    db.commit(); db.close()
    return _owner()


def annual(db, owner):
    sid = remanent.create_draft(db,owner.actor_id,2026)
    remanent.add_entry(db,sid,owner.actor_id,product_id=801,kind='opening',
        period_start='2026-01-01',period_end='2026-01-01',quantity=10,
        unit_value_pln='2',source='Synthetic opening document')
    remanent.confirm_purchase_coverage(db,sid,owner.actor_id,'2026-09-28')
    remanent_sources.select_method(db,sid,owner.actor_id,'periodic_weighted_average','goods_net',confirm_manual_basis=True)
    db.commit()
    remanent.start_count(db,sid,owner.actor_id,'2026-09-28')
    return sid


def test_current_summary_and_full_history_paginate_beyond_500_observations(stock):
    _,opened = start()
    assert opened.status == 'SUCCESS'
    sid = opened.data['count_id']
    db = backend.conn()
    stamp = backend.now_iso()
    # Large existing session; the newest observation still uses the public write operation.
    for pid in range(10000,10501):
        db.execute('INSERT INTO products(id,sku,model,name,created_at) VALUES(?,?,?,?,?)',
                   (pid,f'PAGE-{pid}','Paged','Synthetic',stamp))
        db.execute('INSERT INTO stock(product_id,qty) VALUES(?,10)',(pid,))
        db.execute('''INSERT INTO internal_inventory_count_items(session_id,product_id,expected_quantity,
            counted_quantity,difference,stock_version,status,created_by,created_at)
            VALUES(?,?,10,10,0,0,'MATCHED',?,?)''',(sid,pid,stock.actor_id,stamp))
    db.commit(); db.close()
    latest = count(sid,8,10500)
    assert latest.status == 'SUCCESS'
    current = []
    offset = 0
    while True:
        page = operation('inventory.count.summary',{'count_session_id':sid,'offset':offset,'limit':200})
        assert page.status == 'SUCCESS'
        assert page.data['total_current_items'] == 501
        assert (page.data['matched_count'],page.data['unresolved_count'],page.data['negative_units']) == (500,1,2)
        current.extend(page.data['items'])
        offset = page.data['next_offset']
        if offset is None: break
    assert len(current) == len({row['product_id'] for row in current}) == 501
    assert current[-1]['product_id'] == 10500 and current[-1]['counted_quantity'] == 8
    history = []
    cursor = 0
    while True:
        page = operation('inventory.count.history',{'count_session_id':sid,'after_item_id':cursor,'limit':200})
        assert page.status == 'SUCCESS'
        history.extend(page.data['items'])
        cursor = page.data['next_cursor']
        if cursor is None: break
    assert len(history) == len({row['item_id'] for row in history}) == 502
    versions = [row for row in history if row['product_id'] == 10500]
    assert [(row['status'],row['counted_quantity']) for row in versions] == [('SUPERSEDED',10),('PENDING_ADJUSTMENT',8)]


def test_measured_pause_resume_excludes_pause_and_blocks_writes(stock,monkeypatch):
    clock = [datetime(2026,9,28,10,tzinfo=timezone.utc)]
    class MeasuredDateTime(datetime):
        @classmethod
        def now(cls,tz=None):
            return clock[0]
    monkeypatch.setattr(lifecycle,'datetime',MeasuredDateTime)
    _,opened = start()
    sid = opened.data['count_id']
    clock[0] += timedelta(seconds=40)
    paused = operation('inventory.count.pause',{'count_session_id':sid,'idempotency_key':'pause'})
    assert paused.status == 'SUCCESS' and paused.data['timing']['active_seconds'] == 40
    clock[0] += timedelta(seconds=120)
    summary = operation('inventory.count.summary',{'count_session_id':sid}).data
    assert summary['timing']['active_seconds'] == 40 and summary['timing']['calendar_seconds'] == 160
    denied = count(sid,10)
    assert denied.status == 'CONFLICT' and denied.error_code == 'COUNT_SESSION_PAUSED'
    assert operation('inventory.count.resume',{'count_session_id':sid,'idempotency_key':'resume'}).status == 'SUCCESS'
    assert count(sid,10).status == 'SUCCESS'
    clock[0] += timedelta(seconds=20)
    assert operation('inventory.count.complete',{'count_session_id':sid,'idempotency_key':'close'}).status == 'SUCCESS'
    clock[0] += timedelta(seconds=300)
    timing = operation('inventory.count.summary',{'count_session_id':sid}).data['timing']
    assert timing['active_seconds'] == 60 and timing['calendar_seconds'] == 180
    assert timing['timing_complete'] is True


def test_keep_result_cancels_authorization_and_preserves_stock(stock):
    _,opened = start()
    sid = opened.data['count_id']
    counted = count(sid,8)
    assert counted.status == 'SUCCESS'
    payload = {'count_session_id':sid,'product_id':801,'expected_version':counted.data['version'],
               'idempotency_key':'adjust'}
    pending = operation('inventory.adjust',payload)
    assert pending.status == 'PENDING_APPROVAL'
    kept = operation('inventory.count.keep_result',{**payload,'idempotency_key':'keep-result'})
    assert kept.status == 'SUCCESS' and kept.data['status'] == 'COUNT_ONLY'
    assert internal_approval.get_request_snapshot(pending.approval_id)['status'] == 'CANCELLED'
    db = backend.conn()
    assert db.execute('SELECT qty FROM stock WHERE product_id=801').fetchone()[0] == 10
    assert db.execute('SELECT counted_quantity,status FROM internal_inventory_count_items WHERE session_id=?',(sid,)).fetchone()['counted_quantity'] == 8
    assert db.execute('SELECT status FROM internal_operation_executions WHERE approval_id=?',(pending.approval_id,)).fetchone()[0] == 'CONFLICT'
    db.close()
    assert operation('inventory.count.summary',{'count_session_id':sid}).data['unresolved_count'] == 0
    assert operation('inventory.count.complete',{'count_session_id':sid,'idempotency_key':'complete'}).status == 'SUCCESS'
    assert operation('inventory.adjust',payload).status != 'SUCCESS'


def test_annual_draft_does_not_take_over_or_block_ordinary_count(stock):
    db = backend.conn()
    annual_id = remanent.create_draft(db,stock.actor_id,2026)
    db.close()
    cid,opened = start()
    assert opened.status == 'SUCCESS' and opened.data['count_id'] != annual_id
    assert operations.active_inventory_count_session(ai(),stock,cid) == opened.data['count_id']
    assert count(opened.data['count_id'],10).status == 'SUCCESS'
    db = backend.conn()
    draft = db.execute('SELECT phase,conversation_id FROM internal_inventory_count_sessions WHERE session_id=?',(annual_id,)).fetchone()
    assert draft['phase'] == 'DRAFT' and draft['conversation_id'] == ''
    db.close()


def test_annual_binding_is_explicit_owned_and_does_not_cross_conversations(stock):
    db = backend.conn()
    annual_id = annual(db,stock)
    db.close()
    ordinary_cid,ordinary = start()
    assert ordinary.status == 'SUCCESS' and ordinary.data['count_id'] != annual_id
    selected_cid,selected = start(selected=annual_id)
    assert selected.status == 'SUCCESS' and selected.data['count_id'] == annual_id
    assert operations.active_inventory_count_session(ai(),stock,selected_cid) == annual_id
    assert operations.active_inventory_count_session(ai(),stock,ordinary_cid) == ordinary.data['count_id']
    _,conflict = start(cid=ordinary_cid,selected=annual_id)
    assert conflict.status == 'CONFLICT' and conflict.error_code == 'COUNT_SESSION_IN_USE'
    foreign = _actor()
    _,denied = start(selected=annual_id,owner=foreign)
    assert denied.status == 'DENIED' and denied.error_code == 'COUNT_SESSION_ACCESS_DENIED'
    db = backend.conn()
    db.execute("INSERT INTO products(id,sku,model,name,created_at) VALUES(802,'LATE-802','New','Late product',?)",(backend.now_iso(),))
    db.execute('INSERT INTO stock(product_id,qty) VALUES(802,1)')
    db.commit(); db.close()
    rejected = count(annual_id,1,802)
    assert rejected.status == 'CONFLICT' and rejected.error_code == 'COUNT_PRODUCT_NOT_IN_SNAPSHOT'


def test_nonexistent_count_session_is_still_rejected(stock):
    rejected = count(str(uuid.uuid4()),10)
    assert rejected.status == 'CONFLICT' and rejected.error_code == 'COUNT_SESSION_NOT_FOUND'
