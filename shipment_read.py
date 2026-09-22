"""Read physical shipments through their complete invoices; packing is audit evidence.

No migration, PDF generation, status update or recovery write is performed here.
"""
from collections import Counter
from datetime import date, datetime, timedelta
import json
import re
import unicodedata
from zoneinfo import ZoneInfo

OPERATION = 'shipment.read'
WARSAW = ZoneInfo('Europe/Warsaw')
INPUT = {'type': 'object', 'additionalProperties': False, 'required': ['mode'], 'properties': {
    'mode': {'type': 'string', 'enum': ['latest', 'date', 'customer', 'tracking']},
    'date': {'type': 'string', 'minLength': 10, 'maxLength': 10},
    'customer_id': {'type': 'integer', 'minimum': 1},
    'customer': {'type': 'string', 'minLength': 1, 'maxLength': 160},
    'tracking': {'type': 'string', 'minLength': 1, 'maxLength': 160},
    'order_number': {'type': 'string', 'minLength': 1, 'maxLength': 80},
}}
OUTPUT = {'type': 'object', 'additionalProperties': False,
          'required': ['ok', 'mode', 'shipments', 'count', 'total_units', 'complete'],
          'properties': {'ok': {'type': 'boolean'}, 'mode': {'type': 'string'},
                         'shipments': {'type': 'array', 'items': {'type': 'object'}},
                         'count': {'type': 'integer'}, 'total_units': {'type': 'integer'},
                         'complete': {'type': 'boolean'}}}


class ShipmentReadError(ValueError):
    pass


def is_question(text):
    value = str(text or '').casefold()
    if re.search(r'\b(?:wyślij|wyslij|utw[oó]rz|wygeneruj|nadaj)\b', value):
        return False
    # Explicit LP questions keep the existing LP/current/history workflow.
    if (re.search(r'\b(?:lp|list\w*\s+pakow|li[śs]ci\w*\s+pakow)', value)
            and not re.search(r'\b(?:do|dla)\s+.+', value)):
        return False
    explicit_latest = bool(
        re.search(r'\b(?:ostatni\w*|ostatnio)\b', value)
        and re.search(r'\b(?:paczk\w*|przesył\w*|przesyl\w*|zam[oó]wieni\w*)\b', value)
        and re.search(r'\b(?:wysła\w*|wysla\w*|wysył\w*|wysyl\w*)\b', value)
    )
    return explicit_latest or bool(re.search(r'\b(?:co|jakie|ile|pokaż|pokaz|odczytaj|sprawdź|sprawdz)\b', value) and re.search(
        r'\b(?:wysła\w*|wysla\w*|wysył\w*|wysyl\w*|przesył\w*|przesyl\w*|wyszło|wyszlo|poszło|poszlo|'
        r'było\s+w\s+pacz\w*|bylo\s+w\s+pacz\w*)\b', value))


