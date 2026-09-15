"""Zero-extra-call regressions and controlled request-to-display benchmarks.

Both transports consume exactly the same scripted model responses. Every model
request costs 400 ms, including buffered planning/final responses; a streamed
request emits its first chunk at 80 ms and finishes at 400 ms. These timings are
not measurements of OpenAI, a browser renderer or Render. Set
AGENT_STREAMING_PARITY_RESULTS to append the observed HTTP client timings as
JSONL; fixtures create a fresh local SQLite database for every sample.
"""
import json
import os
import time
from pathlib import Path

import pytest

import agent_runtime as runtime
import app as backend
import business_operations as operations
import human_approval
from test_agent_runtime import isolated, owner, respond, tool
from test_agent_streaming import (
    assert_approved_order_committed, assert_finalized, client, frames,
    pending_order_approval, read_rows,
)


MODEL_REQUEST_MS = 400
FIRST_STREAM_CHUNK_MS = 80
SCENARIOS = (
    ('no_tool_normal_catalog', 1, False),
    ('no_tool_empty_catalog', 1, True),
    ('exact_read', 2, False),
    ('high_level_read', 2, True),
    ('green_parallel_batch', 2, True),
    ('sequential_two_reads', 3, False),
    ('fresh_read_write_proposal', 3, False),
    ('approval_button_summary', 1, False),
    ('chat_approval_summary', 2, False),
)


def private_plan(response):
    return runtime.ProviderResponse(
        text='INTERNAL_PLAN_DO_NOT_DISPLAY', tool_calls=response.tool_calls,
        model='fake-model', input_tokens=10, output_tokens=5)


class IdenticalMeasuredProvider:
    """Fails on regeneration instead of silently supplying another answer."""

    def __init__(self, plans, chunks, verify_final=None):
        self.script = [*plans, respond(''.join(chunks), input_tokens=10, output_tokens=5)]
        self.chunks = chunks
        self.calls = []
        self.verify_final = verify_final
        self.final_generations = 0

    def _take(self, method, kwargs):
        assert self.script, 'Unexpected regeneration: original final response already consumed'
        response = self.script.pop(0)
        self.calls.append({'method': method, 'tool_choice': kwargs['tool_choice'],
                           'tool_names': [item['name'] for item in kwargs['tools']]})
        if not response.tool_calls:
            self.final_generations += 1
            if self.verify_final:
                self.verify_final()
        return response

    def complete(self, **kwargs):
        response = self._take('complete', kwargs)
        time.sleep(MODEL_REQUEST_MS / 1000)
        return response

    def complete_stream(self, *, on_delta, cancelled=None, **kwargs):
        assert not kwargs['tools'] or kwargs['tool_choice'] == 'none'
        response = self._take('complete_stream', kwargs)
        assert not response.tool_calls, 'A tools-enabled pass must not stream'
        time.sleep(FIRST_STREAM_CHUNK_MS / 1000)
        on_delta(self.chunks[0])
        time.sleep((MODEL_REQUEST_MS - FIRST_STREAM_CHUNK_MS) / 1000)
        for chunk in self.chunks[1:]:
            on_delta(chunk)
        return response


