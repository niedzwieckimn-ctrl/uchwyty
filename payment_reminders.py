"""Shared payment-reminder service for UI, scheduler and Business Operations."""
from __future__ import annotations

from datetime import datetime
import inspect
import uuid

from cash_flow_module import cash_flow_overdue_invoices

READ = 'payments.reminders.read'
WRITE = 'payments.reminders.send'
WRITES = frozenset({WRITE})
_backend = None


def configure(backend):
    global _backend
    _backend = backend


def initialize(db):
    schema = '''
    CREATE TABLE IF NOT EXISTS payment_reminder_attempts(
        attempt_id TEXT PRIMARY KEY,
        invoice_id INTEGER NOT NULL,
        attempted_at TEXT NOT NULL,
        completed_at TEXT,
        channel TEXT NOT NULL,
        trigger_source TEXT NOT NULL,
        recipient TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL CHECK(status IN ('RUNNING','PROVIDER_ACCEPTED','SUCCESS','FAILED','UNKNOWN','RECONCILIATION_REQUIRED')),
        error TEXT NOT NULL DEFAULT '',
        provider_confirmed INTEGER NOT NULL DEFAULT 0,
        provider_message_id TEXT NOT NULL DEFAULT '',
        reconciled_at TEXT,
        FOREIGN KEY(invoice_id) REFERENCES invoices(id)
    );'''
    old = db.execute("SELECT sql FROM sqlite_master WHERE name='payment_reminder_attempts'").fetchone()
    if old and 'RECONCILIATION_REQUIRED' not in old[0]:
        db.execute('SAVEPOINT reminder_outcome_migration')
        try:
            db.execute(schema.replace('payment_reminder_attempts', 'payment_reminder_attempts_v2', 1))
            db.execute('''INSERT INTO payment_reminder_attempts_v2(
                attempt_id,invoice_id,attempted_at,completed_at,channel,trigger_source,recipient,status,error,provider_confirmed)
                SELECT attempt_id,invoice_id,attempted_at,completed_at,channel,trigger_source,recipient,status,error,
                       CASE WHEN status='SUCCESS' THEN 1 ELSE 0 END FROM payment_reminder_attempts''')
            db.execute('DROP TABLE payment_reminder_attempts')
            db.execute('ALTER TABLE payment_reminder_attempts_v2 RENAME TO payment_reminder_attempts')
            db.execute('RELEASE reminder_outcome_migration')
        except Exception:
            db.execute('ROLLBACK TO reminder_outcome_migration')
            db.execute('RELEASE reminder_outcome_migration')
            raise
    else:
        db.execute(schema)
    db.executescript('''
    CREATE INDEX IF NOT EXISTS idx_payment_reminder_attempts_invoice
        ON payment_reminder_attempts(invoice_id,attempted_at DESC);
    ''')


def _error(code, message, status='FAILED'):
    from business_operations import ControlledOperationError
    return ControlledOperationError(code, message, status=status)


def _invoice(invoice_id, db=None):
    b = _backend
    if b is None:
        raise _error('SERVICE_UNAVAILABLE', 'Usługa przypomnień nie jest dostępna.')
    owned = db is None
    db = db or b.conn()
    try:
        row = db.execute('''SELECT i.*,COALESCE(m.paid,0) AS paid,
                                   COALESCE(m.payment_reminder,0) AS payment_reminder,
                                   o.customer_email,o.customer_name,o.customer_id
                              FROM invoices i
                              LEFT JOIN invoice_meta m ON m.invoice_id=i.id
                              LEFT JOIN orders o ON o.id=i.order_id
                             WHERE i.id=?''', (int(invoice_id),)).fetchone()
        if row is None:
            raise _error('INVOICE_NOT_FOUND', 'Nie znaleziono faktury.', 'NOOP')
        return dict(row)
    finally:
        if owned:
            db.close()


_UNRESOLVED = ('RUNNING', 'PROVIDER_ACCEPTED', 'UNKNOWN', 'RECONCILIATION_REQUIRED')


