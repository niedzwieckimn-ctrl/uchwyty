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


class ReconciliationPending(ValueError):
    def __init__(self, order_id):
        self.order_id = int(order_id)
        super().__init__('Najpierw uzgodnij wcześniejszy zapis metadanych.')


def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS fulfillment_reconciliation_versions(order_id INTEGER PRIMARY KEY, revision INTEGER NOT NULL)')
    db.execute('''CREATE TABLE IF NOT EXISTS fulfillment_verifications(order_id INTEGER NOT NULL,kind TEXT NOT NULL,
        payload TEXT NOT NULL,PRIMARY KEY(order_id,kind))''')
    db.execute('''CREATE TABLE IF NOT EXISTS fulfillment_reconciliation_pending(order_id INTEGER PRIMARY KEY,
        expected_revision INTEGER NOT NULL,payload TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS fulfillment_packing_id_map(
        identity_key TEXT PRIMARY KEY, local_id INTEGER NOT NULL)''')


def _remap_packing_ids(db, payload):
    """Give colliding SQLite-era IDs a local identity without changing durable evidence.

    Separate Render files have each started their AUTOINCREMENT at one. The
    immutable Supabase snapshots therefore cannot be inserted by bare integer ID.
    Keep the mapping in the same transaction as the restored evidence so every
    repeated member snapshot resolves to the same local batch and allocation.
    """
    db.execute('''CREATE TABLE IF NOT EXISTS fulfillment_packing_id_map(
        identity_key TEXT PRIMARY KEY, local_id INTEGER NOT NULL)''')
    result = dict(payload)
    for name in ('packing_batches', 'packing_allocations', 'packing_lists',
                 'packing_shipments', 'fulfillment_documents', 'fulfillment_document_history'):
        if name in payload:
            result[name] = [dict(row) for row in payload[name] or []]

    batch_map = {}
    batches = result.get('packing_batches') or []
    next_batch = max([
        int(db.execute('SELECT COALESCE(MAX(id),0) FROM packing_batches').fetchone()[0]),
        *(int(row['id']) for row in batches),
    ]) + 1
    rekey_batches = any(
        (existing := db.execute('SELECT root_order_id,created_at FROM packing_batches WHERE id=?',
                                (row['id'],)).fetchone()) and
        (int(existing['root_order_id']) != int(row['root_order_id']) or
         existing['created_at'] != row['created_at'])
        for row in batches)
    assigned_batches = set()
    for row in sorted(batches, key=lambda item: int(item['id'])):
        remote_id = int(row['id'])
        identity = json.dumps(['batch', int(row['root_order_id']), remote_id,
                               row['created_at']], separators=(',', ':'))
        saved = db.execute('SELECT local_id FROM fulfillment_packing_id_map WHERE identity_key=?',
                           (identity,)).fetchone()
        if saved:
            local_id = int(saved[0])
        else:
            existing = db.execute('SELECT * FROM packing_batches WHERE id=?', (remote_id,)).fetchone()
            if existing and (int(existing['root_order_id']) != int(row['root_order_id'])
                             or existing['created_at'] != row['created_at']):
                matches = db.execute('''SELECT id,packing_list_id FROM packing_batches
                    WHERE root_order_id=? AND created_at=?''',
                    (row['root_order_id'], row['created_at'])).fetchall()
                compatible = [match for match in matches if not (
                    match['packing_list_id'] and row.get('packing_list_id') and
                    match['packing_list_id'] != row['packing_list_id'])]
                if len(compatible) == 1:
                    local_id = int(compatible[0]['id'])
                else:
                    local_id, next_batch = next_batch, next_batch + 1
            elif remote_id in assigned_batches:
                raise ValueError('Powtórzony identyfikator różnych batchy w trwałej liście.')
            elif rekey_batches and not existing:
                local_id, next_batch = next_batch, next_batch + 1
            else:
                local_id = remote_id
            db.execute('INSERT INTO fulfillment_packing_id_map VALUES(?,?)', (identity, local_id))
        if remote_id in batch_map and batch_map[remote_id] != local_id:
            raise ValueError('Niejednoznaczny identyfikator batcha w trwałej liście.')
        batch_map[remote_id] = local_id
        assigned_batches.add(local_id)
        row['id'] = local_id
    for row in batches:
        if row.get('previous_batch_id') is not None:
            row['previous_batch_id'] = batch_map.get(int(row['previous_batch_id']), row['previous_batch_id'])

    allocations = result.get('packing_allocations') or []
    next_allocation = max([
        int(db.execute('SELECT COALESCE(MAX(id),0) FROM packing_allocations').fetchone()[0]),
        *(int(row['id']) for row in allocations),
    ]) + 1
    rekey_allocations = any(
        (existing := db.execute('SELECT * FROM packing_allocations WHERE id=?',
                                (row['id'],)).fetchone()) and
        any(existing[key] != (batch_map.get(int(row['batch_id'])) if key == 'batch_id'
                              else row[key]) for key in row if key != 'id')
        for row in allocations)
    assigned_allocations = set()
    for row in sorted(allocations, key=lambda item: int(item['id'])):
        remote_id, remote_batch = int(row['id']), int(row['batch_id'])
        if remote_batch not in batch_map:
            raise ValueError('Alokacja odwołuje się do brakującego trwałego batcha.')
        row['batch_id'] = batch_map[remote_batch]
        identity = json.dumps(['allocation', remote_batch, remote_id,
                               int(row['order_id']), int(row['order_item_id']),
                               row['created_at']], separators=(',', ':'))
        saved = db.execute('SELECT local_id FROM fulfillment_packing_id_map WHERE identity_key=?',
                           (identity,)).fetchone()
        if saved:
            local_id = int(saved[0])
        else:
            existing = db.execute('SELECT * FROM packing_allocations WHERE id=?', (remote_id,)).fetchone()
            if existing and any(existing[key] != row[key] for key in row if key != 'id'):
                fields = [key for key in row if key != 'id']
                matches = db.execute('SELECT id FROM packing_allocations WHERE ' +
                    ' AND '.join(f'{key} IS ?' for key in fields),
                    tuple(row[key] for key in fields)).fetchall()
                if len(matches) == 1:
                    local_id = int(matches[0]['id'])
                else:
                    local_id, next_allocation = next_allocation, next_allocation + 1
            elif remote_id in assigned_allocations:
                raise ValueError('Powtórzony identyfikator różnych alokacji w trwałej liście.')
            elif rekey_allocations and not existing:
                local_id, next_allocation = next_allocation, next_allocation + 1
            else:
                local_id = remote_id
            db.execute('INSERT INTO fulfillment_packing_id_map VALUES(?,?)', (identity, local_id))
        assigned_allocations.add(local_id)
        row['id'] = local_id

    for row in result.get('packing_lists') or []:
        row['current_batch_id'] = batch_map.get(int(row['current_batch_id']), row['current_batch_id'])
    for row in result.get('packing_shipments') or []:
        row['final_batch_id'] = batch_map.get(int(row['final_batch_id']), row['final_batch_id'])
    for table in ('fulfillment_documents', 'fulfillment_document_history'):
        for row in result.get(table) or []:
            if row.get('kind') == 'packing_list':
                row['document_id'] = batch_map.get(int(row['document_id']), row['document_id'])
    return result


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
        c.execute('''INSERT INTO fulfillment_reconciliation_versions VALUES(?,?)
                     ON CONFLICT(order_id) DO UPDATE SET revision=MAX(revision,excluded.revision)''',
                  (oid, result['revision']))
        # An acknowledgement of an earlier payload must not discard a newer
        # local write staged while the remote request was in flight.
        c.execute('''DELETE FROM fulfillment_reconciliation_pending
                     WHERE order_id=? AND expected_revision=? AND payload=?''',
                  (oid, revision, json.dumps(payload)))
        c.commit()
    finally:
        c.close()