def direct_selector(text, today=None):
    """Recognize explicit dates and scope without another model call."""
    value = ' '.join(str(text or '').casefold().split()).strip(' ?!.')
    today = today or datetime.now(WARSAW).date()
    scope = {}
    tracking = re.search(r'\b(?:tracking(?:u)?|numerze(?:\s+tracking(?:u)?)?)\s*[:#]?\s*([\w-]+)', value)
    if tracking:
        # Preserve the caller's exact identifier, including case.
        original = str(text or '')
        exact = re.search(re.escape(tracking[1]), original, re.IGNORECASE)
        return {'mode':'tracking', 'tracking':exact[0] if exact else tracking[1]}
    order = re.search(r'\bzam\s*[-–—]?\s*((?:\d[\s-]*){5,20})\b', value)
    if order:
        scope['order_number'] = 'ZAM-' + re.sub(r'\D', '', order[1])
    # Combined date/customer questions go through the model selector so neither
    # filter is silently discarded by this deliberately small direct parser.
    has_customer = re.search(r'\b(?:do|dla)\s+', value) and not order
    day = re.search(r'\b(\d{4}-\d{2}-\d{2}|\d{2}\.\d{2}\.\d{4})\b', value)
    if day:
        if has_customer:
            return None
        raw = day[1]
        return dict(mode='date', date=raw if '-' in raw else '-'.join(reversed(raw.split('.'))), **scope)
    months = ('stycznia','lutego','marca','kwietnia','maja','czerwca','lipca','sierpnia',
              'września','października','listopada','grudnia')
    match = re.search(r'\b(\d{1,2})\s+('+'|'.join(months)+r')(?:\s+(\d{4}))?\b', value)
    if match:
        if has_customer:
            return None
        return dict(mode='date', date=f'{int(match[3] or today.year):04d}-{months.index(match[2])+1:02d}-{int(match[1]):02d}', **scope)
    if re.search(r'\b(?:dziś|dzis|dzisiaj|wczoraj)\b', value):
        if has_customer:
            return None
        selected = today-timedelta(days=1) if 'wczoraj' in value else today
        return dict(mode='date', date=selected.isoformat(), **scope)
    customer = re.search(r'\b(?:do|dla)\s+(.+)$', value)
    if customer and not order:
        return {'mode': 'customer', 'customer': customer[1]}
    if (order or re.search(r'\b(?:ostatni\w*|ostatnio)\b', value)
            or re.fullmatch(r'co\s+(?:ostatnio\s+)?(?:wysłałem|wyslalem|wysłaliśmy|wyslalismy)', value)):
        return dict(mode='latest', **scope)
    return None


def _moment(value):
    try:
        dt = datetime.fromisoformat(str(value or '').replace('Z', '+00:00'))
        return dt.astimezone(WARSAW) if dt.tzinfo else dt.replace(tzinfo=WARSAW)
    except ValueError:
        raise ShipmentReadError('Wysyłka ma nieprawidłową datę; wymaga wyjaśnienia.')


def _fold(value):
    return ''.join(c for c in unicodedata.normalize('NFKD', str(value).casefold().replace('ł','l')) if not unicodedata.combining(c))


def _tokens(value):
    return [re.sub(r'(?:iego|ego|iej|ej|y|a|e|i|u)$', '', t) if len(t)>5 else t
            for t in re.findall(r'\w+', _fold(value))]


def _customer_ids(db, data):
    if data.get('customer_id'):
        return {int(data['customer_id'])}, None
    if not data.get('customer'):
        return None, None
    wanted = _fold(data['customer']).strip()
    identities = {}
    for r in db.execute('SELECT DISTINCT customer_id,customer_name,customer_email FROM orders'):
        exact = wanted in {_fold(r['customer_name'] or ''), _fold(r['customer_email'] or '')}
        tokens = _tokens(data['customer'])
        matches = tokens and all(t in _tokens(r['customer_name'] or '') for t in tokens)
        if exact or matches:
            key = str(r['customer_id'] or _fold(r['customer_email'] or r['customer_name']))
            identities[key] = dict(r)
    if len(identities) != 1:
        raise ShipmentReadError('Nie znaleziono klienta jednoznacznie. Podaj jego dokładną nazwę lub identyfikator.')
    customer = next(iter(identities.values()))
    return ({int(customer['customer_id'])}, None) if customer['customer_id'] else (None, customer['customer_email'] or customer['customer_name'])


