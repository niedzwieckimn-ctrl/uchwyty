import json
import logging
from pathlib import Path
import sqlite3

import pytest
import agent_conversation
import agent_runtime as runtime
import app as backend
import internal_rbac
from test_agent_runtime import isolated, owner, respond, tool


def test_normal_init_migrates_existing_sqlite_without_memory_table(tmp_path, monkeypatch):
    database = tmp_path / 'existing-without-memory.db'
    db = sqlite3.connect(database)
    internal_rbac.initialize_schema(db)
    base_sql = (Path(__file__).parent / 'migrations' / 'agent_runtime_history.sql').read_text(encoding='utf-8')
    db.executescript(base_sql)
    assert db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='internal_agent_memory'").fetchone() is None
    db.close()

    monkeypatch.setattr(backend, 'DB_PATH', str(database))
    monkeypatch.setattr(backend, 'supabase_enabled', lambda: False)
    backend.init_db()
    backend.init_db()

    db = backend.conn()
    assert db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='internal_agent_memory'").fetchone() is not None
    db.close()
    human = internal_rbac.load_actor_context(internal_rbac.BOOTSTRAP_OWNER_ACTOR_ID)
    ai = internal_rbac.load_actor_context(internal_rbac.AI_OWNER_ASSISTANT_ACTOR_ID)
    assert agent_conversation.memory_for_model(human, ai, 'test')['relevant_company_memory'] == []


def test_confirmed_company_procedure_survives_new_conversation_via_supabase(isolated, monkeypatch):
    remote_rows = {}

    monkeypatch.setattr(backend, 'supabase_enabled', lambda: True)

    def select_rows(table, order_by='id', **_kwargs):
        assert table == 'internal_agent_memory'
        return list(remote_rows.values())

    def upsert_rows(table, rows, on_conflict):
        assert table == 'internal_agent_memory' and on_conflict == 'memory_id'
        for row in rows:
            remote_rows[row['memory_id']] = json.loads(json.dumps(row))

    monkeypatch.setattr(backend, 'supabase_select_rows', select_rows)
    monkeypatch.setattr(backend, 'supabase_upsert_rows', upsert_rows)

    saved = runtime.run_agent_turn(owner(), 'Zapamiętaj: najpierw działania operacyjne, potem płatności i blokery.',
        runtime.FakeModelProvider([
            tool('agent.memory.remember', {
                'memory_key': 'kolejność obsługi',
                'category': 'work_preferences',
                'scope': 'company',
                'content': 'Najpierw działania operacyjne, potem płatności i blokery.',
                'relevance_terms': ['priorytety', 'kolejność', 'działania operacyjne', 'płatności', 'blokery'],
                'confirmed_by_user': True,
                'expected_version': 0,
            }),
            respond('Zapamiętałem kolejność pracy.'),
        ]))
    assert saved['status'] == 'SUCCESS' and remote_rows

    unrelated = dict(next(iter(remote_rows.values())))
    unrelated.update({
        'memory_id': 'unrelated-memory',
        'memory_key': 'styl kampanii',
        'category': 'procedures',
        'content': 'Materiały marketingowe przygotowuj w piątki.',
        'relevance_terms': ['marketing', 'kampania', 'materiały'],
    })
    remote_rows[unrelated['memory_id']] = unrelated

    # Prove conversation B reloads the authoritative copy rather than relying on the SQLite row.
    db = backend.conn()
    db.execute('DELETE FROM internal_agent_memory')
    db.commit()
    db.close()

    def respect_memory(kwargs):
        injected = kwargs['input_items'][0]['content']
        assert 'Najpierw działania operacyjne, potem płatności i blokery.' in injected
        assert 'Materiały marketingowe przygotowuj w piątki.' not in injected
        return respond('Najpierw zajmę się działaniami operacyjnymi, potem płatnościami i blokerami.')

    resumed = runtime.run_agent_turn(owner(), 'Jakie są priorytety pracy?',
                                     runtime.FakeModelProvider([respect_memory]))
    assert resumed['status'] == 'SUCCESS'
    assert resumed['conversation_id'] != saved['conversation_id']
    assert resumed['message'].startswith('Najpierw zajmę się działaniami operacyjnymi')
    db = backend.conn()
    assert db.execute('SELECT COUNT(*) FROM internal_agent_memory').fetchone()[0] == 2
    db.close()


