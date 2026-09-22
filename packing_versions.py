"""Versioned packing scope. Call writers inside the existing business transaction.

A batch is a version, never a shipment. Only confirm_shipment records the latter.
Reads neither publish versions nor repair documents or change order statuses.
"""
import hashlib
import json
from pathlib import Path
import uuid


class PackingConflict(ValueError):
    pass


def initialize(db):
    columns = {r[1] for r in db.execute('PRAGMA table_info(packing_batches)')}
    for name, kind in [('packing_list_id', 'TEXT'), ('previous_batch_id', 'INTEGER'),
                       ('selection_hash', 'TEXT')]:
        if name not in columns:
            db.execute(f'ALTER TABLE packing_batches ADD COLUMN {name} {kind}')
    db.executescript('''
        CREATE TABLE IF NOT EXISTS packing_lists(
            packing_list_id TEXT PRIMARY KEY, root_order_id INTEGER NOT NULL,
            invoice_id INTEGER, current_batch_id INTEGER NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_packing_lists_invoice ON packing_lists(invoice_id);
        CREATE INDEX IF NOT EXISTS idx_packing_versions_list ON packing_batches(packing_list_id,id);
        CREATE INDEX IF NOT EXISTS idx_packing_snapshot_order ON packing_allocations(order_number_snapshot,batch_id);
        CREATE TABLE IF NOT EXISTS packing_shipments(
            shipment_key TEXT PRIMARY KEY, packing_list_id TEXT NOT NULL,
            final_batch_id INTEGER NOT NULL, confirmed_at TEXT NOT NULL,
            carrier TEXT NOT NULL, tracking TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_packing_shipments_list ON packing_shipments(packing_list_id,confirmed_at);
        CREATE TRIGGER IF NOT EXISTS packing_shipments_no_update BEFORE UPDATE ON packing_shipments
            BEGIN SELECT RAISE(ABORT,'final shipment is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS packing_shipments_no_delete BEFORE DELETE ON packing_shipments
            BEGIN SELECT RAISE(ABORT,'final shipment is immutable'); END;
    ''')


def normalize_items(db, items):
    """Resolve row labels at WRITE time; preserve selected quantities and source rows."""
    result = []
    seen = set()
    customers = set()
    for source in items:
        item = dict(source)
        oid = int(item.get('source_order_id') or item.get('order_id') or 0)
        iid = int(item.get('order_item_id') or item.get('id') or 0)
        qty = int(item.get('qty') or 0)
        if qty <= 0:
            continue
        row = db.execute('''SELECT oi.order_id,oi.sku,p.model,p.name,o.order_no,o.note,
                               o.customer_id,o.customer_name,o.customer_email
                            FROM order_items oi JOIN orders o ON o.id=oi.order_id
                            LEFT JOIN products p ON p.id=oi.product_id
                            WHERE oi.id=? AND oi.order_id=?''', (iid, oid)).fetchone()
        if row is None or iid in seen:
            raise PackingConflict('Nieprawidłowa lub powtórzona pozycja listy pakowej.')
        seen.add(iid)
        customers.add(str(row['customer_email'] or row['customer_id'] or row['customer_name']).strip().casefold())
        item.update(source_order_id=oid, order_id=oid, order_item_id=iid, id=iid, qty=qty,
                    source_order_no=str(item.get('source_order_no') or row['order_no'] or ''),
                    source_order_note=str(item.get('source_order_note') or row['note'] or ''),
                    sku=str(item.get('sku') or row['sku'] or ''),
                    model=str(item.get('model') or row['model'] or row['name'] or ''))
        result.append(item)
    if not result or len(customers) != 1:
        raise PackingConflict('Lista pakowa wymaga pozycji jednego odbiorcy.')
    return result


def _scope_hash(items):
    scope = sorted((i['source_order_id'], i['order_item_id'], i['qty'], i['source_order_no'],
                    i['sku'], i['model'], i['source_order_note']) for i in items)
    return hashlib.sha256(json.dumps(scope, ensure_ascii=False).encode()).hexdigest()


