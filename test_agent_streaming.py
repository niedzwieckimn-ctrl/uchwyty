"""Local streaming regressions: no provider network, real isolated runtime/SQLite."""
import json
import threading
import time

import pytest

import agent_conversation as conversations
import agent_runtime as runtime
import agent_streaming as streaming
import app as backend
import business_operations as operations
import internal_rbac as rbac
from test_agent_runtime import isolated, owner, respond, tool


class ScriptedStreamingProvider:
    """Tools-enabled passes stay buffered; pre-known final passes may stream."""
    def __init__(self, plans=(), *, chunks=None, first_delay=0, final_delay=0,
                 release=None, verify_final=None):
        self.plans = list(plans)
        self.chunks = chunks or [
            'Na magazynie mamy obecnie dwadzieścia cztery sztuki produktu Avery. ',
            'To jest aktualny potwierdzony stan.',
            '<speech_text mode="direct">Masz 24 sztuki Avery.</speech_text>',
        ]
        self.first_delay = first_delay
        self.final_delay = final_delay
        self.release = release
        self.verify_final = verify_final
        self.calls = []
        self.stream_finished = threading.Event()

    def complete(self, **kwargs):
        self.calls.append(('complete', kwargs))
        assert self.plans, 'Unexpected extra planning call'
        value = self.plans.pop(0)
        return value(kwargs) if callable(value) else value

    def complete_stream(self, *, on_delta, cancelled=None, **kwargs):
        self.calls.append(('complete_stream', kwargs))
        assert not kwargs['tools'] or kwargs['tool_choice'] == 'none'
        if self.verify_final:
            self.verify_final()
        time.sleep(self.first_delay)
        on_delta(self.chunks[0])
        if self.release:
            assert self.release.wait(5), 'Test did not release final model response'
        time.sleep(self.final_delay)
        for chunk in self.chunks[1:]:
            on_delta(chunk)
        self.stream_finished.set()
        return respond(''.join(self.chunks))


def client():
    result = backend.app.test_client()
    with result.session_transaction() as session:
        session['admin_authenticated'] = True
        session['csrf_token'] = 'csrf'
    return result


def frames(response):
    buffer = ''
    for chunk in response.response:
        buffer += chunk.decode('utf-8') if isinstance(chunk, bytes) else chunk
        while '\n\n' in buffer:
            frame, buffer = buffer.split('\n\n', 1)
            fields = dict(line.split(': ', 1) for line in frame.splitlines() if ': ' in line)
            if 'event' in fields:
                yield fields['event'], json.loads(fields['data'])


def post_stream(provider, message='Dzień dobry', **payload):
    backend.AGENT_MODEL_PROVIDER = provider
    return client().post('/api/internal/ai/chat', json={'message': message, **payload},
                         headers={'Accept': 'text/event-stream'}, buffered=False)


def read_rows(sql, parameters=()):
    db = backend.conn()
    try:
        return db.execute(sql, parameters).fetchall()
    finally:
        db.close()


def assert_finalized(result):
    history = read_rows('SELECT assistant_text FROM internal_agent_turns WHERE run_id=?',
                        (result['agent_run_id'],))
    assert len(history) == 1 and history[0]['assistant_text'] == result['message']
    audit = read_rows("SELECT operation FROM internal_audit_log WHERE correlation_id=? AND operation='agent.completed'",
                     (result['correlation_id'],))
    assert len(audit) == 1
    assert not read_rows('SELECT 1 FROM internal_agent_turn_leases WHERE run_id=?',
                         (result['agent_run_id'],))


