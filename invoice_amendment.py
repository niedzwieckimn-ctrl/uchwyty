"""Supervised adapter over the existing invoice removal service.

No invoice accounting, stock mutation or shipping mutation is implemented here.
An uncertain multi-step removal is never replayed automatically.
"""
import hashlib
import json

from werkzeug.exceptions import HTTPException

READ = 'invoices.removal.preview'
WRITE = 'invoices.remove'
WRITES = frozenset({WRITE})
_backend = None


def configure(backend):
    global _backend
    _backend = backend


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS internal_invoice_removal_claims(
        invoice_id INTEGER PRIMARY KEY, execution_id TEXT NOT NULL UNIQUE,
        state TEXT NOT NULL CHECK(state IN ('RUNNING','SUCCESS','UNKNOWN')),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''')


def _error(code, message, status='FAILED'):
    from business_operations import ControlledOperationError
    return ControlledOperationError(code, message, status=status)


def _snapshot(invoice_id, db=None):
    b = _backend
    if b is None:
        raise _error('SERVICE_UNAVAILABLE', 'Usługa faktur jest niedostępna.')
    owned = db is None
    db = db or b.conn()
    try:
        invoice = db.execute('SELECT * FROM invoices WHERE id=?', (invoice_id,)).fetchone()
        if invoice is None:
            raise _error('NOT_FOUND', 'Nie znaleziono faktury.')
        result = {'invoice': dict(invoice)}
        for table in ('invoice_meta', 'invoice_allocations', 'invoice_stock_applied',
                      'invoice_jobs', 'ksef_documents', 'ksef_attempts'):
            result[table] = [dict(r) for r in db.execute(
                f'SELECT * FROM {table} WHERE invoice_id=? ORDER BY rowid', (invoice_id,))]
        ids = {int(invoice['order_id'])}
        ids.update(int(r['order_id']) for r in result['invoice_allocations'])
        for meta in result['invoice_meta']:
            try:
                rows = json.loads(meta.get('invoice_items_json') or '[]')
                if not isinstance(rows, list):
                    raise ValueError('Invalid invoice items')
                ids.update(int(r.get('source_order_id') or r.get('order_id') or 0) for r in rows)
            except (ValueError, TypeError, AttributeError):
                raise _error('INVALID_DOCUMENT_STATE', 'Nie można jednoznacznie ustalić zamówień tej faktury.')
        ids.discard(0)
        marks = ','.join('?' for _ in ids)
        params = tuple(sorted(ids))
        result['orders'] = [dict(r) for r in db.execute(f'SELECT * FROM orders WHERE id IN ({marks}) ORDER BY id', params)]
        if len(result['orders']) != len(ids):
            raise _error('INVALID_DOCUMENT_STATE', 'Brakuje zamówienia powiązanego z fakturą.')
        result['items'] = [dict(r) for r in db.execute(f'SELECT * FROM order_items WHERE order_id IN ({marks}) ORDER BY id', params)]
        # Include other allocations: deleting this invoice may not unlock all orders.
        result['other_allocations'] = [dict(r) for r in db.execute(f'SELECT * FROM invoice_allocations WHERE order_id IN ({marks}) ORDER BY id', params)]
        return result
    finally:
        if owned:
            db.close()


def _version(snapshot):
    raw = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str)
    return int(hashlib.sha256(raw.encode()).hexdigest()[:15], 16)


def preview(data, actor=None, correlation_id='', transaction_connection=None):
    snapshot = _snapshot(data['invoice_id'], transaction_connection)
    invoice = snapshot['invoice']
    reason = ''
    try:
        _backend.assert_invoice_mutable(data['invoice_id'])
    except HTTPException as exc:
        reason = str(exc.description)
    # Scope is amendment before shipment, even though the administrative delete
    # endpoint itself can operate on orders in other states.
    if any(str(o.get('status') or '').lower() in {
        'shipped', 'partially_shipped', 'in_delivery', 'completed', 'issued', 'cancelled'
    } or o.get('shipped_at') for o in snapshot['orders']):
        reason = 'Zamówienie zostało wysłane lub zamknięte. Ta operacja służy do zmian przed wysyłką.'
    c = _backend.conn()
    try:
        claim = c.execute('SELECT state FROM internal_invoice_removal_claims WHERE invoice_id=?', (data['invoice_id'],)).fetchone()
    finally:
        c.close()
    if claim:
        reason = 'Usuwanie tej faktury już rozpoczęto. Najpierw trzeba zweryfikować jego wynik.'
    return {
        'ok': True, 'invoice_id': data['invoice_id'], 'invoice_number': invoice['invoice_no'],
        'expected_version': _version(snapshot), 'removable': not reason, 'blocker': reason,
        'publication_state': invoice.get('publication_state') or 'complete',
        'affected_orders': [
            {'order_id': o['id'], 'order_number': o.get('order_no') or '',
             'status': o['status'], 'editable': not bool(o.get('warehouse_issued')),
             'shipment_exists': bool(o.get('inpost_shipment_id'))}
            for o in snapshot['orders']],
        'effects': ['Usunięcie obecnej faktury i jej plików',
                    'Cofnięcie ilości rozliczonych przez fakturę istniejącym mechanizmem',
                    'Przeliczenie możliwości edycji wszystkich powiązanych zamówień',
                    'Konieczność przygotowania nowej listy pakowej i nowej faktury'],
        'shipment_cancelled': False,
    }


def validate(data):
    state = preview(data)
    if state['expected_version'] != data['expected_version']:
        raise _error('ENTITY_VERSION_CONFLICT', 'Faktura lub zamówienie zmieniły się. Sprawdź aktualne dane.', 'CONFLICT')
    if not state['removable']:
        raise _error('INVOICE_REMOVAL_BLOCKED', state['blocker'], 'DENIED')
    return state


def execute(execution_id, definition, actor, data, approval_id, entity_type, entity_id, expected_version, correlation_id):
    import business_operations as operations
    import internal_approval as approval
    from internal_audit import record_audit_event
    from internal_rbac import load_actor_context, DENY
    b = _backend
    claimed = False
    db = None
    try:
        db = b.conn()
        binding = db.execute('SELECT human_id FROM internal_business_write_actors WHERE execution_id=?', (execution_id,)).fetchone()
        db.close()
        db = None
        if binding is None or binding['human_id'] != (actor.delegated_by_actor_id or actor.actor_id):
            raise _error('INITIATOR_CHANGED', 'Zmienił się inicjator operacji.', 'DENIED')
        human = load_actor_context(binding['human_id'])
        if human is None or human.permission_decision(definition.required_permission) == DENY:
            raise _error('PERMISSION_REVOKED', 'Inicjator utracił uprawnienie.', 'DENIED')
        validate(data)
        before = _snapshot(data['invoice_id'])
        db = b.conn()
        db.execute('BEGIN IMMEDIATE')
        current_version = _version(_snapshot(data['invoice_id'], db))
        approval.authorize_execution(
            approval_id, actor, WRITE, payload=data, entity_type=entity_type, entity_id=entity_id,
            operation_version=1, expected_entity_version=expected_version,
            current_entity_version=current_version, transaction_connection=db)
        # Unique across actors, idempotency keys and processes. No expiry/replay.
        db.execute('INSERT INTO internal_invoice_removal_claims VALUES(?,?,\'RUNNING\',?,?)',
                   (data['invoice_id'], execution_id, b.now_iso(), b.now_iso()))
        record_audit_event(WRITE, result='SUCCESS', actor_context=actor,
            permission=definition.required_permission, risk_level=definition.risk_level,
            entity_type=entity_type, entity_id=entity_id, correlation_id=correlation_id,
            approval_required=True, approval_id=approval_id, before_state=before,
            after_state={'phase': 'started'}, transaction_connection=db)
        db.commit()
        claimed = True
        db.close()
        db = None

        # Deliberately outside a SQLite transaction: the unchanged service owns
        # its transactions, filesystem work and Supabase calls.
        b._delete_invoice_everywhere(data['invoice_id'])
        affected = [r['id'] for r in before['orders']]
        db = b.conn()
        if db.execute('SELECT 1 FROM invoices WHERE id=?', (data['invoice_id'],)).fetchone():
            raise _error('REMOVAL_UNVERIFIED', 'Nie potwierdzono usunięcia faktury. Nie ponawiaj usuwania.')
        after = [dict(db.execute('SELECT * FROM orders WHERE id=?', (oid,)).fetchone()) for oid in affected]
        for old, new in zip(before['orders'], after):
            if any(old.get(k) != new.get(k) for k in ('inpost_shipment_id', 'tracking_no', 'carrier', 'status')):
                raise _error('ORDER_CHANGED', 'W trakcie usuwania zmienił się stan zamówienia. Sprawdź aktualne dane.')
        if b.supabase_enabled():
            for table, key in (('invoices', 'id'), ('invoice_allocations', 'invoice_id'),
                               ('invoice_meta', 'invoice_id'), ('ksef_documents', 'invoice_id')):
                remote = b.supabase_request('/rest/v1/' + table,
                    params={key: 'eq.' + str(data['invoice_id']), 'select': key})
                if remote != []:
                    raise _error('REMOTE_REMOVAL_UNVERIFIED', 'Nie potwierdzono usunięcia faktury i jej powiązań w chmurze. Wymagana jest kontrola.')
        output = {'ok': True, 'invoice_id': data['invoice_id'], 'removed': True,
                  'affected_orders': [{'order_id': o['id'], 'editable': not bool(o.get('warehouse_issued')),
                                       'shipment_exists': bool(o.get('inpost_shipment_id'))} for o in after],
                  'next_step': 'Odczytaj aktualne zamówienie i dostępność. Zmieniaj tylko odblokowane pozycje. Przygotuj nowe dokumenty; zachowaj istniejącą przesyłkę.'}
        db.execute("UPDATE internal_invoice_removal_claims SET state='SUCCESS',updated_at=? WHERE execution_id=?", (b.now_iso(), execution_id))
        record_audit_event(WRITE, result='SUCCESS', actor_context=actor,
            permission=definition.required_permission, risk_level=definition.risk_level,
            entity_type=entity_type, entity_id=entity_id, correlation_id=correlation_id,
            approval_required=True, approval_id=approval_id, before_state=before,
            after_state=output, transaction_connection=db)
        db.commit()
        db.close()
        db = None
        row = operations._transition(execution_id, definition, actor, 'SUCCESS',
            'business_operation.success', 'SUCCESS', data=output, completed=True, expected_statuses=('RUNNING',))
        if operations._write_success_observer:
            try:
                operations._write_success_observer(WRITE, {'order_ids': affected, **output})
            except Exception:
                operations.logger.exception('Invoice removal committed; freshness observer failed')
        return operations._result_from_row(row)
    except Exception as exc:
        if db is not None:
            db.rollback()
            db.close()
            db = None
        status = getattr(exc, 'status', 'FAILED')
        if isinstance(exc, approval.StaleApproval):
            status = 'CONFLICT'
        elif isinstance(exc, approval.ApprovalDenied):
            status = 'DENIED'
        code = getattr(exc, 'error_code', getattr(exc, 'code', 'INVOICE_REMOVAL_FAILED'))
        message = str(exc)
        if claimed:
            c = b.conn()
            try:
                c.execute("UPDATE internal_invoice_removal_claims SET state='UNKNOWN',updated_at=? WHERE execution_id=?", (b.now_iso(), execution_id))
                c.commit()
            finally:
                c.close()
            code = 'REMOVAL_REQUIRES_RECONCILIATION'
            status = 'FAILED'
            message = 'Usuwanie faktury przerwano lub nie potwierdzono jego wyniku. Nie ponawiam go automatycznie. Sprawdź fakturę i powiązane zamówienia.'
        row = operations._transition(execution_id, definition, actor, status,
            'business_operation.' + status.lower(), status, error_code=str(code), message=message,
            completed=True, expected_statuses=('RUNNING',))
        return operations._result_from_row(row or operations._execution(execution_id))


def install(operations):
    common = {'invoice_id': {'type': 'integer', 'minimum': 1}}
    preview_fields = {'ok': {'type': 'boolean'}, 'invoice_id': {'type': 'integer'},
        'invoice_number': {'type': 'string'}, 'expected_version': {'type': 'integer'},
        'removable': {'type': 'boolean'}, 'blocker': {'type': 'string'},
        'publication_state': {'type': 'string'}, 'affected_orders': {'type': 'array'},
        'effects': {'type': 'array'}, 'shipment_cancelled': {'type': 'boolean'}}
    # The registry validator expects arrays of mappings.
    preview_fields['effects'] = {'type': 'string'}
    for name, read, permission, risk, properties, required, output in [
        (READ, True, 'invoices.read', 'GREEN', common, ['invoice_id'], preview_fields),
        (WRITE, False, 'invoices.reverse', 'RED', {**common,
         'expected_version': {'type': 'integer', 'minimum': 0},
         'idempotency_key': {'type': 'string', 'minLength': 1, 'maxLength': 200}},
         ['invoice_id', 'expected_version', 'idempotency_key'],
         {'ok': {'type': 'boolean'}, 'invoice_id': {'type': 'integer'}, 'removed': {'type': 'boolean'},
          'affected_orders': {'type': 'array'}, 'next_step': {'type': 'string'}}),
    ]:
        operations.OPERATION_REGISTRY[name] = operations.BusinessOperationDefinition(
            name, 1,
            'Sprawdza usuwalność konkretnej faktury i wszystkie zamówienia, których dotyczy.' if read else
            'Po jawnej zgodzie HUMAN usuwa fakturę przez istniejący mechanizm aplikacji. Najpierw pokaż skutki i wszystkie affected_orders z invoices.removal.preview. Nie usuwaj po cichu. Po sukcesie ponownie odczytaj zamówienie; nie zakładaj odblokowania ani anulowania przesyłki.',
            permission, risk, 'NONE' if read else 'REQUIRED', frozenset({'HUMAN', 'AI_AGENT'}),
            {'type': 'object', 'additionalProperties': False, 'required': required, 'properties': properties},
            {'type': 'object', 'required': list(output), 'properties': output},
            operations.IDEMPOTENCY_NONE if read else operations.IDEMPOTENCY_REQUIRED,
            'READ_STANDARD' if read else 'WRITE', read)
    def read_handler(*args, **kwargs):
        result = preview(*args, **kwargs)
        result['effects'] = '; '.join(result['effects'])
        return result
    operations._HANDLERS[READ] = read_handler
    operations._HANDLERS[WRITE] = execute
    operations.FRESHNESS_GROUP_BY_OPERATION[READ] = 'invoice_amendment'
