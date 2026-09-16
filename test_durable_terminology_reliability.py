import json
import logging
import uuid

import pytest

import agent_conversation as conversations
import agent_runtime as runtime
import app as backend
import business_operations as operations
import internal_rbac as rbac
from test_agent_runtime import isolated, owner, respond, tool


def terminology_call(term='Warszawa', meaning='Artystyczna Manufaktura', version=0):
    return tool('agent.terminology.remember', {
        'term':term,
        'meaning':meaning,
        'confirmed_by_user':True,
        'expected_version':version,
    })


def _actors():
    human = owner()
    ai = rbac.load_actor_context(
        rbac.AI_OWNER_ASSISTANT_ACTOR_ID,
        delegated_by_actor_id=human.actor_id,
    )
    return human, ai


def _insert_term(term, meaning, version=1, updated_at='2026-09-15T08:00:00+00:00'):
    db = backend.conn()
    db.execute(
        '''INSERT INTO internal_agent_terminology(
            term,meaning,scope,source,source_run_id,confirmed_by_actor_id,version,updated_at
        ) VALUES(?,?,'company','confirmed_by_user',?,?,?,?)''',
        (term, meaning, str(uuid.uuid4()), owner().actor_id, version, updated_at),
    )
    db.commit()
    db.close()


def test_success_receipt_precedes_confirmation_and_all_warszawa_forms_prefetch():
    first_provider = runtime.FakeModelProvider([
        terminology_call(),
        respond('Zapamiętałem i od teraz będę pamiętał.'),
    ])
    first = runtime.run_agent_turn(
        owner(),
        'Do Warszawy, czyli do Artystycznej Manufaktury, zapisz to.',
        first_provider,
    )

    assert first['status'] == 'SUCCESS'
    assert first['tool_calls'] == 1
    assert first['message'] == 'Zapisane: Warszawa oznacza Artystyczna Manufaktura.'
    db = backend.conn()
    row = db.execute('SELECT term,meaning,version FROM internal_agent_terminology').fetchone()
    executions = db.execute(
        "SELECT COUNT(*) FROM internal_operation_executions "
        "WHERE operation='agent.terminology.remember' AND status='SUCCESS'"
    ).fetchone()[0]
    db.close()
    assert dict(row) == {
        'term':'Warszawa', 'meaning':'Artystyczna Manufaktura', 'version':1,
    }
    assert executions == 1
    _human, ai = _actors()

    for index, form in enumerate(('Warszawa', 'Warszawy', 'Warszawę', 'Warszawie')):
        question = f'Jakie miałem ostatnie zamówienie do {form}?'
        explicit = conversations.search_terminology(
            {'query':form}, ai, f'explicit-{index}')['results']
        assert explicit == [{
            'term':'Warszawa', 'meaning':'Artystyczna Manufaktura',
            'scope':'company', 'source':'confirmed_by_user', 'version':1,
        }]

        def inspect(kwargs, expected_question=question):
            assert kwargs['input_items'][-1] == {'role':'user', 'content':expected_question}
            injected = json.loads(kwargs['input_items'][0]['content'].split(': ', 1)[1])
            assert injected['confirmed_terminology'] == [{
                'term':'Warszawa',
                'meaning':'Artystyczna Manufaktura',
                'scope':'company',
                'source':'confirmed_by_user',
                'version':1,
            }]
            return respond('Sprawdzę właściwą firmę.')

        result = runtime.run_agent_turn(
            owner(), question, runtime.FakeModelProvider([inspect]),
            conversation_id=first['conversation_id'] if index == 0 else '',
        )
        assert result['status'] == 'SUCCESS'


@pytest.mark.parametrize('fabricated', [
    'Zapisane.',
    'Zapamiętałem.',
    'Zapisałem tę definicję.',
    'Od teraz będę pamiętał.',
    'Od teraz mam zapisane.',
])
def test_model_cannot_confirm_without_write(fabricated):
    result = runtime.run_agent_turn(
        owner(),
        'Warszawa oznacza Artystyczną Manufakturę, zapisz to.',
        runtime.FakeModelProvider([respond(fabricated)]),
    )

    assert result['status'] == 'SUCCESS'
    assert result['tool_calls'] == 0
    assert result['message'] == (
        'Nie zapisano tej informacji, ponieważ operacja zapisu pamięci nie została wykonana.'
    )