def resolve_list(db, root_order_id, invoice_id=None, packing_list_id=None):
    if packing_list_id:
        row = db.execute('SELECT * FROM packing_lists WHERE packing_list_id=?', (packing_list_id,)).fetchone()
        if not row or int(row['root_order_id']) != int(root_order_id):
            raise PackingConflict('Lista pakowa nie należy do tego zakresu.')
        return row
    if invoice_id:
        rows = db.execute('SELECT * FROM packing_lists WHERE invoice_id=?', (invoice_id,)).fetchall()
        if len(rows) > 1:
            raise PackingConflict('Faktura ma kilka list pakowych. Wskaż konkretną listę.')
        if rows:
            return rows[0]
    # Reuse only the explicitly current, open packing list. A completed shipment
    # starts a new logical list on the next packing action, including partial orders.
    return db.execute('''SELECT pl.* FROM packing_lists pl
                         JOIN fulfillment_documents fd ON fd.document_id=pl.current_batch_id AND fd.kind='packing_list'
                         WHERE fd.order_id=? AND pl.invoice_id IS NULL
                         AND NOT EXISTS(SELECT 1 FROM packing_shipments ps WHERE ps.packing_list_id=pl.packing_list_id)
                         ORDER BY pl.current_batch_id DESC LIMIT 1''', (root_order_id,)).fetchone()


