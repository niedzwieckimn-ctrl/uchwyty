"""Transactional outbox for local payment flags and their order status changes.

The caller stages inside its business transaction, then flushes after commit.
Remote acknowledgement is revision-scoped. Pulls must use protect_incoming and
protected_keys inside their own write transaction so pending changes cannot be
reverted by an older cloud snapshot. No mail or payment-provider call occurs here.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import uuid


TABLE_KEYS = {'invoice_meta':'invoice_id', 'orders':'id'}
PROTECTED_FIELDS = {
    'invoice_meta': ('payment_reminder','paid','paid_at','seen_by_client','seen_at','updated_at'),
    'orders': ('status',),
}
LEASE_SECONDS = 180


def _now():
    return datetime.now(timezone.utc).isoformat()


def initialize(db):
    # execute (not executescript) preserves an already-open caller transaction.
    db.execute('''CREATE TABLE IF NOT EXISTS invoice_payment_sync_outbox(
        table_name TEXT NOT NULL, record_id INTEGER NOT NULL, revision INTEGER NOT NULL,
        payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'PENDING',
        error_code TEXT NOT NULL DEFAULT '', error_message TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL, synced_at TEXT,
        lease_token TEXT, lease_until TEXT, PRIMARY KEY(table_name,record_id))''')
    db.execute('''CREATE TABLE IF NOT EXISTS invoice_payment_sync_links(
        invoice_id INTEGER NOT NULL, table_name TEXT NOT NULL, record_id INTEGER NOT NULL,
        PRIMARY KEY(invoice_id,table_name,record_id))''')


def stage(db, invoice_id, order_ids=()):
    """Snapshot current rows atomically; never commits or opens a second DB."""
    if not db.in_transaction:
        raise ValueError('Payment outbox must be staged inside a business transaction')
    initialize(db)
    invoice_id = int(invoice_id)
    # Preserve previously linked order targets if only reminder flags changed.
    order_ids = set(int(item) for item in order_ids)
    order_ids.update(int(row[0]) for row in db.execute(
        "SELECT record_id FROM invoice_payment_sync_links WHERE invoice_id=? AND table_name='orders'",
        (invoice_id,)))
    targets = [('invoice_meta',invoice_id), *[('orders',item) for item in sorted(order_ids)]]
    for table, key in targets:
        row = db.execute(f'SELECT * FROM {table} WHERE {TABLE_KEYS[table]}=?',(key,)).fetchone()
        if row is None:
            raise ValueError('Payment outbox target does not exist')
        payload = json.dumps(dict(row),ensure_ascii=False,sort_keys=True,separators=(',',':'))
        old = db.execute('SELECT payload,revision FROM invoice_payment_sync_outbox WHERE table_name=? AND record_id=?',
                         (table,key)).fetchone()
        if old is None or old['payload'] != payload:
            db.execute('''INSERT INTO invoice_payment_sync_outbox(
                table_name,record_id,revision,payload,state,updated_at) VALUES(?,?,1,?,'PENDING',?)
                ON CONFLICT(table_name,record_id) DO UPDATE SET
                revision=invoice_payment_sync_outbox.revision+1,payload=excluded.payload,
                state='PENDING',error_code='',error_message='',updated_at=excluded.updated_at,synced_at=NULL''',
                (table,key,payload,_now()))
        db.execute('INSERT OR IGNORE INTO invoice_payment_sync_links VALUES(?,?,?)',(invoice_id,table,key))


def _pending(db, table):
    if table not in TABLE_KEYS:
        return {}
    return {int(row['record_id']):json.loads(row['payload']) for row in db.execute(
        "SELECT record_id,payload FROM invoice_payment_sync_outbox WHERE table_name=? AND state<>'SYNCED'",(table,))}


def refresh_order_snapshot(db, order_id):
    """Advance an existing payment intent when shipping changes the same status.

    No artificial invoice/link is created. Other payment fields and links remain
    owned by the payment outbox; old worker acknowledgements cannot clear this.
    """
    old = db.execute("SELECT payload FROM invoice_payment_sync_outbox WHERE table_name='orders' AND record_id=?",
                     (order_id,)).fetchone()
    if old:
        row = db.execute('SELECT * FROM orders WHERE id=?', (order_id,)).fetchone()
        data = json.dumps(dict(row), ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        if data != old['payload']:
            db.execute("""UPDATE invoice_payment_sync_outbox SET revision=revision+1,payload=?,state='PENDING',
                updated_at=?,synced_at=NULL WHERE table_name='orders' AND record_id=?""", (data, _now(), order_id))


def protect_incoming(db, table, rows):
    """Overlay only locally pending domain fields; keep unrelated remote updates."""
    if table not in TABLE_KEYS or not rows:
        return rows
    pending = _pending(db,table)
    key = TABLE_KEYS[table]
    result = []
    for source in rows:
        row = dict(source)
        stored = pending.get(int(row[key])) if row.get(key) is not None else None
        if stored:
            row.update({field:stored[field] for field in PROTECTED_FIELDS[table] if field in stored})
        result.append(row)
    return result


def protected_keys(db, table):
    """Pending groups also keep parent invoices/orders from cloud deletion."""
    if table not in {'invoice_meta','orders','invoices'}:
        return set()
    if table == 'invoices':
        rows = db.execute('''SELECT DISTINCT l.invoice_id FROM invoice_payment_sync_links l
            JOIN invoice_payment_sync_outbox o ON o.table_name=l.table_name AND o.record_id=l.record_id
            WHERE o.state<>'SYNCED' ''')
    else:
        rows = db.execute('''SELECT DISTINCT l.record_id FROM invoice_payment_sync_links l
            WHERE l.table_name=? AND l.invoice_id IN(
                SELECT p.invoice_id FROM invoice_payment_sync_links p JOIN invoice_payment_sync_outbox o
                ON o.table_name=p.table_name AND o.record_id=p.record_id WHERE o.state<>'SYNCED')''',(table,))
    return {str(row[0]) for row in rows}


def status(backend, invoice_id, connection=None):
    owned = connection is None
    db = connection or backend.conn()
    try:
        rows = list(db.execute('''SELECT o.* FROM invoice_payment_sync_outbox o
            JOIN invoice_payment_sync_links l ON l.table_name=o.table_name AND l.record_id=o.record_id
            WHERE l.invoice_id=? ORDER BY o.table_name,o.record_id''',(int(invoice_id),)))
        local_saved = db.execute('SELECT 1 FROM invoice_meta WHERE invoice_id=?',(int(invoice_id),)).fetchone() is not None
        pending = [row for row in rows if row['state'] != 'SYNCED']
        if not backend.supabase_enabled():
            mode = 'LOCAL_ONLY'
        elif not rows:
            mode = 'NOT_TRACKED'
        elif pending:
            mode = 'ERROR' if any(row['error_code'] for row in pending) else 'PENDING'
        else:
            mode = 'SYNCED'
        return {'local_saved':local_saved,'sync_status':mode,
                'cloud_synced':True if mode == 'SYNCED' else None if mode in {'LOCAL_ONLY','NOT_TRACKED'} else False,
                'pending':bool(pending) and backend.supabase_enabled(),
                'pending_records':len(pending) if backend.supabase_enabled() else 0,
                'error_codes':sorted({row['error_code'] for row in pending if row['error_code']})}
    finally:
        if owned:
            db.close()


def _claim(backend, table, key):
    db = backend.conn()
    try:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM invoice_payment_sync_outbox WHERE table_name=? AND record_id=?',(table,key)).fetchone()
        if not row or row['state'] == 'SYNCED' or (row['lease_token'] and row['lease_until'] and row['lease_until'] > _now()):
            return None
        token = str(uuid.uuid4())
        expires = (datetime.now(timezone.utc)+timedelta(seconds=LEASE_SECONDS)).isoformat()
        db.execute('UPDATE invoice_payment_sync_outbox SET lease_token=?,lease_until=? WHERE table_name=? AND record_id=?',
                   (token,expires,table,key))
        db.commit()
        return {**dict(row),'lease_token':token}
    finally:
        db.close()


class SyncVerificationError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _read_remote(backend, table, key):
    fields = [TABLE_KEYS[table], *PROTECTED_FIELDS[table]]
    rows = backend.supabase_select_rows(table,order_by=TABLE_KEYS[table],page_size=2,
        extra_params={TABLE_KEYS[table]:'eq.'+str(key),'select':','.join(fields)})
    if not isinstance(rows,list) or len(rows) > 1:
        raise SyncVerificationError('REMOTE_RESPONSE_INVALID')
    if not rows:
        return None
    if any(field not in rows[0] for field in fields):
        raise SyncVerificationError('REMOTE_SCHEMA_INCOMPATIBLE')
    if str(rows[0][TABLE_KEYS[table]]) != str(key):
        raise SyncVerificationError('REMOTE_RESPONSE_INVALID')
    return rows[0]


def _field_matches(field, actual, expected):
    if actual == expected:
        return True
    if field.endswith('_at') and actual and expected:
        try:
            # PostgREST may serialize a timestamp with T / UTC instead of a
            # space. This compares the transmitted value, not a new business date.
            values = [datetime.fromisoformat(str(value).replace('Z','+00:00')) for value in (actual,expected)]
            values = [value if value.tzinfo else value.replace(tzinfo=timezone.utc) for value in values]
            return values[0] == values[1]
        except ValueError:
            pass
    return False


def _publish(backend, claim):
    table,key = claim['table_name'],claim['record_id']
    stored = json.loads(claim['payload'])
    fields = {field:stored[field] for field in PROTECTED_FIELDS[table]}
    before = _read_remote(backend,table,key)  # Explicit columns: schema mismatch cannot silently pass.
    db = backend.conn()
    try:
        # Order status is also written by shipment reconciliation. Serialize its
        # PATCH with local shipping commits; invoice-only writes retain their
        # existing independent revision protocol.
        if table == 'orders':
            db.execute('BEGIN IMMEDIATE')
        current = db.execute('''SELECT 1 FROM invoice_payment_sync_outbox WHERE table_name=? AND record_id=?
            AND revision=? AND payload=? AND lease_token=? AND lease_until>?''',
            (table,key,claim['revision'],claim['payload'],claim['lease_token'],_now())).fetchone()
        if current is None:
            raise SyncVerificationError('REVISION_SUPERSEDED')
        if table == 'orders':
            local = db.execute('SELECT status FROM orders WHERE id=?', (key,)).fetchone()
            if not local or local['status'] != fields['status']:
                raise SyncVerificationError('LOCAL_STATUS_SUPERSEDED')
        if before is None:
            compatible = backend.supabase_compatible_rows(table,[stored])
            if len(compatible) != 1 or any(field not in compatible[0] or compatible[0][field] != value for field,value in fields.items()):
                raise SyncVerificationError('REMOTE_SCHEMA_INCOMPATIBLE')
            backend.supabase_upsert_rows(table,[stored],TABLE_KEYS[table])
        else:
            # Existing remote rows retain unrelated fields, including fresher PDFs.
            params = {TABLE_KEYS[table]:'eq.'+str(key)}
            if table == 'orders':
                params['status'] = 'eq.' + str(before['status'])
            backend.supabase_request('/rest/v1/'+table,method='PATCH',params=params,payload=fields)
        if table == 'orders':
            db.commit()
    finally:
        db.close()
    after = _read_remote(backend,table,key)
    if after is None or any(not _field_matches(field,after.get(field),value) for field,value in fields.items()):
        raise SyncVerificationError('REMOTE_WRITE_UNVERIFIED')


def _complete_claim(backend, claim, error_code=''):
    db = backend.conn()
    try:
        db.execute('BEGIN IMMEDIATE')
        if error_code:
            db.execute('''UPDATE invoice_payment_sync_outbox SET state='PENDING',error_code=?,
                error_message='Zapis lokalny oczekuje na potwierdzoną synchronizację.'
                WHERE table_name=? AND record_id=? AND revision=? AND payload=? AND lease_token=?''',
                (error_code,claim['table_name'],claim['record_id'],claim['revision'],claim['payload'],claim['lease_token']))
        else:
            db.execute('''UPDATE invoice_payment_sync_outbox SET state='SYNCED',synced_at=?,error_code='',error_message=''
                WHERE table_name=? AND record_id=? AND revision=? AND payload=? AND lease_token=?''',
                (_now(),claim['table_name'],claim['record_id'],claim['revision'],claim['payload'],claim['lease_token']))
        # An old acknowledgement releases its lease but never clears a newer revision.
        db.execute('''UPDATE invoice_payment_sync_outbox SET lease_token=NULL,lease_until=NULL
            WHERE table_name=? AND record_id=? AND lease_token=?''',
            (claim['table_name'],claim['record_id'],claim['lease_token']))
        db.commit()
    finally:
        db.close()


def flush_pending(backend, invoice_id=None, limit=50):
    if not backend.supabase_enabled():
        return {'attempted':0,'errors':[],'sync_status':'LOCAL_ONLY'}
    db = backend.conn()
    try:
        sql = "SELECT o.table_name,o.record_id FROM invoice_payment_sync_outbox o WHERE o.state<>'SYNCED'"
        params = []
        if invoice_id is not None:
            sql += ''' AND EXISTS(SELECT 1 FROM invoice_payment_sync_links l WHERE l.invoice_id=?
                AND l.table_name=o.table_name AND l.record_id=o.record_id)'''
            params.append(int(invoice_id))
        sql += ' ORDER BY o.updated_at,o.table_name,o.record_id LIMIT ?'
        params.append(max(1,min(int(limit),500)))
        targets = [tuple(row) for row in db.execute(sql,params)]
    finally:
        db.close()
    errors,attempted = [],0
    for table,key in targets:
        claim = _claim(backend,table,key)
        if claim is None:
            continue
        attempted += 1
        code = ''
        try:
            _publish(backend,claim)
        except Exception as exc:
            text = str(exc).lower()
            code = (exc.code if isinstance(exc,SyncVerificationError) else
                    'REMOTE_SCHEMA_INCOMPATIBLE' if any(part in text for part in ('column','schema cache','pgrst204','pgrst200'))
                    else 'REMOTE_SYNC_FAILED')
            errors.append({'table':table,'record_id':key,'error_code':code})
        _complete_claim(backend,claim,code)
    result = {'attempted':attempted,'errors':errors}
    if invoice_id is not None:
        result.update(status(backend,invoice_id))
    return result