def invoice_contents(db, invoice_id):
    """Use the full saved invoice JSON, or full invoice allocations, never order qty."""
    inv = db.execute('SELECT i.*,m.invoice_items_json FROM invoices i LEFT JOIN invoice_meta m ON m.invoice_id=i.id WHERE i.id=?', (invoice_id,)).fetchone()
    if not inv:
        raise ShipmentReadError('Brak faktury powiązanej z wysyłką.')
    if inv['publication_state'] != 'complete':
        raise ShipmentReadError('Faktura wysyłki nie ma ukończonej publikacji.')
    allocations = [dict(r) for r in db.execute('''SELECT ia.order_id,ia.order_item_id,ia.sku,ia.qty,
        COALESCE(NULLIF(p.model,''),p.name,ia.sku,'') AS name,o.order_no AS order_number
        FROM invoice_allocations ia LEFT JOIN products p ON p.id=ia.product_id
        LEFT JOIN orders o ON o.id=ia.order_id WHERE ia.invoice_id=? ORDER BY ia.id''', (invoice_id,))]
    by_id = {r['order_item_id']: r for r in allocations}
    raw = None
    try:
        raw = json.loads(inv['invoice_items_json'] or 'null')
    except (ValueError, TypeError):
        pass
    saved = []
    if isinstance(raw, list) and raw:
        try:
            for item in raw:
                iid = int(item.get('order_item_id') or item.get('id') or 0)
                linked = by_id.get(iid, {})
                oid = int(item.get('source_order_id') or item.get('order_id') or linked.get('order_id') or 0)
                qty = int(item['qty'])
                sku = str(item.get('sku') or linked.get('sku') or '')
                if not oid or qty <= 0 or qty != float(item['qty']) or not sku:
                    raise ValueError('Incomplete invoice row')
                saved.append(dict(order_id=oid, order_item_id=iid, sku=sku,
                    name=str(item.get('model') or item.get('name') or linked.get('name') or sku), qty=qty,
                    order_number=str(item.get('source_order_no') or linked.get('order_number') or '')))
        except (ValueError, TypeError, KeyError, AttributeError):
            saved = []
    selected = saved or allocations
    if not selected:
        raise ShipmentReadError('Faktura nie ma kompletnych zapisanych pozycji; nie zastępuję ich zamówieniem.')
    if any(not r['order_id'] or not r['sku'] or not isinstance(r['qty'],int) or r['qty'] <= 0 for r in selected):
        raise ShipmentReadError('Pozycje faktury są niekompletne. Wymagają wyjaśnienia.')
    issue = bool(saved and allocations and _scope(saved) != _scope(allocations))
    return dict(inv), selected, ('invoice_items_json' if saved else 'invoice_allocations'), (allocations if issue else [])


def _scope(items):
    result = Counter()
    for r in items:
        result[(int(r['order_id']), str(r['sku']).strip())] += int(r['qty'])
    return result


def _invoice_candidates(db, order_ids):
    candidates = set()
    for oid in order_ids:
        candidates.update(r[0] for r in db.execute("""SELECT DISTINCT i.id FROM invoices i
            LEFT JOIN invoice_allocations ia ON ia.invoice_id=i.id
            WHERE i.publication_state='complete' AND (i.order_id=? OR ia.order_id=?)
              AND (EXISTS(SELECT 1 FROM invoice_allocations a WHERE a.invoice_id=i.id)
                   OR EXISTS(SELECT 1 FROM invoice_meta m WHERE m.invoice_id=i.id
                             AND TRIM(COALESCE(m.invoice_items_json,'')) NOT IN ('','[]','null')))""", (oid, oid)))
    return sorted(candidates)


