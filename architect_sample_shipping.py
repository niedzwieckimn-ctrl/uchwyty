"""Narrow adapter into existing warehouse packing, labels and pickup workflow."""


def linked_sample(backend, order_id, order=None):
    if order is None:
        db = backend.conn()
        try:
            row = db.execute('SELECT * FROM orders WHERE id=?', (order_id,)).fetchone()
            order = dict(row) if row else {}
        finally:
            db.close()
    key = str(order.get('idempotency_key') or '')
    if not key.startswith('architect-sample:'):
        return None
    rows = backend.supabase_request('/rest/v1/architect_sample_orders', params={
        'select': 'id,status,fulfillment_order_id', 'fulfillment_order_id': f'eq.{int(order_id)}',
        'limit': 1}, timeout=15) or []
    if not rows or key != 'architect-sample:' + rows[0]['id']:
        raise ValueError('Nie potwierdzono powiązania paczki z zamówieniem próbek.')
    if rows[0]['status'] == 'cancelled':
        raise ValueError('Ten pakiet został anulowany.')
    if int(order.get('warehouse_issued') or 0) != 1:
        raise ValueError('Nie potwierdzono wydania próbek z magazynu.')
    return rows[0]


def require_complete_pack(items, selected):
    expected = {int(i['id']): int(i['qty']) for i in items}
    actual = {int(i.get('order_item_id') or i['id']): int(i['qty']) for i in selected}
    if actual != expected:
        raise ValueError('Pakiet próbek wyślij w całości. Sprawdź wszystkie pozycje listy pakowej.')


def validate_sample_items(backend, sample_id, items):
    rows = backend.supabase_request('/rest/v1/architect_sample_items', params={
        'select': 'product_id,qty', 'sample_order_id': f'eq.{sample_id}',
        'cancelled_at': 'is.null', 'limit': 4}, timeout=15) or []
    expected = {int(i['product_id']): int(i['qty']) for i in rows}
    actual = {int(i['product_id']): int(i['qty']) for i in items}
    if not expected or len(items) != len(expected) or expected != actual:
        raise ValueError('Zawartość paczki różni się od zatwierdzonych próbek. Nie nadano przesyłki.')
