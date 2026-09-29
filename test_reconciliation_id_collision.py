"""Two durable snapshots created on separate ephemeral SQLite disks reuse IDs."""
import sqlite3

import reconciliation_store
import packing_versions


def _database():
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.execute('''CREATE TABLE packing_batches(id INTEGER PRIMARY KEY AUTOINCREMENT,
        root_order_id INTEGER NOT NULL, invoice_id INTEGER, created_at TEXT NOT NULL)''')
    db.execute('''CREATE TABLE packing_allocations(id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id INTEGER NOT NULL, order_id INTEGER NOT NULL, order_item_id INTEGER NOT NULL,
        qty INTEGER NOT NULL, created_at TEXT NOT NULL)''')
    for column in ('order_number_snapshot', 'sku_snapshot', 'model_name_snapshot',
                   'note_snapshot', 'customer_name_snapshot', 'customer_email_snapshot'):
        db.execute(f'ALTER TABLE packing_allocations ADD COLUMN {column} TEXT')
    db.execute('ALTER TABLE packing_allocations ADD COLUMN customer_id_snapshot INTEGER')
    for column, kind in (('packing_list_id', 'TEXT'), ('previous_batch_id', 'INTEGER'),
                         ('selection_hash', 'TEXT')):
        db.execute(f'ALTER TABLE packing_batches ADD COLUMN {column} {kind}')
    packing_versions.initialize(db)
    reconciliation_store.initialize(db)
    return db


def _snapshot(root, stamp, *, final=False):
    key = f'list-{root}'
    payload = {
        'packing_batches': [{'id': 1, 'root_order_id': root, 'invoice_id': None,
                             'created_at': stamp, 'packing_list_id': key,
                             'previous_batch_id': None, 'selection_hash': f'hash-{root}'}],
        'packing_allocations': [{'id': 1, 'batch_id': 1, 'order_id': root,
                                 'order_item_id': root * 10, 'qty': 4,
                                 'created_at': stamp}],
        'packing_lists': [{'packing_list_id': key, 'root_order_id': root,
                           'invoice_id': None, 'current_batch_id': 1,
                           'revision': 1, 'created_at': stamp}],
        'packing_shipments': [],
        'fulfillment_document_history': [{'order_id': root, 'kind': 'packing_list',
                                           'document_id': 1, 'file_hash': 'hash'}],
    }
    if final:
        payload['packing_shipments'] = [{'shipment_key': f'inpost:{root}',
            'packing_list_id': key, 'final_batch_id': 1,
            'confirmed_at': stamp, 'carrier': 'inpost', 'tracking': str(root)}]
    return payload


def test_colliding_batch_and_allocation_ids_remain_distinct_and_repeatable():
    db = _database()
    older = _snapshot(100, '2026-09-16T10:11:23')
    newer = _snapshot(113, '2026-09-28T08:00:00', final=True)
    reconciliation_store.restore_packing_evidence(db, older)
    translated = reconciliation_store._remap_packing_ids(db, newer)
    assert translated['packing_batches'][0]['id'] != 1
    assert translated['packing_allocations'][0]['id'] != 1
    assert translated['packing_allocations'][0]['batch_id'] == translated['packing_batches'][0]['id']
    assert translated['packing_shipments'][0]['final_batch_id'] == translated['packing_batches'][0]['id']
    assert translated['fulfillment_document_history'][0]['document_id'] == translated['packing_batches'][0]['id']
    reconciliation_store.restore_packing_evidence(db, translated, already_translated=True)
    reconciliation_store.restore_packing_evidence(db, newer)
    assert db.execute('SELECT COUNT(*) FROM packing_batches').fetchone()[0] == 2
    assert db.execute('SELECT COUNT(*) FROM packing_allocations').fetchone()[0] == 2
    assert db.execute('SELECT COUNT(*) FROM packing_shipments').fetchone()[0] == 1
    assert db.execute('SELECT COUNT(DISTINCT root_order_id) FROM packing_batches').fetchone()[0] == 2
    db.close()


def test_previous_batch_and_current_document_survive_nonmonotonic_local_ids():
    db = _database()
    reconciliation_store.restore_packing_evidence(db, _snapshot(100, '2026-09-16T10:11:23'))
    payload = _snapshot(113, '2026-09-28T08:00:00', final=True)
    second = dict(payload['packing_batches'][0], id=2,
                  created_at='2026-09-28T08:30:00', previous_batch_id=1)
    payload['packing_batches'].append(second)
    payload['packing_allocations'].append(dict(payload['packing_allocations'][0],
                                               id=2, batch_id=2))
    payload['packing_lists'][0]['current_batch_id'] = 2
    payload['packing_lists'][0]['revision'] = 2
    payload['packing_shipments'][0]['final_batch_id'] = 2
    payload['fulfillment_document_history'][0]['document_id'] = 2
    translated = reconciliation_store._remap_packing_ids(db, payload)
    older, current = translated['packing_batches']
    assert older['id'] != 1
    assert older['id'] < current['id']
    assert current['previous_batch_id'] == older['id']
    assert translated['packing_lists'][0]['current_batch_id'] == current['id']
    assert translated['packing_shipments'][0]['final_batch_id'] == current['id']
    assert translated['fulfillment_document_history'][0]['document_id'] == current['id']
    reconciliation_store.restore_packing_evidence(db, translated, already_translated=True)
    assert db.execute('SELECT current_batch_id FROM packing_lists WHERE root_order_id=113').fetchone()[0] == current['id']
    db.close()


def test_allocation_order_stays_stable_when_only_some_remote_ids_collide():
    db = _database()
    older = _snapshot(100, '2026-09-16T10:11:23')
    older['packing_allocations'] = [dict(older['packing_allocations'][0], id=i,
                                         order_item_id=1000 + i) for i in range(1, 6)]
    reconciliation_store.restore_packing_evidence(db, older)
    newer = _snapshot(113, '2026-09-28T08:00:00')
    newer['packing_allocations'] = [dict(newer['packing_allocations'][0], id=i,
                                         order_item_id=1130 + i) for i in range(1, 9)]
    mapped = reconciliation_store._remap_packing_ids(db, newer)
    assert [r['order_item_id'] for r in sorted(mapped['packing_allocations'],
                                               key=lambda r: r['id'])] == list(range(1131, 1139))
    reconciliation_store.restore_packing_evidence(db, mapped, already_translated=True)
    reconciliation_store.restore_packing_evidence(db, newer)
    assert db.execute('SELECT COUNT(*) FROM packing_allocations').fetchone()[0] == 13
    db.close()