def read(data, *, connection_factory, packing_allowed=True):
    mode = data.get('mode')
    if mode not in ('latest','date','customer','tracking'):
        raise ShipmentReadError('Wskaż tryb odczytu wysyłki: latest, date, customer albo tracking.')
    if mode == 'tracking' and not data.get('tracking'):
        raise ShipmentReadError('Podaj dokładny tracking wysyłki.')
    requested_day = None
    if mode == 'date':
        try:
            requested_day = date.fromisoformat(data.get('date') or '')
        except ValueError:
            raise ShipmentReadError('Podaj poprawną datę wysyłki w formacie RRRR-MM-DD.')
    if mode == 'customer' and not (data.get('customer_id') or data.get('customer')):
        raise ShipmentReadError('Wskaż klienta wysyłki.')
    db = connection_factory()
    try:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')  # A consistent read snapshot across invoice and LP tables.
        customer_ids, customer_text = _customer_ids(db, data)
        def matches_order(r):
            return ((not customer_ids or r['customer_id'] in customer_ids)
                    and (not customer_text or customer_text in (r['customer_email'],r['customer_name']))
                    and (not data.get('order_number') or r['order_no'] == data['order_number']))
        def date_filter(column):
            if not requested_day:
                return '', []
            return f' AND substr({column},1,10) BETWEEN ? AND ?', [
                (requested_day-timedelta(days=1)).isoformat(),(requested_day+timedelta(days=1)).isoformat()]
        events, covered = [], set()
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if {'packing_shipments','packing_batches','packing_lists'} <= tables:
            where, args = date_filter('ps.confirmed_at')
            finals = db.execute('''SELECT ps.*,COALESCE(pb.invoice_id,pl.invoice_id) AS invoice_id
                FROM packing_shipments ps JOIN packing_batches pb ON pb.id=ps.final_batch_id
                LEFT JOIN packing_lists pl ON pl.packing_list_id=ps.packing_list_id WHERE 1=1'''+where, args).fetchall()
            members_by_batch = {}
            for row in db.execute('''SELECT DISTINCT pa.batch_id,o.* FROM packing_shipments ps
                JOIN packing_allocations pa ON pa.batch_id=ps.final_batch_id
                JOIN orders o ON o.id=pa.order_id WHERE 1=1'''+where,args):
                members_by_batch.setdefault(row['batch_id'],[]).append(dict(row))
            for final in finals:
                members = members_by_batch.get(final['final_batch_id'],[])
                if not members or not any(matches_order(r) for r in members):
                    continue
                moment = _moment(final['confirmed_at'])
                if requested_day and moment.date() != requested_day:
                    continue
                events.append(dict(shipment_id=final['shipment_key'], shipped_at=final['confirmed_at'],
                    tracking=final['tracking'], carrier=final['carrier'], invoice_id=final['invoice_id'],
                    final_packing_batch_id=final['final_batch_id'], members=members, source='confirmed_shipment', moment=moment))
                for r in members:
                    covered.add((r['id'],final['tracking'],final['carrier'],None if final['tracking'] else moment))
        where, args = date_filter('shipped_at')
        orders = db.execute("SELECT * FROM orders WHERE TRIM(COALESCE(shipped_at,''))<>'' AND LOWER(COALESCE(status,''))<>'cancelled'"+where, args).fetchall()
        groups = {}
        for row in orders:
            r = dict(row)
            moment = _moment(r['shipped_at'])
            if (r['id'],r['tracking_no'] or '',r['carrier'] or '',None if r['tracking_no'] else moment) in covered:
                continue
            if requested_day and moment.date() != requested_day:
                continue
            key = ('inpost:'+str(r['inpost_shipment_id']) if r['inpost_shipment_id'] else
                   (r['carrier'] or '')+':'+r['tracking_no'] if r['tracking_no'] else f"order:{r['id']}:{r['shipped_at']}")
            event = groups.setdefault(key, dict(shipment_id=key, shipped_at=r['shipped_at'], tracking=r['tracking_no'] or '',
                carrier=r['carrier'] or '', invoice_id=None, final_packing_batch_id=None,
                members=[], source='orders.shipped_at', moment=moment))
            event['members'].append(r)
            if moment > event['moment']:
                event.update(moment=moment,shipped_at=r['shipped_at'])
        for event in groups.values():
            if any(matches_order(r) for r in event['members']):
                events.append(event)
        # Several final lists / order rows may describe the same physical parcel.
        physical = {}
        for event in events:
            if data.get('tracking') and event['tracking'] != data['tracking']:
                continue
            key = (event['carrier'], event['tracking']) if event['tracking'] else event['shipment_id']
            if key not in physical:
                event['final_packing_batch_ids'] = set()
                event['explicit_invoice_ids'] = set()
                physical[key] = event
            target = physical[key]
            target['members'] = list({r['id']:r for r in target['members'] + event['members']}.values())
            if event['final_packing_batch_id']:
                target['final_packing_batch_ids'].add(int(event['final_packing_batch_id']))
            if event['invoice_id']:
                target['explicit_invoice_ids'].add(int(event['invoice_id']))
        events = sorted(physical.values(), key=lambda e:(e['moment'],e['shipment_id']), reverse=True)
        chosen = events if mode == 'date' else events[:1]
        for event in chosen:
            candidates = set(_invoice_candidates(db, [r['id'] for r in event['members']]))
            # An immutable invoice attached to another confirmed physical parcel
            # cannot be inferred to belong to this parcel just from its order.
            if 'packing_shipments' in tables:
                other = {r[0] for r in db.execute("""SELECT DISTINCT pb.invoice_id
                    FROM packing_shipments ps JOIN packing_batches pb ON pb.id=ps.final_batch_id
                    WHERE pb.invoice_id IS NOT NULL AND ps.shipment_key<>?
                      AND NOT (ps.tracking<>'' AND ps.tracking=? AND ps.carrier=?)""",
                    (event['shipment_id'],event['tracking'],event['carrier']))}
                candidates -= other - event['explicit_invoice_ids']
            event['invoice_candidates'] = sorted(candidates)
            event['invoice_id'] = next(iter(candidates)) if len(candidates) == 1 else None
        shipments = [_view(db,event,packing_allowed) for event in chosen]
        owners = {}
        for shipment in shipments:
            for invoice_id in shipment.get('invoice_ids', []):
                owners.setdefault(invoice_id, []).append(shipment)
        for invoice_id, matches in owners.items():
            if len(matches) > 1:
                for shipment in matches:
                    shipment.update(complete=False, items=[], total_units=0)
                    shipment['issues'].append('Ta sama faktura wskazuje kilka wysyłek. Brak jednoznacznego podziału zawartości.')
        return dict(ok=True,mode=mode,shipments=shipments,count=len(shipments),
                    total_units=sum(s['total_units'] for s in shipments), complete=all(s['complete'] for s in shipments))
    finally:
        db.close()


