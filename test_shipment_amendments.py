import copy
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

import packing_versions
import shipment_amendments as amendments
import reconciliation_store


def database(path=':memory:'):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript('''
        CREATE TABLE packing_batches(id INTEGER PRIMARY KEY,root_order_id INTEGER,invoice_id INTEGER,created_at TEXT);
        CREATE TABLE packing_allocations(id INTEGER PRIMARY KEY,batch_id INTEGER,order_id INTEGER,order_item_id INTEGER,
            qty INTEGER,created_at TEXT,order_number_snapshot TEXT,sku_snapshot TEXT,model_name_snapshot TEXT,
            note_snapshot TEXT,customer_id_snapshot INTEGER,customer_name_snapshot TEXT,customer_email_snapshot TEXT);
        CREATE TABLE fulfillment_document_history(order_id INTEGER,kind TEXT,document_id INTEGER,content_hash TEXT,
            path TEXT,created_at TEXT,file_hash TEXT,PRIMARY KEY(order_id,kind,document_id));
        CREATE TABLE fulfillment_verifications(order_id INTEGER,kind TEXT,payload TEXT,PRIMARY KEY(order_id,kind));
        CREATE TABLE stock(product_id INTEGER PRIMARY KEY,qty INTEGER);
        INSERT INTO stock VALUES(1,17);
    ''')
    packing_versions.initialize(db)
    return db


def reconciliation_schema(db):
    db.executescript('''
        CREATE TABLE order_shipping_requirements(order_id INTEGER,payload TEXT,updated_at TEXT);
        CREATE TABLE fulfillment_documents(order_id INTEGER,kind TEXT,document_id INTEGER,content_hash TEXT,
            path TEXT,created_at TEXT,file_hash TEXT,PRIMARY KEY(order_id,kind));
        CREATE TABLE fulfillment_document_intents(order_id INTEGER,kind TEXT,content_hash TEXT);
        CREATE TABLE fulfillment_shipping_attempts(order_id INTEGER,reference TEXT,payload TEXT,content_hash TEXT,
            state TEXT,provider_json TEXT,created_at TEXT);
        CREATE TABLE fulfillment_shipping_members(order_id INTEGER,attempt_order_id INTEGER);
        CREATE TABLE inpost_tracking_notifications(shipment_id TEXT PRIMARY KEY,state TEXT,recipient TEXT,
            result_text TEXT,updated_at TEXT,claim_token TEXT,lease_until REAL);
    ''')
    reconciliation_store.initialize(db)


def add_batch(db, tmp_path, bid, quantities, *, customer=7):
    key = 'list-' + str(bid)
    stamp = f'2026-10-19T12:{bid % 60:02}:00'
    selection = hashlib.sha256(json.dumps(quantities).encode()).hexdigest()
    db.execute('INSERT INTO packing_batches VALUES(?,?,?,?,?,?,?)', (bid, max(quantities), 106, stamp, key, None, selection))
    db.execute('INSERT INTO packing_lists VALUES(?,?,?,?,?,?)', (key, max(quantities), 106, bid, 1, stamp))
    content = b'%PDF-1.4\n' + json.dumps(quantities).encode()
    path = tmp_path / (key + '.pdf')
    path.write_bytes(content)
    for oid, qty in quantities.items():
        db.execute('INSERT INTO packing_allocations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (bid * 1000 + oid, bid, oid, oid * 10, qty, stamp, 'ZAM-' + str(oid), 'SKU-' + str(oid),
                    'Model', '', customer, 'Klient', 'client@example.invalid'))
        db.execute('INSERT INTO fulfillment_document_history VALUES(?,?,?,?,?,?,?)',
                   (oid, 'packing_list', bid, 'content-' + str(oid), str(path), stamp, hashlib.sha256(content).hexdigest()))
    return packing_versions.batch_result(db, bid, mode='current')


