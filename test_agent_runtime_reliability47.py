import json

import pytest

import app as backend
import agent_runtime as runtime
import agent_conversation as conversation
import agent_shipping_draft as draft
import business_operations as operations
import shipment_read
from test_agent_runtime import isolated, owner, tool, respond
from test_multi_order_packing_agent import multi_order_flow, ROOT_ORDER_ID, _generate_args


@pytest.mark.parametrize('question', [
    'Co mogę dziś wysłać?', 'Jakie dokładnie modele mogę dzisiaj wysłać?',
    'Jakie modele mogę dzisiaj wysłać?', 'Sprawdź, co mogę dziś wysłać.',
    'Jakie zamówienie mogę dziś wysłać?', 'Czy mogę dziś coś wysłać?',
    'Jakie zamówienia są dziś do wysyłki?', 'Które paczki są gotowe do nadania?',
])
def test_prospective_shipping_uses_readiness_catalog_not_completed_shipments(question):
    provider = runtime.FakeModelProvider([respond('Sprawdzę gotowość wskazanych zamówień.')])
    result = runtime.run_agent_turn(owner(), question, provider)
    assert result['status'] == 'SUCCESS'
    assert len(provider.calls) == 1
    assert not shipment_read.is_question(question)
    assert runtime._detect_read_intent(question) == 'order_readiness'
    assert {'business.query', 'orders.fulfillment.readiness', 'business.orders.state'} & {
        item['name'] for item in provider.calls[0]['tools']}


def test_historical_shipping_keeps_direct_read():
    provider = runtime.FakeModelProvider([])
    result = runtime.run_agent_turn(owner(), 'Co wysłałem dzisiaj?', provider)
    assert result['status'] == 'SUCCESS'
    assert provider.calls == []
    db = backend.conn()
    assert db.execute('SELECT operation FROM internal_operation_executions WHERE correlation_id=?',
                      (result['correlation_id'],)).fetchone()[0] == 'shipment.read'
    db.close()


@pytest.mark.parametrize('question', [
    'Ile mamy zaległych faktur?', 'Ile mamy zamówień?',
    'Sprawdź stan zamówienia ZAM-TEST-10', 'Ile mamy sztuk na magazynie?',
])
def test_non_product_question_reaches_model_instead_of_catalog_search(question):
    provider = runtime.FakeModelProvider([respond('Odczyt wymaga właściwego zakresu biznesowego.')])
    result = runtime.run_agent_turn(owner(), question, provider)
    assert result['status'] == 'SUCCESS' and len(provider.calls) == 1
    assert result['tool_calls'] == 0


@pytest.mark.parametrize('question,operation', [
    ('Oznacz zamówienie ZAM-TEST-10 jako wysłane', 'orders.status.transition'),
    ('Oznacz fakturę FVAT 8/09/2026 opłaconą gotówką', 'invoices.payment.set_status'),
])
def test_write_command_retains_write_and_preflight_tools(question, operation):
    provider = runtime.FakeModelProvider([respond('Najpierw sprawdzę bieżący stan.')])
    result = runtime.run_agent_turn(owner(), question, provider)
    assert result['status'] == 'SUCCESS'
    names = {item['name'] for item in provider.calls[0]['tools']}
    assert operation in names
    assert ('invoices.get' if operation.startswith('invoices.') else 'orders.get') in names


def test_count_controls_use_backend_bound_conversation_session():
    import internal_rbac as rbac
    ai = rbac.load_actor_context(rbac.AI_OWNER_ASSISTANT_ACTOR_ID,delegated_by_actor_id=owner().actor_id)
    catalog = {item['name']:item for item in runtime._tool_descriptors(ai,owner())}
    for name in ('inventory.count.pause','inventory.count.resume','inventory.count.keep_result','inventory.count.history'):
        assert name in catalog and name in runtime.COUNT_SESSION_BOUND
        assert 'count_session_id' not in catalog[name]['parameters']['properties']
        assert 'conversation_id' not in catalog[name]['parameters']['properties']
    assert runtime._is_business_write_request('Wznów liczenie magazynu')


