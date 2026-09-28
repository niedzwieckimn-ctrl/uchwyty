"""Catalog-only voice identity matching; no inferred product rows or fuzzy writes."""

import re
import unicodedata

from inventory_voice_fast import normalize_transcript


COLORS = frozenset({'bb', 'bn', 'mb', 'blk', 'blb', 'ab', 'pb'})
ALIASES = {'karl':'carl', 'andre':'andre', 'andrę':'andre', 'noa':'noah',
           'windsor':'winsor', 'dower':'dover', 'dauber':'dover'}
CONTEXT_SUGGESTIONS = {'tower':'dover', 'uber':'dover', 'plk':'blk'}


def key(value):
    text = str(value or '')
    text = re.sub(r'(?<=[a-ząćęłńóśźż])(?=[A-ZĄĆĘŁŃÓŚŹŻ])', ' ', text)
    text = unicodedata.normalize('NFKD', text.casefold()).replace('ł', 'l')
    text = ''.join(char for char in text if not unicodedata.combining(char))
    return ' '.join(text.replace('–', '-').replace('—', '-').split())


def tokens(value, *, spoken=False):
    text = key(normalize_transcript(value) if spoken else value)
    for speech, color in [('be el ka', 'blk'), ('be el be', 'blb'), ('be be', 'bb'),
                          ('be en', 'bn'), ('em be', 'mb'), ('a be', 'ab')]:
        text = re.sub(r'\b' + speech + r'\b', color, text)
    found = re.findall(r'\d+\s*-\s*\d+|[a-z]+|\d+', text)
    return tuple(ALIASES.get(token.replace(' ', ''), token.replace(' ', '')) for token in found)


def _number_matches(number, available):
    if number in available:
        return True, False
    if number.isdigit():
        for candidate in available:
            if re.fullmatch(r'\d+\s*-\s*\d+', candidate):
                low, high = map(int, candidate.split('-'))
                if low <= int(number) <= high:
                    return True, True
    return False, False


def match(row, query, *, exact_only=False):
    """Return rank and whether range confirmation is needed, or None.

    Letter/color tokens must be whole tokens; 96 cannot match 196 and BB cannot
    match BLB. A bare spacing contained in a range is only a candidate, even if
    the catalog currently contains just one such range.
    """
    row = dict(row)
    needle = tokens(query, spoken=True)
    if not needle:
        return None
    fields = [tokens(row.get(field)) for field in ('sku', 'model', 'name', 'ean')]
    canonical = lambda values: ''.join(values).replace(' ', '')
    exact = any(canonical(needle) == canonical(field) for field in fields if field)
    combined = set(token for field in fields for token in field)
    range_confirmation = False
    for token in needle:
        if token.isdigit() or '-' in token:
            matched, used_range = _number_matches(token, combined)
            if not matched:
                return None
            range_confirmation |= used_range
        elif token not in combined:
            return None
    if exact_only and not exact:
        # Attribute order can differ between spoken words, model and name.
        variant = [token for token in needle if token in COLORS or token.isdigit() or '-' in token]
        model = [token for token in needle if token not in COLORS and not token.isdigit() and '-' not in token]
        if not variant or not model or range_confirmation:
            return None
        row_numbers = {token for field in fields[1:3] for token in field if token.isdigit() or '-' in token}
        if not row_numbers.issubset(set(needle)):
            return None
    rank = (0 if canonical(needle) == canonical(fields[0]) else 1 if exact else 2,
            key(row.get('sku')), int(row['id']))
    return rank, range_confirmation


def resolve(rows, query, *, exact_only=False):
    found = []
    for source in rows:
        row = dict(source)
        result = match(row, query, exact_only=exact_only)
        if result is not None:
            rank, needs_confirmation = result
            row['_voice_requires_confirmation'] = needs_confirmation
            found.append((rank, row))
    return [row for _, row in sorted(found, key=lambda item:item[0])[:20]]


def contextual_suggestions(rows, query, *, context_product_ids=()):
    """Risky recognitions are suggestions only inside an already selected scope."""
    context_ids = {int(value) for value in context_product_ids}
    needle = tokens(query, spoken=True)
    risky = [token for token in needle if token in CONTEXT_SUGGESTIONS]
    if len(risky) != 1 or not context_ids:
        return []
    corrected = [CONTEXT_SUGGESTIONS.get(token, token) for token in needle]
    if not any(token in COLORS for token in corrected) or not any(token.isdigit() for token in corrected):
        return []
    candidates = resolve((row for row in rows if int(row['id']) in context_ids), ' '.join(corrected))
    for candidate in candidates:
        candidate['_voice_requires_confirmation'] = True
    return candidates


def display_name(row):
    row = dict(row)
    model, name, sku = (str(row.get(field) or '').strip() for field in ('model', 'name', 'sku'))
    def detail(value):
        return sum(token in COLORS or token.isdigit() or '-' in token for token in tokens(value))
    label = name if name and detail(name) > detail(model) else model or name or sku
    if sku and key(sku) not in key(label):
        label += f' [{sku}]'
    return label[:240]