def test_topical_procedure_is_not_visible_without_relevance(isolated, monkeypatch):
    memory_content = 'Nie pokazuj podglądów zamówień bez wyraźnej prośby.'
    remote_rows = [{
        'memory_id':'diagnostic-memory',
        'memory_key':'podglądy zamówień',
        'category':'procedures',
        'scope':'company',
        'human_actor_id':'',
        'content':memory_content,
        'relevance_terms':['podglądy', 'zamówienia', 'wyraźna prośba'],
        'source_run_id':'00000000-0000-4000-8000-000000000001',
        'confirmed_by_actor_id':internal_rbac.BOOTSTRAP_OWNER_ACTOR_ID,
        'version':1,
        'updated_at':'2026-09-14T08:00:00+00:00',
    }]
    monkeypatch.setattr(agent_conversation, '_remote_memory_enabled', lambda: True)
    monkeypatch.setattr(agent_conversation, '_remote_memory_select', lambda: list(remote_rows))

    def inspect_first_call(kwargs):
        serialized = json.dumps(kwargs['input_items'], ensure_ascii=False)
        assert memory_content not in serialized
        return respond('Odpowiedź bez widocznej procedury.')

    result = runtime.run_agent_turn(
        owner(),
        'Co mam dziś do zrobienia?',
        runtime.FakeModelProvider([inspect_first_call]),
    )

    assert result['status'] == 'SUCCESS'
    db = backend.conn()
    assert db.execute("SELECT category FROM internal_agent_memory WHERE memory_id='diagnostic-memory'").fetchone()[0] == 'procedures'
    db.close()


@pytest.mark.parametrize(('content', 'question', 'category'), [
    ('Nie pokazuj podglądów zamówień bez wyraźnej prośby.', 'Co mam dziś do zrobienia?', 'procedures'),
    ('Planned P/O nie liczy się jako pokrycie braków.', 'Co muszę zamówić na cito?', 'work_preferences'),
])
def test_always_apply_survives_cache_deletion_and_is_visible_in_first_model_call(
        isolated, monkeypatch, content, question, category):
    remote_rows = {}
    monkeypatch.setattr(backend, 'supabase_enabled', lambda: True)

    def select_rows(table, order_by='id', **_kwargs):
        assert table == 'internal_agent_memory'
        return list(remote_rows.values())

    def upsert_rows(table, rows, on_conflict):
        assert table == 'internal_agent_memory' and on_conflict == 'memory_id'
        for row in rows:
            remote_rows[row['memory_id']] = json.loads(json.dumps(row))

    monkeypatch.setattr(backend, 'supabase_select_rows', select_rows)
    monkeypatch.setattr(backend, 'supabase_upsert_rows', upsert_rows)

    saved = runtime.run_agent_turn(owner(), 'Zapisz tę regułę jako obowiązującą zawsze.',
        runtime.FakeModelProvider([
            tool('agent.memory.remember', {
                'memory_key':f'always-{category}', 'category':category, 'scope':'company',
                'content':content, 'relevance_terms':['__always_apply__'],
                'confirmed_by_user':True, 'expected_version':0,
            }),
            respond('Zapisane.'),
        ]))
    assert saved['status'] == 'SUCCESS' and len(remote_rows) == 1

    db = backend.conn()
    db.execute('DELETE FROM internal_agent_memory')
    db.commit()
    db.close()

    def inspect_first_call(kwargs):
        assert content in kwargs['input_items'][0]['content']
        return respond('Reguła została uwzględniona.')

    loaded = runtime.run_agent_turn(owner(), question, runtime.FakeModelProvider([inspect_first_call]))
    assert loaded['status'] == 'SUCCESS'
    assert loaded['conversation_id'] != saved['conversation_id']


