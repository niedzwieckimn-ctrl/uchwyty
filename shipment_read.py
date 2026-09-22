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
    'mode': {'type': 'string', 'enum': ['latest', 'date', 'customer']},
    'date': {'type': 'string', 'minLength': 10, 'maxLength': 10},
    'customer_id': {'type': 'integer', 'minimum': 1},
    'customer': {'type': 'string', 'minLength': 1, 'maxLength': 160},
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
    if re.search(r'\bco\s+(?:było|bylo|jest)\s+w\s+zam[oó]wieni', value):
        return False
    # Explicit LP questions keep the existing LP/current/history workflow.
    if re.search(r'\b(?:lp|list\w*\s+pakow|li[śs]ci\w*\s+pakow)', value):
        return False
    return bool(re.search(r'\b(?:co|jakie|ile|pokaż|pokaz|odczytaj)\b', value) and re.search(
        r'\b(?:wysła\w*|wysla\w*|wysyłc\w*|wysylc\w*|wyszło|wyszlo|poszło|poszlo|'
        r'było\s+w\s+pacz\w*|bylo\s+w\s+pacz\w*)\b', value))


def direct_selector(text, today=None):
    """Recognize explicit dates and scope without another model call."""
    value = ' '.join(str(text or '').casefold().split()).strip(' ?!.')
    today = today or datetime.now(WARSAW).date()
    scope = {}
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
    if order or re.fullmatch(r'co\s+(?:ostatnio\s+)?(?:wysłałem|wyslalem|wysłaliśmy|wyslalismy)', value):
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
    sets = []
    for oid in order_ids:
        sets.append({r[0] for r in db.execute('''SELECT DISTINCT i.id FROM invoices i
            LEFT JOIN invoice_allocations ia ON ia.invoice_id=i.id WHERE i.order_id=? OR ia.order_id=?''', (oid,oid))})
    common = set.intersection(*sets) if sets else set()
    return sorted(common or set.union(*sets)) if sets else []


def read(data, *, connection_factory, packing_allowed=True):
    mode = data.get('mode')
    if mode not in ('latest','date','customer'):
        raise ShipmentReadError('Wskaż tryb odczytu wysyłki: latest, date albo customer.')
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
        events.sort(key=lambda e:(e['moment'],bool(e['final_packing_batch_id']),e['shipment_id']), reverse=True)
        # Resolve invoice scope in the backend; the model cannot guess an invoice.
        merged = {}
        for event in events:
            candidates = [event['invoice_id']] if event['invoice_id'] else _invoice_candidates(db,[r['id'] for r in event['members']])
            event['invoice_candidates'] = candidates
            if len(candidates) == 1:
                event['invoice_id'] = candidates[0]
            key = (event['invoice_id'],event['shipped_at']) if not event['tracking'] and event['invoice_id'] else event['shipment_id']
            if key not in merged:
                merged[key] = event
            elif not merged[key]['final_packing_batch_id'] and event['final_packing_batch_id']:
                merged[key] = event
            if mode != 'date':
                break  # Never resolve invoices for the entire shipment history.
        chosen = list(merged.values()) if mode == 'date' else list(merged.values())[:1]
        shipments = [_view(db,event,packing_allowed) for event in chosen]
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
    if len(event['invoice_candidates']) != 1:
        result.update(complete=False,invoice_candidates=event['invoice_candidates'])
        result['issues'].append('Brak jednoznacznego powiązania wysyłki z fakturą. Wymaga wyjaśnienia.')
        return result
    try:
        inv,items,source,conflicting_allocations = invoice_contents(db,event['invoice_id'])
    except ShipmentReadError as exc:
        result.update(complete=False)
        result['issues'].append(str(exc))
        return result
    result.update(invoice_number=inv['invoice_no'],items=items,total_units=sum(i['qty'] for i in items),items_source=source)
    result['customer']['name'] = inv['buyer_name'] or result['customer']['name']
    result['orders'] = [dict(order_id=oid,order_number=next((i['order_number'] for i in items if i['order_id']==oid),'')) for oid in sorted({i['order_id'] for i in items})]
    if conflicting_allocations:
        result.update(complete=False,invoice_allocation_items=conflicting_allocations)
        result['issues'].append('Snapshot JSON faktury i alokacje faktury różnią się. Wymaga wyjaśnienia.')
    if event['final_packing_batch_id'] and packing_allowed:
        final = [dict(order_id=r['order_id'],order_item_id=r['order_item_id'],sku=r['sku_snapshot'],
                      name=r['model_name_snapshot'] or '',qty=r['qty']) for r in db.execute(
                          'SELECT * FROM packing_allocations WHERE batch_id=? ORDER BY id', (event['final_packing_batch_id'],))]
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
        lines.append(f"Wysyłka: {s['shipped_at']} — {s['customer']['name']}")
        lines.append(f"Faktura: {s['invoice_number'] or 'nieustalona'}; tracking: {s['tracking'] or 'brak'}.")
        for issue in s['issues']:
            lines.append('Wymaga wyjaśnienia: '+issue)
        if s.get('invoice_candidates'):
            lines.append('Możliwe faktury (ID): '+', '.join(map(str,s['invoice_candidates']))+'.')
        if s['items']:
            lines.append('Wszystkie pozycje powiązanej faktury:')
            for item in s['items']:
                lines.append(f"- {item['sku']} {item['name']} — {item['qty']} szt. ({item.get('order_number') or item['order_id']})")
            lines.append(f"Razem na fakturze: {s['total_units']} sztuk.")
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
