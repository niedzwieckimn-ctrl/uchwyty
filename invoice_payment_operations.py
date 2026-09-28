"""Approved payment-status adapter over the application's shared domain service.

The BO local-write transaction owns approval consumption, the payment mutation,
the domain audit and the terminal execution record. This module sends no mail
and does not change invoice amounts, stock or shipping identifiers.
"""
import hashlib
import json

WRITE = 'invoices.payment.set_status'
WRITES = frozenset({WRITE})
_backend = None


def configure(backend):
    global _backend
    _backend = backend


def _error(code, message, status='CONFLICT'):
    from business_operations import ControlledOperationError
    return ControlledOperationError(code, message, status=status)


def snapshot(invoice_id, connection=None):
    b = _backend
    if b is None:
        raise _error('SERVICE_UNAVAILABLE', 'Usługa płatności nie jest dostępna.', 'FAILED')
    owned = connection is None
    db = connection or b.conn()
    try:
        row = db.execute('SELECT * FROM invoices WHERE id=?', (invoice_id,)).fetchone()
        if row is None:
            raise _error('INVOICE_NOT_FOUND', 'Nie znaleziono faktury.')
        meta = db.execute('SELECT * FROM invoice_meta WHERE invoice_id=?', (invoice_id,)).fetchone()
        if meta and meta['invoice_items_json']:
            try:
                items = json.loads(meta['invoice_items_json'])
                if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                    raise ValueError('Invalid items')
            except (ValueError, TypeError):
                raise _error('INVALID_DOCUMENT_STATE', 'Nie można odczytać powiązań tej faktury. Najpierw sprawdź dokument.')
        cur = db.cursor()
        order_ids = b._invoice_source_order_ids(cur, int(invoice_id))
        if not order_ids:
            raise _error('INVALID_DOCUMENT_STATE', 'Faktura nie wskazuje zamówienia.')
        marks = ','.join('?' for _ in order_ids)
        orders = [dict(r) for r in db.execute(
            f'SELECT * FROM orders WHERE id IN ({marks}) ORDER BY id', order_ids)]
        if len(orders) != len(order_ids):
            raise _error('INVALID_DOCUMENT_STATE', 'Brakuje zamówienia powiązanego z fakturą.')
        related = sorted({int(invoice_id)} | {
            iid for oid in order_ids for iid in b._order_invoice_ids(cur, oid)})
        invoice_marks = ','.join('?' for _ in related)
        # Include every input used by the shared order-completion calculation.
        return {
            'invoice': dict(row), 'meta': dict(meta) if meta else {}, 'orders': orders,
            'order_items': [dict(r) for r in db.execute(
                f'SELECT * FROM order_items WHERE order_id IN ({marks}) ORDER BY id', order_ids)],
            'allocations': [dict(r) for r in db.execute(
                f'SELECT * FROM invoice_allocations WHERE order_id IN ({marks}) ORDER BY id', order_ids)],
            'related_invoices': [dict(r) for r in db.execute(
                f'SELECT * FROM invoices WHERE id IN ({invoice_marks}) ORDER BY id', related)],
            'related_meta': [dict(r) for r in db.execute(
                f'SELECT * FROM invoice_meta WHERE invoice_id IN ({invoice_marks}) ORDER BY invoice_id', related)],
        }
    finally:
        if owned:
            db.close()


def _version(state):
    raw = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str)
    # Keep the version exact in JavaScript as well as Python/SQLite.
    return int(hashlib.sha256(raw.encode('utf-8')).hexdigest()[:13], 16)


def version(invoice_id, connection=None):
    return _version(snapshot(invoice_id, connection))


def validate(db, data, actor, definition):
    import business_operations as operations
    operations._assert_operation_permission(actor, definition)
    current = snapshot(data['invoice_id'], db)
    current_version = _version(current)
    if current_version != data['expected_version']:
        raise _error('ENTITY_VERSION_CONFLICT', 'Faktura, płatność lub powiązane zamówienie zmieniły się. Odczytaj fakturę ponownie.')
    if (current['invoice'].get('publication_state') or 'complete') != 'complete':
        raise _error('INVOICE_NOT_PUBLISHED', 'Nie można oznaczyć płatności dla nieukończonej faktury.', 'DENIED')
    return current_version


