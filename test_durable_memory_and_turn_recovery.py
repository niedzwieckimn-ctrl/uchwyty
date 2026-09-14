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
