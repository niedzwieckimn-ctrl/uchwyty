"""Thin speech IO adapters. They never call business operations or the agent runtime."""
from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Protocol

import requests


MAX_AUDIO_BYTES = 10 * 1024 * 1024
MAX_SPEECH_TEXT = 700
ALLOWED_AUDIO_TYPES = frozenset({
    'audio/webm', 'audio/ogg', 'audio/mp4', 'audio/mpeg', 'audio/wav', 'audio/x-wav',
})


class VoiceIOError(RuntimeError):
    pass


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
        self.stt_model = stt_model or os.environ.get('AI_STT_MODEL', 'gpt-4o-mini-transcribe')
        self.tts_model = tts_model or os.environ.get('AI_TTS_MODEL', 'gpt-4o-mini-tts')
        self.voice = voice or os.environ.get('AI_TTS_VOICE', 'alloy')
        if not self.api_key:
            raise VoiceIOError('Voice provider is not configured')

    @staticmethod
    def _raise_for_status(response, stage: str) -> None:
        try:
            response.raise_for_status()
        except requests.RequestException as exc:
            status = getattr(response, 'status_code', None)
            raise VoiceIOError(f'{stage} provider HTTP {status or "error"}') from exc

    def transcribe(self, audio: bytes, *, filename: str, content_type: str) -> str:
        try:
            response = requests.post(
                'https://api.openai.com/v1/audio/transcriptions',
                headers={'Authorization': f'Bearer {self.api_key}'},
                files={'file': (filename or 'recording.webm', audio, content_type)},
                data={'model': self.stt_model},
                timeout=60,
            )
        except requests.RequestException as exc:
            raise VoiceIOError('STT provider unavailable') from exc
        self._raise_for_status(response, 'STT')
        try:
            text = str(response.json().get('text') or '').strip()
        except (TypeError, ValueError) as exc:
            raise VoiceIOError('Invalid STT response') from exc
        if not text:
            raise VoiceIOError('Empty transcription')
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