def retry_pending(b, oid, *, packing_only=False):
    restore(b, oid, allow_packing_extension=packing_only)
    c = b.conn()
    try:
        pending = c.execute('SELECT * FROM fulfillment_reconciliation_pending WHERE order_id=?', (oid,)).fetchone()
    finally:
        c.close()
    if pending:
        _save(b, oid, pending['expected_revision'], json.loads(pending['payload']))


def _evidence_section(value):
    # A table snapshot has no row-order semantics. Keep duplicate rows and all
    # fields in the comparison; sorting must never hide changed quantities,
    # invoice bindings, shipment identifiers or document bytes.
    if isinstance(value, list):
        return sorted(json.dumps(row, sort_keys=True, separators=(',', ':')) for row in value)
    return value


def _same_evidence(left, right):
    return left.keys() == right.keys() and all(
        _evidence_section(left[key]) == _evidence_section(right[key]) for key in left)


def _extends_open_packing(remote, pending):
    """Permit only a proven append to an open list, never last-writer-wins.

    Both payloads use the local ID mapping. Every durable history row must
    survive byte-for-byte; all non-packing state must be identical. A changed
    current pointer must follow the original batch's explicit revision chain.
    """
    changing = {'packing_lists', 'packing_batches', 'packing_allocations',
                'fulfillment_document_history', 'fulfillment_documents'}
    def encoded(value):
        return json.dumps(value, sort_keys=True, separators=(',', ':'))
    def rows(value):
        return sorted(encoded(row) for row in value or [])
    for name in (set(remote) | set(pending)) - changing:
        a, z = remote.get(name, []), pending.get(name, [])
        different = rows(a) != rows(z) if isinstance(a, list) and isinstance(z, list) else a != z
        if different:
            return False
    for name in ('packing_batches', 'packing_allocations', 'fulfillment_document_history'):
        old, new = rows(remote.get(name)), rows(pending.get(name))
        if len(new) != len(set(new)) or not set(old).issubset(new):
            return False
    old_lists = {r['packing_list_id']: r for r in remote.get('packing_lists', [])}
    new_lists = {r['packing_list_id']: r for r in pending.get('packing_lists', [])}
    if not old_lists or not old_lists.keys() <= new_lists.keys():
        return False
    batches = {r['id']: r for r in pending.get('packing_batches', [])}
    if len(batches) != len(pending.get('packing_batches', [])):
        return False
    finalized = {r['packing_list_id'] for r in remote.get('packing_shipments', [])}
    advanced = set()
    for key, old in old_lists.items():
        new = new_lists[key]
        if old == new:
            continue
        if (old.get('invoice_id') or new.get('invoice_id') or key in finalized
                or {k: v for k, v in old.items() if k not in {'revision', 'current_batch_id'}}
                != {k: v for k, v in new.items() if k not in {'revision', 'current_batch_id'}}):
            return False
        steps = int(new['revision']) - int(old['revision'])
        if steps <= 0 or steps > len(batches):
            return False
        cursor, seen = new['current_batch_id'], set()
        for _ in range(steps):
            batch = batches.get(cursor)
            if (not batch or cursor in seen or batch['packing_list_id'] != key
                    or batch.get('invoice_id') or cursor == old['current_batch_id']):
                return False
            seen.add(cursor)
            cursor = batch.get('previous_batch_id')
        if cursor != old['current_batch_id']:
            return False
        advanced.add(key)
    # An incomplete cache can have created a separate open list. Accept that
    # append only with a complete new chain and every older list unchanged.
    added_lists = new_lists.keys() - old_lists.keys()
    for key in added_lists:
        logical = new_lists[key]
        steps = int(logical['revision'])
        cursor, seen = logical['current_batch_id'], set()
        if logical.get('invoice_id') or steps <= 0 or steps > len(batches):
            return False
        for _ in range(steps):
            batch = batches.get(cursor)
            if (not batch or cursor in seen or batch['packing_list_id'] != key or batch.get('invoice_id')):
                return False
            seen.add(cursor)
            cursor = batch.get('previous_batch_id')
        if cursor is not None:
            return False
        advanced.add(key)
    if not advanced:
        return False
    old_batch_ids = {r['id'] for r in remote.get('packing_batches', [])}
    added_batches = set(batches) - old_batch_ids
    if any(batches[bid]['packing_list_id'] not in advanced or batches[bid].get('invoice_id')
           for bid in added_batches):
        return False
    old_allocations = set(rows(remote.get('packing_allocations')))
    if any(encoded(r) not in old_allocations and r['batch_id'] not in added_batches
           for r in pending.get('packing_allocations', [])):
        return False
    old_docs, new_docs = remote.get('fulfillment_documents', []), pending.get('fulfillment_documents', [])
    if rows([d for d in old_docs if d['kind'] != 'packing_list']) != rows(
            [d for d in new_docs if d['kind'] != 'packing_list']):
        return False
    history = set(rows(pending.get('fulfillment_document_history')))
    for doc in old_docs + new_docs:
        if doc['kind'] != 'packing_list':
            continue
        batch = batches.get(doc['document_id'])
        if not batch or encoded(doc) not in history:
            return False
        logical = new_lists.get(batch['packing_list_id'])
        if not logical:
            return False
    for doc in new_docs:
        if doc['kind'] == 'packing_list':
            batch = batches[doc['document_id']]
            if new_lists[batch['packing_list_id']]['current_batch_id'] != doc['document_id']:
                return False
    # A removed member may lose its current pointer only on the advanced list.
    for doc in old_docs:
        if doc['kind'] == 'packing_list' and doc not in new_docs:
            old_key = batches[doc['document_id']]['packing_list_id']
            if old_key not in advanced:
                replacement = next((d for d in new_docs if d['kind'] == 'packing_list'
                                    and d['order_id'] == doc['order_id']), None)
                if (old_lists[old_key].get('invoice_id') or old_key in finalized or not replacement
                        or batches[replacement['document_id']]['packing_list_id'] not in added_lists):
                    return False
    return True


