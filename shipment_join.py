"""Explicitly attach another invoiced list to one physical parcel, without replacement."""
import hashlib
import json

from packing_versions import PackingConflict
from shipment_rebind import _scope


def key(package, carrier, tracking):
    return 'join:' + hashlib.sha256(json.dumps(
        [package['packing_list_key'], carrier, tracking]).encode()).hexdigest()


def replay(db, prior, token):
    if not prior or not prior['shipment_key'].startswith('join:'):
        return False
    row = db.execute('SELECT payload FROM fulfillment_verifications WHERE kind=? LIMIT 1',
                     (prior['shipment_key'],)).fetchone()
    return bool(row and json.loads(row[0]).get('request_version') == token)


def validate(db, matches, package):
    """Return an exact confirmation token and document summary; never change data."""
    invoice = db.execute('SELECT * FROM invoices WHERE id=?', (package.get('invoice_id'),)).fetchone()
    if not invoice or invoice['publication_state'] != 'complete':
        raise PackingConflict('Dokończ fakturę aktualnej listy przed dołączeniem jej do paczki.')
    selected = _scope(db.execute('SELECT order_id,order_item_id,qty FROM packing_allocations WHERE batch_id=?',
                                 (package['batch_id'],)))
    billed = _scope(db.execute('SELECT order_id,order_item_id,qty FROM invoice_allocations WHERE invoice_id=?',
                               (invoice['id'],)))
    if not selected or selected != billed:
        raise PackingConflict('Lista pakowa nie odpowiada pozycjom faktury. Sprawdź dokumenty.')
    all_ids = set(package['order_ids'])
    old_invoices = []
    old_lists = []
    for prior in matches:
        old = db.execute('SELECT * FROM packing_batches WHERE id=?', (prior['final_batch_id'],)).fetchone()
        old_invoice = db.execute('SELECT * FROM invoices WHERE id=?', (old['invoice_id'],)).fetchone() if old else None
        if not old_invoice or old_invoice['id'] == invoice['id']:
            raise PackingConflict('Ta lista zastępuje poprzedni dokument. Użyj ponownego przypisania po regeneracji faktury.')
        old_selected = _scope(db.execute('SELECT order_id,order_item_id,qty FROM packing_allocations WHERE batch_id=?', (old['id'],)))
        old_billed = _scope(db.execute('SELECT order_id,order_item_id,qty FROM invoice_allocations WHERE invoice_id=?', (old_invoice['id'],)))
        if old_selected != old_billed or old_invoice['publication_state'] != 'complete':
            raise PackingConflict('Nie potwierdzono dokumentów wcześniejszej paczki. Sprawdź jej listę i fakturę.')
        all_ids.update(int(r[0]) for r in db.execute(
            'SELECT DISTINCT order_id FROM packing_allocations WHERE batch_id=?', (old['id'],)))
        old_invoices.append(old_invoice['invoice_no'])
        old_lists.append([prior['shipment_key'], old['packing_list_id'], old['selection_hash']])
    if not matches:
        raise PackingConflict('Nie znaleziono przesyłki do połączenia.')
    orders = [dict(r) for r in db.execute('SELECT * FROM orders WHERE id IN (' +
        ','.join('?' for _ in all_ids) + ')', sorted(all_ids))]
    identities = {(str(r.get('customer_id') or ''), str(r.get('customer_email') or '').strip().casefold()) for r in orders}
    if len(orders) != len(all_ids) or len(identities) != 1 or next(iter(identities)) == ('', ''):
        raise PackingConflict('Do jednej paczki można dołączyć tylko zamówienia tego samego klienta.')
    if any(str(r.get('status')).lower() in ('cancelled', 'canceled') for r in orders):
        raise PackingConflict('Anulowane zamówienie nie może zostać dołączone do paczki.')
    if db.execute('SELECT 1 FROM packing_shipments WHERE final_batch_id=?', (package['batch_id'],)).fetchone():
        raise PackingConflict('Ta lista ma już zapisane nadanie. Sprawdź jej numer przesyłki.')
    # Do not turn an uncertain API booking into a second physical parcel.
    marks = ','.join('?' for _ in package['order_ids'])
    for attempt in db.execute(f'''SELECT DISTINCT a.* FROM fulfillment_shipping_attempts a
        LEFT JOIN fulfillment_shipping_members m ON m.attempt_order_id=a.order_id
        WHERE a.order_id IN ({marks}) OR m.order_id IN ({marks})''', package['order_ids'] * 2):
        if attempt['state'] in ('FAILED', 'REJECTED'):
            continue
        if attempt['state'] != 'SUCCESS':
            raise PackingConflict('Istnieje niepotwierdzona próba nadania. Najpierw sprawdź jej wynik.')
        payload = json.loads(attempt['payload'] or '{}')
        scope = payload.get('recipient_selection', {}).get('scope', {})
        if scope.get('packing_list') != package['packing_list_key']:
            continue
        provider = json.loads(attempt['provider_json'] or '{}')
        if attempt['state'] != 'SUCCESS' or str(provider.get('tracking_number') or '') != matches[0]['tracking']:
            raise PackingConflict('Bieżąca lista ma inne lub niepotwierdzone nadanie. Najpierw sprawdź jego wynik.')
    token = hashlib.sha256(json.dumps([sorted(old_lists), package['packing_list_key'],
        sorted((list(k), v) for k, v in selected.items()), invoice['id'],
        matches[0]['carrier'], matches[0]['tracking'], sorted(identities)]).encode()).hexdigest()
    return dict(token=token, invoices=sorted(set(old_invoices)), invoice=invoice['invoice_no'],
                total=sum(selected.values()), customer=orders[0]['customer_name'])


def record(db, package, shipment_key, matches, token, actor):
    proof = json.dumps(dict(request_version=token, actor=actor,
        packing_list=package['packing_list_key'],
        parents=sorted(r['shipment_key'] for r in matches)), ensure_ascii=False)
    for oid in package['order_ids']:
        db.execute('INSERT OR IGNORE INTO fulfillment_verifications VALUES(?,?,?)', (oid, shipment_key, proof))