def test_fetch_post_emits_display_before_model_completion_and_done_after_history_audit(monkeypatch):
    monkeypatch.setattr(runtime, '_tool_descriptors', lambda *_args: [])
    release = threading.Event()
    provider = ScriptedStreamingProvider(release=release)
    response = post_stream(provider)
    assert response.status_code == 200 and response.mimetype == 'text/event-stream'
    assert response.headers['X-Accel-Buffering'] == 'no'
    received = []
    try:
        iterator = frames(response)
        received.append(next(iterator))
        assert received[0][0] == 'turn_started'
        received.append(next(iterator))
        assert received[1][0] == 'display_delta' and received[1][1]['delta']
        assert not provider.stream_finished.is_set()
        assert not read_rows("SELECT 1 FROM internal_audit_log WHERE operation='agent.completed'")
        assert read_rows('SELECT assistant_text FROM internal_agent_turns')[0][0] is None
        release.set()
        received.extend(iterator)
        assert received[-1][0] == 'done'
        assert_finalized(received[-1][1])
        assert [name for name, _ in received].count('done') == 1
        assert 'speech_ready' not in [name for name, _ in received]
        display = ''.join(data['delta'] for name, data in received if name == 'display_delta')
        assert display == received[-1][1]['message']
        assert '<speech_text' not in display and 'Internal buffered candidate' not in display
        assert [name for name, _ in provider.calls] == ['complete_stream']
    finally:
        release.set()
        response.close()


def test_tool_enabled_text_and_arguments_never_reach_display():
    plan = runtime.ProviderResponse(text='INTERNAL_PLAN should not be displayed',
        tool_calls=(runtime.ToolCall('get-product', 'inventory.product.get', '{"product_id":1}'),),
        model='fake-model')
    provider = ScriptedStreamingProvider([plan, respond('Avery ma 24 sztuki.')])
    response = post_stream(provider, 'Pokaż Avery 160')
    try:
        received = list(frames(response))
    finally:
        response.close()
    assert received[-1][0] == 'done'
    assert received[-1][1]['tool_calls'] == 1
    display = ''.join(data['delta'] for name, data in received if name == 'display_delta')
    assert 'INTERNAL_PLAN' not in display and 'product_id' not in display
    assert display == received[-1][1]['message'] == 'Avery ma 24 sztuki.'
    assert [name for name, _ in provider.calls] == ['complete', 'complete']
    assert len(read_rows("SELECT 1 FROM internal_operation_executions WHERE operation='inventory.product.get'")) == 1
    assert_finalized(received[-1][1])


@pytest.mark.parametrize('failure', ['history', 'audit'])
def test_finalization_failure_after_deltas_is_terminal_error_without_done(monkeypatch, failure):
    monkeypatch.setattr(runtime, '_tool_descriptors', lambda *_args: [])
    def fail(*_args, **_kwargs):
        raise RuntimeError('test finalization failure')
    if failure == 'history':
        monkeypatch.setattr(conversations, 'finish_turn', fail)
    else:
        original = runtime._audit
        def audit(event, *args, **kwargs):
            if event == 'agent.completed':
                fail()
            return original(event, *args, **kwargs)
        monkeypatch.setattr(runtime, '_audit', audit)
    response = post_stream(ScriptedStreamingProvider())
    try:
        received = list(frames(response))
    finally:
        response.close()
    assert any(name == 'display_delta' for name, _ in received)
    assert received[-1][0] == 'error'
    assert received[-1][1]['error_code'] == ('HISTORY_SAVE_FAILED' if failure == 'history' else 'AUDIT_FAILED')
    assert not any(name in {'done', 'speech_ready'} for name, _ in received)
    assert not read_rows('SELECT 1 FROM internal_agent_turn_leases')


def test_non_stream_json_path_does_not_add_model_pass_or_change_contract():
    provider = ScriptedStreamingProvider([respond('Cześć. Jak mogę pomóc?')])
    backend.AGENT_MODEL_PROVIDER = provider
    response = client().post('/api/internal/ai/chat', json={'message': 'Cześć'})
    result = response.get_json()
    assert response.status_code == 200 and response.mimetype == 'application/json'
    assert result['message'] == 'Cześć. Jak mogę pomóc?'
    assert {'speech_text', 'artifacts', 'approvals', 'conversation_id', 'timings'} <= result.keys()
    assert [name for name, _ in provider.calls] == ['complete']
    assert_finalized(result)