def test_always_apply_has_priority_over_topical_memory_at_entry_limit(isolated):
    common = {
        'category':'procedures', 'scope':'company', 'human_actor_id':'',
        'source_run_id':'00000000-0000-4000-8000-000000000001',
        'confirmed_by_actor_id':internal_rbac.BOOTSTRAP_OWNER_ACTOR_ID,
        'version':1,
    }
    rows = [{
        **common, 'memory_id':f'topical-{index}', 'memory_key':f'priorytety {index}',
        'content':f'Topical {index}', 'relevance_terms':['priorytety'],
        'updated_at':f'2026-09-14T08:00:{index:02d}+00:00',
    } for index in range(8)]
    rows.append({
        **common, 'memory_id':'always', 'memory_key':'globalna procedura',
        'content':'Obowiązuje zawsze.', 'relevance_terms':['__always_apply__'],
        'updated_at':'2026-09-13T08:00:00+00:00',
    })

    selected = agent_conversation._relevant_memory(rows, owner(), 'Jakie są priorytety?', limit=8)

    assert len(selected) == 8
    assert selected[0]['memory_id'] == 'always'
    assert sum(row['memory_id'].startswith('topical-') for row in selected) == 7


def test_memory_write_failure_logs_safe_http_diagnostic_and_does_not_update_cache(isolated, monkeypatch, caplog):
    private_content = 'Poufna treść preferencji'
    calls = []
    monkeypatch.setattr(backend, 'supabase_enabled', lambda: True)
    monkeypatch.setattr(backend, 'supabase_select_rows', lambda *_args, **_kwargs: [])

    def fail_upsert(*_args, **_kwargs):
        calls.append(True)
        try:
            from urllib.error import HTTPError
            raise HTTPError('https://example.invalid', 409, 'foreign key '+private_content, {}, None)
        except HTTPError as cause:
            raise RuntimeError('Supabase HTTP 409: foreign key '+private_content) from cause

    monkeypatch.setattr(backend, 'supabase_upsert_rows', fail_upsert)
    monkeypatch.setattr(agent_conversation, '_remote_memory_enabled', lambda: True)
    monkeypatch.setattr(agent_conversation, '_remote_memory_select', lambda: [])
    monkeypatch.setattr(agent_conversation, '_remote_memory_upsert', lambda row: fail_upsert(row))
    with caplog.at_level(logging.ERROR, logger='agent_conversation'):
        result = runtime.run_agent_turn(owner(), 'Zapamiętaj regułę.', runtime.FakeModelProvider([
            tool('agent.memory.remember', {
                'memory_key':'reguła prywatna','category':'work_preferences','scope':'company',
                'content':private_content,'relevance_terms':['reguła'],
                'confirmed_by_user':True,'expected_version':0,
            }),
            respond('Nie udało się zapisać reguły.'),
        ]))
    assert result['status']=='SUCCESS'
    assert calls, result
    assert 'MEMORY_WRITE_FAILURE' in caplog.text, result
    assert '"stage": "supabase_authoritative_write"' in caplog.text
    assert '"exception_type": "RuntimeError"' in caplog.text
    assert '"supabase_http_status": 409' in caplog.text
    assert private_content not in caplog.text
    db=backend.conn();assert db.execute('SELECT COUNT(*) FROM internal_agent_memory').fetchone()[0]==0;db.close()


