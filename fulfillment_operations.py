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

READS = {'orders.fulfillment.state', 'orders.packing_list.preview', 'shipping.requirements.get', 'orders.documents.print_ready', 'shipping.capabilities'}
READS |= {'orders.documents.adoption.preview', 'shipping.shipment.adoption.preview'}
WRITES = {'orders.packing_list.generate', 'orders.invoice.create', 'orders.items.add',
          'orders.items.update', 'orders.items.remove', 'shipping.requirements.update',
          'shipping.shipment.create', 'shipping.shipment.refresh', 'shipping.shipment.confirm_parameters', 'shipping.pickup.request'}
WRITES |= {'orders.documents.adopt', 'shipping.shipment.adopt'}
WRITES.add('orders.fulfillment.reconcile')
PERMISSIONS = {
    'orders.fulfillment.reconcile': 'orders.update',
    'orders.documents.adoption.preview': 'invoices.read', 'orders.documents.adopt': 'invoices.publish',
    'shipping.shipment.adoption.preview': 'shipping.read', 'shipping.shipment.adopt': 'shipping.prepare',
    'shipping.capabilities': 'shipping.read',
    'orders.fulfillment.state': 'orders.read_full', 'orders.packing_list.preview': 'packing.read',
    'shipping.requirements.get': 'shipping.read',
    'orders.documents.print_ready': 'orders.read_full',
    'orders.packing_list.generate': 'packing.prepare', 'orders.invoice.create': 'invoices.publish',
    'orders.items.add': 'orders.update', 'orders.items.update': 'orders.update', 'orders.items.remove': 'orders.update',
    'shipping.requirements.update': 'shipping.prepare', 'shipping.shipment.create': 'shipping.create',
    'shipping.shipment.refresh': 'shipping.read', 'shipping.shipment.confirm_parameters': 'shipping.prepare',
    'shipping.pickup.request': 'shipping.request_pickup',
}
GREEN = {'shipping.requirements.update', 'shipping.shipment.refresh', 'orders.fulfillment.reconcile'}
b = None


def configure(backend):
    global b
    b = backend


