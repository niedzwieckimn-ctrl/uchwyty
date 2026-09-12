"""Local order mutations shared by the existing form and supervised operations."""

STATUSES = frozenset({
    'new', 'confirmed', 'packed', 'packed_partial', 'in_delivery',
    'shipped', 'partially_shipped', 'issued', 'completed', 'cancelled',
})


def transition(db, order_id, target_status, *, make_qr, canonical_number):
    if target_status not in STATUSES:
        raise ValueError('Invalid order status')
    order = db.execute('SELECT * FROM orders WHERE id=?', (order_id,)).fetchone()
    if order is None:
        raise LookupError('Order not found')
    qr = (order['qr_data_url'] or '').strip()
    if target_status == 'confirmed':
        qr = make_qr(canonical_number(order['id'], order['created_at'], order['order_no']))
    issued = int(order['warehouse_issued'] or 0)
    db.execute('UPDATE orders SET status=?, qr_data_url=?, warehouse_issued=? WHERE id=?',
               (target_status, qr, issued, order_id))
    return {'status': target_status, 'qr_data_url': qr, 'warehouse_issued': issued}