@pytest.mark.parametrize('failure_mode', ['semantic_conflict', 'committed_response_failure'])
def test_failed_upsert_is_reconciled_only_when_authoritative_record_is_identical(
        isolated, monkeypatch, failure_mode):
    content = 'Planned P/O nie liczy się jako pokrycie braków.'
    memory_key = 'pokrycie braków P/O'
    existing = {
        'memory_id':'existing-memory', 'memory_key':memory_key,
        'category':'procedures', 'scope':'company', 'human_actor_id':'',
        'content':content, 'relevance_terms':['__always_apply__'],
        'source_run_id':'00000000-0000-4000-8000-000000000001',
        'confirmed_by_actor_id':internal_rbac.BOOTSTRAP_OWNER_ACTOR_ID,
        'version':1, 'updated_at':'2026-09-14T08:00:00+00:00',
    }
    remote_rows = {'existing-memory':dict(existing)} if failure_mode == 'semantic_conflict' else {}
    select_attempts = 0
    upsert_attempts = 0
    monkeypatch.setattr(backend, 'supabase_enabled', lambda: True)

    def select_rows(table, order_by='id', **_kwargs):
        nonlocal select_attempts
        assert table == 'internal_agent_memory'
        select_attempts += 1
        if failure_mode == 'semantic_conflict' and select_attempts <= 2:
            raise RuntimeError('Supabase HTTP 504: lookup unavailable')
        return list(remote_rows.values())

    def upsert_rows(table, rows, on_conflict):
        nonlocal upsert_attempts
        assert table == 'internal_agent_memory' and on_conflict == 'memory_id'
        upsert_attempts += 1
        row = json.loads(json.dumps(rows[0]))
        semantic_key = (row['category'], row['scope'], row['human_actor_id'], row['memory_key'].casefold())
        collision = next((saved for saved in remote_rows.values()
                          if (saved['category'], saved['scope'], saved['human_actor_id'], saved['memory_key'].casefold()) == semantic_key
                          and saved['memory_id'] != row['memory_id']), None)
        if upsert_attempts == 1 and failure_mode == 'semantic_conflict':
            assert collision is not None
            raise RuntimeError('Supabase HTTP 409: duplicate key internal_agent_memory_identity')
        remote_rows[row['memory_id']] = row
        if upsert_attempts == 1 and failure_mode == 'committed_response_failure':
            raise RuntimeError('Supabase HTTP 504: response unavailable after commit')

    monkeypatch.setattr(backend, 'supabase_select_rows', select_rows)
    monkeypatch.setattr(backend, 'supabase_upsert_rows', upsert_rows)

    def reconciled_result(kwargs):
        output = json.loads(kwargs['input_items'][-1]['output'])
        assert output['ok'] is True and output['version'] == 1
        return respond('Zapisane.')

    first = runtime.run_agent_turn(owner(), 'Zapisz tę regułę.', runtime.FakeModelProvider([
        tool('agent.memory.remember', {
            'memory_key':memory_key, 'category':'procedures', 'scope':'company',
            'content':content, 'relevance_terms':['__always_apply__'],
            'confirmed_by_user':True, 'expected_version':0,
        }),
        reconciled_result,
    ]))
    assert first['status'] == 'SUCCESS'
    assert len(remote_rows) == 1
    db = backend.conn()
    assert db.execute('SELECT COUNT(*) FROM internal_agent_memory').fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM internal_audit_log WHERE operation='agent.memory.remembered'").fetchone()[0] == 1
    db.close()

    second = runtime.run_agent_turn(owner(), 'Spróbuj jeszcze raz.', runtime.FakeModelProvider([
        tool('agent.memory.remember', {
            'memory_key':memory_key, 'category':'procedures', 'scope':'company',
            'content':content, 'relevance_terms':['__always_apply__'],
            'confirmed_by_user':True, 'expected_version':0,
        }),
        lambda kwargs: (
            (lambda output: (assert_memory_replay(output), respond('Zapisane.'))[1])(
                json.loads(kwargs['input_items'][-1]['output']))
        ),
    ]), conversation_id=first['conversation_id'])

    assert second['status'] == 'SUCCESS'
    assert len(remote_rows) == 1
    saved = next(iter(remote_rows.values()))
    assert saved['version'] == 1
    assert upsert_attempts == 1
    db = backend.conn()
    cached = db.execute('SELECT memory_id,version FROM internal_agent_memory').fetchone()
    assert cached['memory_id'] == saved['memory_id'] and cached['version'] == 1
    assert db.execute("SELECT COUNT(*) FROM internal_audit_log WHERE operation='agent.memory.remembered'").fetchone()[0] == 2
    db.close()


def assert_memory_replay(output):
    assert output['ok'] is True
    assert output['version'] == 1