@pytest.fixture
def parcel(tmp_path):
    db = database()
    add_batch(db, tmp_path, 1, {139: 4, 141: 32})
    current = add_batch(db, tmp_path, 2, {139: 4, 141: 28, 142: 18})
    db.execute("INSERT INTO packing_shipments VALUES('inpost:777','list-1',1,'2026-10-19T12:14:00','inpost','520000000000000000000001')")
    db.commit()
    original = dict(db.execute('SELECT * FROM packing_shipments').fetchone())
    yield db, original, current
    db.close()


def correct(parcel):
    db, original, current = parcel
    db.execute('BEGIN IMMEDIATE')
    return amendments.amend(db, original, current, 'operator', 'Faktycznie wysłano razem 50 sztuk')


def test_correction_preserves_original_and_adjusts_quantity_without_business_write(parcel):
    db, original, current = parcel
    result = correct(parcel)
    assert result['union_order_ids'] == [139, 141, 142]
    assert not result['idempotent']
    assert dict(db.execute("SELECT * FROM packing_shipments WHERE shipment_key='inpost:777'").fetchone()) == original
    assert db.execute('SELECT qty FROM stock').fetchone()[0] == 17
    effective = amendments.effective_shipment(db, original)
    assert effective['final_batch_id'] == 2
    assert effective['confirmed_at'] == original['confirmed_at']
    assert effective['original_shipment_key'] == 'inpost:777'
    assert effective['superseded_shipment_keys'] == ['inpost:777']
    assert packing_versions.batch_result(db, effective['final_batch_id'])['total_qty'] == 50
    assert len(amendments.effective_shipments(db, list(db.execute('SELECT * FROM packing_shipments')))) == 1
    assert amendments.amend(db, original, current, 'operator', 'retry')['idempotent']
    assert db.execute('SELECT COUNT(*) FROM fulfillment_verifications').fetchone()[0] == 3
    with pytest.raises(sqlite3.IntegrityError, match='immutable'):
        db.execute("UPDATE packing_shipments SET tracking='new'")


def test_receipts_work_in_compact_projection_without_pdf_data(parcel):
    db, original, _ = parcel
    correct(parcel)
    record = amendments._record(db)
    record.pop('fulfillment_document_history')
    effective = amendments.effective_shipments_from_records([record, copy.deepcopy(record)])
    assert len(effective) == 1
    assert effective[0]['final_batch_id'] == 2
    assert amendments.amendment_list_keys(db, 142) == {'list-1', 'list-2'}


def test_cold_cache_batch_id_remapping_does_not_change_receipt(parcel):
    db, original, _ = parcel
    correct(parcel)
    record = amendments._record(db)
    cold = database()
    for row in record['packing_batches']:
        row['id'] += 100
    for row in record['packing_allocations']:
        row['batch_id'] += 100
    for row in record['packing_lists']:
        row['current_batch_id'] += 100
    for row in record['packing_shipments']:
        row['final_batch_id'] += 100
    for row in record['fulfillment_document_history']:
        row['document_id'] += 100
    for table, rows in record.items():
        for row in rows:
            keys = list(row)
            cold.execute(f'INSERT INTO {table} ({",".join(keys)}) VALUES ({",".join("?" for _ in keys)})', tuple(row[k] for k in keys))
    effective = amendments.effective_shipment(cold, original)
    assert effective['final_batch_id'] == 102
    assert effective['original_shipment_key'] == original['shipment_key']
    cold.close()