def _outcome(row):
    row = dict(row)
    accepted = bool(row.get('provider_confirmed')) or row['status'] == 'SUCCESS'
    unresolved = row['status'] in _UNRESOLVED
    code = ('REMINDER_STATUS_RECONCILIATION_REQUIRED' if accepted and unresolved else
            'REMINDER_OUTCOME_UNKNOWN' if unresolved else
            'REMINDER_SEND_FAILED' if row['status'] == 'FAILED' else '')
    return {'ok': accepted, 'invoice_id': int(row['invoice_id']),
            'reminder_sent': accepted, 'attempt_id': row['attempt_id'],
            'provider_confirmed': accepted, 'delivery_status': row['status'],
            'reconciliation_required': unresolved, 'error_code': code,
            'error': row.get('error') or ('Wynik wcześniejszej wysyłki wymaga sprawdzenia. Nie wysyłam ponownie.' if unresolved and not accepted else '')}


def _save_outcome(attempt_id, status, *, error='', provider_confirmed=False, provider_message_id=''):
    b = _backend
    db = b.conn()
    try:
        db.execute('''UPDATE payment_reminder_attempts SET status=?,error=?,completed_at=?,
            provider_confirmed=MAX(provider_confirmed,?),
            provider_message_id=CASE WHEN ?<>'' THEN ? ELSE provider_message_id END,
            reconciled_at=CASE WHEN ?='SUCCESS' THEN ? ELSE reconciled_at END
            WHERE attempt_id=?''',
            (status, str(error)[:300], b.now_iso(), int(provider_confirmed), provider_message_id,
             provider_message_id, status, b.now_iso(), attempt_id))
        db.commit()
        return dict(db.execute('SELECT * FROM payment_reminder_attempts WHERE attempt_id=?', (attempt_id,)).fetchone())
    finally:
        db.close()


def reconcile_confirmed(attempt_id):
    """Repair only local invoice flags, using durable provider acceptance evidence.

    This function never calls the email provider. UNKNOWN/RUNNING attempts need
    operator/provider reconciliation and cannot authorize an automatic resend.
    """
    b = _backend
    db = b.conn()
    try:
        row = db.execute('SELECT * FROM payment_reminder_attempts WHERE attempt_id=?', (attempt_id,)).fetchone()
        if row is None:
            raise _error('REMINDER_ATTEMPT_NOT_FOUND', 'Nie znaleziono próby wysyłki.')
        row = dict(row)
    finally:
        db.close()
    if row['status'] == 'SUCCESS' or not row['provider_confirmed']:
        return _outcome(row)
    try:
        b._set_invoice_payment_state(int(row['invoice_id']), reminder=1, paid=None)
        row = _save_outcome(attempt_id, 'SUCCESS', provider_confirmed=True)
    except Exception as exc:
        row.update(status='RECONCILIATION_REQUIRED', error=str(exc)[:300])
        try:
            _save_outcome(attempt_id, 'RECONCILIATION_REQUIRED', error=str(exc), provider_confirmed=True)
        except Exception:
            pass  # Persisted PROVIDER_ACCEPTED still blocks another external send.
    return _outcome(row)


