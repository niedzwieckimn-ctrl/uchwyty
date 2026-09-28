"""Backend-owned parcel draft. Parameters are not proof of an executed write."""
from __future__ import annotations

import hashlib
import json
import re
import uuid

PARAMETERS = frozenset({'carrier', 'length', 'width', 'height', 'dimension_unit',
                       'weight', 'weight_unit', 'weight_source', 'sms', 'email'})
_WORDS = {'jeden':1, 'jedna':1, 'dwa':2, 'dwie':2, 'trzy':3, 'cztery':4,
          'pięć':5, 'piec':5, 'sześć':6, 'szesc':6, 'siedem':7, 'osiem':8,
          'dziewięć':9, 'dziewiec':9, 'dziesięć':10, 'dziesiec':10}


def is_parameter_question(text):
    value = str(text).strip().casefold()
    return bool('?' in value or re.match(r'^(?:czy|ile|jaka|jakie|jaki|jak|co)\b', value))


def needs_parameter_review(text):
    """Decline corrections, units and carriers that this narrow parser cannot prove."""
    value = str(text).casefold()
    if is_parameter_question(value):
        return True
    if re.search(r'\b(?:mm|dm|metr\w*|inch\w*|cal\w*)\b', value) and re.search(r'\bwymiar\w*\b', value):
        return True
    if re.search(r'\b\d+(?:[.,]\d+)?\s*(?:g|gram\w*|lb|lbs|funt\w*)\b', value):
        return True
    number = r'\d+(?:[.,]\d+)?|' + '|'.join(sorted(_WORDS, key=len, reverse=True))
    weights = list(re.finditer(r'\b(?:' + number + r')\s*(?:kg|kilogram\w*)\b', value))
    if len(weights) > 1 or (weights and re.search(r'\bnie\s*$', value[:weights[0].start()])):
        return True
    carrier = re.search(r'\b(?:kurier(?:a|em)?|przewo[zź]nik(?:a|iem)?|carrier)\s+(?:firmy\s+)?([a-z][a-z0-9-]*)\b', value)
    if carrier and carrier[1] not in {'inpost','impost','do','dla','tej','tejże','tego','na','z','od',
            'jeszcze','teraz','ponownie','jutro','dzisiaj','dziś','dzis','już','juz','i','oraz','w','jest','ma'}:
        return True
    return False


def shipping_context(text):
    return bool(re.search(r'\b(?:inpost|impost|kurier\w*|przewoźnik\w*|przewoznik\w*|'
                          r'paczk\w*|realizuj\w*|realizacj\w*|list\w*\s+pakow\w*)\b', str(text).casefold()))


def requests_shipping(text):
    value = str(text).casefold()
    return not cancels_shipping(value) and not needs_parameter_review(value) and shipping_context(value) and bool(re.search(
        r'\b(?:zam[oó]w(?:cie|my)?|zamawiam(?:y)?|zamawiaj(?:cie|my)?|nadaj|wyślij|wyslij|realizuj\w*|kontynuuj\w*)\b', value))


def cancels_shipping(text):
    value = str(text).casefold()
    return shipping_context(value) and bool(re.search(
        r'\b(?:nie\s+(?:(?:chcę|chce|chcemy|należy|nalezy|trzeba)\s+)?'
        r'(?:zamaw\w*|zam[oó]w\w*|wysy[lł]\w*|wyślij|wyslij|nadaw\w*|nadaj|realiz\w*)|'
        r'wstrzymaj|zatrzymaj|anuluj)\b', value))


def parse_parameters(text, *, active=False):
    value = ' '.join(str(text or '').casefold().split())
    if (not active and not shipping_context(value)) or needs_parameter_review(value):
        return {}
    result = {}
    if re.search(r'\b(?:inpost|impost)\b', value) and (active or shipping_context(value)):
        result['carrier'] = 'inpost'
    number = r'\d+(?:[.,]\d+)?'
    dimensions = re.search(r'\bwymiar\w*(?:\s+paczki)?\s*[:=]?\s*(' + number +
                           r')\s*(?:cm\s*)?(?:[,x×]|na|\s)\s*(' + number +
                           r')\s*(?:cm\s*)?(?:[,x×]|na|\s)\s*(' + number + r')\s*(?:cm)?\b', value)
    if dimensions:
        result.update(zip(('length', 'width', 'height'),
                          (float(v.replace(',', '.')) for v in dimensions.groups())))
        result['dimension_unit'] = 'cm'
    word_number = '|'.join(sorted(_WORDS, key=len, reverse=True))
    weight_pattern = r'\b(' + number + '|' + word_number + r')\s*(?:kg|kilogram\w*)\b'
    weight = re.search(weight_pattern, value)
    if weight and not shipping_context(value) and not re.fullmatch(
            r'(?:(?:waga|masa|waży|wazy)(?:\s+(?:to|wynosi))?\s*[:=]?\s*)?' + weight_pattern + r'[.!\s]*', value):
        weight = None
    if weight:
        raw = weight[1]
        result.update(weight=float(_WORDS[raw] if raw in _WORDS else raw.replace(',', '.')),
                      weight_unit='kg', weight_source='manual')
    for name, pattern in (('sms', r'sms'), ('email', r'e[ -]?mail')):
        if re.search(r'\b' + pattern + r'\b', value) and (
                re.search(r'powiadom|zamaw|włącz|wlacz|wyłącz|wylacz|bez\s', value)):
            result[name] = not bool(re.search(r'\b(?:bez|nie(?:\s+chcę|\s+chce)?|wyłącz|wylacz)\s+(?:powiadom\w*\s+)?' + pattern + r'\b', value))
    if re.search(r'\b(?:bez|nie(?:\s+chcę|\s+chce)?|wyłącz|wylacz)\s+(?:powiadom\w*\s+)?'
                 r'(?:sms\s*(?:,|i|oraz)\s*e[ -]?mail|e[ -]?mail\s*(?:,|i|oraz)\s*sms)\b', value):
        result.update(sms=False, email=False)
    return result


