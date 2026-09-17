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
_TTS_ABBREVIATION = re.compile(r'\b(AB|BB|BN|MB|BLK)\b')
_TTS_ABBREVIATIONS = {
    'AB': 'a be',
    'BB': 'be be',
    'BN': 'be en',
    'MB': 'em be',
    'BLK': 'be el ka',
}


_TTS_SMALL = (
    'zero', 'jeden', 'dwa', 'trzy', 'cztery',
    'pięć', 'sześć', 'siedem', 'osiem', 'dziewięć',
    'dziesięć', 'jedenaście', 'dwanaście', 'trzynaście', 'czternaście',
    'piętnaście', 'szesnaście', 'siedemnaście', 'osiemnaście', 'dziewiętnaście',
)


_TTS_TENS = (
    '', '', 'dwadzieścia', 'trzydzieści', 'czterdzieści',
    'pięćdziesiąt', 'sześćdziesiąt', 'siedemdziesiąt', 'osiemdziesiąt', 'dziewięćdziesiąt',
)
_TTS_HUNDREDS = (
    '', 'sto', 'dwieście', 'trzysta', 'czterysta',
    'pięćset', 'sześćset', 'siedemset', 'osiemset', 'dziewięćset',
)


# Protect whole technical fragments before expanding numbers or colour codes.
# A labelled identifier clause is left intact up to a sentence/list boundary.
_TTS_PROTECTED = re.compile(r'''
    https?://\S+ | [\w.+-]+@[\w.-]+\.[A-Za-z]{2,} | \x60[^\x60]*\x60
    | (?<!\w)[\#@][A-Za-z0-9_]+
    | \b(?i:numer(?:u|em)?|nr|identyfikator(?:a|em)?|id|sku|uuid|ksef|nip|regon|
        pesel|iban|ean|gtin|awb|tracking|telefon(?:u)?|tel|kod(?:u)?|
        fvat|fv|faktur(?:a|y|\u0119|ze)|zam\u00f3wieni(?:e|a|u)|data|dnia|rok|roku|
        [a-z][a-z0-9_]*_(?:id|no|number|key|code|uuid|version))
        \b\.?(?:[^;.!?\n,]|[.,](?=[0-9]))*
    | (?<!\w)[0-9]{1,2}\s+(?i:stycznia|lutego|marca|kwietnia|maja|czerwca|
        lipca|sierpnia|wrze\u015bnia|pa\u017adziernika|listopada|grudnia)
        (?:,?\s+[0-9]{4})?\b
    | (?<!\w)[0-9]{1,2}\s+[IVX]{1,4}\s+[0-9]{4}\b
    | (?<!\w)[0-9]{4}\s*(?i:r\.|rok(?:u)?\b)
    | (?<!\w)(?:\+[0-9]{1,3}\s*)?\([0-9]{2,3}\)\s*[0-9]+(?:[\ -]+[0-9]+)*
    | (?<!\w)[+\u2212-]?[0-9]+(?:[\ \u00a0\u202f-]+[0-9]+)+(?:[.,][0-9]+)?(?!\w)
    | (?<!\w)(?=[\w,./\\:-]*[^\W\d_])\w+(?:[,./\\:-]\w+)+(?!\w)
    | (?<!\w)[\w]+(?:[-/\\.:][\w]+)+(?!\w)
    | \b(?=\w*[0-9])(?=\w*[^\W\d])\w+\b
    | (?<!\w)[0-9]+,[0-9]+(?!\w)(?!\s*(?:z\u0142|PLN)\b)
''', re.VERBOSE)
_TTS_NUMBER = re.compile(r'''
    (?<![\w/\\.,:+-])
    (?P<sign>[-+\u2212]?)(?P<whole>0|[1-9][0-9]{0,5})
    (?:(?:,(?P<fraction>[0-9]{1,2}))?(?P<currency>\s*(?:z\u0142|PLN)\b))?
    (?![\w]|[.,:/\\-][0-9])
''', re.VERBOSE)

# An explicit currency makes a grouped amount distinguishable from an opaque
# phone/tracking number. Match it before the conservative identifier protector.
_TTS_MONEY = r'''
    (?<![\w/\\.,:+-])(?P<money_sign>[-+\u2212]?)
    (?P<money_whole>(?:[1-9][0-9]{0,2}(?:[\ \u00a0\u202f][0-9]{3}){1,2}
        |[1-9][0-9]{0,2}(?:\.[0-9]{3}){1,2}|0|[1-9][0-9]{0,8}))
    (?:[,.](?P<money_fraction>[0-9]{1,2}))?
    [\ \u00a0\u202f]+(?P<money_currency>(?i:zł|PLN|EUR)|€)(?!\w)
'''
_TTS_TOKENS = re.compile(
    '(?P<money>' + _TTS_MONEY + ')|(?P<protected>' + _TTS_PROTECTED.pattern + ')', re.VERBOSE)


