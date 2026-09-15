import io
import json
import logging
from html.parser import HTMLParser

import pytest

import agent_runtime as runtime
import app as backend
import business_operations as operations
import voice_io
from test_agent_runtime import isolated, owner, respond, tool
from voice_io import SynthesizedAudio, VoiceIOError


class FakeVoiceProvider:
    def __init__(self, transcript='Sprawdź zamówienie.', *, stt_error=False, tts_error=False):
        self.transcript = transcript
        self.stt_error = stt_error
        self.tts_error = tts_error
        self.transcriptions = []
        self.speeches = []

    def transcribe(self, audio, *, filename, content_type):
        self.transcriptions.append((audio, filename, content_type))
        if self.stt_error:
            raise VoiceIOError('stt failed')
        return self.transcript

    def synthesize(self, text):
        self.speeches.append(text)
        if self.tts_error:
            raise VoiceIOError('tts failed')
        return SynthesizedAudio(b'fake-mp3', 'audio/mpeg')


def client():
    result = backend.app.test_client()
    with result.session_transaction() as session:
        session['admin_authenticated'] = True
        session['csrf_token'] = 'voice-csrf'
    return result


def transcribe(test_client):
    return test_client.post('/api/internal/ai/voice/transcribe',
        data={'audio': (io.BytesIO(b'webm-audio'), 'recording.webm', 'audio/webm')},
        headers={'X-CSRF-Token': 'voice-csrf'})


def test_rendered_assistant_supplies_csrf_for_actual_audio_upload(isolated, monkeypatch):
    class AssistantFormParser(HTMLParser):
        in_form = False
        csrf = ''

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == 'form':
                self.in_form = attrs.get('id') == 'aiForm'
            if self.in_form and tag == 'input' and attrs.get('name') == 'csrf_token':
                self.csrf = attrs.get('value', '')

        def handle_endtag(self, tag):
            if tag == 'form':
                self.in_form = False

    voice = FakeVoiceProvider('Jakie mam zaległe faktury?')
    monkeypatch.setattr(backend, 'VOICE_IO_PROVIDER', voice)
    test_client = client()
    page = test_client.get('/ai-assistant')
    assert page.status_code == 200
    form = AssistantFormParser()
    form.feed(page.get_data(as_text=True))
    uploaded = test_client.post('/api/internal/ai/voice/transcribe',
        data={'audio': (io.BytesIO(b'webm-audio'), 'recording.webm', 'audio/webm;codecs=opus')},
        headers={'X-CSRF-Token': form.csrf})
    assert uploaded.status_code == 200, uploaded.get_data(as_text=True)
    assert form.csrf == 'voice-csrf'
    assert uploaded.get_json() == {'ok': True, 'text': voice.transcript}
    assert voice.transcriptions == [(b'webm-audio', 'recording.webm', 'audio/webm')]


def test_ptt_stt_existing_conversation_chat_speech_text_and_tts(isolated, monkeypatch):
    voice = FakeVoiceProvider('Sprawdź zamówienie.')
    monkeypatch.setattr(backend, 'VOICE_IO_PROVIDER', voice)
    backend.AGENT_MODEL_PROVIDER = runtime.FakeModelProvider([
        respond('Rozmowa rozpoczęta.'),
        respond('Zamówienie jest gotowe. Etykieta jest gotowa do druku. Dodatkowe szczegóły są na ekranie.'),
    ])
    test_client = client()
    opened = test_client.post('/api/internal/ai/chat', json={'message': 'Cześć'}).get_json()

    stt = transcribe(test_client)
    assert stt.status_code == 200 and stt.get_json()['text'] == 'Sprawdź zamówienie.'
    answer_response = test_client.post('/api/internal/ai/chat', json={
        'message': stt.get_json()['text'], 'conversation_id': opened['conversation_id'],
    })
    answer = answer_response.get_json()
    assert answer_response.status_code == 200
    assert answer['conversation_id'] == opened['conversation_id']
    assert answer['message'].endswith('Dodatkowe szczegóły są na ekranie.')
    assert answer['speech_text'] == 'Zamówienie jest gotowe. Etykieta jest gotowa do druku.'

    tts = test_client.post('/api/internal/ai/voice/synthesize', json={'speech_text': answer['speech_text']})
    assert tts.status_code == 200 and tts.data == b'fake-mp3'
    assert voice.speeches == [answer['speech_text']]