def _view(db, event, packing_allowed):
    first = event['members'][0]
    result = {k:event[k] for k in ('shipment_id','shipped_at','tracking','carrier','invoice_id','final_packing_batch_id')}
    result.update(customer=dict(id=first['customer_id'],name=first['customer_name'] or '',email=first['customer_email'] or ''),
                  invoice_number='',orders=[],items=[],total_units=0,packing_verified=False,
                  packing_status='missing',final_packing_items=[],issues=[],complete=True,source=event['source'])
    result.update(invoice_ids=[], invoice_numbers=[], invoices=[])
    batch_ids = sorted(event.get('final_packing_batch_ids') or [])
    result['final_packing_batch_ids'] = batch_ids
    final = []
    if batch_ids and packing_allowed:
        marks = ','.join('?' for _ in batch_ids)
        final = [dict(order_id=r['order_id'],order_item_id=r['order_item_id'],sku=r['sku_snapshot'],
                      name=r['model_name_snapshot'] or '',qty=r['qty']) for r in db.execute(
                          f'SELECT * FROM packing_allocations WHERE batch_id IN ({marks}) ORDER BY id', batch_ids)]
    if not event['invoice_candidates']:
        result['complete'] = False
        result['issues'].append('Brak kompletnej faktury powiązanej z wysyłką. Wymaga wyjaśnienia.')
        return result
    items, conflicting_allocations, sources = [], [], set()
    members = {r['id'] for r in event['members']}
    covered = set()
    for invoice_id in event['invoice_candidates']:
        try:
            inv, lines, source, conflict = invoice_contents(db, invoice_id)
        except ShipmentReadError as exc:
            result['complete'] = False
            result['issues'].append(str(exc))
            continue
        scope = {line['order_id'] for line in lines}
        if not scope <= members:
            result['complete'] = False
            result['issues'].append('Faktura obejmuje zamówienia poza ustaloną wysyłką. Nie przypisuję całej faktury do tej paczki.')
        covered.update(scope)
        result['invoice_ids'].append(invoice_id)
        result['invoice_numbers'].append(inv['invoice_no'])
        result['invoices'].append(dict(id=invoice_id, number=inv['invoice_no'], items_source=source))
        items.extend(dict(line, invoice_id=invoice_id) for line in lines)
        conflicting_allocations.extend(conflict)
        sources.add(source)
    if covered != members:
        result['complete'] = False
        result['issues'].append('Nie wszystkie zamówienia wysyłki mają kompletne zapisane pozycje faktur.')
    line_owners = {}
    for item in items:
        key = (item['order_id'], item['order_item_id'] or item['sku'])
        line_owners.setdefault(key, set()).add(item['invoice_id'])
    if (any(len(ids) > 1 and not ids <= event['explicit_invoice_ids'] for ids in line_owners.values())
            and not (final and _scope(final) == _scope(items))):
        result.update(complete=False, invoice_candidates=event['invoice_candidates'])
        result['issues'].append('Kilka faktur dotyczy tych samych pozycji zamówienia bez jednoznacznego przypisania do paczki.')
        items = []
    identities = {str(r['customer_id'] or r['customer_email'] or r['customer_name']) for r in event['members']}
    if len(identities) != 1:
        result['complete'] = False
        result['issues'].append('Ten sam tracking wskazuje różnych odbiorców. Wymaga wyjaśnienia.')
        items = []
    result.update(invoice_number=', '.join(result['invoice_numbers']), items=items,
                  total_units=sum(i['qty'] for i in items), items_source='+'.join(sorted(sources)))
    result['orders'] = [dict(order_id=oid,order_number=next((i['order_number'] for i in items if i['order_id']==oid),'')) for oid in sorted(covered)]
    if conflicting_allocations:
        result.update(complete=False,invoice_allocation_items=conflicting_allocations)
        result['issues'].append('Snapshot JSON faktury i alokacje faktury różnią się. Wymaga wyjaśnienia.')
    if batch_ids and packing_allowed:
        result['final_packing_items'] = final
        verified = bool(final) and _scope(final)==_scope(items)
        result.update(packing_verified=verified,packing_status='matched' if verified else 'discrepancy')
        if not verified:
            result['complete'] = False
            result['issues'].append('Faktura i finalna LP różnią się. Oba źródła wymagają wyjaśnienia; nie rozstrzygam faktycznej ilości.')
    elif event['final_packing_batch_id']:
        result['packing_status'] = 'permission_denied'
    return result