def test_buffered_final_preserves_original_text_speech_mode_model_and_usage_without_regeneration():
    screen = 'Avery ma 24 sztuki na magazynie. Szczegóły pozostają w pełnej odpowiedzi.'
    speech = 'Masz 24 sztuki Avery.'
    provider = ScriptedStreamingProvider([respond(
        screen + '<speech_text mode="direct">' + speech + '</speech_text>',
        model='original-final-model', input_tokens=321, output_tokens=54,
    )], chunks=['REGENERATED_TEXT_MUST_NOT_BE_USED'])
    response = post_stream(provider, 'Ile mamy Avery?')
    try:
        received = []
        for name, data in frames(response):
            if name == 'display_delta':
                assert len(read_rows("SELECT 1 FROM internal_audit_log WHERE operation='agent.completed'")) == 1
                assert read_rows('SELECT assistant_text FROM internal_agent_turns')[0][0] == screen
            received.append((name, data))
    finally:
        response.close()
    assert received[-1][0] == 'done'
    result = received[-1][1]
    assert result['message'] == screen
    assert result['speech_text'] == speech and result['voice_response_mode'] == 'direct'
    assert result['model'] == 'original-final-model'
    assert result['usage'] == {'input_tokens': 321, 'output_tokens': 54}
    assert ''.join(data['delta'] for name, data in received if name == 'display_delta') == screen
    assert [name for name, _ in provider.calls] == ['complete']
    assert_finalized(result)


@pytest.mark.parametrize('failure', ['history', 'audit'])
def test_buffered_finalization_failure_never_emits_successful_display(monkeypatch, failure):
    def fail(*_args, **_kwargs):
        raise RuntimeError('test buffered finalization failure')
    if failure == 'history':
        monkeypatch.setattr(conversations, 'finish_turn', fail)
    else:
        original = runtime._audit
        def audit(event, *args, **kwargs):
            if event == 'agent.completed':
                fail()
            return original(event, *args, **kwargs)
        monkeypatch.setattr(runtime, '_audit', audit)
    provider = ScriptedStreamingProvider([respond('Gotowa odpowiedź modelu.')])
    response = post_stream(provider)
    try:
        received = list(frames(response))
    finally:
        response.close()
    assert received[-1][0] == 'error'
    assert received[-1][1]['error_code'] == ('HISTORY_SAVE_FAILED' if failure == 'history' else 'AUDIT_FAILED')
    assert not any(name in {'display_delta', 'done', 'speech_ready'} for name, _ in received)
    assert [name for name, _ in provider.calls] == ['complete']
    assert not read_rows('SELECT 1 FROM internal_agent_turn_leases')


def test_legacy_provider_without_stream_method_still_returns_sse_done():
    provider = runtime.FakeModelProvider([respond('Odpowiedź zgodnego starszego adaptera.')])
    response = post_stream(provider)
    try:
        received = list(frames(response))
    finally:
        response.close()
    assert received[-1][0] == 'done'
    assert received[-1][1]['message'] == 'Odpowiedź zgodnego starszego adaptera.'
    assert len(provider.calls) == 1


def test_streaming_preserves_same_turn_read_before_write_guard():
    human = owner()
    opened = runtime.run_agent_turn(human, 'Rozpocznij remanent', runtime.FakeModelProvider([
        tool('inventory.count.session.start', {}), respond('Remanent rozpoczęty.'),
    ]))
    cid = opened['conversation_id']
    observed = runtime.run_agent_turn(human, 'Sprawdź Avery 160', runtime.FakeModelProvider([
        tool('inventory.product.get', {'product_id': 1}), respond('Avery ma 24 sztuki.'),
    ]), conversation_id=cid)
    assert observed['status'] == 'SUCCESS'
    provider = ScriptedStreamingProvider([tool('inventory.count.record', {
        'product_id': 1, 'counted_quantity': 20, 'expected_version': 0,
        'idempotency_key': 'streaming-guard',
    })])
    response = post_stream(provider, 'Zapisz 20 dla produktu 1', conversation_id=cid)
    try:
        received = list(frames(response))
    finally:
        response.close()
    # V45 rejects the unrecognized identity before a model can supply a hidden ID.
    assert received[-1][0] == 'done'
    assert received[-1][1]['inventory_fast_failure'] == 'product_not_found'
    assert received[-1][1]['tool_calls'] == 0
    assert not read_rows('SELECT 1 FROM internal_inventory_count_items')
    assert not read_rows('SELECT 1 FROM stock_adjustments')


