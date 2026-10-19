"""Append-only corrections to the documented contents of one physical parcel.

Call ``amend`` inside the caller's transaction after explicit operator approval.
This module never books, notifies, invoices, changes stock or updates orders.
"""
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from packing_versions import PackingConflict

PREFIX = 'shipment_amendment:'
FINAL_FIELDS = ('shipment_key', 'packing_list_id', 'final_batch_id', 'confirmed_at', 'carrier', 'tracking')
IDENTITY_FIELDS = ('version', 'original_shipment_key', 'predecessor_key', 'carrier', 'tracking', 'before', 'after', 'order_ids')


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def _digest(value):
    return hashlib.sha256(_encoded(value).encode()).hexdigest()


def _conflict():
    raise PackingConflict('Nie można jednoznacznie potwierdzić korekty powiązania przesyłki. Sprawdź jej historię.')


def _customer(items):
    identities = set()
    for row in items:
        cid = row.get('customer_id_snapshot')
        email = str(row.get('customer_email_snapshot') or '').strip().casefold()
        name = str(row.get('customer_name_snapshot') or '').strip().casefold()
        identity = ('id', str(cid)) if cid else (('email', email) if email else ('name', name))
        if not identity[1]:
            _conflict()
        identities.add(identity)
    if len(identities) != 1:
        _conflict()
    return next(iter(identities))


def _scope(batch, items, documents=None, *, check_files=False):
    if not batch or not batch.get('packing_list_id') or not batch.get('selection_hash') or not items:
        _conflict()
    if any(int(r.get('qty') or 0) <= 0 for r in items):
        _conflict()
    ids = sorted({int(r['order_id']) for r in items})
    if len({(r['order_id'], r['order_item_id']) for r in items}) != len(items):
        _conflict()
    stable_items = sorted((int(r['order_id']), int(r['order_item_id']), int(r['qty']),
                           str(r.get('order_number_snapshot') or ''), str(r.get('sku_snapshot') or ''),
                           str(r.get('model_name_snapshot') or ''), str(r.get('note_snapshot') or ''),
                           str(r.get('customer_id_snapshot') or ''), str(r.get('customer_name_snapshot') or ''),
                           str(r.get('customer_email_snapshot') or '')) for r in items)
    result = {'packing_list_id': batch['packing_list_id'], 'selection_hash': batch['selection_hash'],
              'allocation_hash': _digest(stable_items)}
    if documents is not None:
        if sorted({int(d['order_id']) for d in documents}) != ids:
            _conflict()
        hashes = {str(d.get('file_hash') or '') for d in documents}
        if len(hashes) != 1 or not re.fullmatch(r'[0-9a-f]{64}', next(iter(hashes))):
            _conflict()
        result['file_hash'] = next(iter(hashes))
        if check_files:
            for doc in documents:
                path = Path(doc.get('path') or '')
                if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != result['file_hash']:
                    _conflict()
    return result, ids, _customer(items)


