"""Short, deterministic speech for an open inventory count session only."""

import re

from voice_io import normalize_tts_text
from inventory_voice_products import display_name


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


def timing_summary(timing):
    if not isinstance(timing, dict) or timing.get('active_seconds') is None:
        return 'Brak zapisanej historii czasu dla tej sesji. Nie mogę ustalić czasu wcześniejszego liczenia.'
    def duration(value):
        value = max(0, int(value or 0))
        hours, minutes = divmod(value // 60, 60)
        return f'{hours} godz. {minutes} min {value % 60} s' if hours else f'{minutes} min {value % 60} s'
    measured = f'Czas aktywnego liczenia: {duration(timing["active_seconds"])}.'
    if timing.get('calendar_seconds') is not None:
        measured += f' Czas kalendarzowy od rozpoczęcia pomiaru: {duration(timing["calendar_seconds"])}.'
    if not timing.get('timing_complete'):
        measured += ' Pomiar nie obejmuje wcześniejszej pracy; jej czas jest nieznany.'
    return measured


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
            if difference == 0:
                return 'Zgodne. Następny.'
            if pending_adjustment:
                return difference_prompt(difference)
            return normalize_tts_text(
                f'Różnica {difference:+d}. Wynik liczenia zapisany. Korekta nie jest przygotowana.')
    if operation == 'inventory.adjust' and pending_adjustment:
        difference = data.get('difference')
        if isinstance(difference, int):
            return difference_prompt(difference)
        return 'Zatwierdzić?'
    if operation in {'inventory.product.get', 'inventory.count.get_expected'}:
        return product_prompt(data)
    return 'Powtórz.'


def approval_response(outcome, decision):
    """Describe only the confirmed approval result, without reading product cards."""
    if (outcome.get('execution_status') == 'SUCCESS'
            and outcome.get('result', {}).get('status') == 'SUCCESS'):
        after = outcome.get('after') or {}
        quantity = after.get('quantity') if isinstance(after, dict) else None
        display = ('Korekta została zapisana. Nowy stan: '
                   f'{quantity} szt.' if isinstance(quantity, int) and quantity >= 0
                   else 'Korekta została zapisana.')
        result_data = outcome.get('result', {}).get('data') or {}
        label = display_name(result_data) if isinstance(result_data, dict) else ''
        if label:
            display = f'{label} — {display}'
        return display, 'Zapisano. Następny.'
    if decision == 'reject':
        return ('Korekta odrzucona. Wynik remanentu pozostaje zapisany, '
                'magazyn nie został zmieniony.', 'Odrzucono. Następny.')
    return 'Nie zapisano korekty. Sprawdź stan produktu.', 'Nie zapisano. Powtórz.'
