"""Short, deterministic speech for an open inventory count session only."""

import re

from voice_io import normalize_tts_text


def product_prompt(data):
    """Speak a product name when it is distinct from its technical SKU."""
    model = str(data.get('model') or data.get('name') or '').strip()
    sku = str(data.get('sku') or '').strip()
    if (not model or model.casefold() == sku.casefold()
            or re.search(r'\bSKU\b', model, re.IGNORECASE)
            or not re.fullmatch(r'[\w .-]{1,65}', model)):
        return 'Ile?'
    return f'{model}. Ile?'


def difference_prompt(difference):
    return normalize_tts_text(f'Różnica {int(difference):+d}. Zatwierdzić?')


def from_operation(operation, data, *, pending_adjustment=False):
    """Use trusted operation fields, never generated answer text."""
    data = data if isinstance(data, dict) else {}
    if operation == 'inventory.count.session.start':
        return 'Podaj produkt.'
    if operation == 'inventory.count.complete':
        return 'Zakończono.'
    if operation == 'inventory.count.record':
        difference = data.get('difference')
        if isinstance(difference, int):
            return ('Zgodne. Następny.' if difference == 0
                    else difference_prompt(difference))
    if operation == 'inventory.adjust' and pending_adjustment:
        difference = data.get('difference')
        if isinstance(difference, int):
            return difference_prompt(difference)
        return 'Zatwierdzić?'
    if operation in {'inventory.product.get', 'inventory.count.get_expected'}:
        return product_prompt(data)
    return 'Sprawdź ekran.'


def approval_response(outcome, decision):
    """Describe only the confirmed approval result, without reading product cards."""
    if (outcome.get('execution_status') == 'SUCCESS'
            and outcome.get('result', {}).get('status') == 'SUCCESS'):
        after = outcome.get('after') or {}
        quantity = after.get('quantity') if isinstance(after, dict) else None
        display = ('Korekta została zapisana. Nowy stan: '
                   f'{quantity} szt.' if isinstance(quantity, int) and quantity >= 0
                   else 'Korekta została zapisana.')
        return display, 'Zapisano. Następny.'
    if decision == 'reject':
        return ('Korekta odrzucona. Wynik remanentu pozostaje zapisany, '
                'magazyn nie został zmieniony.', 'Odrzucono. Następny.')
    return 'Nie zapisano korekty. Sprawdź stan produktu.', 'Nie zapisano. Sprawdź ekran.'
