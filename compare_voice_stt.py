"""Compare explicitly selected STT models through the application's adapter.

Reads one local recording once. Prints JSONL results; never saves audio copies or
provider bodies. A successful mocked test does not establish model availability.
"""
import argparse
import json
from pathlib import Path
import re
import sys
import time

from voice_io import MAX_AUDIO_BYTES, OpenAIVoiceIOProvider, VoiceIOError

TYPES = {'.webm':'audio/webm', '.ogg':'audio/ogg', '.mp4':'audio/mp4',
         '.m4a':'audio/mp4', '.mp3':'audio/mpeg', '.mpeg':'audio/mpeg', '.wav':'audio/wav'}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Porównaj STT na tym samym nagraniu; modele wybierz jawnie.')
    parser.add_argument('audio', type=Path, help='Lokalny plik nagrania (maksymalnie 10 MB).')
    parser.add_argument('--models', nargs='+', required=True,
                        help='Identyfikatory modeli zgodnych z obecnym adapterem Voice. Dostępność sprawdzi API.')
    args = parser.parse_args(argv)
    if (len(args.models) > 8 or len(set(args.models)) != len(args.models)
            or any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}', name) for name in args.models)):
        parser.error('Podaj od 1 do 8 różnych identyfikatorów modeli.')
    content_type = TYPES.get(args.audio.suffix.lower())
    if content_type is None:
        parser.error('Nieobsługiwany format audio.')
    try:
        with args.audio.open('rb') as handle:
            audio = handle.read(MAX_AUDIO_BYTES + 1)
    except OSError:
        print('Nie można odczytać nagrania.', file=sys.stderr)
        return 2
    if not audio or len(audio) > MAX_AUDIO_BYTES:
        print('Nagranie jest puste albo przekracza 10 MB.', file=sys.stderr)
        return 2
    failed = False
    for model in args.models:
        started = time.perf_counter()
        result = {'model':model}
        try:
            provider = OpenAIVoiceIOProvider(stt_model=model)
            transcript = provider.transcribe(audio, filename='comparison'+args.audio.suffix.lower(),
                                             content_type=content_type)
            result.update(status='SUCCESS', transcript=transcript)
        except VoiceIOError as exc:
            failed = True
            # Never print exception text, request headers or provider response bodies.
            result.update(status='FAILED', error_code=exc.error_code, stage=exc.stage,
                          http_status=exc.http_status)
        result['latency_ms'] = round((time.perf_counter()-started)*1000, 2)
        print(json.dumps(result, ensure_ascii=False))
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