def test_voice_approval_uses_existing_approval_decide(isolated, monkeypatch):
    voice = FakeVoiceProvider('zatwierdzam')
    monkeypatch.setattr(backend, 'VOICE_IO_PROVIDER', voice)
    db = backend.conn()
    version = operations._order_version(db, 10)
    db.close()
    backend.AGENT_MODEL_PROVIDER = runtime.FakeModelProvider([
        tool('orders.status.transition', {
            'order_id': 10, 'target_status': 'packed', 'expected_version': version,
            'idempotency_key': 'voice-approval-pending',
        }),
        respond('Zmiana wymaga zatwierdzenia.'),
    ])
    test_client = client()
    pending_response = test_client.post('/api/internal/ai/chat', json={'message': 'Oznacz jako spakowane.'})
    pending = pending_response.get_json()
    approval_id = pending['pending_approvals'][0]['approval_id']

    transcript = transcribe(test_client).get_json()['text']
    backend.AGENT_MODEL_PROVIDER = runtime.FakeModelProvider([
        tool('approval.decide', {'approval_id': approval_id, 'decision': 'approve'}),
        respond('Zamówienie oznaczono jako spakowane.'),
    ])
    approved_response = test_client.post('/api/internal/ai/chat', json={
        'message': transcript, 'conversation_id': pending['conversation_id'],
    })
    approved = approved_response.get_json()
    assert approved_response.status_code == 200
    assert approved['conversation_id'] == pending['conversation_id']
    assert approved['decisions'][0]['approval_id'] == approval_id
    db = backend.conn()
    assert db.execute('SELECT status FROM orders WHERE id=10').fetchone()[0] == 'packed'
    db.close()


def test_stt_failure_does_not_block_normal_chat(isolated, monkeypatch):
    monkeypatch.setattr(backend, 'VOICE_IO_PROVIDER', FakeVoiceProvider(stt_error=True))
    test_client = client()
    failed = transcribe(test_client)
    assert failed.status_code == 503 and failed.get_json()['error_code'] == 'STT_FAILED'

    backend.AGENT_MODEL_PROVIDER = runtime.FakeModelProvider([respond('Chat działa normalnie.')])
    chat = test_client.post('/api/internal/ai/chat', json={'message': 'Napisana wiadomość'})
    assert chat.status_code == 200 and chat.get_json()['message'] == 'Chat działa normalnie.'


def test_tts_failure_keeps_text_response_available(isolated, monkeypatch):
    monkeypatch.setattr(backend, 'VOICE_IO_PROVIDER', FakeVoiceProvider(tts_error=True))
    backend.AGENT_MODEL_PROVIDER = runtime.FakeModelProvider([respond('Pełna odpowiedź pozostaje na ekranie.')])
    test_client = client()
    chat = test_client.post('/api/internal/ai/chat', json={'message': 'Odpowiedz'}).get_json()
    tts = test_client.post('/api/internal/ai/voice/synthesize', json={'speech_text': chat['speech_text']})
    assert chat['message'] == 'Pełna odpowiedź pozostaje na ekranie.'
    assert tts.status_code == 503 and tts.get_json()['error_code'] == 'TTS_FAILED'


def test_voice_and_text_have_identical_permission_deny(isolated, monkeypatch):
    monkeypatch.setattr(backend, 'VOICE_IO_PROVIDER', FakeVoiceProvider())
    db = backend.conn()
    db.execute("UPDATE internal_role_permissions SET decision='DENY' WHERE role_key='OWNER' AND permission_key='inventory.read'")
    db.commit()
    db.close()
    test_client = client()
    text_deny = test_client.post('/api/internal/ai/chat', json={'message': 'test'})
    voice_deny = transcribe(test_client)
    assert text_deny.status_code == voice_deny.status_code == 403
    assert text_deny.get_json() == voice_deny.get_json()


