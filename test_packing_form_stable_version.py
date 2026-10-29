"""A cache refresh is not a change to the customer's packing selection."""
import re

import app as b
import packing_correction
import packing_versions
from test_multi_order_packing_agent import multi_order_flow
from test_packing_ui_reconciliation import packing_cloud


def token():
    db = b.conn()
    try:
        return packing_correction.form_version(db, 103)
    finally:
        db.close()


def test_allocation_cache_ids_do_not_change_form_version(packing_cloud):
    before = token()
    db = b.conn()
    assert db.execute('SELECT COUNT(*) FROM invoice_allocations').fetchone()[0] > 0
    db.execute('UPDATE invoice_allocations SET id=id+100000')
    db.commit()
    db.close()
    assert token() == before


def test_real_quantity_changes_still_invalidate_form(packing_cloud):
    before = token()
    db = b.conn()
    db.execute('UPDATE invoice_allocations SET qty=qty+1')
    db.commit()
    db.close()
    assert token() != before
    before = token()
    db = b.conn()
    db.execute('UPDATE order_items SET qty=qty+1 WHERE order_id=103')
    db.commit()
    db.close()
    assert token() != before


def test_first_submit_survives_preflight_cache_rebuild(packing_cloud, monkeypatch):
    client, cloud, calls = packing_cloud
    html = client.get('/orders/103/packing-list').text
    version = re.search(r'name="packing_form_version" value="([^"]+)"', html).group(1)
    prepare = packing_versions.prepare_write_evidence

    def refresh(backend, ids):
        prepare(backend, ids)
        db = b.conn()
        db.execute('UPDATE invoice_allocations SET id=id+100000')
        db.commit()
        db.close()

    monkeypatch.setattr(packing_versions, 'prepare_write_evidence', refresh)
    result = client.post('/orders/103/packing-list', data={
        'csrf_token': 'csrf', 'carrier': 'pending', 'packing_form_version': version,
        'pack_qty_1001': '3', 'pack_qty_1002': '0', 'pack_qty_1003': '2'})
    assert result.status_code == 302, result.text
    assert '/invoice' in result.location
    db = b.conn()
    try:
        assert db.execute('SELECT COUNT(*) FROM packing_batches').fetchone()[0] == 1
        assert sum(r[0] for r in db.execute('SELECT qty FROM packing_allocations')) == 5
        assert [r[0] for r in db.execute('SELECT qty FROM stock ORDER BY product_id')] == [3, 0, 2]
        before = packing_correction.form_version(db, 103)
        db.execute("UPDATE fulfillment_documents SET document_id=document_id+100000 WHERE kind='packing_list'")
        assert packing_correction.form_version(db, 103) == before
        db.execute("UPDATE fulfillment_documents SET file_hash='changed-content' WHERE kind='packing_list'")
        assert packing_correction.form_version(db, 103) != before
        db.rollback()
    finally:
        db.close()
