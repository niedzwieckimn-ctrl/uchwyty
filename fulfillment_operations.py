"""Order fulfillment adapters; business mutations stay in the existing services."""
import hashlib
import json
import math
import os
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from werkzeug.datastructures import MultiDict
from werkzeug.exceptions import HTTPException, Conflict

READS = {'orders.fulfillment.state', 'shipping.requirements.get', 'orders.documents.print_ready'}
WRITES = {'orders.packing_list.generate', 'orders.invoice.create', 'orders.items.add',
          'orders.items.update', 'orders.items.remove', 'shipping.requirements.update',
          'shipping.shipment.create', 'shipping.shipment.refresh', 'shipping.shipment.confirm_parameters', 'shipping.pickup.request'}
PERMISSIONS = {
    'orders.fulfillment.state': 'orders.read_full', 'shipping.requirements.get': 'shipping.read',
    'orders.documents.print_ready': 'orders.read_full',
    'orders.packing_list.generate': 'packing.prepare', 'orders.invoice.create': 'invoices.publish',
    'orders.items.add': 'orders.update', 'orders.items.update': 'orders.update', 'orders.items.remove': 'orders.update',
    'shipping.requirements.update': 'shipping.prepare', 'shipping.shipment.create': 'shipping.create',
    'shipping.shipment.refresh': 'shipping.read', 'shipping.shipment.confirm_parameters': 'shipping.prepare',
    'shipping.pickup.request': 'shipping.request_pickup',
}
GREEN = {'shipping.requirements.update', 'shipping.shipment.refresh'}
b = None


def configure(backend):
    global b
    b = backend


