"""Provider-input-only Polish numbers, with opaque technical identifiers."""
from types import SimpleNamespace

import pytest

import voice_io


CASES = [
    ('Winstor 192 AB', 'Winstor sto dziewięćdziesiąt dwa a be'),
    ('Andre 128 BB', 'Andre sto dwadzieścia osiem be be'),
    ('Victor 160 AB', 'Victor sto sześćdziesiąt a be'),
    ('96', 'dziewięćdziesiąt sześć'),
    ('224', 'dwieście dwadzieścia cztery'),
    ('320', 'trzysta dwadzieścia'),
    ('Masz 108 sztuk.', 'Masz sto osiem sztuk.'),
    ('Systemowo 19.', 'Systemowo dziewiętnaście.'),
    ('Różnica minus 2.', 'Różnica minus dwa.'),
    ('Różnica -2.', 'Różnica minus dwa.'),
    ('Różnica −2.', 'Różnica minus dwa.'),
    ('Zmiana +2.', 'Zmiana plus dwa.'),
    ('Na półce jest 17 sztuk.', 'Na półce jest siedemnaście sztuk.'),
    ('Masz 1 sztukę i 2 sztuki.', 'Masz jedną sztukę i dwie sztuki.'),
    ('Została 1 sztuka.', 'Została jedna sztuka.'),
    ('Masz 22 sztuki i 12 sztuk.', 'Masz dwadzieścia dwie sztuki i dwanaście sztuk.'),
    ('Masz 0 sztuk.', 'Masz zero sztuk.'),
    ('Masz 1000 sztuk.', 'Masz tysiąc sztuk.'),
    ('Masz 2001 sztuk.', 'Masz dwa tysiące jeden sztuk.'),
    ('Masz 12000 sztuk.', 'Masz dwanaście tysięcy sztuk.'),
    ('Masz 22000 sztuk.', 'Masz dwadzieścia dwa tysiące sztuk.'),
    ('Cena 12,50 zł.', 'Cena dwanaście złotych pięćdziesiąt groszy.'),
    ('Cena 1,01 PLN.', 'Cena jeden złoty jeden grosz.'),
    ('Cena 2,02 zł.', 'Cena dwa złote dwa grosze.'),
    ('Cena 0,5 zł.', 'Cena zero złotych pięćdziesiąt groszy.'),
    ('Cena 17 zł.', 'Cena siedemnaście złotych.'),
    ('FVAT 9/09/2026', 'FVAT 9/09/2026'),
    ('Faktura 192', 'Faktura 192'),
    ('CH101-BLK', 'CH101-BLK'),
    ('CH010-BB-320', 'CH010-BB-320'),
    ('CH101', 'CH101'),
    ('Numer KSeF 5260250274-20260915-0123456789AB-12', 'Numer KSeF 5260250274-20260915-0123456789AB-12'),
    ('SKU 128 AB; masz 17 sztuk.', 'SKU 128 AB; masz siedemnaście sztuk.'),
    ('ID: 192, masz 108 sztuk.', 'ID: 192, masz sto osiem sztuk.'),
    ('Numer zamówienia 192.', 'Numer zamówienia 192.'),
    ('2026-09-15', '2026-09-15'),
    ('15.09.2026', '15.09.2026'),
    ('15/09/2026', '15/09/2026'),
    ('15 września 2026', '15 września 2026'),
    ('Godzina 12:30.', 'Godzina 12:30.'),
    ('2026-09-15T12:30:00Z', '2026-09-15T12:30:00Z'),
    ('01a095d5-272d-73b1-a5a8-12822e74c071', '01a095d5-272d-73b1-a5a8-12822e74c071'),
    ('Numer KSeF 123 456 789 AB', 'Numer KSeF 123 456 789 AB'),
    ('Tracking 123456789012345678901234', 'Tracking 123456789012345678901234'),
    ('1Z999AA10123456784', '1Z999AA10123456784'),
    ('+48 123 456 789', '+48 123 456 789'),
    ('123-456-789', '123-456-789'),
    ('123 456 789', '123 456 789'),
    ('Telefon: 123456789', 'Telefon: 123456789'),
    ('007', '007'),
    ('1000000', '1000000'),
    ('1 234,56 zł', '1 234,56 zł'),
    ('12.50', '12.50'),
    ('12,50', '12,50'),
    ('1,234', '1,234'),
    ('1e3', '1e3'),
    ('#192', '#192'),
    ('product_id: 192; stan 17.', 'product_id: 192; stan siedemnaście.'),
    ('invoice_no: 9/09/2026', 'invoice_no: 9/09/2026'),
    ('(22) 123 45 67', '(22) 123 45 67'),
    ('+48 (22) 123-45-67', '+48 (22) 123-45-67'),
    ('9 IX 2026', '9 IX 2026'),
    ('15 września, 2026', '15 września, 2026'),
    ('2026 r.', '2026 r.'),
    ('192,AB', '192,AB'),
    ('12,50zł', '12,50zł'),
    ('1,2abc', '1,2abc'),
    ('https://example.test/192/AB', 'https://example.test/192/AB'),
    ('a192@example.test', 'a192@example.test'),
    ('\x60sku=192\x60', '\x60sku=192\x60'),
    ('BB, BN, MB, BLK, AB', 'be be, be en, em be, be el ka, a be'),
    ('ABBA, tABlica, WH, GP', 'ABBA, tABlica, WH, GP'),
    ('Rozstawy: 96, 128 i 192 mm.', 'Rozstawy: dziewięćdziesiąt sześć, sto dwadzieścia osiem i sto dziewięćdziesiąt dwa mm.'),
]


@pytest.mark.parametrize('original,expected', CASES)
def test_tts_numbers_preserve_identifiers_and_visible_bytes(original, expected, monkeypatch):
    # Only the outbound provider JSON may differ from the original text.
    fields = dict(visible_text=original, final_response=original, speech_text=original)
    before = {name: value.encode('utf-8') for name, value in fields.items()}
    calls = []
    audio = b'unchanged-mp3'
    monkeypatch.setattr(voice_io.requests, 'post', lambda url, **kwargs: (
        calls.append((url, kwargs)) or SimpleNamespace(
            content=audio, status_code=200, raise_for_status=lambda: None)))
    provider = voice_io.OpenAIVoiceIOProvider(
        api_key='test-only', tts_model='gpt-4o-mini-tts', voice='cedar')
    response = provider.synthesize(fields['speech_text'])
    assert len(calls) == 1
    assert calls[0][0] == 'https://api.openai.com/v1/audio/speech'
    assert calls[0][1]['json']['input'] == expected
    assert calls[0][1]['json']['voice'] == 'cedar'
    assert 'speed' not in calls[0][1]['json']
    assert response.content == audio and response.content_type == 'audio/mpeg'
    assert {name: value.encode('utf-8') for name, value in fields.items()} == before
    assert voice_io.normalize_tts_text(expected) == expected


def test_tts_expansion_never_overflows_valid_input_or_truncates_its_content():
    original = 'Masz 192 sztuki. ' * 200
    result = voice_io.normalize_tts_text(original)
    assert len(original) < 4096
    assert len(result) <= 4096
    assert result.count('Masz ') == original.count('Masz ')
    assert result.count(' sztuki. ') == original.count(' sztuki. ')
    assert result.startswith('Masz sto dziewięćdziesiąt dwie sztuki.')
    assert result.endswith('Masz 192 sztuki. ')
    assert voice_io.normalize_tts_text(result) == result
