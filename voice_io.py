"""Thin speech IO adapters. They never call business operations or the agent runtime."""
from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Protocol

import requests


MAX_AUDIO_BYTES = 10 * 1024 * 1024
MAX_SPEECH_TEXT = 700
DEFAULT_STT_MODEL = 'gpt-4o-mini-transcribe'
STT_LANGUAGE = 'pl'
STT_CONTEXT_PROMPT = (
    'Dokładnie transkrybuj mowę po polsku w aplikacji biznesowej. '
    'Zachowaj nazwy produktów i firm, numery zamówień oraz kody SKU, '
    'w tym litery, cyfry i łączniki, dokładnie tak, jak zostały wypowiedziane.'
)
ALLOWED_AUDIO_TYPES = frozenset({
    'audio/webm', 'audio/ogg', 'audio/mp4', 'audio/mpeg', 'audio/wav', 'audio/x-wav',
})


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
    def __init__(self, *, api_key: str = '', stt_model: str = '', tts_model: str = '', voice: str = ''):
        self.api_key = api_key or os.environ.get('OPENAI_API_KEY', '')
        self.stt_model = stt_model or os.environ.get('AI_STT_MODEL', DEFAULT_STT_MODEL)
        self.tts_model = tts_model or os.environ.get('AI_TTS_MODEL', 'gpt-4o-mini-tts')
        self.voice = voice or os.environ.get('AI_TTS_VOICE', 'alloy')
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
        except requests.Timeout as exc:
            raise VoiceIOError('STT provider timed out', error_code='STT_TIMEOUT',
                               stage='provider_request') from exc
        except requests.RequestException as exc:
            raise VoiceIOError('STT provider unavailable', error_code='STT_NETWORK_ERROR',
                               stage='provider_request') from exc
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
        try:
            response = requests.post(
                'https://api.openai.com/v1/audio/speech',
                headers={'Authorization': f'Bearer {self.api_key}', 'Content-Type': 'application/json'},
                json={'model': self.tts_model, 'voice': self.voice, 'input': text, 'response_format': 'mp3'},
                timeout=60,
            )
        except requests.RequestException as exc:
            raise VoiceIOError('TTS provider unavailable') from exc
        self._raise_for_status(response, 'TTS')
        if not response.content:
            raise VoiceIOError('Empty speech audio')
        return SynthesizedAudio(response.content, 'audio/mpeg')


def provider_from_env() -> VoiceIOProvider:
    return OpenAIVoiceIOProvider()