def identity(state):
    package = state.get('package') or {}
    order_ids = package.get('order_ids') or []
    root = package.get('root_order_id') or state.get('order_id')
    if (not package.get('packing_list_id') or not package.get('batch_id')
            or not order_ids or root not in order_ids):
        return None
    return {'packing_list_id': str(package['packing_list_id']), 'batch_id': int(package['batch_id']),
            'order_ids': sorted(set(int(item) for item in order_ids)), 'root_order_id': int(root)}


def observe(draft, state, message, run_id):
    """Bind only a real LP; another card never silently changes the parcel."""
    selected = identity(state)
    if selected is None:
        return draft
    if draft and draft.get('identity') != selected:
        explicit_order = str(state.get('order_number') or '').casefold()
        if not explicit_order or explicit_order not in str(message).casefold():
            return draft
        draft = None  # Explicit new parcel starts without the previous parameters.
    if not draft:
        if not shipping_context(message):
            return None
        if selects_other_scope(message, {'identity':selected, 'order_number':state.get('order_number')}):
            return None
        draft = {'draft_id': str(uuid.uuid4()), 'identity': selected, 'parameters': {},
                 'execution_refs': [], 'pending_approval': None}
    draft = dict(draft)
    draft['parameters'] = {**draft['parameters'], **parse_parameters(message, active=True)}
    draft['shipping_requested'] = (False if (cancels_shipping(message) or
                                    (needs_parameter_review(message) and not is_parameter_question(message)))
                                   else bool(draft.get('shipping_requested') or requests_shipping(message)))
    draft['source_run_id'] = run_id
    draft['order_number'] = state.get('order_number') or draft.get('order_number') or ''
    draft['expected_version'] = state['expected_version']
    draft['known'] = dict((state.get('requirements') or {}).get('known') or {})
    draft['next_step'] = state.get('next_step')
    draft['package_fingerprint'] = (state.get('package') or {}).get('fingerprint') or ''
    draft['packed_quantity'] = (state.get('package') or {}).get('packed_quantity')
    return draft


def selects_other_scope(message, draft):
    """A new explicit target must be resolved before interpreting its parameters."""
    value = str(message).casefold()
    if not draft or not draft.get('identity'):
        return False
    if re.search(r'\b(?:inn\w*|drug\w*|now\w*)\s+(?:paczk\w*|przesy[lł]\w*|zam[oó]w\w*|list\w*|klient\w*)\b', value):
        return True
    explicit = re.findall(r'\b[a-z][a-z0-9]{1,15}[-–—/][a-z0-9/–—-]*\d[a-z0-9/–—-]*', value, re.IGNORECASE)
    explicit += re.findall(r'\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b', value)
    allowed = {str(draft.get('order_number') or '').casefold(),
               str(draft['identity'].get('packing_list_id') or '').casefold()}
    if any(item not in allowed for item in explicit):
        return True
    # "Wymiary paczki 45, 20 na 20" describes dimensions, not parcel number 45.
    selectors = re.sub(r'\b(?:wymiar\w*|waga|masa)\s+paczki\s*[:=]?\s*(?=\d)', '', value)
    return bool(re.search(r'\b(?:paczk\w*|zam[oó]wieni\w*|list\w*\s+pakow\w*)\s+(?:nr\.?\s*|numer\s*)?#?\d+\b', selectors))


def is_followup(message, draft):
    if not draft or not draft.get('identity'):
        return False
    if re.search(r'\b(?:zatwierdz\w*|zatwierdź|akceptuj\w*|akceptuję|akceptuje|odrzuc\w*|odrzuć)\b', str(message).casefold()):
        return False
    if needs_parameter_review(message):
        return False
    if selects_other_scope(message, draft):
        return False
    return bool(parse_parameters(message, active=True) or shipping_context(message)
                or re.fullmatch(r'\s*(?:kontynuuj\w*|dalej|podaj szczegóły|podaj szczegoly|co cię blokuje|co cie blokuje)[.!?\s]*', str(message).casefold()))


def merge_parameters(draft, message):
    result = dict(draft)
    result['parameters'] = {**draft.get('parameters', {}), **parse_parameters(message, active=True)}
    result['shipping_requested'] = (False if (cancels_shipping(message) or
                                     (needs_parameter_review(message) and not is_parameter_question(message)))
                                    else bool(draft.get('shipping_requested') or requests_shipping(message)))
    return result


def missing_fields(draft, state):
    merged = {**(state.get('requirements') or {}).get('known', {}), **draft.get('parameters', {})}
    return [key for key in (state.get('requirements') or {}).get('missing_fields', [])
            if key.startswith('recipient.') or key not in merged or merged[key] is None or merged[key] == '']


def idempotency_key(draft, operation, fields):
    digest = hashlib.sha256(json.dumps(fields, sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:32]
    return 'parcel-draft:' + draft['draft_id'] + ':' + operation.rsplit('.', 1)[-1] + ':' + digest


def model_context(draft):
    return {'kind': 'verified_parcel_draft', 'identity': draft['identity'],
            'parameters_collected_from_user': draft['parameters'],
            'parameters_persisted_in_business_storage': draft.get('known', {}),
            'shipping_requested': draft.get('shipping_requested', False),
            'expected_version': draft.get('expected_version'),
            'package_fingerprint': draft.get('package_fingerprint'),
            'packed_quantity': draft.get('packed_quantity'),
            'next_step': draft.get('next_step'), 'pending_approval': draft.get('pending_approval')}