def _run(message, provider, cid=''):
    with backend.app.test_request_context():
        return runtime.run_agent_turn(owner(), message, provider, conversation_id=cid)


def _generate(kwargs):
    assert kwargs['tool_choice'] == 'auto'
    assert 'orders.packing_list.generate' in {item['name'] for item in kwargs['tools']}
    output = json.loads(kwargs['input_items'][-1]['output'])
    arguments = _generate_args(output)
    arguments['packing_scope'] = output['preview']['packing_scope']
    arguments['packing_order_ids'] = output['preview']['packing_order_ids']
    return tool('orders.packing_list.generate', arguments, 'generate')


@pytest.mark.parametrize('mode', ['parallel', 'high_level_sequential'])
def test_write_preflight_continues_to_real_pending_approval(multi_order_flow, monkeypatch, mode):
    monkeypatch.setenv('AGENT_HIGH_LEVEL_READ_MODELS_ENABLED', '1' if mode == 'high_level_sequential' else '0')
    if mode == 'parallel':
        prelude = [runtime.ProviderResponse(tool_calls=(
            runtime.ToolCall('order', 'orders.get', json.dumps({'id':ROOT_ORDER_ID})),
            runtime.ToolCall('preview', 'orders.packing_list.preview', json.dumps({'order_id':ROOT_ORDER_ID})),
        ))]
    else:
        prelude = [tool('orders.fulfillment.state', {'order_id':ROOT_ORDER_ID}, 'state'),
                   tool('orders.packing_list.preview', {'order_id':ROOT_ORDER_ID}, 'preview')]
    provider = runtime.FakeModelProvider([*prelude, _generate, respond('Operacja oczekuje na zatwierdzenie.')])
    result = _run('Przygotuj realizację.', provider)
    assert result['status'] == 'SUCCESS', result
    assert len(result['pending_approvals']) == 1
    assert not multi_order_flow['pdf_calls']


def test_fresh_packing_preview_restores_scope_after_text_only_turn(multi_order_flow):
    first = _run('Pokaż zamówienie', runtime.FakeModelProvider([
        tool('orders.get', {'id':ROOT_ORDER_ID}), respond('Oto wskazane zamówienie.')]))
    cid = first['conversation_id']
    _run('Co cię blokuje?', runtime.FakeModelProvider([respond('Możemy kontynuować.')]), cid)
    result = _run('Kontynuujemy realizację.', runtime.FakeModelProvider([
        tool('orders.packing_list.preview', {'order_id':ROOT_ORDER_ID}), _generate,
        respond('Operacja oczekuje na zatwierdzenie.')]), cid)
    assert result['status'] == 'SUCCESS', result
    assert len(result['pending_approvals']) == 1


def test_prepared_approval_survives_final_provider_failure(multi_order_flow):
    result = _run('Przygotuj realizację.', runtime.FakeModelProvider([
        tool('orders.fulfillment.state', {'order_id':ROOT_ORDER_ID}, 'state'),
        tool('orders.packing_list.preview', {'order_id':ROOT_ORDER_ID}, 'preview'),
        _generate, TimeoutError('simulated final model timeout')]))
    assert result['status'] == 'SUCCESS', result
    assert result['confirmation_source'] == 'stored_pending_approval'
    assert result['synthesis_error_code'] == 'MODEL_FAILED'
    assert 'oczekuje na zatwierdzenie' in result['message']
    assert len(result['pending_approvals']) == 1
    assert result['executed_operations'] == []
    assert not multi_order_flow['pdf_calls']