def publish(b, db, root_order_id, items, path, *, invoice_id=None,
            packing_list_id=None, expected_current=None):
    """Publish a ready file and selection atomically with the caller's transaction."""
    import fulfillment_operations as fulfillment
    items = normalize_items(db, items)
    logical = resolve_list(db, root_order_id, invoice_id, packing_list_id)
    if not logical and invoice_id:
        # Existing invoice-linked batches are retained as earlier versions. This
        # happens only during an explicit WRITE; a READ never migrates history.
        invoice_history = db.execute('''SELECT * FROM packing_batches WHERE invoice_id=?
            AND packing_list_id IS NULL ORDER BY id''', (invoice_id,)).fetchall()
        if invoice_history:
            key, previous = str(uuid.uuid4()), None
            for old in invoice_history:
                db.execute('UPDATE packing_batches SET packing_list_id=?,previous_batch_id=?,selection_hash=? WHERE id=?',
                           (key, previous, '', old['id']))
                previous = old['id']
            db.execute('INSERT INTO packing_lists VALUES(?,?,?,?,?,?)',
                       (key, root_order_id, invoice_id, previous, len(invoice_history), invoice_history[0]['created_at']))
            logical = db.execute('SELECT * FROM packing_lists WHERE packing_list_id=?', (key,)).fetchone()
            if expected_current == 0:
                expected_current = previous
    if not logical:
        legacy = db.execute('''SELECT * FROM packing_batches WHERE root_order_id=? AND invoice_id IS NULL
                               AND packing_list_id IS NULL ORDER BY id DESC''', (root_order_id,)).fetchall()
        if legacy:
            old = legacy[0]
            old_scope = sorted(tuple(r) for r in db.execute('SELECT order_id,order_item_id,qty FROM packing_allocations WHERE batch_id=?', (old['id'],)))
            new_scope = sorted((i['source_order_id'], i['order_item_id'], i['qty']) for i in items)
            documented = db.execute("SELECT 1 FROM fulfillment_documents WHERE kind='packing_list' AND document_id=?", (old['id'],)).fetchone()
            if len(legacy) > 1 or (old_scope != new_scope and not documented):
                raise PackingConflict('Istnieje niejednoznaczny lub niezgodny otwarty batch pakowania. Wymagana kontrola wcześniejszego zapisu.')
            key = str(uuid.uuid4())
            db.execute('UPDATE packing_batches SET packing_list_id=?,selection_hash=? WHERE id=?',
                       (key, _scope_hash(items) if old_scope == new_scope else '', old['id']))
            db.execute('INSERT INTO packing_lists VALUES(?,?,?,?,1,?)',
                       (key, root_order_id, invoice_id, old['id'], old['created_at']))
            logical = db.execute('SELECT * FROM packing_lists WHERE packing_list_id=?', (key,)).fetchone()
            if expected_current == 0:
                expected_current = old['id']
    actual = int(logical['current_batch_id']) if logical else 0
    if expected_current is not None and actual != int(expected_current):
        raise PackingConflict('Lista pakowa zmieniła się podczas przygotowania. Odśwież i ponów.')
    digest = _scope_hash(items)
    batch = db.execute('SELECT * FROM packing_batches WHERE id=?', (actual,)).fetchone() if actual else None
    source_current = True
    for oid in {i['source_order_id'] for i in items}:
        old = db.execute("SELECT content_hash FROM fulfillment_document_history WHERE order_id=? AND kind='packing_list' AND document_id=?",
                         (oid, actual)).fetchone()
        if old and old['content_hash'] != fulfillment.snapshot(oid, db, include_package=False)['content_hash']:
            source_current = False
            break
    if batch and batch['selection_hash'] == digest and source_current:
        batch_id = actual
    else:
        batch_id = b.save_packing_selection(root_order_id, items, connection=db)
        key = logical['packing_list_id'] if logical else str(uuid.uuid4())
        db.execute('''UPDATE packing_batches SET packing_list_id=?,previous_batch_id=?,selection_hash=?,invoice_id=?
                      WHERE id=?''', (key, actual or None, digest, invoice_id or (logical['invoice_id'] if logical else None), batch_id))
        if logical:
            db.execute('''UPDATE packing_lists SET current_batch_id=?,revision=revision+1,
                          invoice_id=COALESCE(?,invoice_id) WHERE packing_list_id=? AND current_batch_id=?''',
                       (batch_id, invoice_id, key, actual))
        else:
            db.execute('INSERT INTO packing_lists VALUES(?,?,?,?,1,?)',
                       (key, int(root_order_id), invoice_id, batch_id, b.now_iso()))
    if invoice_id:
        db.execute('UPDATE packing_batches SET invoice_id=? WHERE id=?', (invoice_id, batch_id))
        db.execute('UPDATE packing_lists SET invoice_id=? WHERE current_batch_id=?', (invoice_id, batch_id))
    # Content-addressed archives cannot be overwritten by a later regeneration or
    # by reusing a rolled-back SQLite id. Mutable invoice PDF names are not history.
    # The final PDF consumes exactly the same uncommitted snapshot that all
    # subsequent reads use. A prepared PDF is only a preflight, never authority.
    snapshot = batch_result(db, batch_id, mode='current')
    root = db.execute('SELECT * FROM orders WHERE id=?', (root_order_id,)).fetchone()
    invoice = db.execute('SELECT * FROM invoices WHERE id=?', (invoice_id,)).fetchone() if invoice_id else None
    meta = b.invoice_meta_payload(dict(invoice)) if invoice else dict(
        invoice_no=root['order_no'], document_label_key='order', buyer_name=snapshot['customer']['name'])
    meta['packing_document_token'] = uuid.uuid4().hex
    path = b.generate_invoice_packing_list_pdf(root, pdf_items(snapshot), meta)
    content = Path(path).read_bytes()
    if not content:
        raise PackingConflict('Wygenerowany dokument listy pakowej jest pusty.')
    file_hash = hashlib.sha256(content).hexdigest()
    directory = Path(b.DATA_DIR) / 'packing-history'
    directory.mkdir(parents=True, exist_ok=True)
    archived = directory / (file_hash + '.pdf')
    if not archived.exists():
        with archived.open('xb') as out:
            out.write(content)
    elif hashlib.sha256(archived.read_bytes()).hexdigest() != file_hash:
        raise PackingConflict('Historyczny plik ma niezgodną sumę kontrolną.')
    order_ids = sorted({i['source_order_id'] for i in items})
    # Remove stale current pointers for members removed from this logical list;
    # their immutable history is deliberately retained.
    if actual and actual != batch_id:
        db.execute("DELETE FROM fulfillment_documents WHERE kind='packing_list' AND document_id=?", (actual,))
    for oid in order_ids:
        saved = db.execute("SELECT content_hash FROM fulfillment_document_history WHERE order_id=? AND kind='packing_list' AND document_id=?",
                           (oid, batch_id)).fetchone()
        fulfillment.save_document(oid, 'packing_list', batch_id, str(archived), connection=db,
                                 content_hash=saved['content_hash'] if saved else fulfillment.snapshot(oid, db, include_package=False)['content_hash'],
                                 file_hash=file_hash)
    stage_evidence(b, db, order_ids)
    return batch_id


