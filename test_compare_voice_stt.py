"""Comparator guards and safe failure output, with no live provider calls."""
import json

import pytest

import compare_voice_stt as compare
from voice_io import VoiceIOError


def test_comparator_requires_explicit_model_selection(tmp_path):
    path = tmp_path/'input.webm'
    path.write_bytes(b'audio')
    with pytest.raises(SystemExit) as error:
        compare.main([str(path)])
    assert error.value.code == 2


def test_comparator_continues_after_failure_and_does_not_print_provider_secrets(tmp_path,monkeypatch,capsys):
    path = tmp_path/'private-filename.webm'
    path.write_bytes(b'same audio')
    calls = []
    class Provider:
        def __init__(self, *, stt_model):
            self.model = stt_model
        def transcribe(self, audio, **kwargs):
            calls.append((self.model,audio,kwargs))
            if self.model == 'first-selected-model':
                raise VoiceIOError('Bearer private-secret, private-provider-body',
                                   error_code='STT_PROVIDER_HTTP_ERROR',http_status=403)
            return 'Rozpoznana treść'
    monkeypatch.setattr(compare,'OpenAIVoiceIOProvider',Provider)
    assert compare.main([str(path),'--models','first-selected-model','second-selected-model']) == 1
    output = capsys.readouterr().out
    assert 'private-secret' not in output and 'private-provider-body' not in output
    assert 'private-filename' not in output
    results = [json.loads(line) for line in output.splitlines()]
    assert [row['status'] for row in results] == ['FAILED','SUCCESS']
    assert results[0]['http_status'] == 403
    assert results[1]['transcript'] == 'Rozpoznana treść'
    assert calls[0][1:] == calls[1][1:]
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize('content',[b'',b'x'*(10*1024*1024+1)],ids=['empty','over-10mb'])
def test_comparator_rejects_empty_and_large_audio_before_provider(tmp_path,monkeypatch,content):
    path = tmp_path/'input.webm'
    path.write_bytes(content)
    def unexpected(**kwargs):
        raise AssertionError('Provider must not be constructed')
    monkeypatch.setattr(compare,'OpenAIVoiceIOProvider',unexpected)
    assert compare.main([str(path),'--models','selected-model']) == 2