@pytest.mark.parametrize('mime,filename', [
    ('audio/webm;codecs=opus', 'recording.webm'),
    ('audio/webm', 'recording.webm'),
    ('audio/mp4', 'recording.m4a'),
])
def test_stt_accepts_browser_audio_formats(isolated, monkeypatch, mime, filename):
    voice = FakeVoiceProvider('Jakie mam zaległe faktury?')
    monkeypatch.setattr(backend, 'VOICE_IO_PROVIDER', voice)
    response = client().post('/api/internal/ai/voice/transcribe',
        data={'audio': (io.BytesIO(b'audio-bytes'), filename, mime)},
        headers={'X-CSRF-Token': 'voice-csrf'})
    assert response.status_code == 200
    assert response.get_json() == {'ok': True, 'text': voice.transcript}
    assert voice.transcriptions == [(b'audio-bytes', filename, mime.split(';')[0])]


@pytest.mark.parametrize('kind,expected_code', [
    ('missing', 'MISSING_AUDIO'),
    ('empty', 'EMPTY_AUDIO'),
    ('unsupported', 'UNSUPPORTED_AUDIO_TYPE'),
    ('oversized', 'AUDIO_TOO_LARGE'),
])
def test_invalid_audio_never_reaches_stt(isolated, monkeypatch, kind, expected_code):
    voice = FakeVoiceProvider()
    monkeypatch.setattr(backend, 'VOICE_IO_PROVIDER', voice)
    monkeypatch.setattr(backend, 'MAX_AUDIO_BYTES', 16)
    audio = b'' if kind == 'empty' else b'x' * 17 if kind == 'oversized' else b'audio'
    mime = 'text/plain' if kind == 'unsupported' else 'audio/webm'
    data = {} if kind == 'missing' else {'audio': (io.BytesIO(audio), 'recording.webm', mime)}
    response = client().post('/api/internal/ai/voice/transcribe', data=data,
        headers={'X-CSRF-Token': 'voice-csrf'})
    assert response.status_code == 400
    assert response.get_json()['error_code'] == expected_code
    assert voice.transcriptions == []


def test_stt_requires_authenticated_session(isolated, monkeypatch):
    voice = FakeVoiceProvider()
    monkeypatch.setattr(backend, 'VOICE_IO_PROVIDER', voice)
    response = transcribe(backend.app.test_client())
    assert response.status_code == 401
    assert voice.transcriptions == []


def test_stt_still_rejects_missing_csrf(isolated, monkeypatch):
    voice = FakeVoiceProvider()
    monkeypatch.setattr(backend, 'VOICE_IO_PROVIDER', voice)
    response = client().post('/api/internal/ai/voice/transcribe',
        data={'audio': (io.BytesIO(b'audio'), 'recording.webm', 'audio/webm')})
    assert response.status_code == 403
    assert voice.transcriptions == []


class StubTranscriptionResponse:
    def __init__(self, payload=None, *, status=200, json_error=False):
        self.payload = payload
        self.status_code = status
        self.json_error = json_error

    def raise_for_status(self):
        if self.status_code >= 400:
            raise voice_io.requests.HTTPError('secret-provider-response')

    def json(self):
        if self.json_error:
            raise ValueError('secret-invalid-json')
        return self.payload


def test_stt_adapter_sends_multipart_and_returns_only_transcript(isolated, monkeypatch):
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return StubTranscriptionResponse({'text': '  Jakie mam zaległe faktury?  '})

    monkeypatch.setattr(voice_io.requests, 'post', post)
    provider = voice_io.OpenAIVoiceIOProvider(api_key='test-secret', stt_model='test-stt-model')
    transcript = provider.transcribe(b'audio-bytes', filename='recording.webm', content_type='audio/webm')
    assert transcript == 'Jakie mam zaległe faktury?'
    assert len(calls) == 1
    url, request = calls[0]
    assert url == 'https://api.openai.com/v1/audio/transcriptions'
    assert request['files'] == {'file': ('recording.webm', b'audio-bytes', 'audio/webm')}
    assert request['data'] == {'model': 'test-stt-model'}
    assert request['headers']['Authorization'] == 'Bearer test-secret'