@pytest.mark.parametrize(('bo_status', 'error_code', 'safe_message'), [
    ('DENIED', 'PERMISSION_DENIED', 'Brak uprawnień do pamięci firmy'),
    ('FAILED', 'MEMORY_STORAGE_UNAVAILABLE', 'Magazyn pamięci jest niedostępny'),
])
def test_failed_or_denied_receipt_overrides_model_success_claim(
        monkeypatch, bo_status, error_code, safe_message):
    original = operations.execute_business_operation

    def controlled_result(actor, operation, arguments, **kwargs):
        if operation != 'agent.terminology.remember':
            return original(actor, operation, arguments, **kwargs)
        definition = operations.OPERATION_REGISTRY[operation]
        return operations.OperationResult(
            status=bo_status,
            data=None,
            operation=operation,
            operation_version=definition.operation_version,
            execution_id='controlled-memory-write',
            request_id='request',
            correlation_id='correlation',
            error_code=error_code,
            safe_error_message=safe_message,
        )

    monkeypatch.setattr(operations, 'execute_business_operation', controlled_result)
    result = runtime.run_agent_turn(
        owner(),
        'Warszawa oznacza Artystyczną Manufakturę, zapisz to.',
        runtime.FakeModelProvider([terminology_call(), respond('Zapisane.')]),
    )

    assert result['status'] == 'SUCCESS'
    assert result['message'] == f'Nie zapisano tej informacji. {safe_message}.'
    db = backend.conn()
    assert db.execute('SELECT COUNT(*) FROM internal_agent_terminology').fetchone()[0] == 0
    db.close()


def test_pending_approval_receipt_never_confirms_write():
    db = backend.conn()
    db.execute(
        "UPDATE internal_approval_policies SET requires_approval=1 "
        "WHERE operation='agent.terminology.remember'"
    )
    db.commit()
    db.close()

    result = runtime.run_agent_turn(
        owner(),
        'Warszawa oznacza Artystyczną Manufakturę, zapisz to.',
        runtime.FakeModelProvider([terminology_call(), respond('Zapisane.')]),
    )

    assert result['status'] == 'SUCCESS'
    assert result['message'] == 'Zapis pamięci oczekuje na zatwierdzenie.'
    db = backend.conn()
    assert db.execute('SELECT COUNT(*) FROM internal_agent_terminology').fetchone()[0] == 0
    db.close()


def test_invalid_input_and_version_conflict_never_confirm_write():
    invalid = runtime.run_agent_turn(
        owner(),
        'Zapisz to jako termin.',
        runtime.FakeModelProvider([
            terminology_call(term='', meaning='definicja'),
            respond('Zapisane.'),
        ]),
    )
    assert invalid['message'].startswith('Nie zapisano tej informacji.')
    assert invalid['message'] != 'Zapisane.'

    created = runtime.run_agent_turn(
        owner(),
        'Warszawa oznacza Artystyczną Manufakturę, zapisz to.',
        runtime.FakeModelProvider([terminology_call(), respond('Zapisane.')]),
    )
    assert created['message'].startswith('Zapisane:')
    conflict = runtime.run_agent_turn(
        owner(),
        'Zmień zapis: Warszawa oznacza Firmę X.',
        runtime.FakeModelProvider([
            terminology_call(meaning='Firma X', version=0),
            respond('Zapamiętałem.'),
        ]),
    )
    assert conflict['message'].startswith('Nie zapisano tej informacji.')
    db = backend.conn()
    current = db.execute(
        "SELECT meaning,version FROM internal_agent_terminology WHERE term='Warszawa'"
    ).fetchone()
    db.close()
    assert dict(current) == {'meaning':'Artystyczna Manufaktura', 'version':1}


