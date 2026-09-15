"""Thin speech IO adapters. They never call business operations or the agent runtime."""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
from contextvars import ContextVar
import os
import re
import time
from typing import Protocol

import requests


MAX_AUDIO_BYTES = 10 * 1024 * 1024
DEFAULT_STT_MODEL = 'gpt-4o-mini-transcribe'
DEFAULT_TTS_MODEL = 'gpt-4o-mini-tts'
DEFAULT_TTS_VOICE = 'cedar'
STT_LANGUAGE = 'pl'
STT_CONTEXT_PROMPT = (
    'Dokładnie transkrybuj mowę po polsku w aplikacji biznesowej. '
    'Zachowaj nazwy produktów i firm, numery zamówień oraz kody SKU, '
    'w tym litery, cyfry i łączniki, dokładnie tak, jak zostały wypowiedziane.'
)
TTS_INSTRUCTIONS = (
    "Mów naturalnym językiem polskim, męskim głosem o naturalnej, niższej barwie, "
    "w sprawnym tempie zwykłej rozmowy biznesowej. Mów konkretnie, bez przeciągania słów "
    "i długich pauz. Krótkie odpowiedzi wypowiadaj energicznie. "
    "Stosuj polską wymowę i intonację, bez angielskiego akcentu. "
    "Wymawiaj BB jako 'be be', BN jako 'be en', MB jako 'em be', BLK jako 'be el ka'."
)
ALLOWED_AUDIO_TYPES = frozenset({
    'audio/webm', 'audio/ogg', 'audio/mp4', 'audio/mpeg', 'audio/wav', 'audio/x-wav',
})

_SPOKEN_URL = re.compile(r'https?://\S+', re.IGNORECASE)
_SPOKEN_TECHNICAL = re.compile(
    r'(?i)\b(?:approval_id|operation_id|execution_id|correlation_id|pending_approval|json)\b')
_TTS_ABBREVIATION = re.compile(r'(?<![A-Z0-9])(BB|BN|MB|BLK)(?![A-Z0-9])')
_TTS_ABBREVIATIONS = {
    'BB': 'be be',
    'BN': 'be en',
    'MB': 'em be',
    'BLK': 'be el ka',
}


def normalize_tts_text(text: str) -> str:
    """Expand selected Polish business abbreviations only in the provider input."""
    return _TTS_ABBREVIATION.sub(lambda match: _TTS_ABBREVIATIONS[match.group(1)], str(text))


def compact_speech_text(final_text, *, existing_speech_text='', user_message='',
                        voice_response_mode='adaptive'):
    """Sanitize semantic speech from the final model turn without reshaping its length."""
    del user_message, voice_response_mode

    def clean(value):
        safe_lines = []
        for source_line in str(value or '').splitlines():
            line = source_line.strip()
            if (not line or '|' in line or _SPOKEN_URL.search(line)
                    or _SPOKEN_TECHNICAL.search(line)
                    or line.startswith(('{', '['))):
                continue
            line = re.sub(r'^\s*(?:#{1,6}\s*|[-*•]\s+)', '', line)
            line = line.replace('**', '').replace('__', '').replace('`', '')
            line = re.sub(r'\s+', ' ', line).strip(' -–—,;:')
            if line:
                safe_lines.append(line)
        return ' '.join(safe_lines).strip()

    provided = clean(existing_speech_text)
    if provided:
        return provided
    return clean(final_text)


@dataclass
class TranscriptionDiagnostics:
    configured_stt_model: str = DEFAULT_STT_MODEL
    language: str = STT_LANGUAGE
    provider_http_status: int | None = None
    provider_latency_ms: float | None = None


_stt_diagnostics = ContextVar('stt_diagnostics', default=None)


@contextmanager
def observe_transcription(diagnostics):
    """Request-local metadata; no audio, transcript or provider contract changes."""
    token = _stt_diagnostics.set(diagnostics)
    try:
        yield diagnostics
    finally:
        _stt_diagnostics.reset(token)


class VoiceIOError(RuntimeError):
    def __init__(self, message: str, *, error_code: str = 'STT_FAILED',
                 stage: str = 'provider', http_status: int | None = None):
        super().__init__(message)
        self.error_code = error_code
        self.stage = stage
        self.http_status = http_status


@dataclass(frozen=True)
class SynthesizedAudio:
    content: bytes
    content_type: str


class VoiceIOProvider(Protocol):
    def transcribe(self, audio: bytes, *, filename: str, content_type: str) -> str: ...
    def synthesize(self, text: str) -> SynthesizedAudio: ...