def set_status(data, actor, correlation_id='', transaction_connection=None):
    if transaction_connection is None:
        raise _error('TRANSACTION_REQUIRED', 'Zmiana płatności wymaga zatwierdzonej transakcji.', 'DENIED')
    import business_operations as operations
    from internal_audit import record_audit_event
    db = transaction_connection
    definition = operations.OPERATION_REGISTRY[WRITE]
    validate(db, data, actor, definition)
    before = snapshot(data['invoice_id'], db)
    changed = bool(before['meta'].get('paid')) != data['paid']
    if changed:
        _backend._set_invoice_payment_state(
            int(data['invoice_id']), paid=int(data['paid']), transaction_connection=db)
    after = snapshot(data['invoice_id'], db)
    if bool(after['meta'].get('paid')) != data['paid']:
        raise _error('PAYMENT_UPDATE_UNVERIFIED', 'Nie potwierdzono zapisu statusu płatności.', 'FAILED')
    output = {
        'ok': True, 'invoice_id': int(data['invoice_id']),
        'invoice_number': str(after['invoice']['invoice_no']),
        'paid': bool(after['meta'].get('paid')), 'changed': changed,
        'paid_at': str(after['meta'].get('paid_at') or ''),
        'expected_version': _version(after),
        'order_ids': [int(o['id']) for o in after['orders']],
        'affected_orders': [{'order_id': int(o['id']), 'status': str(o['status'])}
                            for o in after['orders']],
    }
    import invoice_payment_sync
    output['payment_sync'] = invoice_payment_sync.status(_backend, int(data['invoice_id']), db)
    output['message'] = ('Status płatności zapisany lokalnie; trwa synchronizacja z chmurą.'
        if output['payment_sync']['pending'] else 'Status płatności zapisany lokalnie.')
    record_audit_event(
        WRITE, result='SUCCESS', actor_context=actor,
        permission=definition.required_permission, risk_level=definition.risk_level,
        entity_type='invoice', entity_id=str(data['invoice_id']), correlation_id=correlation_id,
        before_state={'paid': bool(before['meta'].get('paid')),
                      'paid_at': before['meta'].get('paid_at'),
                      'orders': [{'order_id': o['id'], 'status': o['status']} for o in before['orders']]},
        after_state=output, transaction_connection=db)
    return output


def install(operations):
    properties = {
        'invoice_id': {'type': 'integer', 'minimum': 1},
        'paid': {'type': 'boolean'},
        'expected_version': {'type': 'integer', 'minimum': 0},
        'idempotency_key': {'type': 'string', 'minLength': 1, 'maxLength': 200},
    }
    output = {
        'ok': {'type': 'boolean'}, 'invoice_id': {'type': 'integer'},
        'invoice_number': {'type': 'string'}, 'paid': {'type': 'boolean'},
        'changed': {'type': 'boolean'}, 'paid_at': {'type': 'string'},
        'expected_version': {'type': 'integer'}, 'order_ids': {'type': 'array', 'items': {'type': 'integer'}},
        'affected_orders': {'type': 'array'},
        'payment_sync': {'type': 'object'}, 'message': {'type': 'string'},
    }
    operations.OPERATION_REGISTRY[WRITE] = operations.BusinessOperationDefinition(
        WRITE, 1,
        'Po świeżym invoices.get i jawnej zgodzie HUMAN oznacza konkretną ukończoną fakturę '
        'jako opłaconą (paid=true) albo cofa oznaczenie (paid=false). Przelicza statusy '
        'powiązanych zamówień wspólnym mechanizmem aplikacji. Nie wysyła przypomnienia ani nie wykonuje przelewu.',
        'payments.set_status', 'YELLOW', 'REQUIRED', frozenset({'HUMAN', 'AI_AGENT'}),
        {'type': 'object', 'additionalProperties': False, 'required': list(properties), 'properties': properties},
        {'type': 'object', 'required': list(output), 'properties': output},
        operations.IDEMPOTENCY_REQUIRED, 'WRITE', False)
    operations._HANDLERS[WRITE] = set_status