def test_committed_note_survives_final_provider_failure_with_execution_receipt():
    db = backend.conn()
    version = operations._order_version(db, 10)
    db.close()
    result = runtime.run_agent_turn(owner(), 'Dodaj notatkę do zamówienia', runtime.FakeModelProvider([
        tool('orders.internal_note.add', {'order_id':10, 'note':'receipt-test',
             'expected_version':version, 'idempotency_key':'receipt-note-47'}),
        TimeoutError('simulated final model timeout')]))
    assert result['status'] == 'SUCCESS' and result['confirmation_source'] == 'stored_execution'
    assert 'zapisana' in result['message']
    assert result['synthesis_error_code'] == 'MODEL_FAILED'
    receipt = result['executed_operations'][0]
    assert receipt['idempotency_key'] == 'receipt-note-47'
    db = backend.conn()
    assert db.execute("SELECT COUNT(*) FROM internal_order_notes WHERE note='receipt-test'").fetchone()[0] == 1
    assert db.execute('SELECT status FROM internal_operation_executions WHERE execution_id=?',
                      (receipt['execution_id'],)).fetchone()[0] == 'SUCCESS'
    db.close()


def _parcel_state(order_id=10, logical='parcel-one', batch=77):
    return {'order_id':order_id, 'order_number':'ZAM-TEST-'+str(order_id), 'expected_version':3,
            'package':{'root_order_id':order_id, 'order_ids':[order_id, order_id+1, order_id+2, order_id+3],
                       'packing_list_id':logical, 'batch_id':batch, 'fingerprint':'a'*64, 'packed_quantity':47},
            'requirements':{'known':{}, 'missing_fields':['weight']}, 'next_step':'shipping_requirements'}


def test_parcel_draft_collects_dimensions_then_weight_without_changing_scope():
    value = draft.observe(None, _parcel_state(),
        'Zamawiam kuriera InPost, wymiary paczki 45, 20 na 20. Powiadomienie SMS i powiadomienie e-mail też zamawiaj.', 'first')
    changed = draft.merge_parameters(value, 'Pięć kilogramów')
    assert changed['identity'] == value['identity']
    assert changed['parameters'] == {'carrier':'inpost','length':45.0,'width':20.0,'height':20.0,
        'dimension_unit':'cm','sms':True,'email':True,'weight':5.0,'weight_unit':'kg','weight_source':'manual'}
    assert draft.observe(changed, _parcel_state(200, 'unrelated', 90), 'Podaj szczegóły', 'other') == changed
    replacement = draft.observe(changed, _parcel_state(200, 'unrelated', 90), 'Teraz paczka ZAM-TEST-200', 'other')
    assert replacement['parameters'] == {} and replacement['identity']['root_order_id'] == 200


def test_parcel_draft_does_not_interpret_unrelated_weight_question_or_negative_command_as_shipping():
    value = draft.observe(None, _parcel_state(), 'Zamawiam kuriera InPost', 'first')
    assert not draft.is_followup('Ile kosztuje pięć kilogramów aluminium?', value)
    assert not draft.requests_shipping('Nie zamawiaj kuriera.')
    paused = draft.merge_parameters(value, 'Nie zamawiaj kuriera.')
    assert paused['shipping_requested'] is False
    assert draft.parse_parameters('Waga wynosi pięć kilogramów.', active=True)['weight'] == 5


@pytest.mark.parametrize('message', ['Nie chcę zamawiać kuriera.', 'Nie realizuj jeszcze paczki.'])
def test_parcel_draft_respects_explicit_refusal(message):
    value = draft.observe(None, _parcel_state(), 'Zamawiam kuriera InPost', 'first')
    assert draft.cancels_shipping(message) and not draft.requests_shipping(message)
    assert draft.merge_parameters(value, message)['shipping_requested'] is False


@pytest.mark.parametrize('message', ['Zamawiam kuriera do MAG-999. Waga 5 kg',
    'Do innej paczki: 5 kg', 'Do listy pakowej LP-999: waga 5 kg',
    'Zatwierdzam zamówienie kuriera.', 'Odrzuć zamówienie kuriera.'])
def test_new_parcel_selectors_and_approval_decisions_bypass_draft_fastpath(message):
    value = draft.observe(None, {**_parcel_state(), 'order_number':'MAG-702'}, 'Zamawiam kuriera InPost', 'first')
    assert not draft.is_followup(message, value)