def stage_evidence(b, db, order_ids, *, allow_replace=False):
    if not b.supabase_enabled():
        return
    import reconciliation_store
    # Root is the durable aggregate owner. Secondary members carry the same
    # immutable versions, but never overwrite a newer logical-list revision.
    for oid in evidence_members(db, order_ids):
        reconciliation_store.stage(b, oid, connection=db, allow_replace=allow_replace)


def evidence_members(db, order_ids):
    """Include removed members so their durable current pointers are cleared too."""
    members = set(int(i) for i in order_ids)
    marks = ','.join('?' for _ in members)
    if members:
        members.update(r[0] for r in db.execute(f'''SELECT DISTINCT pa.order_id FROM packing_allocations pa
            JOIN packing_batches pb ON pb.id=pa.batch_id WHERE pb.packing_list_id IN (
                SELECT b.packing_list_id FROM packing_batches b JOIN packing_allocations a ON a.batch_id=b.id
                WHERE a.order_id IN ({marks}))''', tuple(sorted(members))))
    return sorted(members)


def sync_evidence(b, order_ids):
    if not b.supabase_enabled():
        return
    import reconciliation_store
    db = b.conn()
    try:
        order_ids = evidence_members(db, order_ids)
    finally:
        db.close()
    for oid in order_ids:
        reconciliation_store.publish(b, oid)


def batch_result(db, batch_id, *, mode='historical', shipment=None):
    """The shared structural READ used by the UI, the agent and PDF adapters."""
    batch = db.execute('SELECT * FROM packing_batches WHERE id=?', (batch_id,)).fetchone()
    rows = [dict(r) for r in db.execute('SELECT * FROM packing_allocations WHERE batch_id=? ORDER BY id', (batch_id,))]
    if not batch or not rows or any(not r.get('order_number_snapshot') or not r.get('sku_snapshot') for r in rows):
        raise PackingConflict('Brak kompletnej zapisanej zawartości listy pakowej.')
    allocations = [dict(order_id=r['order_id'], order_item_id=r['order_item_id'],
                        order_number=r['order_number_snapshot'], sku=r['sku_snapshot'],
                        model_name=r['model_name_snapshot'] or '', note=r['note_snapshot'] or '', packed_qty=r['qty']) for r in rows]
    docs = list(db.execute("SELECT * FROM fulfillment_document_history WHERE kind='packing_list' AND document_id=?", (batch_id,)))
    document = docs[0] if docs else None
    groups = {}
    for row in allocations:
        groups.setdefault(row['order_id'], dict(order_id=row['order_id'], order_number=row['order_number'], items=[]))['items'].append(row)
    total = sum(r['packed_qty'] for r in allocations)
    return dict(ok=True, batch_id=int(batch_id), packing_list_id=int(batch_id),
                packing_list_key=batch['packing_list_id'] or '', created_at=batch['created_at'],
                invoice_id=batch['invoice_id'], order_ids=sorted(groups), allocations=allocations,
                orders=list(groups.values()), all_items=allocations, total_lines=len(rows), total_qty=total, total_units=total,
                document_id=int(batch_id) if document else None,
                document_path=document['path'] if document else '',
                document_available=bool(document and Path(document['path']).is_file()), document_verified=False,
                complete=True, incomplete_fields=[], history_source='allocation_snapshot',
                document_type=mode, verified=True, source='verified_packing_list',
                shipment_confirmed=bool(shipment), shipment_key=shipment['shipment_key'] if shipment else '',
                confirmed_at=shipment['confirmed_at'] if shipment else '',
                customer=dict(id=rows[0]['customer_id_snapshot'], name=rows[0]['customer_name_snapshot'] or '',
                              email=rows[0]['customer_email_snapshot'] or ''))


def pdf_items(result):
    return [dict(id=r['order_item_id'], order_item_id=r['order_item_id'],
                 order_id=r['order_id'], source_order_id=r['order_id'], source_order_no=r['order_number'],
                 source_order_note=r['note'], sku=r['sku'], model=r['model_name'], qty=r['packed_qty'])
            for r in result['allocations']]