def _graph(records, *, documents=False):
    """Validate only explicit correction edges; equal tracking alone is no edge."""
    finals, scopes, receipts = {}, {}, {}
    for record in records:
        batches = {int(r['id']): dict(r) for r in record.get('packing_batches') or []}
        lists = {r['packing_list_id']: r for r in record.get('packing_lists') or []}
        for source in record.get('packing_shipments') or []:
            row = dict(source)
            key = row['shipment_key']
            batch = batches.get(int(row['final_batch_id']))
            if batch:
                items = [dict(r) for r in record.get('packing_allocations') or [] if int(r['batch_id']) == int(batch['id'])]
                docs = ([dict(r) for r in record.get('fulfillment_document_history') or []
                         if r.get('kind') == 'packing_list' and int(r['document_id']) == int(batch['id'])]
                        if documents else None)
                signature = (row['packing_list_id'], batch.get('selection_hash'), row['carrier'], row['tracking'], row['confirmed_at'])
                if key in scopes and scopes[key][4] != signature:
                    _conflict()
                scopes[key] = (batch, items, docs, lists.get(batch.get('packing_list_id'), {}), signature)
            if key in finals and any(finals[key][field] != row[field] for field in FINAL_FIELDS if field != 'final_batch_id'):
                _conflict()
            finals[key] = row
        for proof in record.get('fulfillment_verifications') or []:
            if not str(proof.get('kind') or '').startswith(PREFIX):
                continue
            try:
                payload = json.loads(proof['payload'])
                digest = proof['kind'][len(PREFIX):]
                identity = {key: payload[key] for key in IDENTITY_FIELDS}
                if (payload.get('version') != 1 or _digest(identity) != digest
                        or payload.get('successor_key') != 'amend:' + digest
                        or not str(payload.get('actor') or '').strip() or not str(payload.get('reason') or '').strip()
                        or int(proof['order_id']) not in payload['order_ids']):
                    _conflict()
                datetime.fromisoformat(payload['recorded_at'])
                if digest in receipts and receipts[digest] != payload:
                    _conflict()
                receipts[digest] = payload
            except (KeyError, TypeError, ValueError):
                _conflict()
    edges, incoming = {}, {}
    for proof in receipts.values():
        before, after = proof['predecessor_key'], proof['successor_key']
        if before == after or before not in finals or after not in finals or before not in scopes or after not in scopes:
            _conflict()
        if before in edges and edges[before] != after or after in incoming and incoming[after] != before:
            _conflict()
        a, z = finals[before], finals[after]
        if any(a[k] != z[k] or a[k] != proof[k] for k in ('carrier', 'tracking')) or a['confirmed_at'] != z['confirmed_at']:
            _conflict()
        descriptions = []
        for key, field in ((before, 'before'), (after, 'after')):
            batch, items, docs, _, _ = scopes[key]
            scope, ids, customer = _scope(batch, items, docs)
            expected = proof[field]
            if (set(expected) != {'packing_list_id', 'selection_hash', 'allocation_hash', 'file_hash'}
                    or not re.fullmatch(r'[0-9a-f]{64}', str(expected['file_hash']))
                    or any(expected[k] != v for k, v in scope.items())
                    or finals[key]['packing_list_id'] != scope['packing_list_id']):
                _conflict()
            descriptions.append((ids, customer))
        if (not set(descriptions[0][0]) <= set(descriptions[1][0])
                or descriptions[0][1] != descriptions[1][1]
                or proof['order_ids'] != sorted(set(descriptions[0][0]) | set(descriptions[1][0]))):
            _conflict()
        edges[before], incoming[after] = after, before
    for key in finals:
        seen, node = set(), key
        while node in incoming:
            if node in seen:
                _conflict()
            seen.add(node)
            node = incoming[node]
        if key.startswith('amend:') and key not in incoming:
            _conflict()
        for proof in receipts.values():
            if proof['successor_key'] in seen | {key} and proof['original_shipment_key'] != node:
                _conflict()
    return finals, scopes, edges, incoming


def _resolved(key, graph):
    finals, scopes, edges, incoming = graph
    if key not in finals:
        _conflict()
    original = key
    while original in incoming:
        original = incoming[original]
    current, previous = original, []
    while current in edges:
        previous.append(current)
        current = edges[current]
    result = dict(finals[current], original_shipment_key=original, superseded_shipment_keys=previous)
    if current in scopes:
        batch, _, _, logical, _ = scopes[current]
        result['invoice_id'] = batch.get('invoice_id') or logical.get('invoice_id')
    return result


def effective_shipments_from_records(records):
    graph = _graph(records)
    return list({r['shipment_key']: r for key in graph[0] for r in [_resolved(key, graph)]}.values())


def _record(db):
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return {name: [dict(r) for r in db.execute('SELECT * FROM ' + name)] if name in tables else []
            for name in ('packing_shipments', 'packing_batches', 'packing_lists', 'packing_allocations',
                         'fulfillment_document_history', 'fulfillment_verifications')}


def effective_shipments(db, rows):
    if not rows:
        return []
    graph = _graph([_record(db)], documents=True)
    return list({r['shipment_key']: r for source in rows
                 for r in [_resolved(dict(source)['shipment_key'], graph)]}.values())


def effective_shipment(db, shipment):
    return effective_shipments(db, [shipment])[0]


def amendment_list_keys(db, order_id):
    """Include predecessor evidence when a newly added member is restored alone."""
    return amendment_evidence(db, order_id)[0]