@pytest.mark.parametrize('failure,expected_code', [
    ('config', 'VOICE_NOT_CONFIGURED'),
    ('timeout', 'STT_TIMEOUT'),
    ('network', 'STT_NETWORK_ERROR'),
    ('http', 'STT_PROVIDER_HTTP_ERROR'),
    ('invalid_json', 'STT_INVALID_RESPONSE'),
    ('non_object', 'STT_INVALID_RESPONSE'),
    ('non_string', 'STT_INVALID_RESPONSE'),
    ('empty_text', 'STT_EMPTY_TRANSCRIPT'),
])
def test_stt_provider_failures_have_safe_diagnostics(isolated, monkeypatch, caplog, failure, expected_code):
    secret = 'secret-provider-response-audio-transcript'

    def post(*args, **kwargs):
        if failure == 'timeout':
            raise voice_io.requests.Timeout(secret)
        if failure == 'network':
            raise voice_io.requests.ConnectionError(secret)
        if failure == 'http':
            return StubTranscriptionResponse({'error': secret}, status=429)
        if failure == 'invalid_json':
            return StubTranscriptionResponse(json_error=True)
        if failure == 'non_object':
            return StubTranscriptionResponse([secret])
        if failure == 'non_string':
            return StubTranscriptionResponse({'text': {'secret': secret}})
        if failure == 'empty_text':
            return StubTranscriptionResponse({'text': '   '})
        raise AssertionError('Missing configuration must not make a provider request')

    monkeypatch.setattr(voice_io.requests, 'post', post)
    monkeypatch.setattr(backend, 'VOICE_IO_PROVIDER', None)
    if failure == 'config':
        monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    else:
        monkeypatch.setenv('OPENAI_API_KEY', secret)
    with caplog.at_level(logging.INFO):
        response = transcribe(client())
    assert response.status_code == 503
    assert response.get_json()['error_code'] == expected_code
    events = [record.getMessage() for record in caplog.records if record.getMessage().startswith('VOICE_STT_')]
    assert any(event.startswith('VOICE_STT_REQUEST_START ') for event in events)
    failures = [json.loads(event.split(' ', 1)[1]) for event in events if event.startswith('VOICE_STT_ERROR ')]
    assert len(failures) == 1
    assert failures[0]['error_code'] == expected_code
    assert failures[0]['stage']
    assert failures[0]['latency_ms'] >= 0
    assert failures[0]['http_status'] == 503
    if failure == 'http':
        assert failures[0]['provider_http_status'] == 429
    allowed = {'stage', 'mime_type', 'blob_size', 'duration_ms', 'http_status', 'latency_ms', 'error_code', 'provider_http_status'}
    for event in events:
        assert set(json.loads(event.split(' ', 1)[1])) <= allowed
    assert secret not in '\n'.join(events)
    assert 'webm-audio' not in '\n'.join(events)


def test_successful_stt_diagnostics_exclude_transcript_and_audio(isolated, monkeypatch, caplog):
    transcript = 'Poufna treść transkrypcji klienta.'
    monkeypatch.setattr(backend, 'VOICE_IO_PROVIDER', FakeVoiceProvider(transcript))
    with caplog.at_level(logging.INFO):
        response = transcribe(client())
    assert response.get_json()['text'] == transcript
    events = [record.getMessage() for record in caplog.records if record.getMessage().startswith('VOICE_STT_')]
    assert len([event for event in events if event.startswith('VOICE_STT_REQUEST_START ')]) == 1
    completed = [json.loads(event.split(' ', 1)[1]) for event in events if event.startswith('VOICE_STT_RESPONSE ')]
    assert len(completed) == 1
    assert completed[0]['http_status'] == 200
    assert completed[0]['blob_size'] == len(b'webm-audio')
    assert completed[0]['mime_type'] == 'audio/webm'
    assert transcript not in '\n'.join(events)
    assert 'webm-audio' not in '\n'.join(events)