def initialize(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS order_shipping_requirements(order_id INTEGER PRIMARY KEY,
        payload TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS fulfillment_documents(order_id INTEGER NOT NULL, kind TEXT NOT NULL,
        document_id INTEGER NOT NULL, content_hash TEXT NOT NULL, path TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(order_id,kind));
    CREATE TABLE IF NOT EXISTS fulfillment_locks(order_id INTEGER PRIMARY KEY, token TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS fulfillment_document_intents(order_id INTEGER NOT NULL, kind TEXT NOT NULL,
        content_hash TEXT NOT NULL, PRIMARY KEY(order_id,kind));
    CREATE TABLE IF NOT EXISTS fulfillment_shipping_attempts(order_id INTEGER PRIMARY KEY,
        reference TEXT NOT NULL UNIQUE, payload TEXT NOT NULL, content_hash TEXT NOT NULL,
        state TEXT NOT NULL, provider_json TEXT, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS fulfillment_shipping_members(order_id INTEGER PRIMARY KEY, attempt_order_id INTEGER NOT NULL);
    ''')
    if 'file_hash' not in {r[1] for r in db.execute('PRAGMA table_info(fulfillment_documents)')}:
        db.execute("ALTER TABLE fulfillment_documents ADD COLUMN file_hash TEXT NOT NULL DEFAULT ''")


def error(code, message, status='FAILED'):
    from business_operations import ControlledOperationError
    return ControlledOperationError(code, message, status=status)


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str, separators=(',', ':')).encode()).hexdigest()


def _rows(db, sql, params=()):
    return [dict(r) for r in db.execute(sql, params)]


def snapshot(oid, db=None):
    owned = db is None
    db = db or b.conn()
    try:
        order = db.execute('SELECT * FROM orders WHERE id=?', (oid,)).fetchone()
        if not order:
            raise error('NOT_FOUND', 'Nie znaleziono zamówienia.')
        order = dict(order)
        items = _rows(db, 'SELECT * FROM order_items WHERE order_id=? ORDER BY id', (oid,))
        invoices = _rows(db, '''SELECT DISTINCT i.* FROM invoices i LEFT JOIN invoice_allocations a ON a.invoice_id=i.id
            WHERE i.order_id=? OR a.order_id=? ORDER BY i.id''', (oid, oid))
        metas = [dict(r) for i in invoices for r in db.execute('SELECT * FROM invoice_meta WHERE invoice_id=?', (i['id'],))]
        requirements = db.execute('SELECT payload FROM order_shipping_requirements WHERE order_id=?', (oid,)).fetchone()
        docs = _rows(db, 'SELECT * FROM fulfillment_documents WHERE order_id=? ORDER BY kind', (oid,))
        batches = _rows(db, 'SELECT * FROM packing_batches WHERE root_order_id=? ORDER BY id', (oid,))
        attempts = _rows(db, '''SELECT a.* FROM fulfillment_shipping_attempts a
            WHERE a.order_id=? OR a.order_id IN (SELECT attempt_order_id FROM fulfillment_shipping_members WHERE order_id=?)''', (oid, oid))
        intents = _rows(db, 'SELECT * FROM fulfillment_document_intents WHERE order_id=? ORDER BY kind', (oid,))
        allocations = _rows(db, 'SELECT * FROM invoice_allocations WHERE order_id=? ORDER BY id', (oid,))
        # Content identity excludes workflow flags so creating an invoice does
        # not stale the packing document that was generated for those items.
        content = {k: order.get(k) for k in ('customer_id', 'customer_name', 'customer_email',
                  'customer_phone', 'customer_address', 'currency', 'note')}
        content['items'] = items
        return {'order': order, 'items': items, 'invoices': invoices, 'metas': metas,
                'requirements': json.loads(requirements[0]) if requirements else {},
                'documents': docs, 'batches': batches, 'attempts': attempts,
                'allocations': allocations, 'intents': intents, 'receiver': receiver(order),
                'content_hash': _hash(content)}
    finally:
        if owned:
            db.close()


def version(s):
    return int(_hash(s)[:15], 16)


def receiver(order):
    profile = b._client_profile_for_email(order.get('customer_email')) or {}
    address = b.norm(profile.get('address')) or b.norm(order.get('customer_address'))
    street, post_code, city = b.split_address(address)
    return {'name': order.get('customer_name') or '', 'street': street, 'post_code': post_code,
            'city': city, 'phone': b.norm(profile.get('phone')) or b.norm(order.get('customer_phone')),
            'email': order.get('customer_email') or ''}


def effective_receiver(s):
    return {key: s['requirements'].get('recipient_' + key, value) for key, value in s['receiver'].items()}


def state(data, actor=None, correlation_id='', transaction_connection=None):
    s = snapshot(data['order_id'], transaction_connection)
    o = s['order']
    db = b.conn()
    try:
        from fulfillment_readiness import calculate_fulfillment_readiness
        readiness = next((r for r in calculate_fulfillment_readiness(db) if r['order_id'] == o['id']), None)
        fully = b.order_fully_invoiced(db.cursor(), o['id'])
    finally:
        db.close()
    documents = {}
    for kind in ('packing_list', 'invoice', 'label'):
        record = next((d for d in s['documents'] if d['kind'] == kind), None)
        exists = bool(record and Path(record['path']).is_file())
        current = bool(exists and record['content_hash'] == s['content_hash'] and
                       record.get('file_hash') == hashlib.sha256(Path(record['path']).read_bytes()).hexdigest())
        if kind == 'packing_list' and record:
            batch = next((x for x in s['batches'] if x['id'] == record['document_id']), None)
            if not batch or (batch.get('invoice_id') and not any(i['id'] == batch['invoice_id'] for i in s['invoices'])):
                current = False
        if kind == 'invoice':
            exists = bool(s['invoices'])
            current = current and any(i['id'] == record['document_id'] and i.get('publication_state', 'complete') == 'complete' for i in s['invoices']) if record else False
        documents[kind] = {'exists': exists, 'current': current, 'document_id': record['document_id'] if record else None}
    # Untracked legacy invoices are not silently recreated or assumed current.
    attempt = s['attempts'][0] if s['attempts'] else None
    booked_fields = {}
    if attempt:
        booked = json.loads(attempt['payload'])
        parcel = booked.get('parcel') or {}
        booked_fields = {k: parcel[k] / (1 if k == 'weight' else 10)
                         for k in ('length', 'width', 'height', 'weight') if k in parcel}
        booked_fields.update(carrier='inpost', dimension_unit='cm', weight_unit='kg', weight_source='manual')
        booked_fields.update({k: k in ((booked.get('options') or {}).get('additional_services') or []) for k in ('sms', 'email')})
    fields = {**booked_fields, **s['requirements']}
    if not fields.get('carrier') and o.get('carrier'):
        fields['carrier'] = o['carrier']
    target = effective_receiver(s)
    missing = [k for k in ('carrier', 'length', 'width', 'height', 'dimension_unit', 'weight', 'weight_unit', 'sms', 'email') if k not in fields or fields[k] is None or fields[k] == '']
    missing += ['recipient.' + k for k in ('name', 'street', 'post_code', 'city', 'phone', 'email') if not target.get(k)]
    changed = bool(o.get('inpost_shipment_id') and (not attempt or attempt['content_hash'] != s['content_hash'] or
        json.loads(attempt['payload']).get('receiver') != target))
    if attempt and o.get('inpost_shipment_id'):
        booked = json.loads(attempt['payload'])
        changed = changed or not parameters_match(booked, fields)
    ready = bool(fully or (readiness and readiness['ready']))
    pickup = b.inpost_pickup_status(o.get('inpost_shipment_id')) if o.get('inpost_shipment_id') else None
    next_step = ('readiness' if not ready and not s['invoices'] else
                 'review_existing_invoice' if s['invoices'] and not documents['invoice']['current'] else
                 'packing_list' if not documents['packing_list']['current'] else
                 'invoice' if not documents['invoice']['current'] else
                 'reconcile_shipment' if attempt and (not o.get('inpost_shipment_id') or attempt['state'] in ('SENDING', 'UNKNOWN')) else
                 'verify_shipment_parameters' if changed else
                 'shipping_requirements' if not o.get('inpost_shipment_id') and missing else
                 'shipment' if not o.get('inpost_shipment_id') else
                 'label' if not documents['label']['current'] else
                 'pickup' if not (pickup or {}).get('state') else
                 'pickup_review' if (pickup or {}).get('state') in {'unknown', 'rejected', 'configuration_error'} else 'print')
    if next_step == 'review_existing_invoice' and any(i['kind'] == 'invoice' and i['content_hash'] == s['content_hash'] for i in s['intents']):
        next_step = 'invoice'
    return {'ok': True, 'state': {'order_id': o['id'], 'order_number': o.get('order_no'),
        'customer': o.get('customer_name'), 'order_status': o['status'], 'expected_version': version(s),
        'editable': not bool(o.get('warehouse_issued')), 'content_hash': s['content_hash'],
        'readiness': {'complete': ready, 'details': readiness},
        'packing': {'confirmed': o['status'] in ('packed', 'packed_partial'), 'packed_at': o.get('packed_at')},
        **documents, 'invoices': [{'invoice_id': i['id'], 'invoice_number': i['invoice_no'], 'publication_state': i.get('publication_state', 'complete')} for i in s['invoices']],
        'shipment': {'exists': bool(o.get('inpost_shipment_id')), 'carrier': o.get('carrier'),
                     'tracking': o.get('tracking_no'), 'parameters_need_review': changed,
                     'label_exists': documents['label']['exists'],
                     'pickup_confirmed': (pickup or {}).get('state') in {'sent', 'collected'},
                     'pickup': pickup,
                     'attempt_state': attempt['state'] if attempt else None},
        'requirements': {'known': fields, 'recipient': target, 'missing_fields': missing},
        'next_step': next_step}}


@contextmanager
def order_lease(oid, token):
    # OS releases the lock on process exit. Acquiring it proves that an old
    # SQLite marker is orphaned; no clock-based expiry can steal a live write.
    directory = Path(b.DATA_DIR) / 'fulfillment-locks'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (_hash(str(Path(b.DB_PATH).resolve()))[:16] + '-' + str(oid) + '.lock')
    handle = path.open('a+b')
    if path.stat().st_size == 0:
        handle.write(b'0'); handle.flush()
    handle.seek(0)
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise Conflict('Trwa operacja na tym zamówieniu. Sprawdź jej wynik przed ponowieniem.')
    c = b.conn()
    try:
        c.execute('DELETE FROM fulfillment_locks WHERE order_id=?', (oid,))
        c.execute('INSERT INTO fulfillment_locks VALUES(?,?)', (oid, token))
        c.commit()
    except Exception:
        c.rollback()
        handle.close()
        raise
    finally:
        c.close()
    try:
        yield
    finally:
        try:
            release(oid, token)
        finally:
            handle.close()


@contextmanager
def ui_write(oid):
    with order_lease(oid, 'ui:' + str(uuid.uuid4())):
        yield


def guard_ui(oid):
    c = b.conn()
    try:
        if c.execute('SELECT 1 FROM fulfillment_locks WHERE order_id=?', (oid,)).fetchone():
            raise Conflict('Trwa operacja na tym zamówieniu.')
    finally:
        c.close()


def release(oid, token):
    c = b.conn()
    try:
        c.execute('DELETE FROM fulfillment_locks WHERE order_id=? AND token=?', (oid, token))
        c.commit()
    finally:
        c.close()


def request_view(method='POST', form=None, args=None):
    return SimpleNamespace(method=method, form=MultiDict(form or {}), args=MultiDict(args or {}))


def _check_response(result):
    if isinstance(result, tuple):
        raise error('BUSINESS_RULE_BLOCKED', str(result[0]))
    if isinstance(result, dict) and not result.get('ok'):
        raise error('BUSINESS_RULE_BLOCKED', result.get('error') or 'Operacja nie została zakończona.')
    if not isinstance(result, dict) and getattr(result, 'status_code', 500) not in (200, 302):
        raise error('BUSINESS_RULE_BLOCKED', 'Aplikacja odmówiła wykonania operacji.')
    return result


def save_document(oid, kind, document_id, path):
    s = snapshot(oid)
    c = b.conn()
    try:
        c.execute('INSERT OR REPLACE INTO fulfillment_documents VALUES(?,?,?,?,?,?,?)',
                  (oid, kind, document_id, s['content_hash'], str(path), b.now_iso(),
                   hashlib.sha256(Path(path).read_bytes()).hexdigest()))
        c.commit()
    finally:
        c.close()


def safe_create_shipment(oid, recipient, parcel, reference, service, options):
    """Used by UI and the service. A lost POST response can only be reconciled."""
    from inpost_module import InPostError
    payload = {'receiver': recipient, 'parcel': parcel, 'service': service, 'options': options}
    s = snapshot(oid)
    c = b.conn()
    try:
        c.execute('BEGIN IMMEDIATE')
        package_ids = sorted(int(o['id']) for o in b._packed_package_orders(c.cursor(), s['order']))
        payload['order_ids'] = package_ids
        marks = ','.join('?' for _ in package_ids)
        old = c.execute(f'''SELECT a.* FROM fulfillment_shipping_attempts a WHERE a.order_id IN ({marks})
            OR a.order_id IN (SELECT attempt_order_id FROM fulfillment_shipping_members WHERE order_id IN ({marks})) LIMIT 1''', tuple(package_ids + package_ids)).fetchone()
        if old and old['state'] == 'SUCCESS':
            previous_id = str((json.loads(old['provider_json'] or '{}')).get('id') or '')
            previous_ids = sorted(json.loads(old['payload']).get('order_ids') or [old['order_id']])
            # Existing prepare_next archives only provider-verified collected
            # shipments. That business evidence permits a genuinely new batch.
            archived = previous_id and previous_ids == package_ids and all(
                c.execute('SELECT 1 FROM inpost_shipment_history WHERE order_id=? AND shipment_id=?', (member, previous_id)).fetchone()
                and not c.execute('SELECT 1 FROM orders WHERE id=? AND inpost_shipment_id=?', (member, previous_id)).fetchone()
                for member in package_ids)
            if archived:
                c.execute('DELETE FROM fulfillment_shipping_members WHERE attempt_order_id=?', (old['order_id'],))
                c.execute('DELETE FROM fulfillment_shipping_attempts WHERE order_id=?', (old['order_id'],))
                old = None
        if old:
            if old['state'] == 'SUCCESS':
                if json.loads(old['payload']) != payload or old['content_hash'] != s['content_hash']:
                    raise InPostError('Istnieje wcześniejsze nadanie z inną zawartością lub parametrami. Zweryfikuj przesyłkę przed kolejną paczką.')
                return json.loads(old['provider_json'])
            raise InPostError('Wynik wcześniejszego nadania jest niepewny. Sprawdź przesyłkę; nie twórz kolejnej.')
        stable = 'fulfillment-' + str(uuid.uuid4())
        c.execute('INSERT INTO fulfillment_shipping_attempts VALUES(?,?,?,?,?,?,?)',
            (oid, stable, json.dumps(payload), s['content_hash'], 'SENDING', None, b.now_iso()))
        c.executemany('INSERT INTO fulfillment_shipping_members VALUES(?,?)', [(member, oid) for member in package_ids])
        c.commit()
    finally:
        c.close()
    try:
        if b.supabase_enabled():
            claim = b.supabase_request('/rest/v1/rpc/claim_fulfillment_shipment', method='POST',
                payload={'p_order_ids': package_ids, 'p_reference': stable, 'p_payload': payload,
                         'p_content_hash': s['content_hash'], 'p_created_at': b.now_iso()})
            if not isinstance(claim, dict) or not isinstance(claim.get('claim'), dict):
                raise InPostError('Nie potwierdzono trwałej rezerwacji nadania. Sprawdź migrację fulfillment.')
            saved = claim['claim']
            if not claim.get('acquired'):
                c = b.conn()
                c.execute('UPDATE fulfillment_shipping_attempts SET reference=?,payload=?,content_hash=?,state=?,provider_json=?,created_at=? WHERE order_id=?',
                    (saved['reference'], json.dumps(saved['payload']), saved['content_hash'], saved['state'], json.dumps(saved.get('provider_json')) if saved.get('provider_json') else None, saved['created_at'], oid))
                c.commit(); c.close()
                if saved['state'] == 'SUCCESS' and saved.get('provider_json'):
                    if saved['payload'] != payload or saved['content_hash'] != s['content_hash']:
                        raise InPostError('Istnieje wcześniejsze nadanie w chmurze. Zweryfikuj jego zawartość i parametry.')
                    return saved['provider_json']
                raise InPostError('W chmurze istnieje niepewna próba nadania. Wolno tylko sprawdzić jej wynik.')
        result = b.create_courier_shipment(recipient, parcel, stable, service, options)
        if not result.get('id'):
            raise InPostError('Brak identyfikatora przesyłki w odpowiedzi.')
    except Exception:
        c = b.conn()
        c.execute("UPDATE fulfillment_shipping_attempts SET state='UNKNOWN' WHERE order_id=?", (oid,))
        c.commit()
        c.close()
        raise
    c = b.conn()
    c.execute("UPDATE fulfillment_shipping_attempts SET state='SUCCESS',provider_json=? WHERE order_id=?", (json.dumps(result), oid))
    c.commit()
    c.close()
    if b.supabase_enabled():
        # Losing this acknowledgement does not permit another POST: the cloud
        # claim remains SENDING and can be recovered by its stable reference.
        try:
            b.supabase_request('/rest/v1/fulfillment_shipping_claims', method='PATCH',
                params={'reference': 'eq.' + stable}, payload={'state': 'SUCCESS', 'provider_json': result})
        except Exception:
            b.app.logger.exception('Shipment accepted; durable cloud result requires reconciliation')
    return result


def _validate_requirements(fields):
    allowed = {'carrier', 'length', 'width', 'height', 'dimension_unit', 'weight', 'weight_unit', 'weight_source', 'sms', 'email'}
    allowed.update('recipient_' + key for key in ('name', 'street', 'post_code', 'city', 'phone', 'email'))
    if set(fields) - allowed:
        raise error('INVALID_INPUT', 'Nieobsługiwane pole paczki.')
    for key in ('length', 'width', 'height', 'weight'):
        if key in fields and (isinstance(fields[key], bool) or not isinstance(fields[key], (int, float)) or not math.isfinite(fields[key]) or fields[key] <= 0):
            raise error('INVALID_INPUT', 'Wymiary i waga muszą być dodatnimi liczbami.')
    for key, lower, upper in [('length', 0.1, 350), ('width', 0.1, 240), ('height', 0.1, 240), ('weight', 0.01, 50)]:
        if key in fields and not lower <= fields[key] <= upper:
            raise error('INVALID_INPUT', 'Parametry paczki przekraczają zakres istniejącego formularza InPost.')
    for key in fields:
        if key.startswith('recipient_') and (not isinstance(fields[key], str) or not fields[key].strip() or len(fields[key]) > 200):
            raise error('INVALID_INPUT', 'Dane odbiorcy muszą być niepustym tekstem do 200 znaków.')
    for key in ('sms', 'email'):
        if key in fields and not isinstance(fields[key], bool):
            raise error('INVALID_INPUT', 'Opcje powiadomień wymagają wartości logicznej.')
    for key, expected in [('carrier', 'inpost'), ('dimension_unit', 'cm'), ('weight_unit', 'kg'), ('weight_source', 'manual')]:
        if key in fields and fields[key] != expected:
            raise error('INVALID_INPUT', 'Nieobsługiwany przewoźnik, jednostka lub źródło wagi.')


def parameters_match(booked, fields):
    parcel = booked.get('parcel') or {}
    for key in ('length', 'width', 'height', 'weight'):
        if key in fields and float(parcel.get(key) or 0) != float(fields[key]) * (10 if key != 'weight' else 1):
            return False
    options = (booked.get('options') or {}).get('additional_services') or []
    return all(key not in fields or fields[key] == (key in options) for key in ('sms', 'email'))


def perform(name, data, actor):
    oid = data['order_id']
    s = snapshot(oid)
    current = state({'order_id': oid})['state']
    if name == 'shipping.requirements.update':
        fields = {k: v for k, v in data.items() if k not in ('order_id', 'expected_version', 'idempotency_key')}
        _validate_requirements(fields)
        merged = {**s['requirements'], **fields}
        if 'weight' in fields:
            merged['weight_source'] = 'manual'
        c = b.conn()
        c.execute('INSERT OR REPLACE INTO order_shipping_requirements VALUES(?,?,?)', (oid, json.dumps(merged), b.now_iso()))
        c.commit(); c.close()
    elif name.startswith('orders.items.'):
        if (s['invoices'] or any(d['kind'] == 'packing_list' for d in s['documents'])) and not data.get('documents_change_confirmed'):
            raise error('DOCUMENTS_CONFIRMATION_REQUIRED', 'Zmiana unieważni dokumenty. Najpierw potwierdź ich ponowne przygotowanie.')
        if not current['editable']:
            raise error('INVOICE_BLOCKS_EDIT', 'Najpierw użyj istniejącego preview i zatwierdzonego usunięcia faktury, potem ponownie odczytaj zamówienie.')
        if name != 'orders.items.add' and not any(i['id'] == data['item_id'] for i in s['items']):
            raise error('ITEM_NOT_FOUND', 'Pozycja nie należy do zamówienia.')
        method = {'orders.items.add': b.order_item_add_service, 'orders.items.update': b.order_item_update_service, 'orders.items.remove': b.order_item_delete_service}[name]
        args = (oid,) if name.endswith('.add') else (oid, data['item_id'])
        _check_response(method(*args, request=request_view(form={'product_id': data.get('product_id'), 'qty': data.get('quantity')}), structured=True))
    elif name == 'orders.packing_list.generate':
        if current['packing_list']['current']:
            return state(data)
        if s['invoices']:
            raise error('EXISTING_INVOICE', 'Najpierw sprawdź istniejącą fakturę. Nie przebudowuję jej listy pakowej w ciemno.')
        if not current['readiness']['complete']:
            raise error('ORDER_NOT_READY', 'Brakuje produktów do kompletnej realizacji.')
        form = {'carrier': 'pending', **{f"pack_qty_{i['id']}": i['qty'] for i in s['items']}}
        result = _check_response(b.order_packing_list_download_admin_service(oid, request=request_view(form=form), session={}, structured=True))
        save_document(oid, 'packing_list', result['batch_id'], result['path'])
    elif name == 'orders.invoice.create':
        if current['invoice']['current']:
            return state(data)
        if not current['packing_list']['current']:
            raise error('PACKING_REQUIRED', 'Najpierw przygotuj aktualną listę pakową.')
        if s['invoices']:
            c = b.conn()
            intent = c.execute("SELECT content_hash FROM fulfillment_document_intents WHERE order_id=? AND kind='invoice'", (oid,)).fetchone()
            c.close()
            if len(s['invoices']) != 1 or not intent or intent[0] != s['content_hash']:
                raise error('EXISTING_INVOICE', 'Faktura już istnieje. Sprawdź ją; nie tworzę duplikatu.')
            iid = s['invoices'][0]['id']
            if s['invoices'][0].get('publication_state', 'complete') != 'complete':
                b.resume_invoice_job(iid)
                b.finalize_fully_invoiced_orders([oid])
            selection = b.load_open_packing_selection(oid)
            if selection:
                b.consume_packing_selection(selection['batch_id'], iid)
            result = {'invoice_id': iid}
        else:
            c = b.conn()
            c.execute("INSERT OR REPLACE INTO fulfillment_document_intents VALUES(?,'invoice',?)", (oid, s['content_hash']))
            c.commit(); c.close()
            plan = _check_response(b.order_invoice_service(oid, request=request_view('GET'), session={}, structured=True))
            form = dict(plan['defaults'])
            form.update({f'invoice_qty_{iid}': qty for iid, qty in plan['packing_qty'].items()})
            result = _check_response(b.order_invoice_service(oid, request=request_view(form=form), session={}, structured=True))
        inv = b.load_invoice_with_meta(result['invoice_id'])
        ok, path = b.invoice_pdf_exists(inv.get('pdf_path', ''), inv.get('invoice_no', ''))
        if not ok:
            raise error('INVOICE_PDF_UNAVAILABLE', 'Faktura została zapisana, ale jej PDF nie jest dostępny lokalnie. Wznów przygotowanie dokumentu.')
        save_document(oid, 'invoice', result['invoice_id'], path)
    elif name == 'shipping.shipment.create':
        if current['shipment']['exists']:
            return state(data)
        if not current['packing_list']['current'] or not current['invoice']['current']:
            raise error('CURRENT_DOCUMENTS_REQUIRED', 'Przed nadaniem wymagane są aktualne dokumenty.')
        if current['requirements']['missing_fields']:
            raise error('MISSING_SHIPPING_FIELDS', 'Brakuje danych paczki: ' + ', '.join(current['requirements']['missing_fields']))
        c = b.conn()
        package_ids = [int(o['id']) for o in b._packed_package_orders(c.cursor(), s['order'])]
        c.close()
        if package_ids != [oid]:
            raise error('COMBINED_PACKAGE_REVIEW', 'Zamówienie należy do paczki zbiorczej. Nadaj ją istniejącym formularzem po sprawdzeniu wszystkich zamówień.')
        fields = current['requirements']['known']
        _validate_requirements(fields)
        form = {**fields, 'service': 'inpost_courier_standard', 'sms': '1' if fields['sms'] else '0', 'email': '1' if fields['email'] else '0', 'quantity': 1}
        _check_response(b.order_inpost_create_service(oid, request=request_view(form=form), session={}, structured=True,
                       receiver_override=current['requirements']['recipient']))
    elif name == 'shipping.shipment.refresh':
        sid = s['order'].get('inpost_shipment_id')
        if not sid and s['attempts']:
            from inpost_module import find_shipment_by_reference
            attempt = s['attempts'][0]
            remote = json.loads(attempt['provider_json']) if attempt['provider_json'] else find_shipment_by_reference(attempt['reference'], attempt['created_at'])
            if not remote:
                raise error('SHIPMENT_OUTCOME_UNKNOWN', 'Nie potwierdzono wyniku nadania. Nie tworzę drugiej przesyłki; ponów tylko sprawdzenie.')
            sid = remote['id']
            members = json.loads(attempt['payload']).get('order_ids') or [oid]
            b.persist_inpost_result(members, str(sid), str(remote.get('tracking_number') or ''), enqueue_pickup=False)
            c = b.conn(); c.execute("UPDATE fulfillment_shipping_attempts SET state='SUCCESS',provider_json=? WHERE order_id=?", (json.dumps(remote), attempt['order_id'])); c.commit(); c.close()
            if b.supabase_enabled():
                b.supabase_request('/rest/v1/fulfillment_shipping_claims', method='PATCH', params={'reference': 'eq.' + attempt['reference']}, payload={'state': 'SUCCESS', 'provider_json': remote})
        if not sid:
            raise error('NO_SHIPMENT', 'Nie ma przesyłki do sprawdzenia.')
        remote = b.inpost_get_shipment(sid)
        b.persist_inpost_result([oid], str(sid), str(remote.get('tracking_number') or ''), enqueue_pickup=False)
        if state(data)['state']['shipment']['parameters_need_review']:
            return state(data)
        pdf = b.inpost_get_label(sid, 'pdf', 'A6')
        directory = Path(b.DATA_DIR) / 'fulfillment'
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / ('label-' + str(oid) + '-' + str(uuid.uuid4()) + '.pdf')
        path.write_bytes(pdf)
        save_document(oid, 'label', int(sid), path)
    elif name == 'shipping.pickup.request':
        if not s['order'].get('inpost_shipment_id'):
            raise error('NO_SHIPMENT', 'Najpierw utwórz przesyłkę.')
        b.enqueue_automatic_inpost_pickup(s['order']['inpost_shipment_id'])
    elif name == 'shipping.shipment.confirm_parameters':
        if not data.get('human_confirmed'):
            raise error('CONFIRMATION_REQUIRED', 'Człowiek musi potwierdzić dane istniejącej przesyłki po zmianie zawartości.')
        if not s['attempts']:
            raise error('LEGACY_SHIPMENT_REVIEW', 'Brakuje zapisanych parametrów przesyłki. Sprawdź je w InPost; nie potwierdzam ich automatycznie.')
        old = json.loads(s['attempts'][0]['payload'])
        if old['receiver'] != effective_receiver(s):
            raise error('RECIPIENT_CHANGED', 'Odbiorca lub adres różni się od nadania. Wymagana jest obsługa u przewoźnika.')
        if not parameters_match(old, current['requirements']['known']):
            raise error('PARCEL_CHANGED', 'Parametry paczki różnią się od nadania. Wymagana jest obsługa u przewoźnika; nie anuluję przesyłki.')
        c = b.conn(); c.execute('UPDATE fulfillment_shipping_attempts SET content_hash=? WHERE order_id=?', (s['content_hash'], oid)); c.commit(); c.close()
    return state(data)


def print_ready(data, actor, correlation_id='', transaction_connection=None):
    from internal_rbac import DENY
    permissions = {'packing_list': 'packing.read', 'invoice': 'invoices.read', 'label': 'shipping.label_read'}
    current = state(data)['state']
    if current['shipment']['parameters_need_review']:
        raise error('SHIPMENT_REVIEW_REQUIRED', 'Po zmianie zamówienia trzeba sprawdzić parametry istniejącej przesyłki przed drukiem.')
    result = []
    for kind, permission in permissions.items():
        if actor.permission_decision(permission) == DENY:
            raise error('PERMISSION_DENIED', 'Brak uprawnień do jednego z dokumentów.', 'DENIED')
        if not current[kind]['current']:
            raise error('STALE_DOCUMENT', 'Nie wszystkie dokumenty są aktualne i dostępne. Najpierw dokończ przygotowanie.')
        result.append({'type': 'document_link', 'document_type': kind,
            'name': {'packing_list': 'Lista pakowa', 'invoice': 'Faktura', 'label': 'Etykieta InPost'}[kind],
            'url': f"/api/internal/fulfillment/{data['order_id']}/documents/{kind}"})
    return {'ok': True, 'state': current, 'documents': result, 'message': 'Aktualne dokumenty są gotowe do otwarcia i druku w przeglądarce. Nie potwierdzam fizycznego wydruku.'}


def execute(execution_id, definition, actor, data, approval_id, entity_type, entity_id, expected_version, correlation_id):
    import business_operations as ops
    import internal_approval as approval
    from internal_rbac import load_actor_context, DENY
    from internal_audit import record_audit_event
    oid = data['order_id']; locked = False; db = None; lease = None
    try:
        human = load_actor_context(actor.delegated_by_actor_id or actor.actor_id)
        if not human or human.permission_decision(definition.required_permission) == DENY:
            raise error('PERMISSION_DENIED', 'Inicjator utracił uprawnienie.', 'DENIED')
        if definition.operation_name == 'shipping.shipment.create' and any(a.permission_decision('shipping.request_pickup') == DENY for a in (human, actor)):
            raise error('PERMISSION_DENIED', 'Brak uprawnienia do zlecenia podjazdu.', 'DENIED')
        if definition.operation_name == 'shipping.shipment.refresh' and any(a.permission_decision('shipping.label_read') == DENY for a in (human, actor)):
            raise error('PERMISSION_DENIED', 'Brak uprawnienia do pobrania etykiety.', 'DENIED')
        lease = order_lease(oid, execution_id)
        lease.__enter__()
        locked = True
        before = snapshot(oid)
        if definition.operation_name not in {'shipping.shipment.refresh', 'shipping.pickup.request'} and (before['order'].get('shipped_at') or before['order']['status'] in {'shipped', 'partially_shipped', 'completed', 'cancelled', 'issued', 'in_delivery'}):
            raise error('ORDER_CLOSED', 'Zamówienie zostało wysłane lub zamknięte.', 'DENIED')
        db = b.conn(); db.execute('BEGIN IMMEDIATE')
        now_version = version(snapshot(oid, db))
        if now_version != expected_version:
            raise error('ENTITY_VERSION_CONFLICT', 'Zamówienie zostało zmienione. Odczytaj aktualne dane.', 'CONFLICT')
        if approval_id:
            approval.authorize_execution(approval_id, actor, definition.operation_name, payload=data,
                entity_type=entity_type, entity_id=entity_id, operation_version=1,
                expected_entity_version=expected_version, current_entity_version=now_version, transaction_connection=db)
        record_audit_event(definition.operation_name, result='SUCCESS', actor_context=actor,
            entity_type='order', entity_id=str(oid), correlation_id=correlation_id, approval_id=approval_id,
            before_state=before, after_state={'phase': 'started'}, transaction_connection=db)
        db.commit(); db.close(); db = None; locked = True
        output = ops.validate_output(definition, perform(definition.operation_name, data, actor))
        record_audit_event(definition.operation_name, result='SUCCESS', actor_context=actor,
            entity_type='order', entity_id=str(oid), correlation_id=correlation_id, approval_id=approval_id,
            before_state=before, after_state=output)
        row = ops._transition(execution_id, definition, actor, 'SUCCESS', 'business_operation.success', 'SUCCESS', data=output, completed=True, expected_statuses=('RUNNING',))
        if ops._write_success_observer:
            try: ops._write_success_observer(definition.operation_name, output)
            except Exception: ops.logger.exception('Fulfillment freshness observer failed')
        return ops._result_from_row(row)
    except Exception as exc:
        if db:
            db.rollback(); db.close()
        status = getattr(exc, 'status', 'FAILED')
        if isinstance(exc, approval.ApprovalDenied): status = 'DENIED'
        if isinstance(exc, approval.StaleApproval): status = 'CONFLICT'
        message = str(exc.description) if isinstance(exc, HTTPException) else str(exc)
        code = getattr(exc, 'error_code', 'FULFILLMENT_STEP_FAILED')
        row = ops._transition(execution_id, definition, actor, status, 'business_operation.' + status.lower(), status,
            error_code=code, message=message, completed=True, expected_statuses=('RUNNING',))
        return ops._result_from_row(row or ops._execution(execution_id))
    finally:
        if locked: lease.__exit__(None, None, None)


def install(ops):
    output = {'ok': {'type': 'boolean'}, 'state': {'type': 'object'}, 'documents': {'type': 'array'}, 'message': {'type': 'string'}}
    for name in sorted(READS | WRITES):
        read = name in READS
        props = {'order_id': {'type': 'integer', 'minimum': 1}}
        required = ['order_id']
        if not read:
            props.update(expected_version={'type': 'integer', 'minimum': 0}, idempotency_key={'type': 'string', 'minLength': 1, 'maxLength': 200})
            required += ['expected_version', 'idempotency_key']
        if name.startswith('orders.items.'):
            props['documents_change_confirmed'] = {'type': 'boolean'}
            if name.endswith('.add'): props['product_id'] = {'type': 'integer', 'minimum': 1}; required += ['product_id']
            else: props['item_id'] = {'type': 'integer', 'minimum': 1}; required += ['item_id']
            if not name.endswith('.remove'): props['quantity'] = {'type': 'integer', 'minimum': 1}; required += ['quantity']
        if name == 'shipping.requirements.update':
            props.update({k: {'type': 'number'} for k in ('length', 'width', 'height', 'weight')})
            props.update({k: {'type': 'string'} for k in ('carrier', 'dimension_unit', 'weight_unit', 'weight_source')})
            props.update({k: {'type': 'boolean'} for k in ('sms', 'email')})
            props.update({'recipient_' + k: {'type': 'string', 'minLength': 1, 'maxLength': 200}
                          for k in ('name', 'street', 'post_code', 'city', 'phone', 'email')})
        if name == 'shipping.shipment.confirm_parameters':
            props['human_confirmed'] = {'type': 'boolean'}; required += ['human_confirmed']
        risk = 'GREEN' if read or name in GREEN else 'YELLOW'
        ops.OPERATION_REGISTRY[name] = ops.BusinessOperationDefinition(name, 1,
            'Realizuje jeden krok istniejącego fulfillment. Najpierw orders.fulfillment.state; użyj zwróconej expected_version. Po WRITE odczytaj state z wyniku i kontynuuj next_step. Nie odtwarzaj poprawnych dokumentów. Pytaj tylko o requirements.missing_fields. Nie anuluj przesyłki. shipping.shipment.refresh odzyskuje wynik timeoutu bez ponownego POST. Druk oznacza przygotowanie PDF, nie fizyczny wydruk.',
            PERMISSIONS[name], risk, 'NONE' if risk == 'GREEN' else 'REQUIRED', frozenset({'HUMAN', 'AI_AGENT'}),
            {'type': 'object', 'additionalProperties': False, 'required': required, 'properties': props},
            {'type': 'object', 'required': ['ok', 'state'], 'properties': output},
            ops.IDEMPOTENCY_NONE if read else ops.IDEMPOTENCY_REQUIRED, 'READ_STANDARD' if read else 'WRITE', read)
        ops._HANDLERS[name] = print_ready if name == 'orders.documents.print_ready' else state
        if read: ops.FRESHNESS_GROUP_BY_OPERATION[name] = 'fulfillment_workflow'