def configure_scenario(scenario, monkeypatch):
    endpoint = '/api/internal/ai/chat'
    body = {'message': 'Pokaż Avery 160'}
    plans = []
    verify_final = None
    screen = 'Na magazynie mamy obecnie dwadzieścia cztery sztuki produktu Avery. To jest aktualny potwierdzony stan.'
    speech = 'Masz 24 sztuki Avery.'
    if scenario == 'no_tool_empty_catalog':
        monkeypatch.setattr(runtime, '_tool_descriptors', lambda *_: [])
    elif scenario == 'exact_read':
        plans = [tool('inventory.product.get', {'product_id': 1})]
    elif scenario == 'sequential_two_reads':
        plans = [tool('inventory.product.search', {'query': 'Avery 160'}, 'search'),
                 tool('inventory.product.get', {'product_id': 1}, 'get')]
    elif scenario == 'green_parallel_batch':
        plans = [runtime.ProviderResponse(tool_calls=(
            runtime.ToolCall('inventory', 'inventory.summary', '{}'),
            runtime.ToolCall('invoices', 'invoices.overdue', '{}'),
        ), model='fake-model')]
        body['message'] = 'Sprawdź stan magazynu i zaległe faktury.'
        screen = 'W magazynie są trzydzieści osiem sztuk, a jedna faktura oczekuje na płatność. To jest wynik obu odczytów.'
        speech = 'W magazynie jest 38 sztuk. Jedna faktura czeka na płatność.'
    elif scenario == 'high_level_read':
        monkeypatch.setenv('AGENT_HIGH_LEVEL_READ_MODELS_ENABLED', '1')
        plans = [tool('business.orders.state', {})]
        body['message'] = 'Które zamówienia są gotowe?'
        screen = 'Sprawdziłem aktualny stan zamówień i dostępne informacje o ich realizacji. Wynik znajduje się w podsumowaniu.'
        speech = 'Sprawdziłem aktualny stan zamówień.'
    elif scenario == 'fresh_read_write_proposal':
        db = backend.conn()
        try:
            db.execute("INSERT INTO orders(id,order_no,customer_name,status,created_at) VALUES(20,'PARITY-20','Test','new',?)",
                       (backend.now_iso(),))
            db.commit()
        finally:
            db.close()
        plans = [tool('orders.get', {'id': 20}, 'fresh-order'),
                 tool('orders.status.transition', {
                     'order_id': 20, 'target_status': 'confirmed', 'expected_version': 0,
                     'idempotency_key': 'parity-write-proposal',
                 }, 'propose-status')]
        body['message'] = 'Sprawdź zamówienie 20 i przygotuj zmianę jego statusu na potwierdzone.'
        screen = 'Przygotowałem zmianę statusu zamówienia, która nadal oczekuje na Twoje zatwierdzenie. Zmiana nie została jeszcze wykonana.'
        speech = 'Zmiana oczekuje na Twoje zatwierdzenie.'
        def verify_pending():
            assert read_rows('SELECT status FROM orders WHERE id=20')[0][0] == 'new'
            pending = read_rows("SELECT status FROM internal_operation_executions WHERE operation='orders.status.transition'")
            assert len(pending) == 1 and pending[0][0] == 'PENDING_APPROVAL'
        verify_final = verify_pending
    elif scenario in {'approval_button_summary', 'chat_approval_summary'}:
        cid, approval_id = pending_order_approval()
        verify_final = lambda: assert_approved_order_committed(approval_id)
        if scenario == 'approval_button_summary':
            endpoint = f'/api/internal/ai/approvals/{approval_id}/approve'
            body = {'conversation_id': cid}
        else:
            human_approval.bind(operations, approval_id, cid, owner(), 'parity-approval-setup')
            body = {'conversation_id': cid, 'message': 'Tak'}
            plans = [tool('approval.decide', {'approval_id': approval_id, 'decision': 'approve'})]
        screen = 'Zamówienie zostało potwierdzone i zmiana jest już zapisana w systemie. Możesz przejść do kolejnego kroku.'
        speech = 'Zamówienie zostało potwierdzone.'
    first, rest = screen.split('. ', 1)
    chunks = [first + '. ', rest, f'<speech_text mode="direct">{speech}</speech_text>']
    return endpoint, body, IdenticalMeasuredProvider([private_plan(p) for p in plans], chunks, verify_final), screen, speech


