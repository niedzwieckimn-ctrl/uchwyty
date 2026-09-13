import json
import uuid

import pytest

import agent_conversation
import agent_runtime as runtime
import app as b
import business_operations as ops
import internal_approval as approvals
import internal_rbac as rbac
from inventory_analytics import build_replenishment_analysis, recommended_replenishments
from test_agent_runtime import isolated, owner, respond, tool
from test_fulfillment_orchestrator import flow, actor, state, run, success


def test_history_serialization_failure_releases_turn(isolated, monkeypatch):
    original = agent_conversation.finish_turn
    monkeypatch.setattr(agent_conversation, 'finish_turn', lambda *_a, **_k: (_ for _ in ()).throw(TypeError('serialization')))
    first = runtime.run_agent_turn(owner(), 'pierwsza', runtime.FakeModelProvider([respond('odpowiedź')]))
    assert first['status'] == 'FAILED' and first['error_code'] == 'HISTORY_SAVE_FAILED'
    monkeypatch.setattr(agent_conversation, 'finish_turn', original)
    second = runtime.run_agent_turn(owner(), 'druga', runtime.FakeModelProvider([respond('działa')]),
                                    conversation_id=first['conversation_id'])
    assert second['status'] == 'SUCCESS'


def test_render_failure_releases_turn_and_returns_controlled_error(isolated, monkeypatch):
    original = runtime._plain_response_text
    monkeypatch.setattr(runtime, '_plain_response_text', lambda *_a, **_k: (_ for _ in ()).throw(TypeError('render')))
    first = runtime.run_agent_turn(owner(), 'pierwsza', runtime.FakeModelProvider([respond('odpowiedź')]))
    assert first['status'] == 'FAILED' and first['error_code'] == 'TURN_FINALIZATION_FAILED'
    monkeypatch.setattr(runtime, '_plain_response_text', original)
    second = runtime.run_agent_turn(owner(), 'druga', runtime.FakeModelProvider([respond('działa')]),
                                    conversation_id=first['conversation_id'])
    assert second['status'] == 'SUCCESS'


@pytest.mark.parametrize('failure', [TimeoutError('timeout'), RuntimeError('provider')])
def test_provider_failure_releases_turn(isolated, failure):
    first = runtime.run_agent_turn(owner(), 'pierwsza', runtime.FakeModelProvider([failure]))
    second = runtime.run_agent_turn(owner(), 'druga', runtime.FakeModelProvider([respond('działa')]),
                                    conversation_id=first['conversation_id'])
    assert first['status'] == 'FAILED' and second['status'] == 'SUCCESS'


def test_pending_exists_before_question_and_one_confirmation_executes(isolated):
    db = b.conn(); version = ops._order_version(db, 10); db.close()
    first = runtime.run_agent_turn(owner(), 'oznacz zamówienie jako packed', runtime.FakeModelProvider([
        tool('orders.status.transition', {'order_id': 10, 'target_status': 'packed',
             'expected_version': version, 'idempotency_key': 'approval-before-question'}),
        respond('Czy zatwierdzasz zmianę statusu?'),
    ]))
    assert len(first['pending_approvals']) == 1
    approval_id = first['pending_approvals'][0]['approval_id']
    second = runtime.run_agent_turn(owner(), 'zatwierdzam', runtime.FakeModelProvider([
        tool('approval.decide', {'approval_id': approval_id, 'decision': 'approve'}),
        respond('Status został zmieniony.'),
    ]), conversation_id=first['conversation_id'])
    db = b.conn()
    assert db.execute('SELECT status FROM orders WHERE id=10').fetchone()[0] == 'packed'
    assert approvals.get_request_snapshot(approval_id)['status'] == 'CONSUMED'
    db.close()
    assert second['status'] == 'SUCCESS' and len(second['decisions']) == 1