def test_fresh_but_wrong_order_read_cannot_bind_an_explicit_other_order_request():
    state = {**_parcel_state(), 'order_number':'MAG-702'}
    assert draft.observe(None,state,'Zamawiam kuriera do MAG-999. Waga 5 kg','test') is None


@pytest.mark.parametrize('message', ['Wymiary paczki 45 x 20 x 20 mm',
    'Waga paczki nie 5 kg, tylko 6 kg', 'Czy waga paczki to 6 kg?', 'Zamów kuriera DHL do tej paczki'])
def test_parameter_questions_unsupported_units_and_corrections_require_review(message):
    value = draft.observe(None, _parcel_state(), 'Zamawiam kuriera InPost', 'first')
    assert draft.parse_parameters(message, active=True) == {}
    assert not draft.is_followup(message, value)


@pytest.mark.parametrize('message', ['Bez SMS i e-mail', 'Wyłącz SMS i e-mail'])
def test_coordinated_notification_negation_disables_both(message):
    assert draft.parse_parameters(message, active=True) == {'sms':False,'email':False}


def test_parcel_draft_is_persisted_across_turns_and_reset_with_conversation():
    human = owner()
    import internal_rbac as rbac
    ai = rbac.load_actor_context(rbac.AI_OWNER_ASSISTANT_ACTOR_ID, delegated_by_actor_id=human.actor_id)
    cid, _, _ = conversation.open_conversation(human, ai)
    conversation.begin_turn(human, ai, cid, 'draft-turn-one', 'Wymiary paczki')
    conversation.save_turn_state(human, ai, cid, 'draft-turn-one', {'shipping_draft':{'parameters':{'length':45}}})
    conversation.finish_turn(human, ai, cid, 'draft-turn-one', 'Podaj masę.', [])
    conversation.open_conversation(human, ai, cid)
    acquired_state = conversation.begin_turn(human, ai, cid, 'draft-turn-two', '5 kg')
    assert acquired_state == {'shipping_draft':{'parameters':{'length':45}}}
    assert conversation.turn_state(human, ai, cid, 'draft-turn-two')['shipping_draft']['parameters']['length'] == 45
    conversation.finish_turn(human, ai, cid, 'draft-turn-two', 'Gotowe.', [])
    conversation.reset_conversation(human, ai, cid)
    conversation.begin_turn(human, ai, cid, 'draft-turn-three', 'Nowa paczka')
    assert conversation.turn_state(human, ai, cid, 'draft-turn-three') == {}


