import io
import json
import logging
import os
from html.parser import HTMLParser
from dataclasses import replace

import pytest

import agent_runtime as runtime
import app as backend
import business_operations as operations
import voice_io
import voice_debug
from test_agent_runtime import isolated, owner, respond, tool
from voice_io import SynthesizedAudio, VoiceIOError


@pytest.fixture(autouse=True)
def isolated_voice_debug(tmp_path, monkeypatch):
    monkeypatch.setattr(voice_debug, 'DEBUG_AUDIO_DIR', tmp_path / 'private-voice-debug')
    monkeypatch.setattr(voice_debug, '_start_janitor', lambda: None)
    monkeypatch.delenv('VOICE_DEBUG_SAVE_AUDIO', raising=False)
    monkeypatch.delenv('AI_STT_MODEL', raising=False)


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
            raise VoiceIOError('tts failed', error_code='TTS_FAILED', stage='provider')
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
        respond('Rozmowa rozpoczęta.\n<speech_text>Rozmowa rozpoczęta.</speech_text>'),
        respond('Zamówienie jest gotowe. Etykieta jest gotowa do druku. Dodatkowe szczegóły są na ekranie.\n'
                '<speech_text>Zamówienie jest gotowe, a etykieta czeka na druk.</speech_text>'),
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
    assert answer['speech_text'] == 'Zamówienie jest gotowe, a etykieta czeka na druk.'

    tts = test_client.post('/api/internal/ai/voice/synthesize', json={'speech_text': answer['speech_text']})
    assert tts.status_code == 200 and tts.data == b'fake-mp3'
    assert tts.content_type == 'audio/mpeg' and len(tts.data) > 0
    assert tts.content_length == len(b'fake-mp3')
    assert voice.speeches == [answer['speech_text']]


def test_voice_speech_text_keeps_ui_detail_but_compacts_spoken_result(isolated):
    provider = runtime.FakeModelProvider([respond(
        'Masz 7 aktywnych zamówień. Najnowsze ZAM-2609141 wymaga uzupełnienia. '
        'Pozostałe zamówienia i szczegółowe pozycje są widoczne na ekranie.\n'
        '<speech_text>Masz 7 aktywnych zamówień. Jedno z nich wymaga uzupełnienia.</speech_text>'
    )])
    backend.AGENT_MODEL_PROVIDER = provider
    response = client().post('/api/internal/ai/chat', json={'message': 'Mam jakieś nowe zamówienia?'})
    payload = response.get_json()
    assert response.status_code == 200
    assert 'ZAM-2609141' in payload['message']
    assert 'ZAM-2609141' not in payload['speech_text']
    assert payload['speech_text'] == 'Masz 7 aktywnych zamówień. Jedno z nich wymaga uzupełnienia.'
    assert len(payload['speech_text']) < len(payload['message'])
    assert payload['speech_text'].count('.') <= 2
    assert len(provider.calls) == 1
    assert '<speech_text>' in provider.calls[0]['instructions']
    assert 'Nie przepisuj pierwszych zdań odpowiedzi ekranowej' in provider.calls[0]['instructions']
    db = backend.conn()
    stored_answer = db.execute(
        'SELECT assistant_text FROM internal_agent_turns WHERE conversation_id=?',
        (payload['conversation_id'],),
    ).fetchone()['assistant_text']
    db.close()
    assert stored_answer == payload['message']
    assert '<speech_text>' not in stored_answer


def test_daily_summary_speech_uses_short_section_highlights(isolated):
    full = (
        '1. Pilne wysyłki\n- MAGMAR — 13 szt., kompletne.\n'
        '2. Płatności po terminie\n- Brak.\n'
        '3. Braki wymagające działania\n- Winsor — 13 szt.\n- Aosta — 2 szt.\n'
        '4. Pozostałe ważne rzeczy\n- Szczegóły na ekranie.'
    )
    backend.AGENT_MODEL_PROVIDER = runtime.FakeModelProvider([respond(
        full + '\n<speech_text>Na dziś najważniejsze: wyślij MAGMAR. Nie masz zaległych płatności. '
        'Do uzupełnienia zostało 15 uchwytów.</speech_text>'
    )])
    response = client().post('/api/internal/ai/chat', json={'message': 'Co mam dziś do zrobienia?'})
    payload = response.get_json()
    assert response.status_code == 200 and payload['message'] == full
    assert payload['speech_text'] == (
        'Na dziś najważniejsze: wyślij MAGMAR. Nie masz zaległych płatności. '
        'Do uzupełnienia zostało 15 uchwytów.'
    )
    assert 'Winsor' not in payload['speech_text'] and 'Aosta' not in payload['speech_text']