def _merge_packing_history(remote, pending):
    """Add missing immutable remote evidence; refuse identity/content conflicts."""
    merged = dict(pending)
    for name, keys in (
            ('packing_batches', ('id',)), ('packing_allocations', ('id',)),
            ('fulfillment_document_history', ('order_id', 'kind', 'document_id'))):
        entries = {tuple(r[k] for k in keys): r for r in pending.get(name, [])}
        if len(entries) != len(pending.get(name, [])):
            return None
        for row in remote.get(name, []):
            identity = tuple(row[k] for k in keys)
            if identity in entries and entries[identity] != row:
                return None
            entries[identity] = row
        merged[name] = list(entries.values())
    return merged if _extends_open_packing(remote, merged) else None


def stage(b, oid, connection=None, allow_replace=False):
    if not b.supabase_enabled():
        return
    c = connection or b.conn()
    try:
        if connection is None:
            c.execute('BEGIN IMMEDIATE')
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
            shipment_ids = [r['shipment_key'].split(':', 1)[1] for r in payload['packing_shipments']
                            if r['carrier'] == 'inpost' and r['shipment_key'].startswith('inpost:')]
            payload['inpost_notifications'] = [dict(r) for sid in shipment_ids for r in c.execute(
                'SELECT * FROM inpost_tracking_notifications WHERE shipment_id=?', (sid,))]
            payload['fulfillment_document_history'] = [dict(r) for r in c.execute(f"SELECT * FROM fulfillment_document_history WHERE kind='packing_list' AND document_id IN ({bid_marks})", ids)]
        row = c.execute('SELECT revision FROM fulfillment_reconciliation_versions WHERE order_id=?', (oid,)).fetchone()
        revision = row[0] if row else 0
        for document in payload['fulfillment_documents'] + payload.get('fulfillment_document_history', []):
            path = Path(document['path'])
            if not path.is_file():
                raise ValueError('Brak pliku dokumentu do trwałego zapisu; najpierw odzyskaj dokument.')
            content = path.read_bytes()
            if len(content) > 10_000_000:
                raise ValueError('Dokument przekracza limit trwałego zapisu 10 MB.')
            if hashlib.sha256(content).hexdigest() != document['file_hash']:
                raise ValueError('Suma kontrolna dokumentu do trwałego zapisu nie zgadza się.')
            document['pdf_base64'] = base64.b64encode(content).decode('ascii')
            document.pop('path', None)
        if not allow_replace and c.execute('SELECT 1 FROM fulfillment_reconciliation_pending WHERE order_id=?', (oid,)).fetchone():
            raise ReconciliationPending(oid)
        c.execute('INSERT OR REPLACE INTO fulfillment_reconciliation_pending VALUES(?,?,?)', (oid, revision, json.dumps(payload)))
        if connection is None:
            c.commit()
    except Exception:
        if connection is None:
            c.rollback()
        raise
    finally:
        if connection is None:
            c.close()
    return revision, payload