def answer(data):
    if not data.get('shipments'):
        return 'Nie znalazłem potwierdzonej wysyłki w podanym zakresie.'
    lines = []
    for s in data['shipments']:
        lines.append(f"Wysyłka: {s['shipped_at']}")
        lines.append(f"Klient: {s['customer']['name']}")
        lines.append(f"Faktura: {s['invoice_number'] or 'nieustalona'}")
        lines.append(f"Tracking: {s['tracking'] or 'brak'}")
        for issue in s['issues']:
            lines.append('Wymaga wyjaśnienia: '+issue)
        if s.get('invoice_candidates'):
            lines.append('Możliwe faktury (ID): '+', '.join(map(str,s['invoice_candidates']))+'.')
        if s['items']:
            lines.append(f"{len(s['items'])} pozycji, {s['total_units']} sztuk.")
            lines.append('Wszystkie pozycje powiązanych faktur:')
            for item in s['items']:
                lines.append(f"- {item['sku']} {item['name']} — {item['qty']} szt. ({item.get('order_number') or item['order_id']})")
            lines.append(f"Razem na fakturach: {s['total_units']} sztuk.")
        if s['packing_status']=='discrepancy':
            lines.append(f"Finalna LP, batch {s['final_packing_batch_id']}:")
            for item in s['final_packing_items']:
                lines.append(f"- {item['sku']} {item['name']} — {item['qty']} szt. (zamówienie {item['order_id']})")
        elif s['packing_verified']:
            lines.append(f"Finalna LP (batch {s['final_packing_batch_id']}) jest zgodna z fakturą.")
        elif s['packing_status']=='missing':
            lines.append('Brak finalnej LP'+('; zawartość odczytana z faktury.' if s['items'] else '.'))
        elif s['packing_status']=='permission_denied':
            lines.append('Nie sprawdzono finalnej LP: brak uprawnień do tego dokumentu.')
        if s.get('invoice_allocation_items'):
            lines.append('Alokacje faktury:')
            lines.extend(f"- {i['sku']} — {i['qty']} szt. (zamówienie {i['order_id']})" for i in s['invoice_allocation_items'])
        lines.append('')
    if len(data['shipments'])>1:
        label = 'Łącznie według powiązanych faktur' if data['complete'] else 'Suma odczytanych pozycji faktur (dane wymagają wyjaśnienia)'
        lines.append(f"{label}: {data['total_units']} sztuk.")
    return '\n'.join(lines).strip()