def test_model_prepared_count_adjustment_becomes_current_voice_decision():
    import internal_approval
    import internal_rbac as rbac

    human = owner()
    ai = rbac.load_actor_context(rbac.AI_OWNER_ASSISTANT_ACTOR_ID,
                                 delegated_by_actor_id=human.actor_id)
    opened = runtime.run_agent_turn(human, 'Robimy remanent', runtime.FakeModelProvider([
        tool('inventory.count.session.start', {}), respond('Rozpoczęto liczenie.')]))
    assert opened['status'] == 'SUCCESS', opened
    cid = opened['conversation_id']
    session_id = operations.active_inventory_count_session(ai, human, cid)
    expected = operations.execute_business_operation(ai, 'inventory.count.get_expected', {'product_id':1})
    assert expected.status == 'SUCCESS', expected
    counted = operations.execute_business_operation(ai, 'inventory.count.record', {
        'product_id':1, 'count_session_id':session_id, 'conversation_id':cid,
        'counted_quantity':20, 'expected_version':expected.data['version'],
        'idempotency_key':'model-prepared-current-count',
    })
    assert counted.status == 'SUCCESS', counted
    provider = runtime.FakeModelProvider([
        tool('inventory.count.get_expected', {'product_id':1}, 'fresh-product'),
        tool('inventory.adjust', {'product_id':1, 'expected_version':counted.data['version'],
                                 'idempotency_key':'model-prepared-current-adjustment'}, 'prepare'),
        respond('Korekta czeka na zatwierdzenie.'),
    ])
    prepared = runtime.run_agent_turn(human, 'Przygotuj korektę z zapisanego wyniku liczenia.',
                                      provider, conversation_id=cid)
    assert prepared['status'] == 'SUCCESS', prepared
    assert len(provider.calls) == 3
    aid = prepared['pending_approvals'][0]['approval_id']
    current = operations.inventory_count_voice_state(ai, human, cid)
    assert current['voice_state'] == 'WAIT_APPROVAL' and current['pending_approval_id'] == aid
    assert internal_approval.get_request_snapshot(aid)['status'] == 'PENDING'
    db = backend.conn()
    assert db.execute('SELECT qty FROM stock WHERE product_id=1').fetchone()[0] == 24
    db.close()

    for _ in range(2):
        no_model = runtime.FakeModelProvider([])
        decided = runtime.run_agent_turn(human, 'Tak', no_model, conversation_id=cid,
                                        voice_fast_mode=True)
        assert decided['status'] == 'SUCCESS', decided
        assert no_model.calls == []
    assert internal_approval.get_request_snapshot(aid)['status'] == 'CONSUMED'
    db = backend.conn()
    assert db.execute('SELECT qty FROM stock WHERE product_id=1').fetchone()[0] == 20
    assert db.execute("SELECT COUNT(*) FROM internal_audit_log WHERE operation='inventory.adjust' AND result='SUCCESS'").fetchone()[0] == 1
    db.close()


def test_bundled_voice_context_is_fresh_owned_and_requires_current_read_permission():
    import internal_rbac as rbac
    import inventory_count_lifecycle
    import inventory_voice_context
    from test_internal_rbac import _create_actor

    human = owner()
    ai = rbac.load_actor_context(rbac.AI_OWNER_ASSISTANT_ACTOR_ID,
                                 delegated_by_actor_id=human.actor_id)
    opened = runtime.run_agent_turn(human, 'Robimy remanent', runtime.FakeModelProvider([
        tool('inventory.count.session.start', {}), respond('Rozpoczęto liczenie.')]))
    assert opened['status'] == 'SUCCESS', opened
    cid = opened['conversation_id']
    inventory_voice_context.save(operations, ai, human, cid, {
        'candidates':[{'id':1, 'sku':'CH101-BLK-160'}], 'quantity_options':[24,25],
    })
    before = operations.inventory_count_voice_state(ai, human, cid, include_context=True)
    assert before['clarification']['candidates'] == [{'id':1, 'sku':'CH101-BLK-160'}]
    assert before['clarification']['quantity_options'] == [24,25]
    assert before['timing']['paused'] is False
    ordinary = operations.inventory_count_voice_state(ai, human, cid)
    assert 'clarification' not in ordinary and 'timing' not in ordinary

    db = backend.conn()
    inventory_count_lifecycle.event(db, before['session_id'], human.actor_id, 'pause')
    db.commit()
    db.close()
    assert operations.inventory_count_voice_state(ai, human, cid, include_context=True)['timing']['paused'] is True
    other_cid, _, _ = conversation.open_conversation(human, ai)
    assert operations.inventory_count_voice_state(ai, human, other_cid, include_context=True) is None

    other_human = rbac.load_actor_context(_create_actor('HUMAN', 'OWNER'))
    other_ai = rbac.load_actor_context(rbac.AI_OWNER_ASSISTANT_ACTOR_ID,
                                       delegated_by_actor_id=other_human.actor_id)
    with pytest.raises(operations.ControlledOperationError) as denied:
        operations.inventory_count_voice_state(other_ai, other_human, cid, include_context=True)
    assert denied.value.status == 'DENIED'

    # A previously loaded ActorContext cannot retain access after revocation.
    db = backend.conn()
    db.execute('DELETE FROM internal_actor_roles WHERE actor_id=?', (human.actor_id,))
    db.commit()
    db.close()
    with pytest.raises(operations.ControlledOperationError) as revoked:
        operations.inventory_count_voice_state(ai, human, cid, include_context=True)
    assert revoked.value.error_code == 'PERMISSION_DENIED'