def test_latest_resolved_order_wins_and_artifact_is_scoped(isolated):
    db = b.conn(); now = b.now_iso()
    db.execute("INSERT INTO customers(id,name,created_at) VALUES(20,'Niedźwieccy',?)", (now,))
    db.execute("INSERT INTO orders(id,order_no,customer_id,customer_name,status,created_at,currency,price_list) VALUES(20,'ZAM-B',20,'Niedźwieccy','confirmed',?,'PLN','pln')", (now,))
    version = ops._order_version(db, 20); db.commit(); db.close()
    provider = runtime.FakeModelProvider([
        tool('orders.get', {'id': 10}, 'a'),
        tool('orders.get', {'id': 20}, 'b'),
        tool('orders.internal_note.add', {'order_id': 20, 'note': 'nowa pozycja',
             'expected_version': version, 'idempotency_key': 'scope-b'}, 'write-b'),
        respond('Przygotowałem zmianę dla Niedźwieckich.'),
    ])
    result = runtime.run_agent_turn(owner(), 'Niedźwieccy chcą zmianę', provider)
    assert result['status'] == 'SUCCESS'
    db = b.conn(); assert db.execute("SELECT COUNT(*) FROM internal_order_notes WHERE order_id=20 AND note='nowa pozycja'").fetchone()[0] == 1; db.close()
    assert all(str(item.get('id') or item.get('order_id')) != '10' for item in result['artifacts']
               if item.get('type') in {'order_card', 'document_link'})
    assert any(item.get('type') == 'order_card' and item.get('id') == 20 for item in result['artifacts'])


def test_historical_order_cannot_be_inherited_for_write_without_fresh_resolution(isolated):
    first = runtime.run_agent_turn(owner(), 'pokaż A', runtime.FakeModelProvider([
        tool('orders.get', {'id': 10}), respond('Pokazuję A.'),
    ]))
    db = b.conn(); version = ops._order_version(db, 10); db.close()
    second = runtime.run_agent_turn(owner(), 'teraz chodzi o innego klienta', runtime.FakeModelProvider([
        tool('orders.internal_note.add', {'order_id': 10, 'note': 'wrong',
             'expected_version': version, 'idempotency_key': 'stale-context'}),
    ]), conversation_id=first['conversation_id'])
    assert second['status'] == 'DENIED' and second['error_code'] == 'ENTITY_SCOPE_REQUIRED'
    db = b.conn(); assert db.execute("SELECT COUNT(*) FROM internal_order_notes WHERE note='wrong'").fetchone()[0] == 0; db.close()


def test_direct_followup_reuses_previous_fresh_order_and_runs_write_preflight(isolated):
    first = runtime.run_agent_turn(owner(), 'pokaż A', runtime.FakeModelProvider([
        tool('orders.get', {'id': 10}), respond('Zamówienie A jest kompletne.'),
    ]))
    db = b.conn(); version = ops._order_version(db, 10); db.close()

    second = runtime.run_agent_turn(owner(), 'realizujemy zamówienie', runtime.FakeModelProvider([
        tool('orders.internal_note.add', {'order_id': 10, 'note': 'realizacja rozpoczęta',
             'expected_version': version, 'idempotency_key': 'direct-followup-a'}),
        respond('Rozpoczęto realizację.'),
    ]), conversation_id=first['conversation_id'])

    assert second['status'] == 'SUCCESS'
    db = b.conn()
    assert db.execute("SELECT COUNT(*) FROM internal_order_notes WHERE order_id=10 AND note='realizacja rozpoczęta'").fetchone()[0] == 1
    db.close()


def test_explicit_new_order_wins_and_prevents_write_to_previous_order(isolated):
    db = b.conn(); now = b.now_iso()
    db.execute("INSERT INTO orders(id,order_no,customer_name,status,created_at,currency,price_list) VALUES(20,'ZAM-B','Klient B','confirmed',?,'PLN','pln')", (now,))
    version_a = ops._order_version(db, 10)
    db.commit(); db.close()
    first = runtime.run_agent_turn(owner(), 'pokaż A', runtime.FakeModelProvider([
        tool('orders.get', {'id': 10}), respond('Pokazuję A.'),
    ]))

    second = runtime.run_agent_turn(owner(), 'realizujemy ZAM-B', runtime.FakeModelProvider([
        tool('orders.get', {'id': 20}, 'read-b'),
        tool('orders.internal_note.add', {'order_id': 10, 'note': 'wrong-order',
             'expected_version': version_a, 'idempotency_key': 'must-not-write-a'}, 'write-a'),
    ]), conversation_id=first['conversation_id'])

    assert second['status'] == 'DENIED' and second['error_code'] == 'ENTITY_SCOPE_CONFLICT'
    db = b.conn()
    assert db.execute("SELECT COUNT(*) FROM internal_order_notes WHERE order_id=10 AND note='wrong-order'").fetchone()[0] == 0
    db.close()


