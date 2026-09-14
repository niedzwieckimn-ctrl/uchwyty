import json

import agent_runtime as runtime
import app as backend
from test_agent_runtime import isolated, owner, respond, tool


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