@pytest.mark.parametrize('change', ['customer', 'missing_old_order', 'document', 'current_pointer', 'already_shipped'])
def test_invalid_new_scope_is_rejected_without_mutation(parcel, change):
    db, original, current = parcel
    if change == 'customer':
        db.execute('UPDATE packing_allocations SET customer_id_snapshot=8 WHERE batch_id=2')
    elif change == 'missing_old_order':
        db.execute('DELETE FROM packing_allocations WHERE batch_id=2 AND order_id=139')
        db.execute('DELETE FROM fulfillment_document_history WHERE document_id=2 AND order_id=139')
    elif change == 'document':
        db.execute("UPDATE fulfillment_document_history SET file_hash=? WHERE document_id=2", ('0' * 64,))
    elif change == 'current_pointer':
        db.execute("UPDATE packing_lists SET current_batch_id=1 WHERE packing_list_id='list-2'")
    else:
        db.execute("INSERT INTO packing_shipments VALUES('dpd:other','list-2',2,'2026-10-19T12:14:00','dpd','other')")
    db.commit()
    before = db.execute('SELECT COUNT(*) FROM packing_shipments').fetchone()[0]
    db.execute('BEGIN')
    with pytest.raises(packing_versions.PackingConflict):
        amendments.amend(db, original, current, 'operator', 'same parcel')
    assert db.execute('SELECT COUNT(*) FROM packing_shipments').fetchone()[0] == before
    assert db.execute('SELECT COUNT(*) FROM fulfillment_verifications').fetchone()[0] == 0


@pytest.mark.parametrize('change', ['tampered', 'missing_receipt', 'mismatched_file', 'wrong_owner'])
def test_corrupt_receipt_cannot_override_original(parcel, change):
    db, original, _ = parcel
    correct(parcel)
    if change == 'missing_receipt':
        db.execute('DELETE FROM fulfillment_verifications')
    elif change == 'mismatched_file':
        db.execute("UPDATE fulfillment_document_history SET file_hash=? WHERE document_id=2", ('0' * 64,))
    elif change == 'wrong_owner':
        db.execute('DELETE FROM fulfillment_verifications WHERE order_id<>142')
        db.execute('UPDATE fulfillment_verifications SET order_id=999 WHERE order_id=142')
    else:
        row = db.execute('SELECT * FROM fulfillment_verifications LIMIT 1').fetchone()
        payload = json.loads(row['payload'])
        payload['tracking'] = 'different'
        db.execute('UPDATE fulfillment_verifications SET payload=?', (json.dumps(payload),))
    with pytest.raises(packing_versions.PackingConflict):
        amendments.effective_shipment(db, original)


def test_same_tracking_without_explicit_proof_is_not_hidden(parcel):
    db, original, _ = parcel
    db.execute('INSERT INTO packing_shipments VALUES(?,?,?,?,?,?)',
               ('inpost:other', 'list-2', 2, original['confirmed_at'], 'inpost', original['tracking']))
    rows = amendments.effective_shipments(db, list(db.execute('SELECT * FROM packing_shipments')))
    assert {row['shipment_key'] for row in rows} == {'inpost:777', 'inpost:other'}


def test_chain_and_stale_predecessor(parcel, tmp_path):
    db, original, _ = parcel
    correct(parcel)
    second = amendments.effective_shipment(db, original)
    latest = add_batch(db, tmp_path, 3, {139: 4, 141: 28, 142: 18, 143: 1})
    with pytest.raises(packing_versions.PackingConflict, match='już zmienione'):
        amendments.amend(db, original, latest, 'operator', 'stale')
    amendments.amend(db, second, latest, 'operator', 'Jeszcze jeden element tej samej paczki')
    result = amendments.effective_shipment(db, original)
    assert result['final_batch_id'] == 3
    assert result['superseded_shipment_keys'] == ['inpost:777', second['shipment_key']]
    keys, proofs = amendments.amendment_evidence(db, 143)
    assert keys == {'list-1', 'list-2', 'list-3'}
    cold = amendments._record(db)
    cold['fulfillment_verifications'] = proofs
    assert amendments.effective_shipments_from_records([cold])[0]['final_batch_id'] == 3


def test_modified_allocations_do_not_hide_original_even_with_original_selection_hash(parcel):
    db, original, _ = parcel
    correct(parcel)
    db.execute('UPDATE packing_allocations SET qty=999 WHERE batch_id=2 AND order_id=142')
    with pytest.raises(packing_versions.PackingConflict):
        amendments.effective_shipment(db, original)