def test_conflicting_or_ambiguous_current_scope_blocks_write(isolated):
    db = b.conn(); version = ops._order_version(db, 10)
    db.execute("INSERT INTO customers(id,name,created_at) VALUES(11,'Other Interiors',?)", (b.now_iso(),))
    db.commit(); db.close()
    conflict = runtime.run_agent_turn(owner(), 'zmień drugie zamówienie', runtime.FakeModelProvider([
        tool('orders.get', {'id': 10}, 'read'),
        tool('orders.internal_note.add', {'order_id': 999, 'note': 'x', 'expected_version': version,
             'idempotency_key': 'wrong'}, 'write'),
    ]))
    assert conflict['error_code'] == 'ENTITY_SCOPE_CONFLICT'
    ambiguous = runtime.run_agent_turn(owner(), 'znajdź klienta i zmień zamówienie', runtime.FakeModelProvider([
        tool('customers.search', {'query': 'Interiors'}, 'search'),
        tool('orders.internal_note.add', {'order_id': 10, 'note': 'x', 'expected_version': version,
             'idempotency_key': 'ambiguous'}, 'write'),
    ]))
    assert ambiguous['status'] == 'DENIED' and ambiguous['error_code'] == 'ENTITY_SCOPE_AMBIGUOUS'


def test_customer_search_ignores_diacritics(isolated):
    db = b.conn(); db.execute("UPDATE customers SET name='Niedźwieccy' WHERE id=10"); db.commit(); db.close()
    ai = rbac.load_actor_context(rbac.AI_OWNER_ASSISTANT_ACTOR_ID)
    found = ops.execute_business_operation(ai, 'customers.search', {'query': 'Niedzwieccy'})
    assert found.status == 'SUCCESS' and found.data['results'][0]['name'] == 'Niedźwieccy'


def test_replenishment_read_is_exact_existing_ranking(isolated):
    ai = rbac.load_actor_context(rbac.AI_OWNER_ASSISTANT_ACTOR_ID)
    direct = recommended_replenishments(build_replenishment_analysis(b.conn, today=ops._business_now().date()), limit=10)
    result = ops.execute_business_operation(ai, 'inventory.replenishment.ranking', {'include_all': True, 'limit': 10})
    assert result.status == 'SUCCESS' and result.data['results'] == direct, result


def test_orphan_invoice_intent_resumes_without_duplicate(flow):
    success('orders.packing_list.generate')
    snapshot = state()
    db = b.conn()
    db.execute("INSERT INTO fulfillment_document_intents VALUES(?,'invoice',?)", (702, snapshot['content_hash']))
    db.commit(); db.close()
    success('orders.invoice.create')
    db = b.conn(); assert db.execute('SELECT COUNT(*) FROM invoices WHERE order_id=702').fetchone()[0] == 1; db.close()


def test_existing_untracked_invoice_never_creates_approval_loop(flow):
    success('orders.packing_list.generate'); success('orders.invoice.create')
    db = b.conn(); db.execute("DELETE FROM fulfillment_documents WHERE order_id=702 AND kind='invoice'"); db.commit(); db.close()
    first = run('orders.invoice.create', approve=False)
    second = run('orders.invoice.create', approve=False)
    assert first.status == second.status == 'FAILED'
    assert first.error_code == second.error_code == 'EXISTING_INVOICE_RECONCILE_REQUIRED'
    assert not first.approval_id and not second.approval_id
    db = b.conn(); assert db.execute('SELECT COUNT(*) FROM invoices WHERE order_id=702').fetchone()[0] == 1; db.close()


def test_add_item_price_snapshot_changes_fresh_total(flow):
    db = b.conn()
    db.execute("INSERT INTO products(id,sku,model,name,created_at) VALUES(704,'CERNE','Cerne','Cerne',?)", (b.now_iso(),))
    db.execute("INSERT INTO stock(product_id,qty) VALUES(704,20)")
    db.execute("INSERT INTO pricing(model,net_price,gross_price,created_at) VALUES('Cerne',15,18.45,?)", (b.now_iso(),))
    db.commit(); db.close()
    ai = actor()
    before = ops.execute_business_operation(ai, 'orders.get', {'id': 702}).data['record']['totals']['PLN']['gross']
    success('orders.items.add', product_id=704, quantity=1)
    after = ops.execute_business_operation(ai, 'orders.get', {'id': 702}).data['record']['totals']['PLN']['gross']
    assert after == pytest.approx(before + 18.45)
