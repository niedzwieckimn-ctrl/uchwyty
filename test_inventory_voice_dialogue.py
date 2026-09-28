"""Real local count/approval state with scripted model and a synthetic catalog."""
import pytest
import json
import uuid

import app as backend
import agent_runtime as runtime
import internal_approval
from test_agent_runtime import isolated, owner, tool, respond


@pytest.fixture
def counting(isolated):
    db = backend.conn()
    for identifier, model, color, spacing, qty in [
        (901,'Sam','BB','96',7), (902,'Carl','BB','320',5),
        (903,'Avery','BB','160',36), (904,'Dover','BLB','192-224',5),
        (905,'Dover','BLB','224-256',5), (906,'Dover','BLK','160',9),
    ]:
        db.execute('INSERT INTO products(id,sku,model,name,created_at) VALUES(?,?,?,?,?)',
                   (identifier,f'VOICE-{identifier}-{color}-{spacing}',model,
                    f'{model} {color} {spacing}',backend.now_iso()))
        db.execute('INSERT INTO stock(product_id,qty) VALUES(?,?)',(identifier,qty))
    db.commit(); db.close()
    opened = runtime.run_agent_turn(owner(),'Robimy remanent',runtime.FakeModelProvider([
        tool('inventory.count.session.start',{}), respond('Rozpoczęto liczenie.')]))
    assert opened['status'] == 'SUCCESS'
    cid = opened['conversation_id']
    def turn(text):
        provider = runtime.FakeModelProvider([])
        result = runtime.run_agent_turn(owner(),text,provider,conversation_id=cid,voice_fast_mode=True)
        assert result['status'] == 'SUCCESS', result
        assert provider.calls == [], 'This concrete count must not fall back to a model'
        return result
    return cid,turn


def observations():
    db = backend.conn()
    try:
        return [dict(row) for row in db.execute('SELECT * FROM internal_inventory_count_items ORDER BY item_id')]
    finally:
        db.close()


def test_sam_and_carl_identity_does_not_consume_spacing_as_count(counting):
    _,turn = counting
    first = turn('Sam BB 96')
    assert first['inventory_fast_state'] == 'WAIT_COUNT'
    assert observations() == []
    turn('7 sztuk')
    turn('Karl BB 320')
    assert len(observations()) == 1
    turn('5 sztuk')
    assert [(row['product_id'],row['counted_quantity']) for row in observations()] == [(901,7),(902,5)]


def test_range_choice_retains_quantity_and_next_product_cannot_inherit_it(counting):
    _,turn = counting
    ambiguous = turn('Dover BLB 224 5')
    assert '192-224' in ambiguous['message'] and '224-256' in ambiguous['message']
    assert observations() == []
    selected = turn('tę drugą')
    assert '224-256' in selected['message']
    assert [(row['product_id'],row['counted_quantity']) for row in observations()] == [(905,5)]
    turn('Sam BB 96')
    assert len(observations()) == 1
    turn('7')
    assert observations()[-1]['product_id'] == 901


def test_avery_ambiguous_quantity_is_resolved_without_losing_product(counting):
    _,turn = counting
    first = turn('Avery BB 160 30 czy 36?')
    assert '30 czy 36' in first['message']
    assert observations() == []
    turn('36 sztuk')
    assert [(row['product_id'],row['counted_quantity']) for row in observations()] == [(903,36)]


def test_correction_records_final_quantity_then_same_product_recount_cancels_old_approval(counting):
    _,turn = counting
    turn('Sam BB 96')
    corrected = turn('mam cztery, poprawka pięć')
    approval = corrected['approvals'][0]
    assert 'Sam BB 96' in approval['product_name']
    card = next(item for item in corrected['artifacts'] if item['type'] == 'inventory_count_card')
    assert 'Sam BB 96' in card['display_name']
    assert 'VOICE-901-BB-96' in card['display_name']
    assert observations()[-1]['counted_quantity'] == 5
    matched = turn('Sam BB 96 mam 7')
    assert matched['inventory_fast_state'] == 'WAIT_PRODUCT'
    assert observations()[-1]['status'] == 'MATCHED'
    assert internal_approval.get_request_snapshot(approval['approval_id'])['status'] != 'PENDING'
    turn('Carl BB 320')
    assert observations()[-1]['product_id'] == 901