_SPEECH_MONTHS = ('stycznia', 'lutego', 'marca', 'kwietnia', 'maja', 'czerwca',
                  'lipca', 'sierpnia', 'września', 'października', 'listopada', 'grudnia')


def _speech_date(value, *, include_time=True):
    raw = str(value or '')
    try:
        moment = _moment(raw)
    except ShipmentReadError:
        return ''
    label = f'{moment.day} {_SPEECH_MONTHS[moment.month - 1]}'
    if include_time and re.search(r'[T ]\d{2}:\d{2}', raw):
        label += f' o {moment:%H:%M}'
    return label


def _speech_plural(number, singular, few, many):
    return singular if number == 1 else few if number % 10 in (2, 3, 4) and number % 100 not in (12, 13, 14) else many


def _speech_items(items):
    """Brief human product names only; never read invoice, order or SKU identifiers."""
    names = Counter()
    for item in items:
        name = ' '.join(str(item.get('name') or '').split())
        sku = str(item.get('sku') or '').strip()
        if (not name or name.casefold() == sku.casefold() or len(name) > 32
                or not re.fullmatch(r'[\w .-]+', name, re.UNICODE)
                or re.search(r'\d{5,}', name)):
            return ''
        names[name.title() if name.isupper() else name] += int(item.get('qty') or 0)
    if not 1 <= len(names) <= 4:
        return ''
    parts = [f'{name} {quantity}' for name, quantity in names.items()]
    return ', '.join(parts[:-1]) + (' i ' if len(parts) > 1 else '') + parts[-1] + '.'


def speech(data):
    """Deterministic, short voice summary of structured shipment.read data."""
    shipments = data.get('shipments') or []
    if not shipments:
        return 'Nie znalazłem potwierdzonej wysyłki w podanym zakresie.'
    if not data.get('complete', True) or any(not s.get('complete', True) for s in shipments):
        return 'Dane wysyłki wymagają wyjaśnienia. Szczegóły są na ekranie.'
    if len(shipments) > 1:
        day = _speech_date(shipments[0].get('shipped_at'), include_time=False)
        count = len(shipments)
        total = sum(int(s.get('total_units') or 0) for s in shipments)
        prefix = f'{day} ' if day else ''
        return (f'{prefix}wysłano {count} {_speech_plural(count, "wysyłkę", "wysyłki", "wysyłek")}, '
                f'łącznie {total} {_speech_plural(total, "sztukę", "sztuki", "sztuk")}. Szczegóły są na ekranie.')
    shipment = shipments[0]
    day = _speech_date(shipment.get('shipped_at'))
    prefix = f'{day} ' if day else ''
    quantity = int(shipment.get('total_units') or 0)
    positions = len(shipment.get('items') or [])
    result = (f'{prefix}wysłano {quantity} {_speech_plural(quantity, "sztukę", "sztuki", "sztuk")} '
              f'w {positions} {_speech_plural(positions, "pozycji", "pozycjach", "pozycjach")}.')
    customer = ' '.join(str((shipment.get('customer') or {}).get('name') or '').split())
    if customer and len(customer) <= 50 and '@' not in customer and '/' not in customer:
        result += f' Odbiorca: {customer.title() if customer.isupper() else customer}.'
    summary = _speech_items(shipment.get('items') or [])
    if summary:
        result += ' ' + summary
    return result
