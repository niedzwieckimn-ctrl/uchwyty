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


def publish(b, oid):
    if not b.supabase_enabled():
        return
    c = b.conn()
    try:
        payload = {table: [dict(r) for r in c.execute(f'SELECT * FROM {table} WHERE {key}=?', (oid,))] for table, key in TABLES.items()}
        payload['fulfillment_shipping_attempts'] = [dict(r) for r in c.execute('''SELECT * FROM fulfillment_shipping_attempts WHERE order_id=?
            OR order_id IN (SELECT attempt_order_id FROM fulfillment_shipping_members WHERE order_id=?)''', (oid, oid))]
        payload['packing_batches'] = [dict(r) for r in c.execute('SELECT * FROM packing_batches WHERE root_order_id=?', (oid,))]
        payload['packing_allocations'] = [dict(r) for r in c.execute('SELECT * FROM packing_allocations WHERE batch_id IN (SELECT id FROM packing_batches WHERE root_order_id=?)', (oid,))]
        row = c.execute('SELECT revision FROM fulfillment_reconciliation_versions WHERE order_id=?', (oid,)).fetchone()
        revision = row[0] if row else 0
    finally:
        c.close()
    for document in payload['fulfillment_documents']:
        path = Path(document['path'])
        if path.is_file():
            content = path.read_bytes()
            if len(content) > 10_000_000:
                raise ValueError('Dokument przekracza limit trwałego zapisu 10 MB.')
            document['pdf_base64'] = base64.b64encode(content).decode('ascii')
        document.pop('path', None)
    c = b.conn()
    try:
        if c.execute('SELECT 1 FROM fulfillment_reconciliation_pending WHERE order_id=?', (oid,)).fetchone():
            raise ValueError('Najpierw uzgodnij wcześniejszy zapis metadanych.')
        c.execute('INSERT INTO fulfillment_reconciliation_pending VALUES(?,?,?)', (oid, revision, json.dumps(payload)))
        c.commit()
    finally:
        c.close()
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
        if local and local[0] == record['revision']:
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
        for table in ('packing_batches', 'packing_allocations'):
            for source in payload.get(table, []):
                names = list(source)
                columns = {r[1] for r in c.execute(f'PRAGMA table_info({table})')}
                if not set(names) <= columns:
                    raise ValueError('Nieobsługiwana wersja zawartości paczki.')
                c.execute(f'INSERT OR IGNORE INTO {table} ({",".join(names)}) VALUES ({",".join("?" for _ in names)})', tuple(source.values()))
        c.execute('INSERT OR REPLACE INTO fulfillment_reconciliation_versions VALUES(?,?)', (oid, record['revision']))
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()