def _repair_same_revision_files(b, c, payload):
    """Repair missing cached bytes without replaying already-applied metadata.

    A same-revision read can race a local write that has not yet been published.
    Only a document whose identity and hashes still match may be repaired.
    """
    for table in ('fulfillment_documents', 'fulfillment_document_history'):
        for source in payload.get(table, []):
            local = c.execute(f'''SELECT * FROM {table}
                WHERE order_id=? AND kind=? AND document_id=?''',
                (source['order_id'], source['kind'], source['document_id'])).fetchone()
            if not local or any(local[key] != source[key] for key in ('content_hash', 'file_hash')):
                continue
            path = Path(local['path'])
            if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == local['file_hash']:
                continue
            encoded = source.get('pdf_base64')
            if not encoded:
                raise ValueError('Brak trwałej kopii pliku dokumentu.')
            content = base64.b64decode(encoded, validate=True)
            if hashlib.sha256(content).hexdigest() != local['file_hash']:
                raise ValueError('Suma kontrolna trwałego dokumentu nie zgadza się.')
            # Restore only a previously stored cache path owned by this app.
            root = Path(b.DATA_DIR).resolve()
            if not path.resolve().is_relative_to(root):
                raise ValueError('Nieprawidłowa lokalizacja odtwarzanego dokumentu.')
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)



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