def send(invoice_id, *, trigger_source, attempt_id=None):
    """Persist an attempt before sending; never retry an uncertain external effect."""
    b = _backend
    attempt_id = str(attempt_id or uuid.uuid4())
    db = b.conn()
    try:
        db.execute('BEGIN IMMEDIATE')
        existing = db.execute('SELECT * FROM payment_reminder_attempts WHERE attempt_id=?', (attempt_id,)).fetchone()
        if existing and int(existing['invoice_id']) != int(invoice_id):
            raise _error('IDEMPOTENCY_CONFLICT', 'Identyfikator próby należy do innej faktury.', 'CONFLICT')
        if not existing:
            existing = db.execute('''SELECT * FROM payment_reminder_attempts WHERE invoice_id=?
                AND status IN ('RUNNING','PROVIDER_ACCEPTED','UNKNOWN','RECONCILIATION_REQUIRED')
                ORDER BY attempted_at,attempt_id LIMIT 1''', (int(invoice_id),)).fetchone()
        if existing:
            return _outcome(existing)
        invoice = _invoice(invoice_id, db)
        if int(invoice.get('paid') or 0):
            return {'ok': False, 'invoice_id': int(invoice_id), 'reminder_sent': False,
                    'attempt_id': '', 'error_code': 'INVOICE_ALREADY_PAID',
                    'error': 'Faktura jest oznaczona jako opłacona.'}
        if (invoice.get('publication_state') or 'complete') != 'complete':
            return {'ok': False, 'invoice_id': int(invoice_id), 'reminder_sent': False,
                    'attempt_id': '', 'error_code': 'INVOICE_NOT_COMPLETE',
                    'error': 'Publikacja faktury nie jest ukończona.'}
        db.execute('''INSERT INTO payment_reminder_attempts(
            attempt_id,invoice_id,attempted_at,channel,trigger_source,recipient,status,error)
            VALUES(?,?,?,?,?,?,?,?)''', (attempt_id, int(invoice_id), b.now_iso(), 'email',
            str(trigger_source), str(invoice.get('customer_email') or ''), 'RUNNING', ''))
        db.commit()
    finally:
        db.close()
    try:
        if not b.send_payment_reminder:
            raise RuntimeError('Moduł wysyłki przypomnień nie jest dostępny')
        invoice_context, pdf_url = b._invoice_email_context(int(invoice_id))
    except Exception as exc:
        return _outcome(_save_outcome(attempt_id, 'FAILED', error=str(exc)))
    try:
        kwargs = {'pdf_url': pdf_url}
        parameters = inspect.signature(b.send_payment_reminder).parameters.values()
        if any(p.name == 'idempotency_key' or p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters):
            kwargs['idempotency_key'] = 'payment-reminder/' + attempt_id
        result = b.send_payment_reminder(invoice_context, **kwargs)
    except Exception as exc:
        result = {'ok': False, 'delivery_outcome': 'unknown', 'error': str(exc)}
    if not isinstance(result, dict) or not result.get('ok'):
        result = result if isinstance(result, dict) else {}
        status = 'UNKNOWN' if result.get('delivery_outcome') == 'unknown' or not result else 'FAILED'
        return _outcome(_save_outcome(attempt_id, status, error=result.get('error') or 'Nie potwierdzono wyniku wysyłki.'))
    body = result.get('body')
    provider_id = str((body.get('id') if isinstance(body, dict) else None) or result.get('id') or '')
    try:
        _save_outcome(attempt_id, 'PROVIDER_ACCEPTED', provider_confirmed=True, provider_message_id=provider_id)
    except Exception as exc:
        # RUNNING was committed before the POST and blocks retry even when this
        # confirmation cannot be saved. The caller still receives the known fact.
        return _outcome({'invoice_id': invoice_id, 'attempt_id': attempt_id,
                         'status': 'RECONCILIATION_REQUIRED', 'provider_confirmed': True,
                         'error': 'Dostawca przyjął e-mail; zapis potwierdzenia wymaga uzgodnienia: ' + str(exc)[:180]})
    return reconcile_confirmed(attempt_id)


def read(data, actor=None, correlation_id='', transaction_connection=None):
    del actor, correlation_id
    b = _backend
    db = transaction_connection or b.conn()
    try:
        now = b.app_now()
        overdue = {int(row['id']): int(row.get('overdue_days') or 0)
                   for row in cash_flow_overdue_invoices(db, current_time=now)}
        where = []
        params = []
        if data.get('invoice_id'):
            where.append('i.id=?'); params.append(int(data['invoice_id']))
        query = ' '.join(str(data.get('query') or '').split()).casefold()
        rows = db.execute('''SELECT i.*,COALESCE(m.paid,0) AS paid,
                                    COALESCE(m.payment_reminder,0) AS payment_reminder,
                                    o.customer_id,o.customer_name,o.customer_email
                               FROM invoices i
                               LEFT JOIN invoice_meta m ON m.invoice_id=i.id
                               LEFT JOIN orders o ON o.id=i.order_id
                              WHERE ''' + (' AND '.join(where) if where else '1=1') +
                          ' ORDER BY COALESCE(i.payment_to,i.issue_date),i.id', tuple(params)).fetchall()
        records = []
        only_overdue = data.get('only_overdue', True)
        for raw in rows:
            row = dict(raw)
            if only_overdue and int(row['id']) not in overdue:
                continue
            haystack = ' '.join(str(row.get(field) or '') for field in
                                ('invoice_no', 'buyer_name', 'customer_name', 'customer_email')).casefold()
            if query and query not in haystack:
                continue
            attempts = [dict(item) for item in db.execute(
                '''SELECT attempt_id,attempted_at,completed_at,channel,trigger_source,status,error,
                          provider_confirmed,provider_message_id,reconciled_at
                     FROM payment_reminder_attempts WHERE invoice_id=?
                     ORDER BY attempted_at DESC,attempt_id DESC''', (int(row['id']),)).fetchall()]
            last = attempts[0] if attempts else None
            records.append({
                'invoice_id': int(row['id']), 'invoice_number': str(row.get('invoice_no') or ''),
                'customer_id': row.get('customer_id'),
                'buyer_name': str(row.get('buyer_name') or row.get('customer_name') or ''),
                'due_date': str(row.get('payment_to') or ''),
                'paid': bool(row.get('paid')), 'overdue': int(row['id']) in overdue,
                'overdue_days': overdue.get(int(row['id']), 0),
                'reminder_sent': bool(row.get('payment_reminder')),
                'attempt_count': len(attempts), 'last_attempt': last,
            })
        limit = int(data.get('limit') or 50)
        return {
            'ok': True,
            'scope': {'kind': 'entity_read', 'entity_existence_authoritative': True,
                      'empty_means': 'no_matching_invoice_records'},
            'complete': len(records) <= limit,
            'truncated': len(records) > limit,
            'records': records[:limit],
            'count': min(len(records), limit),
        }
    finally:
        if transaction_connection is None:
            db.close()