def test_initial_count_snapshot_does_not_deny_non_count_message_without_ai_read_access():
    import internal_rbac as rbac

    human = owner()
    opened = runtime.run_agent_turn(human, 'Robimy remanent', runtime.FakeModelProvider([
        tool('inventory.count.session.start', {}), respond('Rozpoczęto liczenie.')]))
    assert opened['status'] == 'SUCCESS', opened
    cid = opened['conversation_id']
    db = backend.conn()
    db.execute("UPDATE internal_role_permissions SET decision='DENY' WHERE role_key='AI_OWNER_ASSISTANT' AND permission_key='inventory.read'")
    db.commit()
    db.close()
    ai = rbac.load_actor_context(rbac.AI_OWNER_ASSISTANT_ACTOR_ID,
                                 delegated_by_actor_id=human.actor_id)
    snapshot = operations._inventory_count_runtime_snapshot(ai, human, cid)
    assert snapshot['_context_read_denied'] is True
    assert 'clarification' not in snapshot and 'timing' not in snapshot
    greeting = ('Cześć, opowiedz w kilku zdaniach, w jaki sposób formułować krótkie pytania, '
                'aby ułatwić nam rozmowę i porządkować kolejne tematy podczas pracy.')
    provider = runtime.FakeModelProvider([respond('Wskazuj jeden temat i oczekiwany rezultat.')])
    result = runtime.run_agent_turn(human, greeting, provider, conversation_id=cid)
    assert result['status'] == 'SUCCESS' and len(provider.calls) == 1, result
    no_model = runtime.FakeModelProvider([])
    denied = runtime.run_agent_turn(human, '24', no_model, conversation_id=cid, voice_fast_mode=True)
    assert denied['status'] == 'DENIED' and denied['error_code'] == 'PERMISSION_DENIED', denied
    assert no_model.calls == []


@pytest.mark.parametrize('revoked_permission', ['inventory.read', 'inventory.discrepancy_report'])
def test_count_snapshot_cannot_authorize_read_or_write_after_permission_revocation(monkeypatch, revoked_permission):
    human = owner()
    opened = runtime.run_agent_turn(human, 'Robimy remanent', runtime.FakeModelProvider([
        tool('inventory.count.session.start', {}), respond('Rozpoczęto liczenie.')]))
    assert opened['status'] == 'SUCCESS', opened
    cid = opened['conversation_id']
    selected = runtime.run_agent_turn(human, 'Avery 160', runtime.FakeModelProvider([]),
                                      conversation_id=cid, voice_fast_mode=True)
    assert selected['status'] == 'SUCCESS' and selected['inventory_fast_state'] == 'WAIT_COUNT'
    original = operations._inventory_count_runtime_snapshot

    def revoke_after_snapshot(*args):
        snapshot = original(*args)
        assert not snapshot.get('_context_read_denied')
        db = backend.conn()
        db.execute("UPDATE internal_role_permissions SET decision='DENY' WHERE role_key='AI_OWNER_ASSISTANT' AND permission_key=?", (revoked_permission,))
        db.commit()
        db.close()
        return snapshot

    monkeypatch.setattr(operations, '_inventory_count_runtime_snapshot', revoke_after_snapshot)
    no_model = runtime.FakeModelProvider([])
    denied = runtime.run_agent_turn(human, '24', no_model, conversation_id=cid, voice_fast_mode=True)
    assert denied['status'] == 'DENIED', denied
    assert no_model.calls == []
    db = backend.conn()
    assert db.execute('SELECT qty FROM stock WHERE product_id=1').fetchone()[0] == 24
    assert db.execute('SELECT COUNT(*) FROM internal_inventory_count_items').fetchone()[0] == 0
    db.close()