def test_voice_cerne_count_keeps_stock_until_existing_human_approval(isolated, monkeypatch):
    transcript = 'mam na półce Cerne 128 BB jedną sztukę'
    monkeypatch.setattr(backend, 'VOICE_IO_PROVIDER', FakeVoiceProvider(transcript))
    db = backend.conn()
    try:
        db.execute('INSERT INTO products(id,sku,model,name,archived,created_at) VALUES(905,?,?,?,?,?)',
            ('VOICE-CERNE-128-BB', 'Cerne 128 BB', 'Cerne 128 BB', 0, backend.now_iso()))
        db.execute('INSERT INTO stock(product_id,qty) VALUES(905,3)')
        db.commit()
    finally:
        db.close()

    test_client = client()
    backend.AGENT_MODEL_PROVIDER = runtime.FakeModelProvider([
        tool('inventory.count.session.start', {}), respond('Remanent rozpoczęty.'),
    ])
    opened_response = test_client.post('/api/internal/ai/chat', json={'message': 'Robimy remanent'})
    assert opened_response.status_code == 200
    conversation_id = opened_response.get_json()['conversation_id']

    stt = transcribe(test_client)
    assert stt.status_code == 200 and stt.get_json()['text'] == transcript
    count_args = {'product_id': 905, 'counted_quantity': 1, 'expected_version': 0,
                  'idempotency_key': 'voice-count-cerne'}
    count_provider = runtime.FakeModelProvider([
        runtime.ProviderResponse(tool_calls=(
            runtime.ToolCall('product', 'inventory.product.get', json.dumps({'product_id': 905})),
            runtime.ToolCall('expected', 'inventory.count.get_expected', json.dumps({'product_id': 905})),
            runtime.ToolCall('count', 'inventory.count.record', json.dumps(count_args)),
        ), model='fake-model'),
        respond('Cerne 128 BB. System: 3. Policzono: 1. Różnica: -2. Skorygować stan do 1?'),
    ])
    backend.AGENT_MODEL_PROVIDER = count_provider
    counted_response = test_client.post('/api/internal/ai/chat', json={
        'message': stt.get_json()['text'], 'conversation_id': conversation_id,
    })
    counted = counted_response.get_json()
    assert counted_response.status_code == 200
    assert counted['conversation_id'] == conversation_id
    assert any(item.get('role') == 'user' and item.get('content') == transcript
               for item in count_provider.calls[0]['input_items'])
    assert counted['approvals'] == []
    count_card = next(item for item in counted['artifacts'] if item['type'] == 'inventory_count_card')
    assert (count_card['expected_quantity'], count_card['counted_quantity'], count_card['difference']) == (3, 1, -2)

    backend.AGENT_MODEL_PROVIDER = runtime.FakeModelProvider([
        tool('inventory.adjust', {'product_id': 905, 'expected_version': 0,
                                  'idempotency_key': 'voice-adjust-cerne'}),
        respond('Korekta czeka na zatwierdzenie przez człowieka.'),
    ])
    pending_response = test_client.post('/api/internal/ai/chat', json={
        'message': 'Tak, przygotuj korektę', 'conversation_id': conversation_id,
    })
    assert pending_response.status_code == 200
    approval = pending_response.get_json()['approvals'][0]
    assert approval['operation'] == 'inventory.adjust'
    assert (approval['from_quantity'], approval['to_quantity']) == (3, 1)
    db = backend.conn()
    try:
        assert db.execute('SELECT qty FROM stock WHERE product_id=905').fetchone()[0] == 3
    finally:
        db.close()

    backend.AGENT_MODEL_PROVIDER = runtime.FakeModelProvider([respond('Stan skorygowany do 1 sztuki.')])
    approved = test_client.post(f"/api/internal/ai/approvals/{approval['approval_id']}/approve",
        json={'conversation_id': conversation_id})
    assert approved.status_code == 200 and approved.get_json()['status'] == 'SUCCESS'
    db = backend.conn()
    try:
        assert db.execute('SELECT qty FROM stock WHERE product_id=905').fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM internal_audit_log WHERE operation='inventory.adjust' AND result='SUCCESS'").fetchone()[0] == 1
    finally:
        db.close()