def test_cancel_during_inflight_write_completes_commit_audit_and_releases_lock(monkeypatch):
    entered, release, cancelled, finished = (threading.Event() for _ in range(4))
    original = operations._HANDLERS['inventory.count.session.start']
    def gate(*args, **kwargs):
        entered.set()
        assert release.wait(5), 'Test write gate was not released'
        return original(*args, **kwargs)
    monkeypatch.setitem(operations._HANDLERS, 'inventory.count.session.start', gate)
    provider = ScriptedStreamingProvider([tool('inventory.count.session.start', {})])
    human = owner()
    results, events = [], []
    def work():
        try:
            results.append(runtime.run_agent_turn(human, 'Rozpocznij remanent', provider,
                emit=lambda name, data: events.append((name, data)), cancelled=cancelled.is_set))
        finally:
            finished.set()
    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    try:
        assert entered.wait(5)
        cancelled.set()
    finally:
        release.set()
    assert finished.wait(5)
    worker.join(timeout=1)
    assert results[0]['error_code'] == 'TURN_CANCELLED'
    assert len(read_rows('SELECT 1 FROM internal_inventory_count_sessions')) == 1
    execution = read_rows("SELECT status FROM internal_operation_executions WHERE operation='inventory.count.session.start'")
    assert execution[0]['status'] == 'SUCCESS'
    assert len(read_rows("SELECT 1 FROM internal_audit_log WHERE operation='business_operation.success'")) == 1
    assert len(read_rows("SELECT 1 FROM internal_audit_log WHERE operation='agent.failed'")) == 1
    assert not read_rows('SELECT 1 FROM internal_agent_turn_leases')
    assert not any(name in {'display_delta', 'done', 'speech_ready'} for name, _ in events)
    # This thread did not own the previous transaction; a leak would block it.
    assert backend._sqlite_write_lock.acquire(timeout=1)
    backend._sqlite_write_lock.release()
    db = backend.conn()
    try:
        db.execute('UPDATE stock SET qty=qty WHERE product_id=1')
        db.commit()
    finally:
        db.close()


def test_slow_consumer_does_not_hold_sqlite_lock_or_delay_finalization():
    provider = ScriptedStreamingProvider([
        tool('inventory.count.session.start', {}), respond('Remanent rozpoczęty.'),
    ])
    response = post_stream(provider, 'Rozpocznij remanent')
    iterator = frames(response)
    try:
        assert next(iterator)[0] == 'turn_started'
        assert next(iterator)[0] == 'display_delta'
        assert [name for name, _ in provider.calls] == ['complete', 'complete']
        assert not provider.stream_finished.is_set()
        # Intentionally do not read the remaining queued output yet.
        deadline = time.perf_counter() + 5
        while not read_rows("SELECT 1 FROM internal_audit_log WHERE operation='agent.completed'"):
            assert time.perf_counter() < deadline, 'Slow consumer prevented final audit'
            time.sleep(0.01)
        assert backend._sqlite_write_lock.acquire(timeout=1)
        backend._sqlite_write_lock.release()
        assert not read_rows('SELECT 1 FROM internal_agent_turn_leases')
        assert list(iterator)[-1][0] == 'done'
    finally:
        response.close()