def amendment_evidence(db, order_id):
    """Return the complete proof chain, including dependencies of later additions."""
    keys, parsed = set(), []
    for row in db.execute('SELECT * FROM fulfillment_verifications WHERE kind LIKE ?', (PREFIX + '%',)):
        try:
            proof = json.loads(row['payload'])
            parsed.append((dict(row), proof))
        except (ValueError, KeyError, TypeError):
            _conflict()
    originals = {proof['original_shipment_key'] for row, proof in parsed if int(row['order_id']) == int(order_id)}
    rows = []
    for row, proof in parsed:
        if proof['original_shipment_key'] in originals:
            keys.update((proof['before']['packing_list_id'], proof['after']['packing_list_id']))
            rows.append(row)
    return keys, rows


def amend(db, predecessor, current_batch, actor, reason, request_version=''):
    if not db.in_transaction:
        raise PackingConflict('Korekta przesyłki wymaga transakcji zapisu.')
    actor, reason = str(actor or '').strip(), str(reason or '').strip()
    if not actor or not reason or len(actor) > 240 or len(reason) > 2000:
        raise PackingConflict('Potwierdź kto i dlaczego poprawia zawartość tej paczki.')
    request_version = str(request_version or '').strip()
    if len(request_version) > 256:
        raise PackingConflict('Nieprawidłowa wersja formularza korekty przesyłki.')
    record = _record(db)
    graph = _graph([record], documents=True)
    source_key = dict(predecessor)['shipment_key']
    if source_key not in graph[0]:
        _conflict()
    effective = _resolved(source_key, graph)
    bid = int(current_batch['batch_id'])
    batch = next((r for r in record['packing_batches'] if int(r['id']) == bid), None)
    logical = next((r for r in record['packing_lists'] if batch and r['packing_list_id'] == batch['packing_list_id']), None)
    if not logical or int(logical['current_batch_id']) != bid or current_batch.get('packing_list_key') != batch['packing_list_id']:
        _conflict()
    items = [r for r in record['packing_allocations'] if int(r['batch_id']) == bid]
    docs = [r for r in record['fulfillment_document_history'] if r['kind'] == 'packing_list' and int(r['document_id']) == bid]
    new_scope, new_ids, new_customer = _scope(batch, items, docs, check_files=True)
    if effective['final_batch_id'] == bid:
        if effective['shipment_key'] == source_key and not source_key.startswith('amend:'):
            raise PackingConflict('Ta lista jest już przypisana do wskazanej przesyłki.')
        return dict(final_key=effective['shipment_key'], union_order_ids=new_ids, idempotent=True,
                    original_shipment_key=effective['original_shipment_key'])
    if effective['shipment_key'] != source_key:
        raise PackingConflict('Powiązanie przesyłki zostało już zmienione. Odśwież widok przed kolejną korektą.')
    old_batch, old_items, old_docs, _, _ = graph[1][source_key]
    old_scope, old_ids, old_customer = _scope(old_batch, old_items, old_docs, check_files=True)
    if not set(old_ids) <= set(new_ids) or old_customer != new_customer:
        raise PackingConflict('Nowa lista musi obejmować wcześniejsze zamówienia tej samej firmy.')
    if any(int(r['final_batch_id']) == bid for r in record['packing_shipments']):
        raise PackingConflict('Nowa lista ma już przypisaną przesyłkę.')
    identity = dict(version=1, original_shipment_key=effective['original_shipment_key'], predecessor_key=source_key,
                    carrier=effective['carrier'], tracking=effective['tracking'], before=old_scope, after=new_scope,
                    order_ids=sorted(set(old_ids) | set(new_ids)))
    digest = _digest(identity)
    proof = dict(identity, successor_key='amend:' + digest, actor=actor, reason=reason, request_version=request_version,
                 recorded_at=datetime.now(timezone.utc).isoformat(timespec='seconds'))
    db.execute('INSERT INTO packing_shipments VALUES(?,?,?,?,?,?)',
               (proof['successor_key'], batch['packing_list_id'], bid, effective['confirmed_at'],
                effective['carrier'], effective['tracking']))
    for oid in proof['order_ids']:
        db.execute('INSERT INTO fulfillment_verifications VALUES(?,?,?)', (oid, PREFIX + digest, _encoded(proof)))
    return dict(final_key=proof['successor_key'], union_order_ids=proof['order_ids'], idempotent=False,
                original_shipment_key=proof['original_shipment_key'])