def test_matching_terminology_is_packed_before_large_always_apply_memory(caplog):
    _insert_term('Warszawa', 'Artystyczna Manufaktura')
    db = backend.conn()
    for index in range(8):
        db.execute(
            '''INSERT INTO internal_agent_memory(
                memory_id,memory_key,category,scope,human_actor_id,content,
                relevance_terms_json,source_run_id,confirmed_by_actor_id,version,updated_at
            ) VALUES(?,?,'procedures','company','',?,?,?,?,1,?)''',
            (
                str(uuid.uuid4()), f'procedura-{index}', 'X' * 850,
                json.dumps([conversations.ALWAYS_APPLY_RELEVANCE_TERM]),
                str(uuid.uuid4()), owner().actor_id,
                f'2026-09-15T08:00:{index:02d}+00:00',
            ),
        )
    db.commit()
    db.close()
    human, ai = _actors()

    caplog.set_level(logging.INFO, logger='agent_conversation')
    payload = conversations.memory_for_model(human, ai, 'Co wysłaliśmy do Warszawie?')

    assert payload['confirmed_terminology'][0]['term'] == 'Warszawa'
    assert len(json.dumps(payload, ensure_ascii=False).encode()) <= conversations.MAX_MEMORY_BYTES
    diagnostic = next(
        record.message for record in caplog.records
        if record.message.startswith('TERMINOLOGY_RETRIEVAL ')
    )
    assert '"matched_count": 1' in diagnostic
    assert '"included_count": 1' in diagnostic
    assert '"dropped_by_limit": 0' in diagnostic
    assert 'Artystyczna Manufaktura' not in diagnostic


def test_version_update_retrieves_only_current_definition():
    first = runtime.run_agent_turn(
        owner(),
        'Warszawa oznacza Artystyczną Manufakturę, zapisz to.',
        runtime.FakeModelProvider([terminology_call(), respond('Zapisane.')]),
    )
    updated = runtime.run_agent_turn(
        owner(),
        'Zaktualizuj zapis: Warszawa oznacza Firma X.',
        runtime.FakeModelProvider([
            terminology_call(meaning='Firma X', version=1),
            respond('Od teraz będę pamiętał nowe znaczenie.'),
        ]),
        conversation_id=first['conversation_id'],
    )
    assert updated['message'] == 'Zapisane: Warszawa oznacza Firma X.'

    human, ai = _actors()
    explicit = conversations.search_terminology(
        {'query':'Warszawie'}, ai, 'correlation')
    prefetched = conversations.memory_for_model(human, ai, 'Wyślij do Warszawy.')
    assert explicit['results'] == [{
        'term':'Warszawa', 'meaning':'Firma X', 'scope':'company',
        'source':'confirmed_by_user', 'version':2,
    }]
    assert prefetched['confirmed_terminology'] == explicit['results']
    db = backend.conn()
    assert db.execute(
        "SELECT COUNT(*) FROM internal_agent_terminology WHERE term='Warszawa'"
    ).fetchone()[0] == 1
    db.close()


def test_ambiguous_inflection_returns_all_candidates_deterministically():
    _insert_term('Gdańsk', 'Firma A', updated_at='2026-09-15T08:00:00+00:00')
    _insert_term('Gdańska', 'Firma B', updated_at='2026-09-15T09:00:00+00:00')
    human, ai = _actors()

    first = conversations.search_terminology({'query':'Gdańsku'}, ai, 'one')['results']
    second = conversations.search_terminology({'query':'Gdańsku'}, ai, 'two')['results']
    prefetched = conversations.memory_for_model(human, ai, 'Zamówienie do Gdańsku.')

    assert first == second
    assert {item['term'] for item in first} == {'Gdańsk', 'Gdańska'}
    assert prefetched['confirmed_terminology'] == first


def test_general_memory_success_uses_receipt_and_keeps_non_status_response():
    result = runtime.run_agent_turn(
        owner(),
        'Zapamiętaj: najpierw sprawdzaj blokery.',
        runtime.FakeModelProvider([
            tool('agent.memory.remember', {
                'memory_key':'kolejność pracy',
                'category':'work_preferences',
                'scope':'company',
                'content':'Najpierw sprawdzaj blokery.',
                'relevance_terms':['blokery', 'kolejność'],
                'confirmed_by_user':True,
                'expected_version':0,
            }),
            respond('Mogę też od razu sprawdzić bieżące blokery.'),
        ]),
    )

    assert result['message'] == (
        'Zapisano w pamięci: kolejność pracy.\n'
        'Mogę też od razu sprawdzić bieżące blokery.'
    )