@pytest.mark.parametrize('repetition', range(3))
@pytest.mark.parametrize('enabled', [False, True], ids=['old_json', 'fixed_streaming'])
@pytest.mark.parametrize('scenario,expected_calls,streamable', SCENARIOS)
def test_model_call_parity_and_request_latency(
        scenario, expected_calls, streamable, enabled, repetition, monkeypatch):
    monkeypatch.setenv('AGENT_HIGH_LEVEL_READ_MODELS_ENABLED', '0')
    monkeypatch.setenv('AI_TEXT_STREAMING_ENABLED', '1' if enabled else '0')
    endpoint, body, provider, screen, speech = configure_scenario(scenario, monkeypatch)
    backend.AGENT_MODEL_PROVIDER = provider
    request_client = client()
    started = time.perf_counter()
    response = request_client.post(endpoint, json=body,
        headers={'Accept': 'text/event-stream'}, buffered=False)
    observed = {}
    events = []
    try:
        assert response.status_code == 200
        if enabled:
            assert response.mimetype == 'text/event-stream'
            payload = None
            for name, data in frames(response):
                observed.setdefault(name, round((time.perf_counter() - started) * 1000, 3))
                events.append((name, data))
                if name in {'done', 'error'}:
                    payload = data
            assert events[0][0] == 'turn_started'
            assert events[-1][0] == 'done', events[-1]
            assert [name for name, _ in events].count('done') == 1
            assert not any(name in {'error', 'speech_ready'} for name, _ in events)
        else:
            assert response.mimetype == 'application/json'
            payload = response.get_json()
            observed['done'] = round((time.perf_counter() - started) * 1000, 3)
    finally:
        response.close()

    assert payload.get('model_status', payload['status']) == 'SUCCESS', payload
    assert payload['message'] == screen
    assert payload['speech_text'] == speech
    assert len(provider.calls) == expected_calls, provider.calls
    assert len(provider.calls) <= expected_calls  # Same old-flow call budget for both transports.
    assert provider.final_generations == 1 and not provider.script
    streaming_calls = sum(call['method'] == 'complete_stream' for call in provider.calls)
    assert streaming_calls == int(enabled and streamable)
    # Intermediate text and tool arguments must not reach display text or the final answer.
    display = ''.join(data['delta'] for name, data in events if name == 'display_delta')
    assert not display or display == screen
    assert all(token not in display + payload['message']
               for token in ('INTERNAL_PLAN_DO_NOT_DISPLAY', 'product_id', 'target_status'))
    if enabled and streamable:
        assert 'display_delta' in observed
        assert observed['display_delta'] < observed['done']
    if scenario == 'approval_button_summary':
        # The unchanged approval endpoint wraps model metadata and does not
        # expose agent_run_id/correlation_id in its legacy JSON response.
        history = read_rows('SELECT assistant_text FROM internal_agent_turns WHERE conversation_id=? ORDER BY turn_id DESC',
                            (payload['conversation_id'],))
        assert history[0][0] == screen
        assert len(read_rows("SELECT 1 FROM internal_audit_log WHERE operation='agent.completed'")) == 2
        assert not read_rows('SELECT 1 FROM internal_agent_turn_leases')
    else:
        assert_finalized(payload)
    if provider.verify_final:
        provider.verify_final()

    first_event = 'first_display_delta' if 'display_delta' in observed else 'final_response_available'
    measurement = {
        'scenario': scenario, 'variant': 'fixed_streaming' if enabled else 'old_json',
        'repetition': repetition + 1, 'model_calls': len(provider.calls),
        'methods': [call['method'] for call in provider.calls],
        'display_mode': 'incremental' if streaming_calls else 'buffered',
        'request_start_ms': 0,
        'first_visible_event': first_event,
        'first_visible_ms': observed.get('display_delta', observed['done']),
        'done_ms': observed['done'],
        'model_request_ms': MODEL_REQUEST_MS, 'first_stream_chunk_ms': FIRST_STREAM_CHUNK_MS,
        'server_timings': payload.get('stream_timings'),
    }
    destination = os.getenv('AGENT_STREAMING_PARITY_RESULTS')
    if destination:
        with Path(destination).open('a', encoding='utf-8') as target:
            target.write(json.dumps(measurement, ensure_ascii=False) + '\n')
    print('STREAMING_ZERO_EXTRA_CALLS ' + json.dumps(measurement, ensure_ascii=False))