def test_addition_is_one_count_and_yes_executes_only_its_pending_adjustment(counting):
    _,turn = counting
    turn('Sam BB 96')
    counted = turn('mam dwie… nie, mam jeszcze siedem')
    assert observations()[-1]['counted_quantity'] == 9
    assert counted['approvals'][0]['to_quantity'] == 9
    turn('tak')
    db = backend.conn()
    try:
        assert db.execute('SELECT qty FROM stock WHERE product_id=901').fetchone()[0] == 9
        assert db.execute("SELECT COUNT(*) FROM internal_audit_log WHERE operation='inventory.adjust' AND result='SUCCESS'").fetchone()[0] == 1
    finally:
        db.close()
    no_old_decision = turn('tak')
    assert 'Nie ma korekty' in no_old_decision['message']


def test_new_unknown_product_clears_old_product_scope_and_cannot_reuse_quantity(counting):
    _,turn = counting
    turn('Sam BB 96')
    unknown = turn('Nieistniejacy BB 128')
    assert unknown['inventory_fast_state'] == 'WAIT_PRODUCT'
    result = turn('7 sztuk')
    assert 'Podaj produkt' in result['message']
    assert observations() == []


def test_new_product_count_keeps_previous_discrepancy_without_stale_yes(counting):
    _, turn = counting
    previous = turn('Sam BB 96 mam 5')['approvals'][0]
    next_count = turn('Carl BB 320 mam 5')
    assert next_count['inventory_fast_state'] == 'WAIT_PRODUCT'
    assert [(row['product_id'], row['counted_quantity'], row['status'])
            for row in observations()] == [(901, 5, 'PENDING_ADJUSTMENT'), (902, 5, 'MATCHED')]
    # Moving to a new product defers the old discrepancy, without deciding it.
    assert internal_approval.get_request_snapshot(previous['approval_id'])['status'] == 'PENDING'
    turn('tak')
    assert internal_approval.get_request_snapshot(previous['approval_id'])['status'] == 'PENDING'
    db = backend.conn()
    try:
        assert [tuple(row) for row in db.execute(
            'SELECT product_id,qty FROM stock WHERE product_id IN (901,902) ORDER BY product_id')
        ] == [(901, 7), (902, 5)]
        assert db.execute("SELECT COUNT(*) FROM internal_audit_log WHERE operation='inventory.adjust' AND result='SUCCESS'").fetchone()[0] == 0
    finally:
        db.close()


def test_second_product_approval_does_not_apply_or_cancel_deferred_first_one(counting):
    _, turn = counting
    previous = turn('Sam BB 96 mam 5')['approvals'][0]
    identified = turn('Carl BB 320')
    assert identified['inventory_fast_state'] == 'WAIT_COUNT'
    # Even before the next quantity, an unqualified yes cannot approve Sam.
    turn('tak')
    assert internal_approval.get_request_snapshot(previous['approval_id'])['status'] == 'PENDING'
    next_count = turn('3 sztuki')
    assert next_count['inventory_fast_state'] == 'WAIT_APPROVAL'
    current = next(item for item in next_count['approvals']
        if json.loads(internal_approval.get_request_snapshot(item['approval_id'])['safe_payload'])['product_id'] == 902)
    assert current['approval_id'] != previous['approval_id']
    turn('tak')
    turn('tak')
    assert internal_approval.get_request_snapshot(previous['approval_id'])['status'] == 'PENDING'
    db = backend.conn()
    try:
        assert [tuple(row) for row in db.execute(
            'SELECT product_id,qty FROM stock WHERE product_id IN (901,902) ORDER BY product_id')
        ] == [(901, 7), (902, 3)]
        assert db.execute("SELECT COUNT(*) FROM internal_audit_log WHERE operation='inventory.adjust' AND result='SUCCESS'").fetchone()[0] == 1
        assert db.execute('SELECT status FROM internal_inventory_count_items WHERE product_id=901').fetchone()[0] == 'PENDING_ADJUSTMENT'
    finally:
        db.close()