def test_real_write_commit_and_business_audit_precede_first_final_display():
    checked = []
    def committed_before_synthesis(_kwargs):
        executions = read_rows("SELECT status FROM internal_operation_executions WHERE operation='inventory.count.session.start'")
        assert len(executions) == 1 and executions[0]['status'] == 'SUCCESS'
        assert len(read_rows('SELECT 1 FROM internal_inventory_count_sessions')) == 1
        assert len(read_rows("SELECT 1 FROM internal_audit_log WHERE operation='business_operation.success'")) == 1
        assert not read_rows("SELECT 1 FROM internal_audit_log WHERE operation='agent.completed'")
        # Check lock ownership from a different thread: same-thread RLock would
        # hide an accidental transaction held during model generation.
        result = []
        def probe():
            acquired = backend._sqlite_write_lock.acquire(timeout=1)
            result.append(acquired)
            if acquired:
                backend._sqlite_write_lock.release()
        thread = threading.Thread(target=probe, daemon=True)
        thread.start()
        thread.join(timeout=2)
        assert result == [True]
        checked.append(True)
        return respond('Remanent rozpoczęty.')
    provider = ScriptedStreamingProvider([
        tool('inventory.count.session.start', {}), committed_before_synthesis,
    ])
    response = post_stream(provider, 'Rozpocznij remanent')
    try:
        received = []
        for name, data in frames(response):
            if name == 'display_delta':
                assert len(read_rows("SELECT 1 FROM internal_audit_log WHERE operation='agent.completed'")) == 1
                assert read_rows('SELECT assistant_text FROM internal_agent_turns')[0][0] == 'Remanent rozpoczęty.'
            received.append((name, data))
    finally:
        response.close()
    assert checked == [True]
    assert [name for name, _ in provider.calls] == ['complete', 'complete']
    assert ''.join(data['delta'] for name, data in received if name == 'display_delta') == 'Remanent rozpoczęty.'
    assert received[-1][0] == 'done'
    assert_finalized(received[-1][1])


def test_closing_response_signals_worker_cancellation_and_still_finalizes():
    saw_cancel, finalized = threading.Event(), threading.Event()
    def run(emit, cancelled, trace):
        emit('display_delta', {'delta': 'Pierwszy fragment.'})
        deadline = time.perf_counter() + 5
        while not cancelled():
            assert time.perf_counter() < deadline, 'Response close did not cancel worker'
            time.sleep(0.005)
        saw_cancel.set()
        return {'status': 'FAILED', 'error_code': 'TURN_CANCELLED'}
    def finalize(result):
        finalized.set()
        return result, 503
    with backend.app.test_request_context('/api/internal/ai/chat', method='POST'):
        response = streaming.sse_response(run, finalize)
        iterator = iter(response.response)
        assert 'event: turn_started' in next(iterator)
        assert 'event: display_delta' in next(iterator)
        response.close()
    assert saw_cancel.wait(5)
    assert finalized.wait(5)


@pytest.mark.parametrize('chunk_size', [1, 2, 7, 31])
def test_display_filter_redacts_split_credentials_and_hides_speech_metadata(chunk_size):
    raw = ('Aktualny publiczny wynik jest dostępny. '
           'api_key = testcredential123; Bearer privatecredential987; '
           'sk-abcdefghijklmnopqrstuv. Dalsza bezpieczna część odpowiedzi. '
           '<speech_text mode="direct">HIDDEN_SPOKEN_SUMMARY</speech_text>')
    screen, _, _ = runtime._split_final_response(raw)
    captured = []
    parser = streaming.DisplayTextFilter(lambda name, data: captured.append((name, data)), runtime._plain_response_text)
    for index in range(0, len(raw), chunk_size):
        parser.feed(raw[index:index + chunk_size])
    parser.finish(screen)
    for name, data in captured:
        assert name == 'display_delta'
    encoded = ''.join(data['delta'] for _, data in captured)
    assert all(secret not in encoded for secret in (
        'testcredential123', 'privatecredential987', 'abcdefghijklmnopqrstuv',
        'HIDDEN_SPOKEN_SUMMARY', '<speech_text', 'mode="direct"',
    ))
    assert encoded.startswith('Aktualny publiczny wynik')
    # Key formatting can change a previously safe prefix ("api_key =" becomes
    # "api_key=[REDACTED]"). The documented filter then stops appending; done
    # reconciles the complete authoritative message instead of leaking a value.
    expected_final = runtime._plain_response_text(screen)
    assert '[REDACTED]' in expected_final
    assert parser.frozen or encoded == expected_final


