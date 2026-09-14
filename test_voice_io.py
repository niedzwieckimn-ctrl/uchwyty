import io
import json

import agent_runtime as runtime
import app as backend
import business_operations as operations
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
