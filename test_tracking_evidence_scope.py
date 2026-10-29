"""A member's later parcel must not widen an older delivery sync."""
import sqlite3
import json
from types import SimpleNamespace

import packing_versions
import reconciliation_store
import inpost_reconciliation


def test_delivery_scope_excludes_other_parcel_conflict(tmp_path, monkeypatch):
    path = tmp_path / 'scope.db'
    def conn():
        return sqlite3.connect(path)
    db = conn()
    db.executescript('''
        CREATE TABLE packing_batches(id INTEGER, packing_list_id TEXT);
        CREATE TABLE packing_allocations(batch_id INTEGER, order_id INTEGER);
        INSERT INTO packing_batches VALUES(1,'old'),(2,'later');
        INSERT INTO packing_allocations VALUES(1,143),(1,144),(1,145),
            (2,143),(2,132);
    ''')
    db.commit()
    db.close()
    backend = SimpleNamespace(conn=conn, supabase_enabled=lambda: True)
    published = []
    def publish(b, oid):
        if oid == 132:
            raise ValueError('unrelated later parcel conflict')
        published.append(oid)
    monkeypatch.setattr(reconciliation_store, 'publish', publish)
    packing_versions.sync_evidence(backend, [145,143,144,143], expand_members=False)
    assert published == [143,144,145]
    published.clear()
    monkeypatch.setattr(inpost_reconciliation, '_owns_sync', lambda db, claim: True)
    inpost_reconciliation.publish(backend, {
        'shipment_id': 'old',
        'sync_payload': json.dumps({'member_ids': [143,144,145], 'orders': []}),
    })
    assert published == [143,144,145]
    # Packing edits still retain the established removed-member expansion.
    import pytest
    with pytest.raises(ValueError, match='unrelated later parcel'):
        packing_versions.sync_evidence(backend, [143,144,145])