def test_valid_but_branched_proofs_are_rejected(parcel, tmp_path):
    db, original, _ = parcel
    correct(parcel)
    add_batch(db, tmp_path, 3, {139: 4, 141: 29, 142: 18})
    record = amendments._record(db)
    source = json.loads(record['fulfillment_verifications'][0]['payload'])
    alternate = copy.deepcopy(source)
    batch = next(r for r in record['packing_batches'] if r['id'] == 3)
    items = [r for r in record['packing_allocations'] if r['batch_id'] == 3]
    docs = [r for r in record['fulfillment_document_history'] if r['document_id'] == 3]
    alternate['after'] = amendments._scope(batch, items, docs)[0]
    digest = amendments._digest({key: alternate[key] for key in amendments.IDENTITY_FIELDS})
    alternate['successor_key'] = 'amend:' + digest
    db.execute('INSERT INTO packing_shipments VALUES(?,?,?,?,?,?)',
               (alternate['successor_key'], 'list-3', 3, original['confirmed_at'], original['carrier'], original['tracking']))
    db.execute('INSERT INTO fulfillment_verifications VALUES(?,?,?)',
               (139, amendments.PREFIX + digest, json.dumps(alternate)))
    with pytest.raises(packing_versions.PackingConflict):
        amendments.effective_shipment(db, original)


def test_requires_explicit_actor_reason_and_transaction(parcel):
    db, original, current = parcel
    with pytest.raises(packing_versions.PackingConflict, match='transakcji'):
        amendments.amend(db, original, current, 'operator', 'same parcel')
    db.execute('BEGIN')
    with pytest.raises(packing_versions.PackingConflict, match='kto'):
        amendments.amend(db, original, current, '', 'same parcel')


def test_form_version_is_preserved_for_exact_request_replay(parcel):
    db, original, current = parcel
    db.execute('BEGIN')
    result = amendments.amend(db, original, current, 'operator', 'same parcel', request_version='form-version-1')
    proof = json.loads(db.execute('SELECT payload FROM fulfillment_verifications LIMIT 1').fetchone()[0])
    assert proof['request_version'] == 'form-version-1'
    assert amendments.amend(db, original, current, 'operator', 'retry', request_version='form-version-1')['final_key'] == result['final_key']


def test_stage_and_restore_new_member_include_entire_correction_chain(parcel, tmp_path):
    db, original, _ = parcel
    correct(parcel)
    second = amendments.effective_shipment(db, original)
    third = add_batch(db, tmp_path, 3, {139: 4, 141: 28, 142: 18, 143: 1})
    amendments.amend(db, second, third, 'operator', 'Actual contents confirmed')
    reconciliation_schema(db)
    backend = SimpleNamespace(supabase_enabled=lambda: True)
    reconciliation_store.stage(backend, 143, connection=db)
    payload = json.loads(db.execute('SELECT payload FROM fulfillment_reconciliation_pending WHERE order_id=143').fetchone()[0])
    assert {r['packing_list_id'] for r in payload['packing_lists']} == {'list-1', 'list-2', 'list-3'}
    assert len({r['kind'] for r in payload['fulfillment_verifications']}) == 2

    path = tmp_path / 'cold.sqlite'
    cold = database(path)
    reconciliation_schema(cold)
    cold.commit()
    cold.close()
    def connect():
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        return connection
    backend = SimpleNamespace(supabase_enabled=lambda: True, conn=connect, DATA_DIR=str(tmp_path / 'cold'),
                              DB_PATH=str(path), supabase_request=lambda *a, **kw: [{'revision': 1, 'payload': payload}])
    reconciliation_store.restore(backend, 143)
    cold = connect()
    assert amendments.effective_shipment(cold, original)['final_batch_id'] == 3
    assert len(amendments.effective_shipments(cold, list(cold.execute('SELECT * FROM packing_shipments')))) == 1
    assert cold.execute('SELECT qty FROM stock').fetchone()[0] == 17
    cold.close()