def test_first_save_identical_replay_semantic_update_and_new_conversation(isolated, monkeypatch):
    remote_rows = {}
    monkeypatch.setattr(backend, 'supabase_enabled', lambda: True)

    def select_rows(table, order_by='id', **_kwargs):
        assert table == 'internal_agent_memory'
        return list(remote_rows.values())

    def upsert_rows(table, rows, on_conflict):
        assert table == 'internal_agent_memory' and on_conflict == 'memory_id'
        for row in rows:
            remote_rows[row['memory_id']] = json.loads(json.dumps(row))

    monkeypatch.setattr(backend, 'supabase_select_rows', select_rows)
    monkeypatch.setattr(backend, 'supabase_upsert_rows', upsert_rows)
    key = 'zamówienia uchwytów na cito'
    initial = 'Przy pytaniu o zamówienie uchwytów na cito uwzględnij istniejące P/O.'
    updated = 'Przy pytaniu o zamówienie uchwytów na cito pomijaj planned i uwzględnij ordered oraz shipped.'
    base = {
        'memory_key':key, 'category':'procedures', 'scope':'company',
        'relevance_terms':['__always_apply__'], 'confirmed_by_user':True,
    }

    def expect_success(version):
        def check(kwargs):
            output = json.loads(kwargs['input_items'][-1]['output'])
            assert output['ok'] is True and output['version'] == version
            return respond('Zapisane.')
        return check

    first = runtime.run_agent_turn(owner(), 'Zapisz tę regułę.', runtime.FakeModelProvider([
        tool('agent.memory.remember', {**base, 'content':initial, 'expected_version':0}),
        expect_success(1),
    ]))
    assert first['status'] == 'SUCCESS'
    assert len(remote_rows) == 1
    memory_id = next(iter(remote_rows))
    db = backend.conn()
    cached = db.execute('SELECT memory_id,content,version FROM internal_agent_memory').fetchone()
    assert dict(cached) == {'memory_id':memory_id, 'content':initial, 'version':1}
    assert db.execute("SELECT COUNT(*) FROM internal_audit_log WHERE operation='agent.memory.remembered'").fetchone()[0] == 1
    db.close()

    replay = runtime.run_agent_turn(owner(), 'Zapisz identyczną regułę ponownie.', runtime.FakeModelProvider([
        tool('agent.memory.remember', {**base, 'content':initial, 'expected_version':0}),
        expect_success(1),
    ]), conversation_id=first['conversation_id'])
    assert replay['status'] == 'SUCCESS'
    assert len(remote_rows) == 1 and remote_rows[memory_id]['version'] == 1

    changed = runtime.run_agent_turn(owner(), 'Zaktualizuj tę regułę.', runtime.FakeModelProvider([
        tool('agent.memory.remember', {
            **base, 'memory_key':key.upper(), 'content':updated, 'expected_version':1,
        }),
        expect_success(2),
    ]), conversation_id=first['conversation_id'])
    assert changed['status'] == 'SUCCESS'
    assert len(remote_rows) == 1 and remote_rows[memory_id]['version'] == 2

    db = backend.conn()
    assert db.execute('SELECT memory_id,content,version FROM internal_agent_memory').fetchone()['memory_id'] == memory_id
    assert db.execute("SELECT COUNT(*) FROM internal_audit_log WHERE operation='agent.memory.remembered'").fetchone()[0] == 3
    db.execute('DELETE FROM internal_agent_memory')
    db.commit()
    db.close()

    def visible_on_first_call(kwargs):
        assert updated in kwargs['input_items'][0]['content']
        return respond('Reguła została zastosowana.')

    loaded = runtime.run_agent_turn(owner(), 'Co mam dziś do zrobienia?',
                                    runtime.FakeModelProvider([visible_on_first_call]))
    assert loaded['status'] == 'SUCCESS'
    assert loaded['conversation_id'] != first['conversation_id']


def test_provider_failure_releases_active_turn_and_next_turn_runs(isolated):
    failed = runtime.run_agent_turn(owner(), 'pierwszy turn',
                                    runtime.FakeModelProvider([TimeoutError('provider timeout')]))
    assert failed['status'] == 'FAILED'
    db = backend.conn()
    assert db.execute('SELECT COUNT(*) FROM internal_agent_turn_leases WHERE conversation_id=?',
                      (failed['conversation_id'],)).fetchone()[0] == 0
    db.close()

    next_turn = runtime.run_agent_turn(owner(), 'następny turn',
                                       runtime.FakeModelProvider([respond('Działa normalnie.')]),
                                       conversation_id=failed['conversation_id'])
    assert next_turn['status'] == 'SUCCESS'
    assert next_turn['error_code'] == ''