@pytest.mark.parametrize('next_product', ['Nieistniejacy BB 128', 'Dover BLB 224'],
                         ids=['unknown', 'ambiguous-range'])
def test_new_unresolved_product_never_reactivates_previous_approval(counting, next_product):
    _, turn = counting
    previous = turn('Sam BB 96 mam 5')['approvals'][0]
    result = turn(next_product)
    assert result['inventory_fast_state'] == 'WAIT_PRODUCT'
    assert len(observations()) == 1
    turn('tak')
    assert internal_approval.get_request_snapshot(previous['approval_id'])['status'] == 'PENDING'
    if next_product == 'Dover BLB 224':
        assert '192-224' in result['message'] and '224-256' in result['message']
        selected = turn('tę drugą')
        assert selected['inventory_fast_state'] == 'WAIT_COUNT'
        assert '224-256' in selected['message']
        turn('5 sztuk')
        assert observations()[-1]['product_id'] == 905
    else:
        turn('7 sztuk')
        assert len(observations()) == 1
    db = backend.conn()
    try:
        assert db.execute('SELECT qty FROM stock WHERE product_id=901').fetchone()[0] == 7
        assert db.execute("SELECT COUNT(*) FROM internal_audit_log WHERE operation='inventory.adjust' AND result='SUCCESS'").fetchone()[0] == 0
    finally:
        db.close()


def test_risky_recognition_only_offers_confirmable_active_variant(counting):
    _,turn = counting
    turn('Dover BLK 160')
    suggestion = turn('Tower BLK 160')
    assert 'Wybierz pełny wariant' in suggestion['message']
    assert observations() == []
    turn('ten pierwszy')
    turn('9')
    assert observations()[-1]['product_id'] == 906


def test_saved_variant_choice_cannot_cross_conversations(counting):
    _,turn = counting
    turn('Dover BLB 224 5')
    opened = runtime.run_agent_turn(owner(),'Robimy remanent',runtime.FakeModelProvider([
        tool('inventory.count.session.start',{}),respond('Nowe liczenie.')]))
    provider = runtime.FakeModelProvider([])
    selection = runtime.run_agent_turn(owner(),'tę drugą',provider,
        conversation_id=opened['conversation_id'],voice_fast_mode=True)
    assert selection['status'] == 'SUCCESS'
    assert 'Nie ma takiego wariantu' in selection['message']
    assert provider.calls == []
    assert observations() == []


def test_changed_catalog_identity_invalidates_saved_variant_choice(counting):
    _,turn = counting
    turn('Dover BLB 224 5')
    db = backend.conn()
    db.execute("UPDATE products SET sku='CHANGED',name='Zmieniony produkt 320' WHERE id=905")
    db.commit(); db.close()
    result = turn('tę drugą')
    assert result.get('inventory_fast_failure') == 'product_not_found'
    assert observations() == []


def test_pause_resume_preserves_step_and_explicit_regular_completion_closes(counting):
    cid,turn = counting
    turn('Sam BB 96')
    paused = turn('kończymy na dziś')
    assert 'Liczenie wstrzymane' in paused['message']
    assert 'Czas aktywnego liczenia' in paused['message']
    assert 'wstrzymane' in turn('7')['message']
    assert observations() == []
    turn('wznów liczenie')
    turn('7')
    elapsed = turn('jak długo liczymy?')
    assert 'Czas aktywnego liczenia' in elapsed['message']
    assert 'Czas kalendarzowy' in elapsed['message']
    ended = turn('zakończ remanent')
    assert 'Remanent zakończony' in ended['message']
    db = backend.conn()
    try:
        session = db.execute('SELECT * FROM internal_inventory_count_sessions WHERE conversation_id=?',(cid,)).fetchone()
        assert session['status'] == 'COMPLETED'
        assert [row[0] for row in db.execute('SELECT event FROM internal_count_activity WHERE session_id=? ORDER BY id',
                                           (session['session_id'],))] == ['start','pause','resume','complete']
    finally:
        db.close()