def test_explicit_request_allows_fuller_spoken_numbers(isolated):
    full = 'Zamówienia: ZAM-2609141. ZAM-2609142. ZAM-2609143.'
    backend.AGENT_MODEL_PROVIDER = runtime.FakeModelProvider([respond(
        full + '\n<speech_text>' + full + '</speech_text>'
    )])
    payload = client().post('/api/internal/ai/chat', json={
        'message': 'Podaj wszystkie numery zamówień.'
    }).get_json()
    assert payload['speech_text'] == full


@pytest.mark.parametrize(('question', 'full', 'spoken'), [
    (
        'Ile mam Winsor 128 BB?',
        'Winsor 128 BB\nSKU: WIN-128-BB\nMAGAZYN: 47\nZAMÓWIONE: 8\nDOSTĘPNE: 39',
        'Masz 47 sztuk Winsor 128 BB na magazynie.',
    ),
    (
        'Czy ktoś zalega z płatnością?',
        'Płatności po terminie\nLiczba faktur: 0\nŁączna zaległość: 0 zł.',
        'Nie, obecnie nie masz faktur po terminie.',
    ),
    (
        'Jakie mam braki?',
        'Braki wymagające działania\nWinsor: 1\nSam: 5\nHugo: 7\nRazem: 13 sztuk.',
        'Masz 13 sztuk niepokrytych braków: Winsor 1, Sam 5 i Hugo 7.',
    ),
])
def test_same_final_model_turn_supplies_intent_aware_speech_text(isolated, question, full, spoken):
    provider = runtime.FakeModelProvider([respond(
        f'{full}\n<speech_text>{spoken}</speech_text>'
    )])
    backend.AGENT_MODEL_PROVIDER = provider
    payload = client().post('/api/internal/ai/chat', json={'message': question}).get_json()
    assert payload['message'] == full
    assert payload['speech_text'] == spoken
    assert len(provider.calls) == 1
    assert all(label not in payload['speech_text'] for label in ('SKU:', 'MAGAZYN:', 'ZAMÓWIONE:', 'DOSTĘPNE:'))


def test_spoken_summary_omits_tables_urls_json_and_internal_identifiers():
    spoken = voice_io.compact_speech_text(
        'Podsumowanie jest gotowe.\n'
        '| SKU | Ilość |\n|---|---|\n| CH010-BB-128168 | 7 |\n'
        '{"operation_id":"private-value"}\n'
        'Szczegóły: https://example.invalid/private',
        user_message='Podsumuj wynik.',
    )
    assert spoken == 'Podsumowanie jest gotowe.'
    assert all(value not in spoken for value in ('CH010', 'operation_id', 'http', '{', '|'))


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


def test_tts_success_and_failure_logs_are_timed_without_speech_content(isolated, monkeypatch, caplog):
    phrase = 'Poufna odpowiedź głosowa nie może trafić do logu.'
    voice = FakeVoiceProvider()
    monkeypatch.setattr(backend, 'VOICE_IO_PROVIDER', voice)
    caplog.set_level(logging.INFO)
    success = client().post('/api/internal/ai/voice/synthesize', json={'speech_text': phrase})
    assert success.status_code == 200
    request_log = next(record.message for record in caplog.records if 'VOICE_TTS_REQUEST_START' in record.message)
    success_log = next(record.message for record in caplog.records if 'VOICE_TTS_RESPONSE' in record.message)
    assert phrase not in request_log and '"chars":' in request_log
    assert phrase not in success_log and '"duration_ms":' in success_log
    assert '"bytes": 8' in success_log and '"content_type": "audio/mpeg"' in success_log

    caplog.clear()
    monkeypatch.setattr(backend, 'VOICE_IO_PROVIDER', FakeVoiceProvider(tts_error=True))
    failed = client().post('/api/internal/ai/voice/synthesize', json={'speech_text': phrase})
    assert failed.status_code == 503
    error_log = next(record.message for record in caplog.records if 'VOICE_TTS_ERROR' in record.message)
    assert phrase not in error_log and '"error_code": "TTS_FAILED"' in error_log
    assert '"exception_type": "VoiceIOError"' in error_log and '"message": "tts failed"' in error_log