def initialize(db):
    import reconciliation_store
    reconciliation_store.initialize(db)
    db.executescript('''
    CREATE TABLE IF NOT EXISTS order_shipping_requirements(order_id INTEGER PRIMARY KEY,
        payload TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS fulfillment_documents(order_id INTEGER NOT NULL, kind TEXT NOT NULL,
        document_id INTEGER NOT NULL, content_hash TEXT NOT NULL, path TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(order_id,kind));
    CREATE TABLE IF NOT EXISTS fulfillment_document_history(
        order_id INTEGER NOT NULL,
        kind TEXT NOT NULL,
        document_id INTEGER NOT NULL,
        content_hash TEXT NOT NULL,
        path TEXT NOT NULL,
        created_at TEXT NOT NULL,
        file_hash TEXT NOT NULL DEFAULT '',
        PRIMARY KEY(order_id,kind,document_id));
    CREATE INDEX IF NOT EXISTS idx_fulfillment_document_history_document
        ON fulfillment_document_history(kind,document_id,order_id);
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
    db.execute('''INSERT OR IGNORE INTO fulfillment_document_history(
                    order_id,kind,document_id,content_hash,path,created_at,file_hash)
                  SELECT order_id,kind,document_id,content_hash,path,created_at,
                         COALESCE(file_hash,'')
                    FROM fulfillment_documents
                   WHERE kind='packing_list' ''')
    db.executescript('''
    CREATE TRIGGER IF NOT EXISTS fulfillment_document_history_no_update
    BEFORE UPDATE ON fulfillment_document_history
    BEGIN SELECT RAISE(ABORT, 'fulfillment document history is immutable'); END;
    CREATE TRIGGER IF NOT EXISTS fulfillment_document_history_no_delete
    BEFORE DELETE ON fulfillment_document_history
    BEGIN SELECT RAISE(ABORT, 'fulfillment document history is immutable'); END;
    ''')


def error(code, message, status='FAILED'):
    from business_operations import ControlledOperationError
    return ControlledOperationError(code, message, status=status)


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str, separators=(',', ':')).encode()).hexdigest()


def _document_file_hash(path):
    try:
        value = str(path or '')
        downloaded = b.supabase_storage_download_bytes(value) if b.parse_supabase_storage_ref(value) else None
        content = downloaded[0] if isinstance(downloaded, tuple) else downloaded
        if content is None:
            content = Path(value).read_bytes()
        if not isinstance(content, bytes):
            raise ValueError('Nieprawidłowa zawartość dokumentu')
        return hashlib.sha256(content).hexdigest()
    except Exception:
        return ''


def _rows(db, sql, params=()):
    return [dict(r) for r in db.execute(sql, params)]


def snapshot(oid, db=None, include_package=True):
    owned = db is None
    if owned:
        import reconciliation_store
        try:
            reconciliation_store.restore(b, oid)
        except Exception as exc:
            raise error('RECONCILIATION_UNAVAILABLE', 'Nie można odczytać trwałych metadanych realizacji. Sprawdź dostęp do Supabase i migrację 14.1.') from exc
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
        batches = _rows(db, '''SELECT DISTINCT pb.* FROM packing_batches pb
            LEFT JOIN packing_allocations pa ON pa.batch_id=pb.id
            WHERE pb.root_order_id=? OR pa.order_id=? ORDER BY pb.id''', (oid, oid))
        attempts = _rows(db, '''SELECT a.* FROM fulfillment_shipping_attempts a
            WHERE a.order_id=? OR a.order_id IN (SELECT attempt_order_id FROM fulfillment_shipping_members WHERE order_id=?)''', (oid, oid))
        intents = _rows(db, 'SELECT * FROM fulfillment_document_intents WHERE order_id=? ORDER BY kind', (oid,))
        allocations = _rows(db, 'SELECT * FROM invoice_allocations WHERE order_id=? ORDER BY id', (oid,))
        verifications = _rows(db, 'SELECT * FROM fulfillment_verifications WHERE order_id=? ORDER BY kind', (oid,))
        import reconciliation_store
        persistence = reconciliation_store.local_status(b, db, oid)
        package_ids = sorted({int(o['id']) for o in b._packed_package_orders(db.cursor(), order)}) if include_package else [oid]
        if owned:
            for member in package_ids:
                if member != oid:
                    reconciliation_store.restore(b, member)
        package_versions = {str(member): version(snapshot(member, db, include_package=False)) for member in package_ids if member != oid}
        pending_members = [member for member in package_ids if reconciliation_store.local_status(b, db, member)['pending']]
        persistence.update(pending=bool(pending_members), durable=not bool(pending_members), pending_order_ids=pending_members)
        # Content identity excludes workflow flags so creating an invoice does
        # not stale the packing document that was generated for those items.
        content = {k: order.get(k) for k in ('customer_id', 'customer_name', 'customer_email',
                  'customer_phone', 'customer_address', 'currency', 'note')}
        content['items'] = items
        return {'order': order, 'items': items, 'invoices': invoices, 'metas': metas,
                'requirements': json.loads(requirements[0]) if requirements else {},
                'documents': docs, 'batches': batches, 'attempts': attempts,
                'allocations': allocations, 'intents': intents, 'receiver': receiver(order),
                'verifications': verifications, 'package_ids': package_ids, 'package_versions': package_versions,
                'persistence': persistence,
                'content_hash': _hash(content)}
    finally:
        if owned:
            db.close()


def version(s):
    return int(_hash(s)[:15], 16)


def shipment_content(s):
    if len(s['package_ids']) == 1:
        return s['content_hash']
    return _hash([(oid, s['content_hash'] if oid == s['order']['id'] else snapshot(oid, include_package=False)['content_hash']) for oid in s['package_ids']])


def receiver(order):
    profile = b._client_profile_for_email(order.get('customer_email')) or {}
    address = b.norm(profile.get('address')) or b.norm(order.get('customer_address'))
    street, post_code, city = b.split_address(address)
    return {'name': order.get('customer_name') or '', 'street': street, 'post_code': post_code,
            'city': city, 'phone': b.norm(profile.get('phone')) or b.norm(order.get('customer_phone')),
            'email': order.get('customer_email') or ''}


def effective_receiver(s):
    return {key: s['requirements'].get('recipient_' + key, value) for key, value in s['receiver'].items()}


def capabilities(data, actor=None, correlation_id='', transaction_connection=None):
    cfg = b.inpost_config_summary()
    return {'ok': True, 'state': {}, 'capabilities': [{
        'provider': 'inpost', 'configured': bool(cfg.get('configured')),
        'blocker': '' if cfg.get('configured') else 'MISSING_CONFIGURATION',
        'create_shipment': True, 'retrieve_label': True, 'tracking': True,
        'parcel_types': ['courier_standard'], 'dimension_unit': 'cm', 'weight_unit': 'kg',
        'required_parameters': ['length', 'width', 'height', 'weight', 'sms', 'email'],
        'missing_configuration': cfg.get('missing') or []}]}


def _order_closed_for_operation(name, order):
    status = str(order.get('status') or '').lower()
    # A failed legacy packing attempt can leave a partially shipped order as
    # packed_partial without its document.  Packing-list generation is the
    # recovery operation for that exact state.
    if name == 'orders.packing_list.generate' and status == 'packed_partial':
        return False
    if order.get('shipped_at') and status != 'partially_shipped':
        return True
    closed = {'shipped', 'completed', 'cancelled', 'issued', 'in_delivery'}
    if status in closed:
        return True
    return status == 'partially_shipped' and name != 'orders.packing_list.generate'


def _has_open_current_packing(snapshot_data, current):
    if not current['packing_list']['current']:
        return False
    document_id = current['packing_list'].get('document_id')
    open_batch = any(
        int(batch.get('id') or 0) == int(document_id or 0) and not batch.get('invoice_id')
        for batch in snapshot_data['batches']
    )
    if not open_batch:
        return False
    db = b.conn()
    try:
        member_ids = sorted({
            int(row['order_id'])
            for row in db.execute(
                'SELECT order_id FROM packing_allocations WHERE batch_id=?',
                (int(document_id),),
            ).fetchall()
        })
        if not member_ids:
            return False
        placeholders = ','.join('?' for _ in member_ids)
        records = {
            int(row['order_id']): dict(row)
            for row in db.execute(
                f'''SELECT * FROM fulfillment_documents
                    WHERE kind='packing_list' AND document_id=?
                      AND order_id IN ({placeholders})''',
                (int(document_id), *member_ids),
            ).fetchall()
        }
    finally:
        db.close()
    if set(records) != set(member_ids):
        return False
    for member in member_ids:
        record = records[member]
        actual_hash = _document_file_hash(record.get('path'))
        if not actual_hash or actual_hash != record.get('file_hash'):
            return False
        if record.get('content_hash') != snapshot(member, include_package=False)['content_hash']:
            return False
    return True


def _packing_scope_matches(data, proposal):
    return (
        data.get('packing_scope_fingerprint') == proposal['fingerprint']
        and data.get('packing_items') == proposal['approval_items']
        and data.get('total_quantity') == proposal['total_quantity']
    )


def preflight(name, data, actor=None):
    s = snapshot(data['order_id'])
    if version(s) != data['expected_version']:
        raise error('ENTITY_VERSION_CONFLICT', 'Zamówienie zmieniło się. Odczytaj aktualny stan.', 'CONFLICT')
    o = s['order']
    if name not in {'shipping.shipment.refresh', 'shipping.pickup.request', 'orders.fulfillment.reconcile'} and _order_closed_for_operation(name, o):
        raise error('ORDER_CLOSED', 'Zamówienie zostało wysłane lub zamknięte.', 'DENIED')
    current = state({'order_id': o['id']})['state']
    if name == 'shipping.shipment.create' and len(s['package_ids']) > 1 and data.get('package_fingerprint') != _hash(s):
        raise error('PACKAGE_SCOPE_CONFLICT', 'Odczytaj aktualny zakres całej paczki i dołącz jego fingerprint do nadania.', 'CONFLICT')
    if name == 'shipping.shipment.create' and current['shipment']['exists']:
        if any(str(snapshot(member)['order'].get('inpost_shipment_id') or '') != str(o.get('inpost_shipment_id') or '') for member in s['package_ids']):
            raise error('PACKAGE_SHIPMENT_CONFLICT', 'Zamówienia w paczce nie mają wspólnej przesyłki. Najpierw zweryfikuj powiązanie istniejącego nadania.', 'CONFLICT')
    if name == 'shipping.shipment.create' and not current['shipment']['exists'] and any(
        attempt['state'] in {'SENDING', 'UNKNOWN'} for attempt in s['attempts']
    ):
        raise error('SHIPMENT_RECOVERY_REQUIRED',
                    'Wynik wcześniejszego nadania jest niepewny. Użyj shipping.shipment.refresh; nie tworzę kolejnej przesyłki.')
    if s['persistence']['pending'] and name != 'orders.fulfillment.reconcile':
        raise error('RECONCILIATION_PENDING', 'Najpierw uzgodnij wcześniejszy zapis metadanych realizacji.', 'CONFLICT')
    if name == 'shipping.shipment.confirm_parameters':
        if not data.get('human_confirmed'):
            raise error('CONFIRMATION_REQUIRED', 'Człowiek musi potwierdzić dane istniejącej przesyłki.')
        if not s['attempts']:
            raise error('LEGACY_SHIPMENT_REVIEW', 'Najpierw zweryfikuj istniejącą przesyłkę przez podgląd adopcji.')
        booked = json.loads(s['attempts'][0]['payload'])
        if sorted(booked.get('order_ids') or [o['id']]) != s['package_ids']:
            raise error('PACKAGE_SHIPMENT_CONFLICT', 'Zmienił się zestaw zamówień paczki.', 'CONFLICT')
        if booked['receiver'] != effective_receiver(s):
            raise error('RECIPIENT_CHANGED', 'Odbiorca lub adres różni się od nadania.')
        if not parameters_match(booked, current['requirements']['known']):
            raise error('PARCEL_CHANGED', 'Parametry paczki różnią się od nadania. Wymagana jest obsługa u przewoźnika.')
    if name in {'orders.documents.adopt', 'shipping.shipment.adopt'}:
        import fulfillment_adoption
        proof = fulfillment_adoption.document_proof(__import__(__name__), o['id']) if name == 'orders.documents.adopt' else fulfillment_adoption.shipment_proof(__import__(__name__), o['id'])
        if proof.get('status') != 'SAFE' or proof.get('fingerprint') != data['preview_fingerprint']:
            raise error('ADOPTION_CONFLICT', 'Adopcja wymaga zgodnego, kompletnego podglądu.', 'CONFLICT')
    if name == 'orders.packing_list.generate' and not _has_open_current_packing(s, current):
        proposal = packing_list_preview(o['id'])
        scope_supplied = any(
            key in data for key in ('packing_scope_fingerprint', 'packing_items', 'total_quantity')
        )
        if len(proposal['candidate_order_ids']) > 1 and not _packing_scope_matches(data, proposal):
            raise error(
                'PACKING_SCOPE_CONFLICT',
                'Odczytaj orders.packing_list.preview i zatwierdź aktualny zakres wspólnej listy pakowej.',
                'CONFLICT',
            )
        if scope_supplied and not _packing_scope_matches(data, proposal):
            raise error('PACKING_SCOPE_CONFLICT', 'Zakres listy pakowej zmienił się. Odczytaj aktualną propozycję.', 'CONFLICT')
        if not proposal['items']:
            raise error('NOTHING_AVAILABLE_TO_PACK', 'Brak pozycji dostępnych obecnie do wspólnego pakowania.')
    if name == 'orders.invoice.create' and not current['invoice']['current']:
        if s['invoices']:
            matching_intent = any(i['kind'] == 'invoice' and i['content_hash'] == s['content_hash'] for i in s['intents'])
            resumable_partial = matching_intent and any(i.get('publication_state', 'complete') != 'complete' for i in s['invoices'])
            if not resumable_partial:
                raise error('EXISTING_INVOICE_RECONCILE_REQUIRED',
                            'Faktura już istnieje. Zweryfikuj ją przez podgląd adopcji/reconciliation; nie tworzę kolejnej.')
        if not current['packing_list']['current']:
            raise error('PACKING_REQUIRED', 'Najpierw przygotuj aktualną listę pakową.')
    if name == 'shipping.shipment.create' and not current['shipment']['exists']:
        if not current['readiness']['complete']:
            raise error('ORDER_NOT_READY', 'Zamówienie nie jest gotowe do wysyłki.')
        if not current['packing_list']['current']:
            raise error('CURRENT_DOCUMENTS_REQUIRED', 'Przed nadaniem wymagana jest aktualna lista pakowa.')
        if current['requirements']['missing_fields']:
            raise error('MISSING_SHIPPING_FIELDS', 'Brakuje danych paczki: ' + ', '.join(current['requirements']['missing_fields']))
        _validate_requirements(current['requirements']['known'])
        if not b.inpost_config_summary().get('configured'):
            raise error('MISSING_CONFIGURATION', 'Brakuje konfiguracji wybranego przewoźnika.')
        for member in s['package_ids']:
            other = state({'order_id': member})['state']
            if other['requirements']['recipient'] != current['requirements']['recipient']:
                raise error('PACKAGE_RECIPIENT_CONFLICT', 'Zamówienia w paczce mają różnych odbiorców lub adresy.', 'CONFLICT')
            if other['shipment']['exists'] or not other['packing_list']['current'] or not other['readiness']['complete']:
                raise error('PACKAGE_STATE_CONFLICT', 'Jedno z zamówień paczki ma przesyłkę, brak gotowości lub nieaktualne dokumenty.', 'CONFLICT')
    if name.startswith('orders.items.') and not current['editable']:
        raise error('INVOICE_BLOCKS_EDIT', 'Najpierw wykonaj istniejącą obsługę faktury, która blokuje edycję.')
    if name.startswith('orders.items.'):
        if (s['invoices'] or s['documents']) and not data.get('documents_change_confirmed'):
            raise error('DOCUMENTS_CONFIRMATION_REQUIRED', 'Zmiana unieważni dokumenty. Potwierdź ich ponowne przygotowanie.')
        if name != 'orders.items.add' and not any(i['id'] == data['item_id'] for i in s['items']):
            raise error('ITEM_NOT_FOUND', 'Pozycja nie należy do zamówienia.')
        if name == 'orders.items.add':
            c = b.conn()
            try:
                product = c.execute('SELECT id FROM products WHERE id=? AND COALESCE(archived,0)=0', (data['product_id'],)).fetchone()
            finally:
                c.close()
            if not product:
                raise error('PRODUCT_NOT_FOUND', 'Nie znaleziono aktywnego produktu.')
    return current


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
        actual_file_hash = _document_file_hash(record['path']) if record else ''
        exists = bool(record and actual_file_hash)
        current = bool(exists and record['content_hash'] == s['content_hash'] and
                       record.get('file_hash') == actual_file_hash)
        if kind == 'packing_list' and record:
            batch = next((x for x in s['batches'] if x['id'] == record['document_id']), None)
            adopted = any(v['kind'] == 'documents' and json.loads(v['payload']).get('content_hash') == s['content_hash'] for v in s['verifications'])
            if (not batch and not adopted) or (batch and batch.get('invoice_id') and not any(i['id'] == batch['invoice_id'] for i in s['invoices'])):
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
    changed = bool(o.get('inpost_shipment_id') and (not attempt or attempt['content_hash'] != shipment_content(s) or
        json.loads(attempt['payload']).get('receiver') != target))
    if attempt and o.get('inpost_shipment_id'):
        booked = json.loads(attempt['payload'])
        changed = changed or not parameters_match(booked, fields)
    ready = bool(fully or (readiness and readiness['ready']))
    pickup = b.inpost_pickup_status(o.get('inpost_shipment_id')) if o.get('inpost_shipment_id') else None
    next_step = ('reconcile_metadata' if s['persistence']['pending'] else
                 'readiness' if not ready and not s['invoices'] else
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
    return {'ok': True, 'state': {'order_id': o['id'], 'order_number': o.get('order_no'),
        'customer': o.get('customer_name'), 'order_status': o['status'], 'expected_version': version(s),
        'package': {'order_ids': s['package_ids'], 'package_key': _hash(s['package_ids']), 'member_versions': s['package_versions'], 'fingerprint': _hash(s)},
        'persistence': s['persistence'],
        'editable': not bool(o.get('warehouse_issued')), 'content_hash': s['content_hash'],
        'readiness': {'complete': ready, 'details': readiness},
        'packing': {'confirmed': o['status'] in ('packed', 'packed_partial'), 'packed_at': o.get('packed_at')},
        **documents, 'invoices': [invoice_outcome(i, s) for i in s['invoices']],
        'shipment': {'exists': bool(o.get('inpost_shipment_id')), 'carrier': o.get('carrier'),
                     'tracking': o.get('tracking_no'), 'parameters_need_review': changed,
                     'label_exists': documents['label']['exists'],
                     'pickup_confirmed': (pickup or {}).get('state') in {'sent', 'collected'},
                     'pickup': pickup,
                     'attempt_state': attempt['state'] if attempt else None},
        'requirements': {'known': fields, 'recipient': target, 'missing_fields': missing},
        'next_step': next_step}}


def invoice_outcome(i, s):
    meta = next((m for m in s['metas'] if m['invoice_id'] == i['id']), {})
    found, _ = b.invoice_pdf_exists(meta.get('pdf_path', ''), i.get('invoice_no', ''))
    c = b.conn()
    try:
        job = c.execute('SELECT state,error FROM invoice_jobs WHERE invoice_id=?', (i['id'],)).fetchone()
    finally:
        c.close()
    return {'invoice_id': i['id'], 'invoice_number': i['invoice_no'], 'record_created': True,
            'number_assigned': bool(i['invoice_no']), 'pdf_available': bool(found),
            'publication_state': i.get('publication_state', 'complete'),
            'publication_error': str(job['error'] or '') if job else '',
            'ksef_state': meta.get('ksef_status') or i.get('ksef_status') or ''}


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


def release(oid, token):
    c = b.conn()
    try:
        c.execute('DELETE FROM fulfillment_locks WHERE order_id=? AND token=?', (oid, token))
        c.commit()
    finally:
        c.close()


def request_view(method='POST', form=None, args=None):
    return SimpleNamespace(method=method, form=MultiDict(form or {}), args=MultiDict(args or {}))


def packing_list_preview(oid):
    proposal = _check_response(b.order_packing_list_download_admin_service(
        oid,
        request=request_view(method='GET'),
        session={},
        structured=True,
    ))
    fingerprint_payload = {
        'root_order_id': proposal['root_order_id'],
        'customer': proposal['customer'],
        'candidate_order_ids': proposal['candidate_order_ids'],
        'order_ids': proposal['order_ids'],
        'items': proposal['items'],
    }
    approval_items = [
        {
            'order_id': item['order_id'],
            'order_number': item['order_number'],
            'order_item_id': item['order_item_id'],
            'sku': item['sku'],
            'quantity': item['pack_qty'],
        }
        for item in proposal['items']
    ]
    return {
        **proposal,
        'approval_items': approval_items,
        'fingerprint': _hash(fingerprint_payload),
    }


def packing_list_preview_operation(data, actor=None, correlation_id='', transaction_connection=None):
    current = state({'order_id': data['order_id']})
    return {**current, 'preview': packing_list_preview(data['order_id'])}


def _check_response(result):
    if isinstance(result, tuple):
        raise error('BUSINESS_RULE_BLOCKED', str(result[0]))
    if isinstance(result, dict) and not result.get('ok'):
        raise error('BUSINESS_RULE_BLOCKED', result.get('error') or 'Operacja nie została zakończona.')
    if not isinstance(result, dict) and getattr(result, 'status_code', 500) not in (200, 302):
        raise error('BUSINESS_RULE_BLOCKED', 'Aplikacja odmówiła wykonania operacji.')
    return result


def save_document(oid, kind, document_id, path, *, connection=None, content_hash=None, file_hash=None):
    owned = connection is None
    c = connection or b.conn()
    try:
        if content_hash is None:
            content_hash = snapshot(oid, c, include_package=False)['content_hash']
        file_hash = file_hash or _document_file_hash(path)
        if not file_hash:
            raise error('DOCUMENT_FILE_UNAVAILABLE', 'Nie można odczytać zapisanego dokumentu.')
        created_at = b.now_iso()
        historical = (int(oid), str(kind), int(document_id), str(content_hash),
                      str(path), created_at, str(file_hash))
        persisted = historical
        if str(kind) == 'packing_list':
            c.execute('''INSERT OR IGNORE INTO fulfillment_document_history(
                             order_id,kind,document_id,content_hash,path,created_at,file_hash)
                         VALUES(?,?,?,?,?,?,?)''', historical)
            saved = c.execute('''SELECT order_id,kind,document_id,content_hash,path,created_at,file_hash
                                   FROM fulfillment_document_history
                                  WHERE order_id=? AND kind=? AND document_id=?''',
                              (int(oid), str(kind), int(document_id))).fetchone()
            if saved is None or str(saved['content_hash']) != str(content_hash):
                raise error(
                    'HISTORICAL_DOCUMENT_CONFLICT',
                    'Ta historyczna wersja listy pakowej ma już inną utrwaloną treść.',
                    status='CONFLICT',
                )
            # Repeated execution of the same open batch is idempotent.  Keep
            # the first immutable document even if a caller rendered a fresh
            # temporary PDF before recognizing the existing batch.
            persisted = (
                int(saved['order_id']), str(saved['kind']), int(saved['document_id']),
                str(saved['content_hash']), str(saved['path']), str(saved['created_at']),
                str(saved['file_hash']),
            )
        c.execute('''INSERT INTO fulfillment_documents(
                         order_id,kind,document_id,content_hash,path,created_at,file_hash)
                     VALUES(?,?,?,?,?,?,?)
                     ON CONFLICT(order_id,kind) DO UPDATE SET
                         document_id=excluded.document_id,
                         content_hash=excluded.content_hash,
                         path=excluded.path,
                         created_at=excluded.created_at,
                         file_hash=excluded.file_hash''', persisted)
        if owned:
            c.commit()
        return {
            'order_id': persisted[0], 'kind': persisted[1],
            'document_id': persisted[2], 'content_hash': persisted[3],
            'path': persisted[4], 'created_at': persisted[5],
            'file_hash': persisted[6],
        }
    except Exception:
        if owned:
            c.rollback()
        raise
    finally:
        if owned:
            c.close()


def finalize_packing_list(
    root_order_id,
    prepared,
    *,
    actor=None,
    correlation_id='',
    approval_id='',
    before_state=None,
):
    """Atomically publish one prepared packing list to every selected order."""
    from internal_audit import record_audit_event

    path = prepared['path']
    items = prepared['items']
    order_ids = sorted({int(value) for value in prepared['order_ids']})
    db = b.conn()
    try:
        db.execute('BEGIN IMMEDIATE')
        content_hashes = {
            member: snapshot(member, db, include_package=False)['content_hash']
            for member in order_ids
        }
        batch_id = b.save_packing_selection(
            root_order_id,
            items,
            connection=db,
            reuse_matching=True,
            reject_mismatched_open=True,
        )
        if not batch_id:
            raise error('PACKING_SELECTION_EMPTY', 'Lista pakowa nie zawiera pozycji.')
        source_path = Path(path)
        archive_path = source_path.with_name(
            f'{source_path.stem}_batch_{batch_id}{source_path.suffix}'
        )
        if not archive_path.exists():
            archive_path.write_bytes(source_path.read_bytes())
        path = str(archive_path)
        file_hash = _document_file_hash(path)
        if not file_hash:
            raise error('DOCUMENT_FILE_UNAVAILABLE', 'Nie można odczytać wygenerowanej listy pakowej.')
        packing_result = b.mark_orders_packed_transaction(db, order_ids, packing_items=items)
        for member in order_ids:
            save_document(
                member,
                'packing_list',
                batch_id,
                path,
                connection=db,
                content_hash=content_hashes[member],
                file_hash=file_hash,
            )
        record_audit_event(
            'orders.packing_list.generate',
            result='SUCCESS',
            actor_context=actor,
            entity_type='order',
            entity_id=str(root_order_id),
            correlation_id=correlation_id,
            approval_id=approval_id,
            before_state=before_state,
            after_state={
                'phase': 'domain_committed',
                'batch_id': batch_id,
                'order_ids': order_ids,
                'path': str(path),
            },
            transaction_connection=db,
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    # Remote sync and e-mail are retry-safe and run only after the complete
    # local state exists.  Their helpers contain their own failure handling.
    b.complete_orders_packed_side_effects(packing_result, packing_path=path)
    return batch_id


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
                if json.loads(old['payload']) != payload or old['content_hash'] != shipment_content(s):
                    raise InPostError('Istnieje wcześniejsze nadanie z inną zawartością lub parametrami. Zweryfikuj przesyłkę przed kolejną paczką.')
                return json.loads(old['provider_json'])
            raise InPostError('Wynik wcześniejszego nadania jest niepewny. Sprawdź przesyłkę; nie twórz kolejnej.')
        stable = 'fulfillment-' + str(uuid.uuid4())
        c.execute('INSERT INTO fulfillment_shipping_attempts VALUES(?,?,?,?,?,?,?)',
            (oid, stable, json.dumps(payload), shipment_content(s), 'SENDING', None, b.now_iso()))
        c.executemany('INSERT INTO fulfillment_shipping_members VALUES(?,?)', [(member, oid) for member in package_ids])
        c.commit()
    finally:
        c.close()
    try:
        if b.supabase_enabled():
            try:
                claim = b.supabase_request('/rest/v1/rpc/claim_fulfillment_shipment', method='POST',
                    payload={'p_order_ids': package_ids, 'p_reference': stable, 'p_payload': payload,
                             'p_content_hash': shipment_content(s), 'p_created_at': b.now_iso()})
            except Exception as exc:
                raise error('SHIPPING_RESERVATION_UNAVAILABLE', 'Nie można potwierdzić rezerwacji nadania w Supabase. Sprawdź migrację i dostępność RPC; nie wysłano nowego żądania do przewoźnika.') from exc
            if not isinstance(claim, dict) or not isinstance(claim.get('claim'), dict):
                raise InPostError('Nie potwierdzono trwałej rezerwacji nadania. Sprawdź migrację fulfillment.')
            saved = claim['claim']
            if not claim.get('acquired'):
                c = b.conn()
                c.execute('UPDATE fulfillment_shipping_attempts SET reference=?,payload=?,content_hash=?,state=?,provider_json=?,created_at=? WHERE order_id=?',
                    (saved['reference'], json.dumps(saved['payload']), saved['content_hash'], saved['state'], json.dumps(saved.get('provider_json')) if saved.get('provider_json') else None, saved['created_at'], oid))
                c.commit(); c.close()
                if saved['state'] == 'SUCCESS' and saved.get('provider_json'):
                    if saved['payload'] != payload or saved['content_hash'] != shipment_content(s):
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
            raise error('UNSUPPORTED_PROVIDER' if key == 'carrier' else 'INVALID_INPUT',
                        'Wybrany przewoźnik nie ma adaptera w aplikacji.' if key == 'carrier' else 'Nieobsługiwana jednostka lub źródło wagi.')


def parameters_match(booked, fields):
    parcel = booked.get('parcel') or {}
    for key in ('length', 'width', 'height', 'weight'):
        if key in fields and float(parcel.get(key) or 0) != float(fields[key]) * (10 if key != 'weight' else 1):
            return False
    options = (booked.get('options') or {}).get('additional_services') or []
    return all(key not in fields or fields[key] == (key in options) for key in ('sms', 'email'))


def perform(name, data, actor, *, correlation_id='', approval_id='', before_state=None):
    oid = data['order_id']
    s = snapshot(oid)
    current = state({'order_id': oid})['state']
    if name == 'orders.fulfillment.reconcile':
        import reconciliation_store
        for member in s['package_ids']:
            reconciliation_store.retry_pending(b, member)
    elif name in {'orders.documents.adopt', 'shipping.shipment.adopt'}:
        import fulfillment_adoption
        fulfillment_adoption.adopt(__import__(__name__), name, data, actor)
    elif name == 'shipping.requirements.update':
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
        if _has_open_current_packing(s, current):
            return state(data)
        proposal = packing_list_preview(oid)
        scope_supplied = any(
            key in data for key in ('packing_scope_fingerprint', 'packing_items', 'total_quantity')
        )
        if scope_supplied and not _packing_scope_matches(data, proposal):
            raise error('PACKING_SCOPE_CONFLICT', 'Zakres listy pakowej zmienił się. Odczytaj aktualną propozycję.', 'CONFLICT')
        form = {
            'carrier': 'pending',
            **{f"pack_qty_{item['order_item_id']}": item['pack_qty'] for item in proposal['items']},
        }
        prepared = _check_response(b.order_packing_list_download_admin_service(
            oid,
            request=request_view(form=form),
            session={},
            structured=True,
            defer_persistence=True,
        ))
        prepared['batch_id'] = finalize_packing_list(
            oid,
            prepared,
            actor=actor,
            correlation_id=correlation_id,
            approval_id=approval_id,
            before_state=before_state,
        )
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
            if s['intents']:
                matching = [intent for intent in s['intents'] if intent['kind'] == 'invoice' and intent['content_hash'] == s['content_hash']]
                prior_completed = any(d['kind'] == 'invoice' for d in s['documents'])
                if not matching and not prior_completed:
                    raise error('INVOICE_ATTEMPT_CONFLICT', 'Rozpoczęta próba dotyczy innej wersji zamówienia. Najpierw uzgodnij jej wynik.', 'CONFLICT')
                if not matching and prior_completed:
                    c = b.conn()
                    c.execute("INSERT OR REPLACE INTO fulfillment_document_intents VALUES(?,'invoice',?)", (oid, s['content_hash']))
                    c.commit(); c.close()
                    import reconciliation_store
                    reconciliation_store.publish(b, oid)
            else:
                c = b.conn()
                c.execute("INSERT OR REPLACE INTO fulfillment_document_intents VALUES(?,'invoice',?)", (oid, s['content_hash']))
                c.commit(); c.close()
                import reconciliation_store
                reconciliation_store.publish(b, oid)
            plan = _check_response(b.order_invoice_service(oid, request=request_view('GET'), session={}, structured=True))
            form = dict(plan['defaults'])
            manual_invoice_no = str(data.get('invoice_no') or '').strip()
            if manual_invoice_no:
                form['invoice_no'] = manual_invoice_no
                form['invoice_no_manual'] = '1'
            else:
                # The BO uses the same auto suggestion and dirty-marker contract
                # as the browser form. It does not reinterpret that suggestion as
                # an agent-supplied manual number.
                form['suggested_invoice_no'] = form.get('invoice_no', '')
                form['invoice_no_manual'] = '0'
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
        if not current['readiness']['complete']:
            raise error('ORDER_NOT_READY', 'Zamówienie nie jest gotowe do wysyłki.')
        if not current['packing_list']['current']:
            raise error('CURRENT_DOCUMENTS_REQUIRED', 'Przed nadaniem wymagana jest aktualna lista pakowa.')
        if current['requirements']['missing_fields']:
            raise error('MISSING_SHIPPING_FIELDS', 'Brakuje danych paczki: ' + ', '.join(current['requirements']['missing_fields']))
        c = b.conn()
        package_ids = [int(o['id']) for o in b._packed_package_orders(c.cursor(), s['order'])]
        c.close()
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
        if sorted(old.get('order_ids') or [oid]) != s['package_ids']:
            raise error('PACKAGE_SHIPMENT_CONFLICT', 'Zmienił się zestaw zamówień paczki. Zweryfikuj powiązanie istniejącej przesyłki przed potwierdzeniem parametrów.', 'CONFLICT')
        if old['receiver'] != effective_receiver(s):
            raise error('RECIPIENT_CHANGED', 'Odbiorca lub adres różni się od nadania. Wymagana jest obsługa u przewoźnika.')
        if not parameters_match(old, current['requirements']['known']):
            raise error('PARCEL_CHANGED', 'Parametry paczki różnią się od nadania. Wymagana jest obsługa u przewoźnika; nie anuluję przesyłki.')
        c = b.conn(); c.execute('UPDATE fulfillment_shipping_attempts SET content_hash=? WHERE order_id=?', (shipment_content(s), s['attempts'][0]['order_id'])); c.commit(); c.close()
    return state(data)


def print_ready(data, actor, correlation_id='', transaction_connection=None):
    from internal_rbac import DENY
    permissions = {'packing_list': 'packing.read', 'invoice': 'invoices.read', 'label': 'shipping.label_read'}
    current = state(data)['state']
    if not current['persistence']['durable']:
        raise error('RECONCILIATION_PENDING', 'Nie potwierdzono trwałego zapisu dokumentów.')
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
    if len(current['package']['order_ids']) > 1:
        for member in current['package']['order_ids']:
            other = state({'order_id': member})['state']
            if not other['persistence']['durable'] or not other['invoice']['current'] or not other['packing_list']['current'] or other['shipment']['parameters_need_review']:
                raise error('STALE_PACKAGE_DOCUMENT', 'Nie wszystkie zamówienia w paczce mają aktualne dokumenty.')
        result = [{'type': 'document_link', 'document_type': 'package', 'name': 'Komplet dokumentów paczki',
                   'url': f"/api/internal/fulfillment/{data['order_id']}/documents/package"}]
    return {'ok': True, 'state': current, 'documents': result, 'message': 'Aktualne dokumenty są gotowe do otwarcia i druku w przeglądarce. Nie potwierdzam fizycznego wydruku.'}


def package_pdf(data, actor):
    from io import BytesIO
    from contextlib import ExitStack
    from pypdf import PdfWriter
    with ExitStack() as stack:
        for oid in snapshot(data['order_id'])['package_ids']:
            stack.enter_context(order_lease(oid, 'print:' + str(uuid.uuid4())))
        current = print_ready(data, actor)['state']
        writer = PdfWriter(); seen = set()
        for member in current['package']['order_ids']:
            for doc in snapshot(member)['documents']:
                if doc['kind'] == 'label' and member != data['order_id']:
                    continue
                if doc['file_hash'] not in seen:
                    writer.append(doc['path']); seen.add(doc['file_hash'])
        stream = BytesIO(); writer.write(stream); stream.seek(0)
        return stream


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
        from contextlib import ExitStack
        lease = ExitStack()
        try:
            lease_members = set(snapshot(oid)['package_ids'])
            if definition.operation_name == 'orders.packing_list.generate':
                lease_members.update(packing_list_preview(oid)['order_ids'])
            for member in sorted(lease_members):
                lease.enter_context(order_lease(member, execution_id))
        except Exception:
            lease.close()
            raise
        locked = True
        preflight(definition.operation_name, data, actor)
        before = snapshot(oid)
        if definition.operation_name not in {'shipping.shipment.refresh', 'shipping.pickup.request', 'orders.fulfillment.reconcile'} and _order_closed_for_operation(definition.operation_name, before['order']):
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
        output = ops.validate_output(definition, perform(
            definition.operation_name,
            data,
            actor,
            correlation_id=correlation_id,
            approval_id=approval_id,
            before_state=before,
        ))
        import reconciliation_store
        if definition.operation_name != 'orders.fulfillment.reconcile':
            for member in snapshot(oid)['package_ids']:
                reconciliation_store.publish(b, member)
        output = ops.validate_output(definition, state(data))
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
        partial = None
        if definition.operation_name == 'orders.invoice.create':
            try:
                partial = state(data)
                if partial['state']['invoices']:
                    message = 'Rekord faktury istnieje. Nie ukończono przygotowania lub publikacji dokumentu; sprawdź dostępność PDF i wznów istniejącą fakturę.'
            except Exception:
                pass
        row = ops._transition(execution_id, definition, actor, status, 'business_operation.' + status.lower(), status,
            error_code=code, message=message, data=partial, completed=True, expected_statuses=('RUNNING',))
        return ops._result_from_row(row or ops._execution(execution_id))
    finally:
        if locked: lease.__exit__(None, None, None)


def install(ops):
    output = {'ok': {'type': 'boolean'}, 'state': {'type': 'object'}, 'documents': {'type': 'array'}, 'capabilities': {'type': 'array'}, 'preview': {'type': 'object'}, 'message': {'type': 'string'}}
    for name in sorted(READS | WRITES):
        read = name in READS
        props = {'order_id': {'type': 'integer', 'minimum': 1}}
        required = ['order_id']
        if name == 'shipping.capabilities':
            props = {}; required = []
        if not read:
            props.update(expected_version={'type': 'integer', 'minimum': 0}, idempotency_key={'type': 'string', 'minLength': 1, 'maxLength': 200})
            required += ['expected_version', 'idempotency_key']
        if name.startswith('orders.items.'):
            props['documents_change_confirmed'] = {'type': 'boolean'}
            if name.endswith('.add'): props['product_id'] = {'type': 'integer', 'minimum': 1}; required += ['product_id']
            else: props['item_id'] = {'type': 'integer', 'minimum': 1}; required += ['item_id']
            if not name.endswith('.remove'): props['quantity'] = {'type': 'integer', 'minimum': 1}; required += ['quantity']
        if name == 'orders.invoice.create':
            props['invoice_no'] = {
                'type': 'string', 'minLength': 1, 'maxLength': 100,
                'description': 'Opcjonalny ręczny numer faktury. Bez niego wspólny backend nada numer automatycznie.',
            }
        if name == 'orders.packing_list.generate':
            props['packing_scope_fingerprint'] = {
                'type': 'string', 'minLength': 64, 'maxLength': 64,
                'description': 'Fingerprint z orders.packing_list.preview. Wymagany, gdy propozycja obejmuje wiele zamówień klienta.',
            }
            props['packing_items'] = {
                'type': 'array', 'maxItems': 200,
                'description': 'Skopiuj approval_items z orders.packing_list.preview, aby approval pokazywał zamówienia, SKU i ilości.',
                'items': {
                    'type': 'object', 'additionalProperties': False,
                    'required': ['order_id', 'order_number', 'order_item_id', 'sku', 'quantity'],
                    'properties': {
                        'order_id': {'type': 'integer', 'minimum': 1},
                        'order_number': {'type': 'string'},
                        'order_item_id': {'type': 'integer', 'minimum': 1},
                        'sku': {'type': 'string'},
                        'quantity': {'type': 'integer', 'minimum': 1},
                    },
                },
            }
            props['total_quantity'] = {
                'type': 'integer', 'minimum': 1,
                'description': 'Łączna ilość sztuk z orders.packing_list.preview.',
            }
        if name == 'shipping.requirements.update':
            props.update({k: {'type': 'number'} for k in ('length', 'width', 'height', 'weight')})
            props.update({k: {'type': 'string'} for k in ('carrier', 'dimension_unit', 'weight_unit', 'weight_source')})
            props.update({k: {'type': 'boolean'} for k in ('sms', 'email')})
            props.update({'recipient_' + k: {'type': 'string', 'minLength': 1, 'maxLength': 200}
                          for k in ('name', 'street', 'post_code', 'city', 'phone', 'email')})
        if name == 'shipping.shipment.confirm_parameters':
            props['human_confirmed'] = {'type': 'boolean'}; required += ['human_confirmed']
        if name == 'shipping.shipment.create':
            props['package_fingerprint'] = {'type': 'string', 'minLength': 64, 'maxLength': 64,
                'description': 'Dla wielu zamówień wymagane package.fingerprint z orders.fulfillment.state. Zawiera cały zakres i wersje wszystkich zamówień.'}
        if name in {'orders.documents.adopt', 'shipping.shipment.adopt'}:
            props['preview_fingerprint'] = {'type': 'string', 'minLength': 64, 'maxLength': 64}
            required += ['preview_fingerprint']
        risk = 'GREEN' if read or name in GREEN else 'YELLOW'
        description = 'Realizuje jeden krok istniejącego fulfillment. Najpierw orders.fulfillment.state; użyj zwróconej expected_version. Po WRITE odczytaj state z wyniku i kontynuuj next_step. Nie odtwarzaj poprawnych dokumentów. Pytaj tylko o requirements.missing_fields. Nie anuluj przesyłki. shipping.shipment.refresh odzyskuje wynik timeoutu bez ponownego POST. Druk oznacza przygotowanie PDF, nie fizyczny wydruk.'
        if name == 'orders.packing_list.preview':
            description = 'Pokazuje tę samą propozycję co UI Wybierz zawartość paczki: wszystkie kwalifikujące się zamówienia tego klienta, dostępne pozycje, ilości i łączną liczbę sztuk. Użyj przy intencji jednej paczki lub jednej listy pakowej dla wielu zamówień; po wyszukaniu klienta podaj dowolny jego kwalifikujący się order_id jako root. Po pokazaniu propozycji przekaż fingerprint i approval_items do orders.packing_list.generate.'
        elif name == 'orders.packing_list.generate':
            description = 'Tworzy jedną wspólną listę pakową przez ten sam service co UI. Przy intencji spakowania wszystkich dostępnych zamówień klienta najpierw wywołaj orders.packing_list.preview, pokaż order IDs, SKU, ilości i sumę, a następnie przekaż fingerprint, approval_items jako packing_items oraz total_quantity. Operacja sama uruchamia wymagany HUMAN approval; nie twórz shipment.merge.'
        ops.OPERATION_REGISTRY[name] = ops.BusinessOperationDefinition(name, 1,
            description,
            PERMISSIONS[name], risk, 'NONE' if risk == 'GREEN' else 'REQUIRED', frozenset({'HUMAN', 'AI_AGENT'}),
            {'type': 'object', 'additionalProperties': False, 'required': required, 'properties': props},
            {'type': 'object', 'required': ['ok', 'state'], 'properties': output},
            ops.IDEMPOTENCY_NONE if read else ops.IDEMPOTENCY_REQUIRED, 'READ_STANDARD' if read else 'WRITE', read)
        ops._HANDLERS[name] = capabilities if name == 'shipping.capabilities' else print_ready if name == 'orders.documents.print_ready' else packing_list_preview_operation if name == 'orders.packing_list.preview' else state
        if name.endswith('.adoption.preview'):
            import fulfillment_adoption
            ops._HANDLERS[name] = lambda data, actor, correlation_id='', transaction_connection=None, operation=name: fulfillment_adoption.preview(__import__(__name__), operation, data)
        if read: ops.FRESHNESS_GROUP_BY_OPERATION[name] = 'fulfillment_workflow'
