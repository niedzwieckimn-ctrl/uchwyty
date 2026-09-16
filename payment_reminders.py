"""Shared payment-reminder service for UI, scheduler and Business Operations."""
from __future__ import annotations

from datetime import datetime
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
    db.executescript('''
    CREATE TABLE IF NOT EXISTS payment_reminder_attempts(
        attempt_id TEXT PRIMARY KEY,
        invoice_id INTEGER NOT NULL,
        attempted_at TEXT NOT NULL,
        completed_at TEXT,
        channel TEXT NOT NULL,
        trigger_source TEXT NOT NULL,
        recipient TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL CHECK(status IN ('RUNNING','SUCCESS','FAILED')),
        error TEXT NOT NULL DEFAULT '',
        FOREIGN KEY(invoice_id) REFERENCES invoices(id)
    );
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


def send(invoice_id, *, trigger_source, attempt_id=None):
    """Send once, record the attempt, and mutate invoice state only on success."""
    b = _backend
    invoice = _invoice(invoice_id)
    if int(invoice.get('paid') or 0):
        return {'ok': False, 'invoice_id': int(invoice_id), 'reminder_sent': False,
                'attempt_id': '', 'error_code': 'INVOICE_ALREADY_PAID',
                'error': 'Faktura jest oznaczona jako opłacona.'}
    attempt_id = str(attempt_id or uuid.uuid4())
    db = b.conn()
    existing = db.execute(
        'SELECT * FROM payment_reminder_attempts WHERE attempt_id=?', (attempt_id,)).fetchone()
    if existing:
        db.close()
        existing = dict(existing)
        return {'ok': existing['status'] == 'SUCCESS', 'invoice_id': int(invoice_id),
                'reminder_sent': existing['status'] == 'SUCCESS', 'attempt_id': attempt_id,
                'error_code': 'REMINDER_SEND_FAILED' if existing['status'] == 'FAILED' else '',
                'error': existing.get('error') or ''}
    attempted_at = b.now_iso()
    db.execute('''INSERT INTO payment_reminder_attempts(
                      attempt_id,invoice_id,attempted_at,channel,trigger_source,recipient,status,error)
                  VALUES(?,?,?,?,?,?,?,?)''',
               (attempt_id, int(invoice_id), attempted_at, 'email', str(trigger_source),
                str(invoice.get('customer_email') or ''), 'RUNNING', ''))
    db.commit()
    db.close()

    error_message = ''
    try:
        if not b.send_payment_reminder:
            raise RuntimeError('Moduł wysyłki przypomnień nie jest dostępny')
        invoice_context, pdf_url = b._invoice_email_context(int(invoice_id))
        result = b.send_payment_reminder(invoice_context, pdf_url=pdf_url)
        if not isinstance(result, dict) or not result.get('ok'):
            raise RuntimeError(str((result or {}).get('error') or 'Wysyłka nie powiodła się'))
        # paid=None is deliberate: sending a reminder must never alter payment state.
        b._set_invoice_payment_state(int(invoice_id), reminder=1, paid=None)
    except Exception as exc:
        error_message = str(exc or type(exc).__name__)[:300]

    db = b.conn()
    status = 'FAILED' if error_message else 'SUCCESS'
    db.execute('''UPDATE payment_reminder_attempts
                     SET status=?,error=?,completed_at=? WHERE attempt_id=?''',
               (status, error_message, b.now_iso(), attempt_id))
    db.commit()
    db.close()
    return {'ok': not error_message, 'invoice_id': int(invoice_id),
            'reminder_sent': not error_message, 'attempt_id': attempt_id,
            'error_code': 'REMINDER_SEND_FAILED' if error_message else '',
            'error': error_message}


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
                '''SELECT attempt_id,attempted_at,completed_at,channel,trigger_source,status,error
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
