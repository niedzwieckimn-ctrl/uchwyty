"""Durable fulfillment evidence; Supabase authority, SQLite/file cache."""
import base64
import hashlib
import json
from pathlib import Path

TABLES = {
    'order_shipping_requirements': 'order_id', 'fulfillment_documents': 'order_id',
    'fulfillment_document_intents': 'order_id', 'fulfillment_shipping_attempts': 'order_id',
    'fulfillment_shipping_members': 'order_id', 'fulfillment_verifications': 'order_id',
}


def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS fulfillment_reconciliation_versions(order_id INTEGER PRIMARY KEY, revision INTEGER NOT NULL)')
    db.execute('''CREATE TABLE IF NOT EXISTS fulfillment_verifications(order_id INTEGER NOT NULL,kind TEXT NOT NULL,
        payload TEXT NOT NULL,PRIMARY KEY(order_id,kind))''')
    db.execute('''CREATE TABLE IF NOT EXISTS fulfillment_reconciliation_pending(order_id INTEGER PRIMARY KEY,
        expected_revision INTEGER NOT NULL,payload TEXT NOT NULL)''')


def local_status(b, db, oid):
    if not b.supabase_enabled():
        return {'durable': True, 'source': 'sqlite', 'pending': False}
    pending = db.execute('SELECT 1 FROM fulfillment_reconciliation_pending WHERE order_id=?', (oid,)).fetchone()
    return {'durable': not bool(pending), 'source': 'supabase', 'pending': bool(pending)}


def _save(b, oid, revision, payload):
    result = b.supabase_request('/rest/v1/rpc/save_fulfillment_reconciliation', method='POST',
                               payload={'p_order_id': oid, 'p_expected_revision': revision, 'p_payload': payload})
    if not isinstance(result, dict) or not result.get('saved'):
        raise ValueError('Nie zapisano trwałych metadanych realizacji. Wymagane jest uzgodnienie konfliktu wersji.')
    c = b.conn()
    try:
        c.execute('INSERT OR REPLACE INTO fulfillment_reconciliation_versions VALUES(?,?)', (oid, result['revision']))
        c.execute('DELETE FROM fulfillment_reconciliation_pending WHERE order_id=?', (oid,))
        c.commit()
    finally:
        c.close()


def retry_pending(b, oid):
    restore(b, oid)
    c = b.conn()
    try:
        pending = c.execute('SELECT * FROM fulfillment_reconciliation_pending WHERE order_id=?', (oid,)).fetchone()
    finally:
        c.close()
    if pending:
        _save(b, oid, pending['expected_revision'], json.loads(pending['payload']))


def stage(b, oid, connection=None, allow_replace=False):
    if not b.supabase_enabled():
        return
    c = connection or b.conn()
    try:
        payload = {table: [dict(r) for r in c.execute(f'SELECT * FROM {table} WHERE {key}=?', (oid,))] for table, key in TABLES.items()}
        payload['fulfillment_shipping_attempts'] = [dict(r) for r in c.execute('''SELECT * FROM fulfillment_shipping_attempts WHERE order_id=?
            OR order_id IN (SELECT attempt_order_id FROM fulfillment_shipping_members WHERE order_id=?)''', (oid, oid))]
        payload['packing_batches'] = [dict(r) for r in c.execute('SELECT * FROM packing_batches WHERE root_order_id=?', (oid,))]
        payload['packing_allocations'] = [dict(r) for r in c.execute('SELECT * FROM packing_allocations WHERE batch_id IN (SELECT id FROM packing_batches WHERE root_order_id=?)', (oid,))]
        lists = [dict(r) for r in c.execute('''SELECT DISTINCT pl.* FROM packing_lists pl
            JOIN packing_batches pb ON pb.packing_list_id=pl.packing_list_id
            JOIN packing_allocations pa ON pa.batch_id=pb.id WHERE pa.order_id=? OR pl.root_order_id=?''', (oid, oid))]
        payload['packing_lists'] = lists
        keys = [r['packing_list_id'] for r in lists]
        if keys:
            marks = ','.join('?' for _ in keys)
            payload['packing_batches'] = [dict(r) for r in c.execute(f'SELECT * FROM packing_batches WHERE packing_list_id IN ({marks}) OR root_order_id=?', (*keys, oid))]
            ids = [r['id'] for r in payload['packing_batches']]
            bid_marks = ','.join('?' for _ in ids)
            payload['packing_allocations'] = [dict(r) for r in c.execute(f'SELECT * FROM packing_allocations WHERE batch_id IN ({bid_marks})', ids)]
            payload['packing_shipments'] = [dict(r) for r in c.execute(f'SELECT * FROM packing_shipments WHERE packing_list_id IN ({marks})', keys)]
            payload['fulfillment_document_history'] = [dict(r) for r in c.execute(f"SELECT * FROM fulfillment_document_history WHERE kind='packing_list' AND document_id IN ({bid_marks})", ids)]
        row = c.execute('SELECT revision FROM fulfillment_reconciliation_versions WHERE order_id=?', (oid,)).fetchone()
        revision = row[0] if row else 0
    finally:
        if connection is None:
            c.close()
    for document in payload['fulfillment_documents'] + payload.get('fulfillment_document_history', []):
        path = Path(document['path'])
        if path.is_file():
            content = path.read_bytes()
            if len(content) > 10_000_000:
                raise ValueError('Dokument przekracza limit trwałego zapisu 10 MB.')
            document['pdf_base64'] = base64.b64encode(content).decode('ascii')
        document.pop('path', None)
    c = connection or b.conn()
    try:
        if not allow_replace and c.execute('SELECT 1 FROM fulfillment_reconciliation_pending WHERE order_id=?', (oid,)).fetchone():
            raise ValueError('Najpierw uzgodnij wcześniejszy zapis metadanych.')
        c.execute('INSERT OR REPLACE INTO fulfillment_reconciliation_pending VALUES(?,?,?)', (oid, revision, json.dumps(payload)))
        if connection is None:
            c.commit()
    finally:
        if connection is None:
            c.close()
    return revision, payload