def test_successful_chat_logs_only_speech_presence_and_length(isolated, caplog):
    backend.AGENT_MODEL_PROVIDER = runtime.FakeModelProvider([respond('Krótka odpowiedź głosowa.')])
    caplog.set_level(logging.INFO)
    response = client().post('/api/internal/ai/chat', json={'message': 'Poufne pytanie klienta'})
    assert response.status_code == 200 and response.get_json()['speech_text']
    entry = next(record.message for record in caplog.records if 'VOICE_SPEECH_TEXT_READY' in record.message)
    assert '"present": true' in entry and '"chars":' in entry
    assert 'Poufne pytanie klienta' not in entry
    assert 'Krótka odpowiedź głosowa' not in entry


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


def test_tts_adapter_uses_polish_instructions_and_normalizes_only_provider_input(isolated, monkeypatch):
    calls = []
    response = StubTranscriptionResponse(status=200)
    response.content = b'provider-mp3'

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return response

    monkeypatch.setattr(voice_io.requests, 'post', post)
    provider = voice_io.OpenAIVoiceIOProvider(api_key='test-secret')
    visible_speech_text = 'Masz BB, BN, MB i BLK. Kod SKU-128 pozostaje techniczny.'
    audio = provider.synthesize(visible_speech_text)

    assert calls[0][0] == 'https://api.openai.com/v1/audio/speech'
    assert calls[0][1]['json'] == {
        'model': 'gpt-4o-mini-tts', 'voice': 'marin',
        'input': 'Masz be be, be en, em be i be el ka. Kod SKU-128 pozostaje techniczny.',
        'response_format': 'mp3', 'instructions': voice_io.TTS_INSTRUCTIONS,
    }
    assert visible_speech_text == 'Masz BB, BN, MB i BLK. Kod SKU-128 pozostaje techniczny.'
    assert audio.content == b'provider-mp3' and audio.content_type == 'audio/mpeg'


def test_tts_legacy_models_do_not_receive_unsupported_instructions(isolated, monkeypatch):
    calls = []
    response = StubTranscriptionResponse(status=200)
    response.content = b'provider-mp3'
    monkeypatch.setattr(voice_io.requests, 'post', lambda url, **kwargs: calls.append((url, kwargs)) or response)
    provider = voice_io.OpenAIVoiceIOProvider(
        api_key='test-secret', tts_model='tts-1', voice='alloy',
    )
    provider.synthesize('BB')
    assert calls[0][1]['json'] == {
        'model': 'tts-1', 'voice': 'alloy', 'input': 'be be', 'response_format': 'mp3',
    }


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
    assert request['data'] == {
        'model': 'test-stt-model',
        'language': 'pl',
        'prompt': voice_io.STT_CONTEXT_PROMPT,
    }
    assert 'polsku' in request['data']['prompt']
    assert 'nazwy produktów i firm' in request['data']['prompt']
    assert 'numery zamówień' in request['data']['prompt']
    assert 'kody SKU' in request['data']['prompt']
    assert request['headers']['Authorization'] == 'Bearer test-secret'


