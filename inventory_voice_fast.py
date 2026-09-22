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
_UNITS = {'szt', 'sztuk', 'sztuki', 'sztuka', 'sztuke'}
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
    if folded in {'nastepny', 'nastepny produkt', 'dalej'}:
        return VoiceCommand('next')
    if folded in {'koniec remanentu', 'zakoncz remanent'}:
        return VoiceCommand('complete')
    if folded in {'robimy remanent', 'rozpocznij remanent', 'remanent'}:
        return VoiceCommand('start')
    if folded in _APPROVE | _REJECT:
        return VoiceCommand('decision', decision='approve' if folded in _APPROVE else 'reject')
    if not text or len(text) > 120:
        return None
    counted = re.fullmatch(r'(?:na polce (?:jest|lezy mi tylko)|jest ich tylko|naliczylem) (\d{1,7})(?: sztuk)?', folded)
    if counted:
        return VoiceCommand('quantity', quantity=int(counted[1]))
    words = text.split()
    location_count = [_fold(word) for word in words[:3]] == ['mam', 'na', 'polce']
    if location_count:
        words = words[3:]
    if words and _fold(words[0]) == 'sprawdz':
        words.pop(0)
        if words and _fold(words[0]) in {'produkt', 'produkty'}:
            words.pop(0)
    if words and _fold(words[-1]) in _UNITS:
        words.pop()
    if not words:
        return None
    if len(words) == 1 and words[0].isdigit() and len(words[0]) <= 7:
        return VoiceCommand('quantity', quantity=int(words[0]))
    if len(words) == 1 and words[0].isdigit() and 8 <= len(words[0]) <= 14:
        return VoiceCommand('product', product=words[0])
    # STT often puts the counted amount before the product, or after "mam".
    # Remove only these explicit count phrases; product numbers remain intact.
    quantity = None
    product_words = words
    if (len(words) >= 2 and _fold(words[0]) in {'mam', 'policzylem', 'policzylam', 'policzono'}
            and words[1].isdigit() and len(words[1]) <= 7):
        quantity = int(words[1])
        offset = 2
        if offset < len(words) and _fold(words[offset]) in _UNITS:
            offset += 1
        if [_fold(word) for word in words[offset:offset + 2]] == ['na', 'stanie']:
            offset += 2
        product_words = words[offset:]
    elif (len(words) >= 3 and _fold(words[-2]) == 'mam'
          and words[-1].isdigit() and len(words[-1]) <= 7):
        quantity = int(words[-1])
        product_words = words[:-2]
    elif (len(words) >= 4 and [_fold(word) for word in words[-3:-1]] == ['na', 'polce']
          and words[-1].isdigit() and len(words[-1]) <= 7):
        quantity = int(words[-1])
        product_words = words[:-3]
    if quantity is not None:
        product = ' '.join(product_words)
        if not product:
            return VoiceCommand('quantity', quantity=quantity)
        if (not re.fullmatch(r'[\w .-]{3,100}', product)
                or not re.search(r'[A-Za-zÀ-ž]', product)):
            return None
        return VoiceCommand('product', product=product,
                            product_without_count=product, quantity=quantity)
    product = ' '.join(words)
    if not re.fullmatch(r'[\w .-]{3,100}', product) or not re.search(r'[A-Za-zÀ-ž]', product):
        return None
    if len(words) > 1 and words[-1].isdigit() and len(words[-1]) <= 7:
        return VoiceCommand('product', product=' '.join(words[:-1]) if location_count else product,
                            product_without_count=' '.join(words[:-1]),
                            quantity=int(words[-1]))
    return VoiceCommand('product', product=product)


def difference_prompt(expected_quantity, difference):
    return normalize_tts_text(f'Różnica {int(difference):+d}. Zatwierdzić?')