def test_done_contains_sanitized_authoritative_text_after_safe_prefix_freezes(monkeypatch):
    monkeypatch.setattr(runtime, '_tool_descriptors', lambda *_args: [])
    chunks = list('Publiczny wynik zawiera pięć poprawnych słów. api_key = testcredential123; '
                  'Dalsza publiczna część odpowiedzi. '
                  '<speech_text mode="direct">Bezpieczne podsumowanie.</speech_text>')
    response = post_stream(ScriptedStreamingProvider(chunks=chunks))
    try:
        received = list(frames(response))
    finally:
        response.close()
    assert received[-1][0] == 'done'
    final = received[-1][1]
    assert final['message'] == runtime._plain_response_text(runtime._split_final_response(''.join(chunks))[0])
    assert 'testcredential123' not in json.dumps(received, ensure_ascii=False)
    assert 'Dalsza publiczna część odpowiedzi.' in final['message']
    assert_finalized(final)


def test_local_first_display_timing_with_controlled_model_delays(capsys, monkeypatch):
    monkeypatch.setattr(runtime, '_tool_descriptors', lambda *_args: [])
    provider = ScriptedStreamingProvider(first_delay=0.05, final_delay=0.20)
    started = time.perf_counter()
    response = post_stream(provider)
    observed = {}
    result = None
    try:
        for name, data in frames(response):
            observed.setdefault(name, (time.perf_counter() - started) * 1000)
            if name == 'done':
                result = data
    finally:
        response.close()
    assert result is not None
    assert [name for name, _ in provider.calls] == ['complete_stream']
    points = result['stream_timings']
    assert points['request_received'] == 0
    assert points['first_model_request'] <= points['first_display_delta'] < points['final_model_done'] <= points['done_sent']
    assert observed['display_delta'] < observed['done']
    assert points['final_model_done'] - points['first_display_delta'] >= 150
    measurement = {'controlled_first_chunk_delay_ms': 50, 'controlled_remainder_delay_ms': 200,
                   'client_first_delta_received_ms': round(observed['display_delta'], 3),
                   'client_done_received_ms': round(observed['done'], 3), 'server': points}
    with capsys.disabled():
        print('LOCAL_AGENT_STREAM_MEASUREMENT ' + json.dumps(measurement, sort_keys=True))


def pending_order_approval():
    db = backend.conn()
    try:
        db.execute("INSERT INTO orders(id,order_no,customer_name,status,created_at) VALUES(20,'STREAM-APPROVAL-20','Test','new',?)",
                   (backend.now_iso(),))
        db.commit()
    finally:
        db.close()
    opened = runtime.run_agent_turn(owner(), 'Dzień dobry', runtime.FakeModelProvider([
        respond('Dzień dobry. W czym mogę pomóc?'),
    ]))
    assert opened['status'] == 'SUCCESS'
    actor = rbac.load_actor_context(rbac.AI_OWNER_ASSISTANT_ACTOR_ID,
                                    delegated_by_actor_id=rbac.BOOTSTRAP_OWNER_ACTOR_ID)
    pending = operations.execute_business_operation(actor, 'orders.status.transition', {
        'order_id': 20, 'target_status': 'confirmed', 'expected_version': 0,
        'idempotency_key': 'stream-order-approval',
    })
    assert pending.status == 'PENDING_APPROVAL'
    assert read_rows('SELECT status FROM orders WHERE id=20')[0][0] == 'new'
    return opened['conversation_id'], pending.approval_id


def assert_approved_order_committed(approval_id):
    execution = read_rows('SELECT status FROM internal_operation_executions WHERE approval_id=?', (approval_id,))
    assert len(execution) == 1 and execution[0]['status'] == 'SUCCESS'
    assert read_rows('SELECT status FROM orders WHERE id=20')[0][0] == 'confirmed'
    assert read_rows('SELECT status FROM internal_approval_requests WHERE approval_id=?', (approval_id,))[0][0] == 'CONSUMED'
    business_audit = read_rows("SELECT 1 FROM internal_audit_log WHERE operation='orders.status.transition' AND result='SUCCESS' AND entity_id='20'")
    assert len(business_audit) == 1