class OpenAIVoiceIOProvider:
    def __init__(self, *, api_key: str = '', stt_model: str = '', tts_model: str = '',
                 voice: str = '', tts_instructions: str = ''):
        self.api_key = api_key or os.environ.get('OPENAI_API_KEY', '')
        self.stt_model = stt_model or os.environ.get('AI_STT_MODEL', DEFAULT_STT_MODEL)
        self.tts_model = tts_model or os.environ.get('AI_TTS_MODEL', DEFAULT_TTS_MODEL)
        self.voice = voice or os.environ.get('AI_TTS_VOICE', DEFAULT_TTS_VOICE)
        self.tts_instructions = (tts_instructions or os.environ.get('AI_TTS_INSTRUCTIONS', '')
                                 or TTS_INSTRUCTIONS)
        if not self.api_key:
            raise VoiceIOError('Voice provider is not configured',
                               error_code='VOICE_NOT_CONFIGURED', stage='configuration')

    @staticmethod
    def _raise_for_status(response, stage: str) -> None:
        try:
            response.raise_for_status()
        except requests.RequestException as exc:
            status = getattr(response, 'status_code', None)
            raise VoiceIOError(f'{stage} provider HTTP {status or "error"}',
                               error_code=f'{stage}_PROVIDER_HTTP_ERROR',
                               stage='provider_response', http_status=status) from exc

    def transcribe(self, audio: bytes, *, filename: str, content_type: str) -> str:
        diagnostics = _stt_diagnostics.get()
        if diagnostics is not None:
            diagnostics.configured_stt_model = self.stt_model
            diagnostics.language = STT_LANGUAGE
        started = time.perf_counter()
        try:
            response = requests.post(
                'https://api.openai.com/v1/audio/transcriptions',
                headers={'Authorization': f'Bearer {self.api_key}'},
                files={'file': (filename or 'recording.webm', audio, content_type)},
                data={
                    'model': self.stt_model,
                    'language': STT_LANGUAGE,
                    'prompt': STT_CONTEXT_PROMPT,
                },
                timeout=60,
            )
            if diagnostics is not None:
                diagnostics.provider_http_status = response.status_code
        except requests.Timeout as exc:
            raise VoiceIOError('STT provider timed out', error_code='STT_TIMEOUT',
                               stage='provider_request') from exc
        except requests.RequestException as exc:
            raise VoiceIOError('STT provider unavailable', error_code='STT_NETWORK_ERROR',
                               stage='provider_request') from exc
        finally:
            if diagnostics is not None:
                diagnostics.provider_latency_ms = round((time.perf_counter() - started) * 1000, 2)
        self._raise_for_status(response, 'STT')
        try:
            data = response.json()
        except (TypeError, ValueError) as exc:
            raise VoiceIOError('Invalid STT response', error_code='STT_INVALID_RESPONSE',
                               stage='provider_response') from exc
        if not isinstance(data, dict) or not isinstance(data.get('text'), str):
            raise VoiceIOError('Invalid STT response', error_code='STT_INVALID_RESPONSE',
                               stage='provider_response')
        text = data['text'].strip()
        if not text:
            raise VoiceIOError('Empty transcription', error_code='STT_EMPTY_TRANSCRIPT',
                               stage='provider_response')
        return text

    def synthesize(self, text: str) -> SynthesizedAudio:
        payload = {
            'model': self.tts_model,
            'voice': self.voice,
            'input': normalize_tts_text(text),
            'response_format': 'mp3',
        }
        if self.tts_model.startswith('gpt-4o-mini-tts'):
            payload['instructions'] = self.tts_instructions
        try:
            response = requests.post(
                'https://api.openai.com/v1/audio/speech',
                headers={'Authorization': f'Bearer {self.api_key}', 'Content-Type': 'application/json'},
                json=payload,
                timeout=60,
            )
        except requests.Timeout as exc:
            raise VoiceIOError('TTS provider timed out', error_code='TTS_TIMEOUT',
                               stage='provider_request') from exc
        except requests.RequestException as exc:
            raise VoiceIOError('TTS provider unavailable', error_code='TTS_NETWORK_ERROR',
                               stage='provider_request') from exc
        self._raise_for_status(response, 'TTS')
        if not response.content:
            raise VoiceIOError('Empty speech audio', error_code='TTS_EMPTY_AUDIO',
                               stage='provider_response', http_status=response.status_code)
        return SynthesizedAudio(response.content, 'audio/mpeg')


def provider_from_env() -> VoiceIOProvider:
    return OpenAIVoiceIOProvider()
