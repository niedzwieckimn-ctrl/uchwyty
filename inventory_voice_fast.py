"""Small deterministic parser and voice phrases for an active count session."""

from dataclasses import dataclass
import re
import unicodedata

from voice_io import normalize_tts_text


_NUMBERS = {
    'zero': 0, 'jeden': 1, 'jedna': 1, 'jedno': 1, 'dwa': 2, 'dwie': 2,
    'trzy': 3, 'cztery': 4, 'piec': 5, 'szesc': 6, 'siedem': 7, 'osiem': 8,
    'dziewiec': 9, 'dziesiec': 10, 'jedenascie': 11, 'dwanascie': 12,
    'trzynascie': 13, 'czternascie': 14, 'pietnascie': 15,
    'szesnascie': 16, 'siedemnascie': 17, 'osiemnascie': 18,
    'dziewietnascie': 19, 'dwadziescia': 20, 'trzydziesci': 30,
    'czterdziesci': 40, 'piecdziesiat': 50, 'szescdziesiat': 60,
    'siedemdziesiat': 70, 'osiemdziesiat': 80, 'dziewiecdziesiat': 90,
    'sto': 100, 'dwiescie': 200, 'trzysta': 300, 'czterysta': 400,
    'piecset': 500, 'szescset': 600, 'siedemset': 700,
    'osiemset': 800, 'dziewiecset': 900,
}
_UNITS = {'szt', 'sztuk', 'sztuki', 'sztuka'}
_APPROVE = {'tak', 'zatwierdz', 'zapisz'}
_REJECT = {'nie', 'odrzuc'}


def _fold(value):
    text = unicodedata.normalize('NFKD', str(value or '').casefold()).replace('ł', 'l')
    return ''.join(ch for ch in text if not unicodedata.combining(ch))


def normalize_transcript(value):
    """Expand common spoken integers locally; keep product words and identifiers."""
    tokens = [part.strip('.,!?;:') for part in str(value or '').replace(',', ' ').split()]
    tokens = [part for part in tokens if part]
    result = []
    index = 0
    while index < len(tokens):
        if _fold(tokens[index]) not in _NUMBERS:
            result.append(tokens[index])
            index += 1
            continue
        number = 0
        while index < len(tokens) and _fold(tokens[index]) in _NUMBERS:
            number += _NUMBERS[_fold(tokens[index])]
            index += 1
        result.append(str(number))
    return ' '.join(result)


@dataclass(frozen=True)
class VoiceCommand:
    kind: str
    product: str = ''
    product_without_count: str = ''
    quantity: int | None = None
    decision: str = ''


def parse(value):
    text = normalize_transcript(value)
    folded = ' '.join(_fold(text).split())
    if folded in _APPROVE | _REJECT:
        return VoiceCommand('decision', decision='approve' if folded in _APPROVE else 'reject')
    if not text or len(text) > 120:
        return None
    words = text.split()
    if words and _fold(words[-1]) in _UNITS:
        words.pop()
    if not words:
        return None
    if len(words) == 1 and words[0].isdigit() and len(words[0]) <= 7:
        return VoiceCommand('quantity', quantity=int(words[0]))
    product = ' '.join(words)
    if not re.fullmatch(r'[\w .-]{3,100}', product) or not re.search(r'[A-Za-zÀ-ž]', product):
        return None
    if len(words) > 1 and words[-1].isdigit() and len(words[-1]) <= 7:
        return VoiceCommand('product', product=product,
                            product_without_count=' '.join(words[:-1]),
                            quantity=int(words[-1]))
    return VoiceCommand('product', product=product)


def difference_prompt(expected_quantity, difference):
    return normalize_tts_text(
        f'System {int(expected_quantity)}. Różnica {int(difference):+d}. Zapisać?')