def execute(execution_id, definition, actor, data, approval_id, entity_type,
            entity_id, expected_version, correlation_id):
    del expected_version
    import business_operations as operations
    import internal_approval as approval
    from internal_rbac import load_actor_context, DENY
    b = _backend
    db = None
    try:
        db = b.conn()
        binding = db.execute(
            'SELECT human_id FROM internal_business_write_actors WHERE execution_id=?',
            (execution_id,)).fetchone()
        human_id = actor.delegated_by_actor_id or actor.actor_id
        if binding is None or binding['human_id'] != human_id:
            raise _error('INITIATOR_CHANGED', 'Zmienił się inicjator operacji.', 'DENIED')
        human = load_actor_context(human_id)
        if human is None or human.permission_decision(definition.required_permission) == DENY:
            raise _error('PERMISSION_REVOKED', 'Inicjator utracił uprawnienie.', 'DENIED')
        _invoice(data['invoice_id'], db)
        db.execute('BEGIN IMMEDIATE')
        approval.authorize_execution(
            approval_id, actor, WRITE, payload=data, entity_type=entity_type,
            entity_id=entity_id, operation_version=1, expected_entity_version=None,
            current_entity_version=None, transaction_connection=db)
        db.commit(); db.close(); db = None

        result = send(data['invoice_id'], trigger_source='agent', attempt_id=execution_id)
        if not result['ok']:
            row = operations._transition(
                execution_id, definition, actor, 'FAILED', 'business_operation.failed', 'FAILED',
                error_code=result['error_code'], message=result['error'], completed=True,
                expected_statuses=('RUNNING',))
            return operations._result_from_row(row)
        output = operations.validate_output(definition, {
            'ok': True, 'invoice_id': int(data['invoice_id']),
            'reminder_sent': True, 'attempt_id': result['attempt_id'],
            'provider_confirmed': result.get('provider_confirmed', True),
            'delivery_status': result.get('delivery_status', 'SUCCESS'),
            'reconciliation_required': result.get('reconciliation_required', False),
            'message': result.get('error') or '',
        })
        row = operations._transition(
            execution_id, definition, actor, 'SUCCESS', 'business_operation.success', 'SUCCESS',
            data=output, completed=True, expected_statuses=('RUNNING',))
        return operations._result_from_row(row)
    except Exception as exc:
        if db:
            db.rollback(); db.close()
        status = getattr(exc, 'status', 'FAILED')
        if isinstance(exc, approval.ApprovalDenied):
            status = 'DENIED'
        row = operations._transition(
            execution_id, definition, actor, status, 'business_operation.failed', status,
            error_code=getattr(exc, 'error_code', 'REMINDER_SEND_FAILED'),
            message=str(exc), completed=True, expected_statuses=('RUNNING',))
        return operations._result_from_row(row)