def _repair_missing_packing_documents(b, db, payload):
    """Same durable revision can have an incomplete local cache. Insert missing
    immutable evidence/pointers only; never replay other fulfillment state.
    Caller has already excluded a pending local write.
    """
    restore_packing_evidence(db, payload, already_translated=True)
    fields = ('order_id','kind','document_id','content_hash','path','created_at','file_hash')
    for table in ('fulfillment_document_history','fulfillment_documents'):
        for saved in payload.get(table, []):
            if saved['kind'] != 'packing_list':
                continue
            existing = db.execute(f'SELECT 1 FROM {table} WHERE order_id=? AND kind=?' +
                (' AND document_id=?' if table.endswith('_history') else ''),
                (saved['order_id'],saved['kind'],saved['document_id']) if table.endswith('_history')
                else (saved['order_id'],saved['kind'])).fetchone()
            if existing:
                continue
            batch = db.execute('''SELECT pb.id,pl.current_batch_id FROM packing_batches pb
                JOIN packing_lists pl ON pl.packing_list_id=pb.packing_list_id WHERE pb.id=?''',
                (saved['document_id'],)).fetchone()
            if not batch or (table=='fulfillment_documents' and batch['id']!=batch['current_batch_id']):
                continue
            content = base64.b64decode(saved.get('pdf_base64') or '', validate=True)
            if not content or hashlib.sha256(content).hexdigest()!=saved['file_hash']:
                raise ValueError('Suma kontrolna trwałego dokumentu nie zgadza się.')
            directory = Path(b.DATA_DIR)/'fulfillment-cache'
            directory.mkdir(parents=True,exist_ok=True)
            path = directory/(saved['file_hash']+'.pdf')
            path.write_bytes(content)
            source = dict(saved,path=str(path))
            db.execute(f'INSERT OR IGNORE INTO {table} VALUES(?,?,?,?,?,?,?)',tuple(source[k] for k in fields))