def legacy_invoice_result(db, invoice_id):
    """Read saved invoice allocations without inventing a historical/final LP."""
    inv = db.execute('SELECT * FROM invoices WHERE id=?', (invoice_id,)).fetchone()
    if not inv:
        return None
    if 'publication_state' in inv.keys() and inv['publication_state'] != 'complete':
        raise PackingConflict('Faktura jest w trakcie publikacji. Bieżąca lista nie jest jeszcze gotowa.')
    rows = list(db.execute('''SELECT ia.*,o.order_no,o.note,p.model,p.name
                             FROM invoice_allocations ia JOIN orders o ON o.id=ia.order_id
                             LEFT JOIN products p ON p.id=ia.product_id
                             WHERE ia.invoice_id=? ORDER BY ia.id''', (invoice_id,)))
    if not rows:
        return None
    allocations = [dict(order_id=r['order_id'], order_item_id=r['order_item_id'], order_number=r['order_no'],
                        sku=r['sku'] or '', model_name=r['model'] or r['name'] or '',
                        note=r['note'] or '', packed_qty=r['qty']) for r in rows]
    groups = {}
    for r in allocations:
        groups.setdefault(r['order_id'], dict(order_id=r['order_id'], order_number=r['order_number'], items=[]))['items'].append(r)
    total = sum(r['packed_qty'] for r in allocations)
    return dict(ok=True, batch_id=0, packing_list_id=None, packing_list_key='', invoice_id=int(invoice_id),
                created_at=inv['created_at'], order_ids=sorted(groups), allocations=allocations,
                orders=list(groups.values()), all_items=allocations, total_lines=len(rows), total_qty=total, total_units=total,
                document_id=None, document_path='', document_available=False, document_verified=False,
                complete=True, incomplete_fields=[], history_source='saved_invoice_allocations',
                document_type='current', verified=True, source='verified_packing_list', shipment_confirmed=False,
                shipment_key='', confirmed_at='', customer=dict(id=None, name=inv['buyer_name'] or '', email=inv['buyer_email'] or ''))


def document(b, selectors):
    """Render the shared READ, with no business writes, stock changes or e-mail."""
    import packing_history
    result = packing_history.shipment_read(selectors, connection_factory=b.conn)
    path = result.get('document_path')
    if path and Path(path).is_file():
        if result.get('document_verified'):
            return str(path), result
        db = b.conn()
        try:
            proof = db.execute("SELECT file_hash FROM fulfillment_document_history WHERE kind='packing_list' AND document_id=? LIMIT 1",
                               (result['batch_id'],)).fetchone()
        finally:
            db.close()
        if proof and hashlib.sha256(Path(path).read_bytes()).hexdigest() == proof['file_hash']:
            return str(path), result
    db = b.conn()
    try:
        root = db.execute('SELECT * FROM orders WHERE id=?', (result['order_ids'][0],)).fetchone()
        inv = db.execute('SELECT * FROM invoices WHERE id=?', (result.get('invoice_id'),)).fetchone()
    finally:
        db.close()
    meta = b.invoice_meta_payload(dict(inv)) if inv else dict(invoice_no='LP-'+str(result['batch_id']),
                                                            buyer_name=result['customer']['name'], document_label_key='order')
    meta['packing_document_token'] = uuid.uuid4().hex
    return b.generate_invoice_packing_list_pdf(root, pdf_items(result), meta), result