@pytest.mark.parametrize('transcript', [
    'co mogę zrobić dzisiaj?',
    'jakie mam zaległe faktury?',
    'mam na półce Cerne 128 BB jedną sztukę',
    'sprawdź CH010-BB-128168',
    'sprawdź zamówienie ZAM-2609141',
])
def test_stt_adapter_preserves_polish_business_transcript(isolated, monkeypatch, transcript):
    monkeypatch.setattr(voice_io.requests, 'post',
                        lambda *args, **kwargs: StubTranscriptionResponse({'text': transcript}))
    provider = voice_io.OpenAIVoiceIOProvider(api_key='test-secret')
    assert provider.transcribe(b'audio', filename='recording.webm',
                               content_type='audio/webm') == transcript


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
    allowed = {'stage', 'mime_type', 'blob_size', 'duration_ms', 'http_status', 'latency_ms',
               'error_code', 'provider_http_status', 'language', 'stt_model', 'request_id',
               'capture_duration_ms', 'file_extension', 'configured_stt_model', 'provider_latency_ms'}
    for event in events:
        payload = json.loads(event.split(' ', 1)[1])
        assert set(payload) <= allowed
        assert payload['language'] == 'pl'
        assert payload['stt_model'] == voice_io.DEFAULT_STT_MODEL
    assert secret not in '\n'.join(events)
    assert 'webm-audio' not in '\n'.join(events)


def test_successful_stt_diagnostics_exclude_transcript_and_audio(isolated, monkeypatch, caplog):
    transcript = 'Poufna treść transkrypcji klienta.'
    monkeypatch.setattr(backend, 'VOICE_IO_PROVIDER', voice_io.OpenAIVoiceIOProvider(api_key='test-secret'))
    monkeypatch.setattr(voice_io.requests, 'post',
                        lambda *a, **k: StubTranscriptionResponse({'text': transcript}))
    with caplog.at_level(logging.INFO):
        response = transcribe(client())
    assert response.get_json()['text'] == transcript
    events = [record.getMessage() for record in caplog.records if record.getMessage().startswith('VOICE_STT_')]
    assert len([event for event in events if event.startswith('VOICE_STT_REQUEST_START ')]) == 1
    completed = [json.loads(event.split(' ', 1)[1]) for event in events if event.startswith('VOICE_STT_RESPONSE ')]
    assert len(completed) == 1
    assert completed[0]['http_status'] == 200
    assert completed[0]['provider_http_status'] == 200
    assert completed[0]['language'] == 'pl'
    assert completed[0]['stt_model'] == voice_io.DEFAULT_STT_MODEL
    assert completed[0]['blob_size'] == len(b'webm-audio')
    assert completed[0]['mime_type'] == 'audio/webm'
    assert transcript not in '\n'.join(events)
    assert 'webm-audio' not in '\n'.join(events)


@pytest.mark.parametrize('flag', [None, '0', 'true', 'unexpected'])
def test_debug_audio_disabled_creates_no_files(isolated, monkeypatch, flag):
    if flag is not None:
        monkeypatch.setenv('VOICE_DEBUG_SAVE_AUDIO', flag)
    monkeypatch.setattr(backend, 'VOICE_IO_PROVIDER', FakeVoiceProvider())
    assert transcribe(client()).status_code == 200
    assert not voice_debug.DEBUG_AUDIO_DIR.exists()