def _tts_plural(number, singular, few, many):
    if number == 1:
        return singular
    return few if 2 <= number % 10 <= 4 and not 12 <= number % 100 <= 14 else many


def _tts_integer(number):
    """Polish cardinal form for an unsigned, bounded spoken quantity."""
    if number < 20:
        return _TTS_SMALL[number]
    if number >= 1_000_000:
        millions, remainder = divmod(number, 1_000_000)
        prefix = ('milion' if millions == 1 else
                  _tts_integer(millions) + ' ' + _tts_plural(millions, 'milion', 'miliony', 'milionów'))
        return prefix + (' ' + _tts_integer(remainder) if remainder else '')
    if number >= 1000:
        thousands, remainder = divmod(number, 1000)
        prefix = ('tysiąc' if thousands == 1 else
                  _tts_integer(thousands) + ' ' + _tts_plural(thousands, 'tysiąc', 'tysiące', 'tysięcy'))
        return prefix + (' ' + _tts_integer(remainder) if remainder else '')
    hundreds, remainder = divmod(number, 100)
    parts = [_TTS_HUNDREDS[hundreds]] if hundreds else []
    if remainder:
        if remainder < 20:
            parts.append(_TTS_SMALL[remainder])
        else:
            tens, units = divmod(remainder, 10)
            parts.append(_TTS_TENS[tens])
            if units:
                parts.append(_TTS_SMALL[units])
    return ' '.join(parts)


def normalize_tts_text(text: str) -> str:
    """Naturalize only the provider input; preserve technical fragments verbatim."""
    source = str(text)
    # Expanding an otherwise valid input must not exceed the Speech API limit.
    # Keep a token's original spelling if it no longer fits; never truncate text.
    remaining_chars = max(0, 4096 - len(source))

    def fit(original, spoken):
        nonlocal remaining_chars
        extra = len(spoken) - len(original)
        if extra > remaining_chars:
            return original
        remaining_chars -= extra
        return spoken

    def spoken_fragment(fragment):
        def number_words(match):
            number = int(match['whole'])
            words = _tts_integer(number)
            if match['currency']:
                words += ' ' + _tts_plural(number, 'złoty', 'złote', 'złotych')
                if match['fraction'] is not None:
                    cents = int(match['fraction'].ljust(2, '0'))
                    words += (' ' + _tts_integer(cents) + ' '
                              + _tts_plural(cents, 'grosz', 'grosze', 'groszy'))
            else:
                # Match the existing noun; never rewrite the sentence or product.
                suffix = fragment[match.end():]
                if number == 1 and re.match(r'\s+sztuk\u0119\b', suffix):
                    words = 'jedną'
                elif number == 1 and re.match(r'\s+sztuka\b', suffix):
                    words = 'jedna'
                elif number % 10 == 2 and number % 100 != 12 and re.match(r'\s+sztuki\b', suffix):
                    words = words[:-3] + 'dwie'
            sign = match['sign']
            spoken = ('minus ' if sign in ('-', '−') else 'plus ' if sign == '+' else '') + words
            return fit(match.group(), spoken)

        fragment = _TTS_NUMBER.sub(number_words, fragment)
        return _TTS_ABBREVIATION.sub(
            lambda match: fit(match.group(), _TTS_ABBREVIATIONS[match.group(1)]), fragment)

    def money_words(match):
        whole = int(re.sub(r'[\s.]', '', match['money_whole']))
        euro = match['money_currency'].casefold() in {'eur', '€'}
        words = _tts_integer(whole) + ' ' + (
            'euro' if euro else _tts_plural(whole, 'złoty', 'złote', 'złotych'))
        if match['money_fraction'] is not None:
            cents = int(match['money_fraction'].ljust(2, '0'))
            forms = ('cent', 'centy', 'centów') if euro else ('grosz', 'grosze', 'groszy')
            words += ' i ' + _tts_integer(cents) + ' ' + _tts_plural(cents, *forms)
        sign = match['money_sign']
        words = ('minus ' if sign in {'-', '−'} else 'plus ' if sign == '+' else '') + words
        return fit(match.group(), words)

    parts = []
    position = 0
    for protected in _TTS_TOKENS.finditer(source):
        parts.append(spoken_fragment(source[position:protected.start()]))
        if protected['money'] is not None:
            parts.append(money_words(protected))
        elif re.match(r'(?i)^(?:fvat|fv|faktur\w*|zamówieni\w*)\b', protected.group()):
            # The conservative invoice/order clause protector may also contain
            # a monetary amount. Expand explicit currency only, preserving IDs.
            parts.append(re.sub(_TTS_MONEY, money_words, protected.group(), flags=re.VERBOSE))
        else:
            parts.append(protected.group())
        position = protected.end()
    parts.append(spoken_fragment(source[position:]))
    return ''.join(parts)


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