def publish(b, oid):
    if not b.supabase_enabled():
        return
    c = b.conn()
    try:
        pending = c.execute('SELECT * FROM fulfillment_reconciliation_pending WHERE order_id=?', (oid,)).fetchone()
    finally:
        c.close()
    if pending:
        _save(b, oid, pending['expected_revision'], json.loads(pending['payload']))
        return
    revision, payload = stage(b, oid)
    _save(b, oid, revision, payload)


def restore(b, oid):
    if not b.supabase_enabled():
        return
    rows = b.supabase_request('/rest/v1/fulfillment_reconciliation', params={'order_id': 'eq.' + str(oid), 'select': 'revision,payload'})
    if not isinstance(rows, list):
        raise ValueError('Nie można odczytać trwałego stanu realizacji. Sprawdź migrację reconciliation.')
    if not rows:
        return
    record = rows[0]
    c = b.conn()
    try:
        pending = c.execute('SELECT * FROM fulfillment_reconciliation_pending WHERE order_id=?', (oid,)).fetchone()
        if pending:
            if record['payload'] == json.loads(pending['payload']):
                c.execute('DELETE FROM fulfillment_reconciliation_pending WHERE order_id=?', (oid,))
                c.commit()
            else:
                return  # Keep the explicit unresolved result; never overwrite a competing revision.
        local = c.execute('SELECT revision FROM fulfillment_reconciliation_versions WHERE order_id=?', (oid,)).fetchone()
        if local and local[0] == record['revision'] and not record['payload'].get('packing_lists'):
            documents = list(c.execute('SELECT path,file_hash FROM fulfillment_documents WHERE order_id=?', (oid,)))
            expected = record['payload'].get('fulfillment_documents', [])
            if len(documents) == len(expected) and all(
                Path(d['path']).is_file() and hashlib.sha256(Path(d['path']).read_bytes()).hexdigest() == d['file_hash']
                for d in documents
            ):
                return
        payload = record['payload']
        for table in TABLES:
            entries = payload.get(table, [])
            if table not in {'fulfillment_shipping_attempts', 'fulfillment_shipping_members'}:
                c.execute(f'DELETE FROM {table} WHERE order_id=?', (oid,))
            for source in entries:
                source = dict(source)
                if table == 'fulfillment_documents':
                    encoded = source.pop('pdf_base64', None)
                    if not encoded:
                        continue
                    content = base64.b64decode(encoded, validate=True)
                    if hashlib.sha256(content).hexdigest() != source['file_hash']:
                        raise ValueError('Suma kontrolna trwałego dokumentu nie zgadza się.')
                    directory = Path(b.DATA_DIR) / 'fulfillment-cache'
                    directory.mkdir(parents=True, exist_ok=True)
                    path = directory / (source['file_hash'] + '.pdf')
                    path.write_bytes(content)
                    source['path'] = str(path)
                columns = {r[1] for r in c.execute(f'PRAGMA table_info({table})')}
                if not set(source) <= columns:
                    raise ValueError('Nieobsługiwana wersja metadanych realizacji.')
                names = list(source)
                c.execute(f'INSERT OR REPLACE INTO {table} ({",".join(names)}) VALUES ({",".join("?" for _ in names)})', tuple(source.values()))
        restore_packing_evidence(c, payload)
        for source in payload.get('fulfillment_document_history', []):
            source = dict(source)
            encoded = source.pop('pdf_base64', None)
            if not encoded:
                continue
            content = base64.b64decode(encoded, validate=True)
            if hashlib.sha256(content).hexdigest() != source['file_hash']:
                raise ValueError('Nieprawidłowa suma kontrolna historycznej LP.')
            directory = Path(b.DATA_DIR) / 'packing-history'
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / (source['file_hash'] + '.pdf')
            if not path.exists():
                path.write_bytes(content)
            source['path'] = str(path)
            columns = ('order_id','kind','document_id','content_hash','path','created_at','file_hash')
            c.execute('INSERT OR IGNORE INTO fulfillment_document_history VALUES(?,?,?,?,?,?,?)', tuple(source[k] for k in columns))
        # A delayed secondary-order payload may contain an old document pointer.
        # Resolve it against the highest logical revision, never the arrival order.
        c.execute('''DELETE FROM fulfillment_documents WHERE kind='packing_list' AND document_id IN (
            SELECT pb.id FROM packing_batches pb JOIN packing_lists pl ON pl.packing_list_id=pb.packing_list_id
            WHERE pb.id<>pl.current_batch_id)''')
        for logical in payload.get('packing_lists', []):
            current = c.execute('SELECT current_batch_id FROM packing_lists WHERE packing_list_id=?', (logical['packing_list_id'],)).fetchone()
            for document in c.execute("SELECT * FROM fulfillment_document_history WHERE kind='packing_list' AND document_id=?", (current[0],)).fetchall():
                names = ('order_id','kind','document_id','content_hash','path','created_at','file_hash')
                c.execute('''INSERT INTO fulfillment_documents VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(order_id,kind) DO UPDATE SET document_id=excluded.document_id,
                    content_hash=excluded.content_hash,path=excluded.path,created_at=excluded.created_at,file_hash=excluded.file_hash
                    WHERE fulfillment_documents.document_id<excluded.document_id''', tuple(document[k] for k in names))
        c.execute('INSERT OR REPLACE INTO fulfillment_reconciliation_versions VALUES(?,?)', (oid, record['revision']))
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def restore_packing_evidence(c, payload):
    """Restore structural evidence only; never delete rows or regenerate a batch."""
    for table in ('packing_batches', 'packing_allocations'):
        for source in payload.get(table) or []:
            names = list(source)
            columns = {r[1] for r in c.execute(f'PRAGMA table_info({table})')}
            if not set(names) <= columns:
                raise ValueError('Nieobsługiwana wersja zawartości paczki.')
            existing = c.execute(f'SELECT * FROM {table} WHERE id=?', (source['id'],)).fetchone()
            if existing:
                immutable = tuple(source) if table == 'packing_allocations' else ('root_order_id', 'created_at', 'invoice_id')
                if any(existing[k] != source[k] for k in immutable):
                    raise ValueError('Konflikt trwałego identyfikatora zawartości listy pakowej.')
                if table == 'packing_batches' and source.get('packing_list_id'):
                    if existing['packing_list_id'] and existing['packing_list_id'] != source['packing_list_id']:
                        raise ValueError('Batch należy już do innej logicznej listy pakowej.')
                    if not existing['packing_list_id']:
                        c.execute('UPDATE packing_batches SET packing_list_id=?,previous_batch_id=?,selection_hash=? WHERE id=?',
                                  (source['packing_list_id'], source.get('previous_batch_id'), source.get('selection_hash'), source['id']))
            c.execute(f'INSERT OR IGNORE INTO {table} ({",".join(names)}) VALUES ({",".join("?" for _ in names)})', tuple(source.values()))
    for source in payload.get('packing_lists') or []:
        if not c.execute('SELECT 1 FROM packing_batches WHERE id=?', (source['current_batch_id'],)).fetchone():
            raise ValueError('Brak bieżącego batcha w trwałej historii LP.')
        existing = c.execute('SELECT * FROM packing_lists WHERE packing_list_id=?', (source['packing_list_id'],)).fetchone()
        if existing and existing['revision'] == source['revision'] and existing['current_batch_id'] != source['current_batch_id']:
            raise ValueError('Konflikt bieżącej wersji listy pakowej.')
        if not existing or existing['revision'] < source['revision']:
            c.execute('''INSERT INTO packing_lists VALUES(?,?,?,?,?,?) ON CONFLICT(packing_list_id) DO UPDATE SET
                         current_batch_id=excluded.current_batch_id,revision=excluded.revision,invoice_id=excluded.invoice_id''',
                      tuple(source[k] for k in ('packing_list_id','root_order_id','invoice_id','current_batch_id','revision','created_at')))
    for source in payload.get('packing_shipments') or []:
        batch = c.execute('SELECT packing_list_id FROM packing_batches WHERE id=?', (source['final_batch_id'],)).fetchone()
        if (not batch or batch['packing_list_id'] != source['packing_list_id']
                or not c.execute('SELECT 1 FROM packing_allocations WHERE batch_id=?', (source['final_batch_id'],)).fetchone()):
            raise ValueError('Brak kompletnego finalnego batcha w trwałej historii wysyłki.')
        existing = c.execute('SELECT * FROM packing_shipments WHERE shipment_key=?', (source['shipment_key'],)).fetchone()
        if existing and any(existing[key] != value for key, value in source.items()):
            raise ValueError('Konflikt finalnej zawartości wysyłki.')
        c.execute('INSERT OR IGNORE INTO packing_shipments VALUES(?,?,?,?,?,?)',
                  tuple(source[k] for k in ('shipment_key','packing_list_id','final_batch_id','confirmed_at','carrier','tracking')))