@pytest.mark.parametrize('mime,extension', [('audio/webm;codecs=opus', '.webm'), ('audio/mp4', '.mp4')])
def test_debug_audio_admin_saves_exact_blob_and_logs_id(isolated, monkeypatch, caplog, mime, extension):
    monkeypatch.setenv('VOICE_DEBUG_SAVE_AUDIO', '1')
    transcript = 'Poufna transkrypcja'
    voice = FakeVoiceProvider(transcript)
    monkeypatch.setattr(backend, 'VOICE_IO_PROVIDER', voice)
    payload = b'private audio bytes\x00\xff\r\n'
    with caplog.at_level(logging.INFO):
        response = client().post('/api/internal/ai/voice/transcribe', data={
            'audio': (io.BytesIO(payload), 'customer-secret.wrong', mime),
            'capture_duration_ms': '2300'}, headers={'X-CSRF-Token': 'voice-csrf'})
    assert response.status_code == 200
    assert response.get_json() == {'ok': True, 'text': transcript}
    files = [p for p in voice_debug.DEBUG_AUDIO_DIR.iterdir() if p.name != '.lock']
    assert len(files) == 1
    saved = files[0]
    assert voice_debug._AUDIO_NAME.fullmatch(saved.name)
    assert saved.suffix == extension
    assert saved.read_bytes() == payload
    assert voice.transcriptions == [(payload, 'customer-secret.wrong', mime.split(';')[0])]
    events = [r.getMessage() for r in caplog.records if r.getMessage().startswith('VOICE_STT_')]
    final = json.loads(next(e.split(' ', 1)[1] for e in events if e.startswith('VOICE_STT_RESPONSE ')))
    assert final['debug_audio_id'] == saved.name
    assert final['request_id'] == response.headers['X-Voice-Request-Id']
    assert len(final['request_id']) == 32
    assert final['capture_duration_ms'] == 2300
    assert final['blob_size'] == len(payload)
    assert final['file_extension'] == extension
    # A fake adapter has no HTTP response: never invent a provider status.
    assert final['provider_http_status'] is None
    assert final['provider_latency_ms'] is None
    for secret in (transcript, 'customer-secret', 'voice-csrf', 'private audio bytes', voice_io.STT_CONTEXT_PROMPT):
        assert secret not in '\n'.join(events)
        assert secret not in saved.name
    assert client().get('/static/' + saved.name).status_code == 404


def test_debug_audio_rotation_age_and_disable_cleanup(isolated, monkeypatch):
    monkeypatch.setenv('VOICE_DEBUG_SAVE_AUDIO', '1')
    ids = [voice_debug.save_debug_audio(bytes([i]), 'audio/webm', is_admin=True) for i in range(5)]
    files = sorted(p.name for p in voice_debug.DEBUG_AUDIO_DIR.iterdir() if p.name != '.lock')
    assert files == sorted(ids[-3:])
    old = voice_debug.DEBUG_AUDIO_DIR / ids[-3]
    os.utime(old, (0, 0))
    assert voice_debug.cleanup_debug_audio() == 2
    assert not old.exists()
    monkeypatch.setenv('VOICE_DEBUG_SAVE_AUDIO', '0')
    assert voice_debug.save_debug_audio(b'not stored', 'audio/webm', is_admin=True) is None
    assert list(voice_debug.DEBUG_AUDIO_DIR.iterdir()) == [voice_debug.DEBUG_AUDIO_DIR / '.lock']