def test_annual_voice_end_pauses_instead_of_finalizing_snapshot(counting):
    cid,turn = counting
    db = backend.conn()
    db.execute("UPDATE internal_inventory_count_sessions SET inventory_year=2026,phase='IN_PROGRESS' WHERE conversation_id=?",(cid,))
    db.commit(); db.close()
    result = turn('zakończ remanent')
    assert 'Roczny spis pozostaje otwarty' in result['message']
    db = backend.conn()
    try:
        session = db.execute('SELECT * FROM internal_inventory_count_sessions WHERE conversation_id=?',(cid,)).fetchone()
        assert session['status'] == 'OPEN' and session['phase'] == 'IN_PROGRESS'
        assert db.execute('SELECT event FROM internal_count_activity WHERE session_id=? ORDER BY id DESC LIMIT 1',
                          (session['session_id'],)).fetchone()[0] == 'pause'
    finally:
        db.close()


@pytest.mark.parametrize('approved',[False,True])
def test_cancel_adjustments_is_atomic_audited_and_scoped(counting,monkeypatch,approved):
    import inventory_recount
    import internal_audit
    cid,turn = counting
    turn('Sam BB 96')
    proposal = turn('5')
    aid = proposal['approvals'][0]['approval_id']
    if approved:
        internal_approval.approve_request(aid,owner())
    before = internal_approval.get_request_snapshot(aid)
    session_id = json.loads(before['safe_payload'])['count_session_id']
    db = backend.conn()
    foreign = dict(before)
    foreign['approval_id'] = str(uuid.uuid4())
    foreign['safe_payload'] = json.dumps(dict(json.loads(before['safe_payload']),count_session_id=str(uuid.uuid4())))
    db.execute('INSERT INTO internal_approval_requests ('+','.join(foreign)+') VALUES ('+','.join('?' for _ in foreign)+')',
               tuple(foreign.values()))
    db.commit()
    real_audit = internal_audit.record_audit_event
    def fail_audit(*args,**kwargs):
        raise RuntimeError('Injected audit failure')
    monkeypatch.setattr(internal_audit,'record_audit_event',fail_audit)
    db.execute('BEGIN IMMEDIATE')
    with pytest.raises(RuntimeError,match='Injected audit failure'):
        inventory_recount.cancel_adjustments(db,session_id,901)
    db.rollback()
    assert db.execute('SELECT status FROM internal_approval_requests WHERE approval_id=?',(aid,)).fetchone()[0] == before['status']
    assert db.execute('SELECT status FROM internal_operation_executions WHERE approval_id=?',(aid,)).fetchone()[0] == 'PENDING_APPROVAL'
    monkeypatch.setattr(internal_audit,'record_audit_event',real_audit)
    db.execute('BEGIN IMMEDIATE')
    assert inventory_recount.cancel_adjustments(db,session_id,901) == [aid]
    db.commit()
    assert db.execute('SELECT status FROM internal_approval_requests WHERE approval_id=?',(aid,)).fetchone()[0] == 'CANCELLED'
    assert db.execute('SELECT status FROM internal_operation_executions WHERE approval_id=?',(aid,)).fetchone()[0] == 'CONFLICT'
    assert db.execute('SELECT status FROM internal_approval_requests WHERE approval_id=?',(foreign['approval_id'],)).fetchone()[0] == before['status']
    assert db.execute("SELECT COUNT(*) FROM internal_audit_log WHERE approval_id=? AND operation IN ('approval.cancelled','business_operation.conflict')",(aid,)).fetchone()[0] == 2
    assert db.execute('SELECT qty FROM stock WHERE product_id=901').fetchone()[0] == 7
    db.close()
