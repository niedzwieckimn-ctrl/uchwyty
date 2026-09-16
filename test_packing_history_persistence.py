from pathlib import Path

from flask import request

import app as backend
import fulfillment_operations as fulfillment
import packing_history
from test_multi_order_packing_agent import (
    _approve_and_execute,
    _generate_args,
    _preview,
    multi_order_flow,
)


def _post_multi_order(monkeypatch):
    monkeypatch.setattr(backend, 'complete_orders_packed_side_effects', lambda *a, **k: None)
    with backend.app.test_request_context('/orders/101/packing-list', method='POST', data={
        'carrier': 'other', 'pack_qty_1001': '3', 'pack_qty_1003': '2',
    }):
        backend._refresh_domain_route_context()
        return backend.order_packing_list_download_admin_service(
            101, request=request, session={}, structured=True)


def test_form_packing_is_historically_available_for_every_member(multi_order_flow, monkeypatch):
    created = _post_multi_order(monkeypatch)
    assert created['order_ids'] == [101, 103]
    db = backend.conn()
    rows = db.execute(
        "SELECT order_id,document_id FROM fulfillment_document_history "
        "WHERE kind='packing_list' AND document_id=? ORDER BY order_id",
        (created['batch_id'],),
    ).fetchall()
    db.close()
    assert [tuple(row) for row in rows] == [
        (101, created['batch_id']), (103, created['batch_id'])]
    for order_id in created['order_ids']:
        result = packing_history.read({'order_id': order_id}, connection_factory=backend.conn)
        assert result['batch_id'] == created['batch_id']
        assert result['order_ids'] == [101, 103]
        assert result['total_qty'] == 5
        assert result['history_source'] == 'allocation_snapshot'


def test_later_list_does_not_break_previous_batch_and_uses_distinct_file(
        multi_order_flow, monkeypatch, tmp_path):
    first = _post_multi_order(monkeypatch)
    first_read = packing_history.read({'batch_id': first['batch_id']}, connection_factory=backend.conn)

    second_file = tmp_path / 'second-packing-list.pdf'
    second_file.write_bytes(b'%PDF-1.4\nsecond immutable packing list')
    item = {
        'source_order_id': 103, 'order_item_id': 1003, 'qty': 1,
        'source_order_no': 'ZAM-2609151', 'sku': 'SKU-AVAILABLE',
        'model': 'MODEL-3', 'source_order_note': '',
    }
    db = backend.conn()
    db.execute('BEGIN IMMEDIATE')
    second_batch = backend.save_packing_selection(103, [item], connection=db)
    content_hash = fulfillment.snapshot(103, db, include_package=False)['content_hash']
    fulfillment.save_document(
        103, 'packing_list', second_batch, str(second_file), connection=db,
        content_hash=content_hash,
    )
    db.commit()
    db.close()

    previous_again = packing_history.read(
        {'batch_id': first['batch_id']}, connection_factory=backend.conn)
    current = packing_history.read({'batch_id': second_batch}, connection_factory=backend.conn)
    assert previous_again == first_read
    assert second_batch != first['batch_id']
    assert current['document_path'] != first_read['document_path']
    assert current['total_qty'] == 1


def test_structural_history_read_does_not_require_pdf(multi_order_flow, monkeypatch):
    created = _post_multi_order(monkeypatch)
    path = Path(created['path'])
    path.unlink()
    result = packing_history.read({'batch_id': created['batch_id']}, connection_factory=backend.conn)
    assert result['total_qty'] == 5
    assert result['allocations'][0]['sku'] == 'SKU-PARTIAL'
    assert result['document_available'] is False
    assert result['document_verified'] is False
    assert result['document_path'] == ''


def test_agent_generated_packing_is_readable_for_every_member(multi_order_flow):
    preview = _preview()
    _pending, generated = _approve_and_execute(_generate_args(preview))
    assert generated.status == 'SUCCESS'
    db = backend.conn()
    batch_id = int(db.execute(
        'SELECT id FROM packing_batches ORDER BY id DESC LIMIT 1').fetchone()['id'])
    db.close()
    for order_id in (101, 103):
        result = packing_history.read({'order_id': order_id}, connection_factory=backend.conn)
        assert result['batch_id'] == batch_id
        assert result['order_ids'] == [101, 103]
        assert result['total_qty'] == 5
        assert result['complete'] is True