def test_debug_audio_expiry_runs_without_another_stt_request(isolated, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setenv('VOICE_DEBUG_SAVE_AUDIO', '1')
    saved = voice_debug.save_debug_audio(b'expiring', 'audio/webm', is_admin=True)
    path = voice_debug.DEBUG_AUDIO_DIR / saved
    expires = path.stat().st_mtime + voice_debug.DEBUG_AUDIO_TTL_SECONDS + 60
    monkeypatch.setattr(voice_debug, 'time', SimpleNamespace(time=lambda: expires))
    # Advance the background cleaner's wait without waiting an hour in the test.
    monkeypatch.setattr(voice_debug, 'threading', SimpleNamespace(
        Event=lambda: SimpleNamespace(wait=lambda seconds: False)))
    voice_debug._expire_audio()
    assert not path.exists()


def test_debug_audio_is_kept_when_provider_fails(isolated, monkeypatch, caplog):
    monkeypatch.setenv('VOICE_DEBUG_SAVE_AUDIO', '1')
    monkeypatch.setattr(backend, 'VOICE_IO_PROVIDER', FakeVoiceProvider(stt_error=True))
    with caplog.at_level(logging.INFO):
        response = transcribe(client())
    assert response.status_code == 503
    event = next(r.getMessage() for r in caplog.records if r.getMessage().startswith('VOICE_STT_ERROR '))
    metadata = json.loads(event.split(' ', 1)[1])
    assert (voice_debug.DEBUG_AUDIO_DIR / metadata['debug_audio_id']).read_bytes() == b'webm-audio'


def test_debug_audio_not_saved_for_non_admin_or_unauthenticated(isolated, monkeypatch):
    monkeypatch.setenv('VOICE_DEBUG_SAVE_AUDIO', '1')
    monkeypatch.setattr(backend, 'VOICE_IO_PROVIDER', FakeVoiceProvider())
    assert transcribe(backend.app.test_client()).status_code == 401
    assert not voice_debug.DEBUG_AUDIO_DIR.exists()
    limited_actor = replace(owner(), roles=('WAREHOUSE',))
    monkeypatch.setattr(backend, 'current_actor_context', lambda: limited_actor)
    assert transcribe(client()).status_code == 200
    assert not voice_debug.DEBUG_AUDIO_DIR.exists()


def test_debug_audio_storage_failure_does_not_change_stt(isolated, monkeypatch, caplog):
    monkeypatch.setenv('VOICE_DEBUG_SAVE_AUDIO', '1')
    monkeypatch.setattr(backend, 'VOICE_IO_PROVIDER', FakeVoiceProvider())
    def fail(*args, **kwargs):
        raise OSError('private disk path')
    monkeypatch.setattr(backend, 'save_debug_audio', fail)
    with caplog.at_level(logging.INFO):
        response = transcribe(client())
    assert response.status_code == 200
    assert 'DEBUG_AUDIO_STORAGE_ERROR' in caplog.text
    assert 'private disk path' not in caplog.text


@pytest.mark.parametrize('case,status', [('unauthenticated', 401), ('csrf', 403)])
def test_rejected_stt_attempt_has_safe_diagnostic_without_audio(isolated, monkeypatch, caplog, case, status):
    monkeypatch.setenv('VOICE_DEBUG_SAVE_AUDIO', '1')
    test_client = backend.app.test_client() if case == 'unauthenticated' else client()
    with caplog.at_level(logging.INFO):
        response = test_client.post('/api/internal/ai/voice/transcribe', data={
            'audio': (io.BytesIO(b'secret-rejected-audio'), 'private-client.webm', 'audio/webm')})
    assert response.status_code == status
    events = [r.getMessage() for r in caplog.records if r.getMessage().startswith('VOICE_STT_')]
    assert len(events) == 1
    metadata = json.loads(events[0].split(' ', 1)[1])
    assert metadata['request_id'] == response.headers['X-Voice-Request-Id']
    assert metadata['provider_http_status'] is None
    assert metadata['capture_duration_ms'] is None
    assert 'debug_audio_id' not in metadata
    assert not voice_debug.DEBUG_AUDIO_DIR.exists()
    assert 'secret-rejected-audio' not in events[0]
    assert 'private-client' not in events[0]


@pytest.mark.parametrize('outcome,status', [('ok', 201), ('http_error', 429), ('invalid_json', 200), ('timeout', None)])
def test_stt_logs_actual_provider_status_latency_and_duration(isolated, monkeypatch, caplog, outcome, status):
    monkeypatch.setattr(backend, 'VOICE_IO_PROVIDER', voice_io.OpenAIVoiceIOProvider(
        api_key='secret-provider-key', stt_model='gpt-4o-mini-transcribe'))
    ticks = iter([10.0, 10.125])
    from types import SimpleNamespace
    monkeypatch.setattr(voice_io, 'time', SimpleNamespace(perf_counter=lambda: next(ticks)))
    def post(*args, **kwargs):
        if outcome == 'timeout':
            raise voice_io.requests.Timeout('private timeout message')
        return StubTranscriptionResponse({'text': 'private transcript'}, status=status,
                                         json_error=(outcome == 'invalid_json'))
    monkeypatch.setattr(voice_io.requests, 'post', post)
    with caplog.at_level(logging.INFO):
        response = client().post('/api/internal/ai/voice/transcribe', data={
            'audio': (io.BytesIO(b'audio'), 'recording.webm', 'audio/webm'),
            'capture_duration_ms': 'not-a-number-secret'}, headers={'X-CSRF-Token': 'voice-csrf'})
    assert response.status_code == (200 if outcome == 'ok' else 503)
    final = json.loads([r.getMessage().split(' ', 1)[1] for r in caplog.records
                        if r.getMessage().startswith(('VOICE_STT_RESPONSE ', 'VOICE_STT_ERROR '))][-1])
    assert final['provider_http_status'] == status
    assert final['provider_latency_ms'] == 125.0
    assert final['configured_stt_model'] == 'gpt-4o-mini-transcribe'
    assert final['capture_duration_ms'] is None
    assert 'debug_audio_id' not in final
    assert voice_io._stt_diagnostics.get() is None
    for secret in ('private transcript', 'not-a-number-secret', 'secret-provider-key'):
        assert secret not in caplog.text


def test_debug_flag_does_not_change_upstream_request(isolated, monkeypatch):
    requests = []
    monkeypatch.setattr(backend, 'VOICE_IO_PROVIDER', voice_io.OpenAIVoiceIOProvider(api_key='test-secret'))
    def post(url, **kwargs):
        requests.append((url, kwargs))
        return StubTranscriptionResponse({'text': 'co mogę zrobić dzisiaj?'})
    monkeypatch.setattr(voice_io.requests, 'post', post)
    for flag in ('0', '1'):
        monkeypatch.setenv('VOICE_DEBUG_SAVE_AUDIO', flag)
        assert transcribe(client()).status_code == 200
    assert len(requests) == 2
    assert requests[0] == requests[1]


def test_provider_diagnostics_are_isolated_between_parallel_requests(isolated, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    barrier = threading.Barrier(2)
    provider = voice_io.OpenAIVoiceIOProvider(api_key='test-secret')
    def post(*args, **kwargs):
        status = int(kwargs['files']['file'][1])
        barrier.wait(timeout=5)
        return StubTranscriptionResponse({'text': 'test'}, status=status)
    monkeypatch.setattr(voice_io.requests, 'post', post)
    def run(status):
        metrics = voice_io.TranscriptionDiagnostics()
        with voice_io.observe_transcription(metrics):
            provider.transcribe(str(status).encode(), filename='recording.webm', content_type='audio/webm')
        assert voice_io._stt_diagnostics.get() is None
        return metrics.provider_http_status
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(run, [201, 202])) == [201, 202]


def test_debug_audio_rotation_lock_is_shared_between_processes(isolated, monkeypatch):
    import subprocess
    import sys
    monkeypatch.setenv('VOICE_DEBUG_SAVE_AUDIO', '1')
    with voice_debug._storage_lock():
        script = '''
import sys
from pathlib import Path
import voice_debug
voice_debug.DEBUG_AUDIO_DIR = Path(sys.argv[1])
try:
    with voice_debug._storage_lock():
        raise AssertionError('Another process must not enter the storage lock')
except OSError:
    pass
'''
        result = subprocess.run([sys.executable, '-B', '-c', script, str(voice_debug.DEBUG_AUDIO_DIR)],
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
    assert voice_debug.save_debug_audio(b'after-release', 'audio/webm', is_admin=True)


def test_local_stt_comparator_uses_same_audio_language_and_context(isolated, monkeypatch, tmp_path, capsys):
    import compare_voice_stt
    audio = tmp_path / 'recording.webm'
    audio.write_bytes(b'the same input')
    monkeypatch.setenv('OPENAI_API_KEY', 'test-secret')
    calls = []
    def post(url, **kwargs):
        calls.append(kwargs)
        return StubTranscriptionResponse({'text': 'porównanie'})
    monkeypatch.setattr(voice_io.requests, 'post', post)
    assert compare_voice_stt.main([str(audio)]) == 0
    assert [c['data']['model'] for c in calls] == ['gpt-4o-mini-transcribe', 'gpt-transcribe']
    assert calls[0]['files'] == calls[1]['files']
    for call in calls:
        assert call['data']['language'] == 'pl'
        assert call['data']['prompt'] == voice_io.STT_CONTEXT_PROMPT
    results = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(results) == 2 and all(r['transcript'] == 'porównanie' for r in results)
    assert not voice_debug.DEBUG_AUDIO_DIR.exists()


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