def restore(b, oid, *, allow_packing_extension=False):
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
        c.execute('BEGIN IMMEDIATE')
        local = c.execute('SELECT revision FROM fulfillment_reconciliation_versions WHERE order_id=?', (oid,)).fetchone()
        if local and int(record['revision']) < int(local[0]):
            return  # A delayed response can never move this cache backwards.
        pending = c.execute('SELECT * FROM fulfillment_reconciliation_pending WHERE order_id=?', (oid,)).fetchone()
        if pending:
            proposed = json.loads(pending['payload'])
            durable = _remap_packing_ids(c, record['payload'])
            if (int(record['revision']) > int(pending['expected_revision'])
                    and (_same_evidence(record['payload'], proposed) or _same_evidence(durable, proposed))):
                c.execute('DELETE FROM fulfillment_reconciliation_pending WHERE order_id=?', (oid,))
                c.execute('''INSERT INTO fulfillment_reconciliation_versions VALUES(?,?)
                    ON CONFLICT(order_id) DO UPDATE SET revision=MAX(revision,excluded.revision)''',
                    (oid, record['revision']))
                _repair_same_revision_files(b, c, durable)
                c.commit()
                return
            else:
                if allow_packing_extension and int(record['revision']) > int(pending['expected_revision']):
                    merged = _merge_packing_history(durable, proposed)
                    if merged is not None:
                        # Restore omitted immutable history before advancing the
                        # CAS base. Current selection and other domain data stay intact.
                        _repair_missing_packing_documents(b, c, durable)
                        _repair_same_revision_files(b, c, durable)
                        c.execute('''UPDATE fulfillment_reconciliation_pending SET expected_revision=?,payload=?
                            WHERE order_id=? AND expected_revision=? AND payload=?''',
                            (record['revision'], json.dumps(merged), oid, pending['expected_revision'], pending['payload']))
                        c.commit()
                        b.app.logger.info('PACKING_RECONCILIATION_REBASE order_id=%s from_revision=%s to_revision=%s',
                                          oid, pending['expected_revision'], record['revision'])
                    else:
                        different = sorted(key for key in set(durable) | set(proposed)
                            if _evidence_section(durable.get(key)) != _evidence_section(proposed.get(key)))
                        b.app.logger.warning('PACKING_RECONCILIATION_CONFLICT order_id=%s local_revision=%s remote_revision=%s sections=%s',
                            oid, pending['expected_revision'], record['revision'], ','.join(different))
                return  # Keep the explicit unresolved result; never overwrite a competing revision.
        if local and int(local[0]) == int(record['revision']):
            payload = _remap_packing_ids(c, record['payload'])
            _repair_missing_packing_documents(b, c, payload)
            _repair_same_revision_files(b, c, payload)
            restore_inpost_notifications(c, payload)
            c.commit()
            return
        payload = _remap_packing_ids(c, record['payload'])
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
        restore_packing_evidence(c, payload, already_translated=True)
        restore_inpost_notifications(c, payload)
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
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != source['file_hash']:
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
                    WHERE fulfillment_documents.document_id<>excluded.document_id''', tuple(document[k] for k in names))
        c.execute('INSERT OR REPLACE INTO fulfillment_reconciliation_versions VALUES(?,?)', (oid, record['revision']))
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def restore_packing_evidence(c, payload, *, already_translated=False):
    """Restore structural evidence only; never delete rows or regenerate a batch."""
    if not already_translated:
        payload = _remap_packing_ids(c, payload)
    for table in ('packing_batches', 'packing_allocations'):
        for source in payload.get(table) or []:
            names = list(source)
            columns = {r[1] for r in c.execute(f'PRAGMA table_info({table})')}
            if not set(names) <= columns:
                raise ValueError('Nieobsługiwana wersja zawartości paczki.')
            existing = c.execute(f'SELECT * FROM {table} WHERE id=?', (source['id'],)).fetchone()
            if existing:
                immutable = tuple(source) if table == 'packing_allocations' else ('root_order_id', 'created_at')
                if any(existing[k] != source[k] for k in immutable):
                    raise ValueError('Konflikt trwałego identyfikatora zawartości listy pakowej.')
                if table == 'packing_batches' and source.get('invoice_id'):
                    if existing['invoice_id'] and existing['invoice_id'] != source['invoice_id']:
                        raise ValueError('Batch należy już do innej faktury.')
                    if not existing['invoice_id']:
                        c.execute('UPDATE packing_batches SET invoice_id=? WHERE id=?',
                                  (source['invoice_id'], source['id']))
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
        if existing and existing['invoice_id'] and source.get('invoice_id') and existing['invoice_id'] != source['invoice_id']:
            raise ValueError('Logiczna lista pakowa należy już do innej faktury.')
        if existing and not existing['invoice_id'] and source.get('invoice_id'):
            c.execute('UPDATE packing_lists SET invoice_id=? WHERE packing_list_id=?',
                      (source['invoice_id'], source['packing_list_id']))
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


RESTORED_NOTICE_UNKNOWN = 'Odtworzono finalną przesyłkę bez trwałego wyniku e-maila. Sprawdź wcześniejszą wysyłkę wiadomości.'


def restore_inpost_notifications(db, payload):
    """Only proven final shipments can restore a receipt; never authorize resend.

    Older payloads have no mail receipt. The final proof is persisted BEFORE any
    send, so absence of a receipt after cache loss means unknown, not unsent.
    """
    saved = {r['shipment_id']: r for r in payload.get('inpost_notifications', [])}
    fields = ('shipment_id','state','recipient','result_text','updated_at','claim_token','lease_until')
    terminal = {'accepted','unknown','failed','skipped'}
    for final in payload.get('packing_shipments', []):
        if final['carrier'] != 'inpost' or not final['shipment_key'].startswith('inpost:'):
            continue
        sid = final['shipment_key'].split(':', 1)[1]
        local = db.execute('SELECT * FROM inpost_tracking_notifications WHERE shipment_id=?', (sid,)).fetchone()
        source = saved.get(sid)
        if source and source['state'] not in terminal | {'sending','pending'}:
            raise ValueError('Nieobsługiwany stan trwałego powiadomienia InPost.')
        if local:
            # Preserve a live local claim or a known outcome. A terminal receipt
            # from another member takes precedence over pending/legacy unknown.
            if not (source and source['state'] in terminal | {'sending'} and
                    (local['state']=='pending' or
                     (local['state']=='unknown' and local['result_text']==RESTORED_NOTICE_UNKNOWN))):
                continue
        if not source:
            source = dict(shipment_id=sid,state='unknown',recipient='',result_text=RESTORED_NOTICE_UNKNOWN,
                          updated_at=final['confirmed_at'],claim_token=None,lease_until=0)
        db.execute('INSERT OR REPLACE INTO inpost_tracking_notifications VALUES(?,?,?,?,?,?,?)',
                   tuple(source[k] for k in fields))