def select(db, data, *, mode='current'):
    """Return one explicit version; never substitute order contents for a shipment."""
    if not any(data.get(k) for k in ('batch_id','packing_list_key','invoice_id','order_id',
                                    'order_number','customer_id','customer','latest','today','date')):
        raise PackingConflict('Wskaż listę pakową, zamówienie albo klienta.')
    if data.get('batch_id'):
        bid = int(data['batch_id'])
        shipment = db.execute('SELECT * FROM packing_shipments WHERE final_batch_id=? ORDER BY confirmed_at DESC LIMIT 1', (bid,)).fetchone()
        if mode == 'shipment' and not shipment:
            return None
        return batch_result(db, bid, mode=mode, shipment=shipment if mode == 'shipment' else None)
    clauses, args = [], []
    if data.get('packing_list_key'):
        clauses.append('pl.packing_list_id=?'); args.append(data['packing_list_key'])
    if data.get('invoice_id'):
        clauses.append('pl.invoice_id=?'); args.append(int(data['invoice_id']))
    for key, column in [('order_id', 'pa.order_id'), ('order_number', 'pa.order_number_snapshot'),
                        ('customer_id', 'pa.customer_id_snapshot')]:
        if data.get(key):
            clauses.append(column+'=?'); args.append(data[key])
    if data.get('customer'):
        clauses.append('(LOWER(pa.customer_name_snapshot)=LOWER(?) OR LOWER(pa.customer_email_snapshot)=LOWER(?))')
        args.extend([data['customer'], data['customer']])
    join = ('JOIN packing_shipments ps ON ps.packing_list_id=pl.packing_list_id '
            'JOIN packing_batches pb ON pb.id=ps.final_batch_id') if mode == 'shipment' else (
            'JOIN packing_batches pb ON pb.id=pl.current_batch_id')
    fields = 'pb.id AS selected_batch_id,pl.*' + (',ps.*' if mode == 'shipment' else '')
    if data.get('date') or data.get('today'):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        day = data.get('date') or datetime.now(ZoneInfo('Europe/Warsaw')).date().isoformat()
        if mode != 'shipment':
            return None
        clauses.append('substr(ps.confirmed_at,1,10)=?'); args.append(day)
    where = ' AND '.join(clauses) or '1=1'
    ordering = 'ps.confirmed_at DESC,ps.shipment_key DESC' if mode == 'shipment' else 'pb.id DESC'
    rows = db.execute(f'''SELECT DISTINCT {fields} FROM packing_lists pl {join}
                          JOIN packing_allocations pa ON pa.batch_id=pb.id
                          WHERE {where} ORDER BY {ordering}''', args).fetchall()
    if not rows:
        return None
    if data.get('customer'):
        identities = db.execute(f'''SELECT DISTINCT COALESCE(NULLIF(LOWER(pa.customer_email_snapshot),''),
            CAST(pa.customer_id_snapshot AS TEXT),LOWER(pa.customer_name_snapshot))
            FROM packing_lists pl {join} JOIN packing_allocations pa ON pa.batch_id=pb.id
            WHERE {where}''', args).fetchall()
        if len(identities) > 1:
            raise PackingConflict('Nazwa pasuje do kilku klientów. Wskaż klienta jednoznacznie.')
    if mode == 'current':
        for row in rows:
            if row['invoice_id']:
                inv = db.execute('SELECT publication_state FROM invoices WHERE id=?', (row['invoice_id'],)).fetchone()
                if inv and inv[0] != 'complete':
                    raise PackingConflict('Faktura jest w trakcie publikacji. Bieżąca lista nie jest jeszcze gotowa.')
    # Multiple current lists of one order/invoice must not be merged or guessed.
    if mode == 'current' and len(rows) > 1 and not data.get('latest') and not data.get('customer') and not data.get('customer_id'):
        raise PackingConflict('Jest kilka bieżących list. Wskaż konkretną listę pakową.')
    if data.get('today') or data.get('date'):
        results = [batch_result(db, row['selected_batch_id'], mode='final', shipment=row) for row in rows]
        if len(results) == 1:
            return results[0]
        result = dict(results[0])
        result.update(batch_id=0, packing_list_id=None, packing_list_key='', invoice_id=None, document_id=None,
                      document_path='', document_available=False,
                      allocations=[i for r in results for i in r['allocations']],
                      all_items=[i for r in results for i in r['allocations']],
                      orders=[o for r in results for o in r['orders']],
                      order_ids=sorted({oid for r in results for oid in r['order_ids']}),
                      total_qty=sum(r['total_qty'] for r in results), total_units=sum(r['total_qty'] for r in results),
                      total_lines=sum(r['total_lines'] for r in results),
                      shipments=[dict(packing_list_id=r['batch_id'], order_ids=r['order_ids'], total_units=r['total_qty']) for r in results])
        return result
    row = rows[0]
    return batch_result(db, row['selected_batch_id'], mode='final' if mode == 'shipment' else mode,
                        shipment=row if mode == 'shipment' else None)