def test_approval_followup_stream_starts_after_commit_and_preserves_business_and_model_status():
    cid, approval_id = pending_order_approval()
    text = ''.join([
        'Zamówienie zostało potwierdzone i zmiana jest już zapisana. ',
        'Możesz przejść do kolejnego kroku.',
        '<speech_text mode="direct">Zamówienie potwierdzone.</speech_text>',
    ])
    def summarize_committed_write(_kwargs):
        assert_approved_order_committed(approval_id)
        return respond(text)
    provider = ScriptedStreamingProvider([summarize_committed_write])
    backend.AGENT_MODEL_PROVIDER = provider
    response = client().post(f'/api/internal/ai/approvals/{approval_id}/approve',
        json={'conversation_id': cid}, headers={'Accept': 'text/event-stream'}, buffered=False)
    try:
        assert response.mimetype == 'text/event-stream'
        # WSGI has sent turn_started, while its lazy model worker is not started.
        assert not provider.calls
        assert_approved_order_committed(approval_id)
        received = list(frames(response))
    finally:
        response.close()
    assert received[0][0] == 'turn_started'
    assert any(name == 'display_delta' for name, _ in received)
    assert received[-1][0] == 'done'
    final = received[-1][1]
    assert final['message'] == runtime._split_final_response(text)[0]
    assert [name for name, _ in provider.calls] == ['complete']
    assert final['status'] == final['model_status'] == 'SUCCESS'
    assert final['execution_outcome']['execution_status'] == 'SUCCESS'
    assert final['execution_outcome']['approval_status'] == 'CONSUMED'
    assert final['conversation_id'] == cid
    assert final['execution_outcome']['after']['status'] == 'confirmed'
    history = read_rows('SELECT assistant_text FROM internal_agent_turns WHERE conversation_id=? ORDER BY turn_id DESC', (cid,))
    assert history[0][0] == final['message']
    assert len(read_rows("SELECT 1 FROM internal_audit_log WHERE operation='agent.completed'")) == 2
    assert not read_rows('SELECT 1 FROM internal_agent_turn_leases')
    assert_approved_order_committed(approval_id)


def test_approval_final_synthesis_error_preserves_committed_success_without_reexecuting_write():
    cid, approval_id = pending_order_approval()
    def fail_after_committed_write(_kwargs):
        assert_approved_order_committed(approval_id)
        raise TimeoutError('Injected final synthesis timeout')
    provider = ScriptedStreamingProvider([fail_after_committed_write])
    backend.AGENT_MODEL_PROVIDER = provider
    response = client().post(f'/api/internal/ai/approvals/{approval_id}/approve',
        json={'conversation_id': cid}, headers={'Accept': 'text/event-stream'}, buffered=False)
    try:
        received = list(frames(response))
    finally:
        response.close()
    assert any(name == 'display_delta' for name, _ in received)
    assert received[-1][0] == 'done'
    final = received[-1][1]
    assert final['status'] == final['model_status'] == 'SUCCESS'
    assert final['confirmation_source'] == 'stored_execution'
    assert final['synthesis_status'] == 'FAILED'
    assert final['message'] == 'Gotowe. Zmiana statusu zamówienia została zapisana.'
    assert final['execution_outcome']['execution_status'] == 'SUCCESS'
    assert final['execution_outcome']['approval_status'] == 'CONSUMED'
    assert_approved_order_committed(approval_id)
    assert not read_rows("SELECT 1 FROM internal_audit_log WHERE operation='agent.failed'")
    assert len(read_rows("SELECT 1 FROM internal_audit_log WHERE operation='agent.completed'")) == 2
    history = read_rows('SELECT assistant_text FROM internal_agent_turns WHERE conversation_id=? ORDER BY turn_id DESC', (cid,))
    assert history[0][0] == final['message']
    assert not read_rows('SELECT 1 FROM internal_agent_turn_leases')
    assert [name for name, _ in provider.calls] == ['complete']


def test_streaming_kill_switch_preserves_single_call_json_fallback(monkeypatch):
    monkeypatch.setenv('AI_TEXT_STREAMING_ENABLED', '0')
    provider = ScriptedStreamingProvider([respond('Odpowiedź bez streamingu.')])
    response = post_stream(provider)
    try:
        assert response.status_code == 200 and response.mimetype == 'application/json'
        result = response.get_json()
    finally:
        response.close()
    assert result['status'] == 'SUCCESS'
    assert result['message'] == 'Odpowiedź bez streamingu.'
    assert 'stream_timings' not in result
    assert [name for name, _ in provider.calls] == ['complete']
    assert_finalized(result)