def confirm_shipment(db, *, batch_id, shipment_key, confirmed_at, carrier, tracking, order_ids):
    previous = db.execute('SELECT * FROM packing_shipments WHERE shipment_key=?', (shipment_key,)).fetchone()
    if previous:
        if int(previous['final_batch_id']) != int(batch_id):
            raise PackingConflict('Numer przesyłki ma już przypisaną inną finalną wersję listy.')
        return int(previous['final_batch_id'])
    row = db.execute('''SELECT pb.*,pl.current_batch_id FROM packing_batches pb
                        JOIN packing_lists pl ON pl.packing_list_id=pb.packing_list_id WHERE pb.id=?''', (batch_id,)).fetchone()
    if not row or int(row['current_batch_id']) != int(batch_id):
        raise PackingConflict('Przed potwierdzeniem wysyłki odśwież bieżącą listę pakową.')
    members = {int(r[0]) for r in db.execute('SELECT DISTINCT order_id FROM packing_allocations WHERE batch_id=?', (batch_id,))}
    if members != {int(i) for i in order_ids}:
        raise PackingConflict('Zakres przesyłki nie odpowiada bieżącej liście pakowej.')
    db.execute('INSERT INTO packing_shipments VALUES(?,?,?,?,?,?)',
               (shipment_key, row['packing_list_id'], batch_id, confirmed_at, carrier, tracking))
    return int(batch_id)


def current_for_order(db, order_id):
    """Resolve the existing UI current pointer, including secondary source orders."""
    row = db.execute("SELECT document_id FROM fulfillment_documents WHERE order_id=? AND kind='packing_list'", (order_id,)).fetchone()
    if not row:
        return None
    batch = db.execute('''SELECT pb.id FROM packing_batches pb JOIN packing_lists pl
                         ON pl.packing_list_id=pb.packing_list_id AND pl.current_batch_id=pb.id
                         WHERE pb.id=?''', (row[0],)).fetchone()
    return batch_result(db, batch[0], mode='current') if batch else None


def ensure_current_for_shipment(b, db, order_id):
    result = current_for_order(db, order_id)
    if result:
        if result.get('invoice_id'):
            inv = db.execute('SELECT publication_state FROM invoices WHERE id=?', (result['invoice_id'],)).fetchone()
            if inv and inv[0] != 'complete':
                raise PackingConflict('Dokończ publikację faktury przed potwierdzeniem wysyłki.')
        return result
    # This is an explicit WRITE (shipping confirmation), never an implicit READ
    # migration. Legacy invoice allocations can supply the full selected scope.
    invoice = db.execute('''SELECT DISTINCT i.id FROM invoices i LEFT JOIN invoice_allocations ia ON ia.invoice_id=i.id
                            WHERE i.order_id=? OR ia.order_id=? ORDER BY i.id DESC LIMIT 1''', (order_id, order_id)).fetchone()
    result = legacy_invoice_result(db, invoice[0]) if invoice else None
    if not result:
        raise PackingConflict('Przed potwierdzeniem wysyłki przygotuj listę pakową z pełnym zakresem pozycji.')
    bid = publish_invoice(b, invoice[0], pdf_items(result), None, connection=db)
    return batch_result(db, bid, mode='current')


def publish_invoice(b, invoice_id, items, path, *, connection=None, expected_current=None):
    owned = connection is None
    db = connection or b.conn()
    try:
        if owned:
            db.execute('BEGIN IMMEDIATE')
        inv = db.execute('SELECT order_id FROM invoices WHERE id=?', (invoice_id,)).fetchone()
        if not inv:
            raise PackingConflict('Nie znaleziono faktury listy pakowej.')
        bid = publish(b, db, inv['order_id'], items, path, invoice_id=invoice_id, expected_current=expected_current)
        if owned:
            db.commit()
            sync_evidence(b, sorted({int(i.get('source_order_id') or i.get('order_id')) for i in items}))
        return bid
    except Exception:
        if owned:
            db.rollback()
        raise
    finally:
        if owned:
            db.close()
