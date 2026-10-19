"""Recognize a regenerated invoice/list when assigning an existing tracking number.

This is metadata reconciliation, not another dispatch. The submitted current
parcel form identifies the intended contents; an unrelated partial shipment is
never accepted just because the customer or tracking number matches.
"""
import json
import time

import packing_versions
import shipment_amendments


def is_replay(db, prior, form_version):
    if not prior or not prior['shipment_key'].startswith('amend:') or not form_version:
        return False
    row = db.execute('SELECT payload FROM fulfillment_verifications WHERE kind=? LIMIT 1',
                     ('shipment_amendment:' + prior['shipment_key'][6:],)).fetchone()
    return bool(row and json.loads(row[0]).get('request_version') == form_version)


def _scope(rows):
    result = {}
    for row in rows:
        key = (int(row['order_id']), int(row['order_item_id']))
        result[key] = result.get(key, 0) + int(row['qty'])
    return result


def validate(db, prior, package):
    """Require an invoice replacement and the latest, unambiguous parcel."""
    conflict = packing_versions.PackingConflict
    invoice_id = package.get('invoice_id')
    invoice = db.execute('SELECT * FROM invoices WHERE id=?', (invoice_id,)).fetchone()
    if not invoice or invoice['publication_state'] != 'complete':
        raise conflict('Dokończ fakturę aktualnej listy przed przypisaniem numeru przesyłki.')
    selected = _scope(db.execute('SELECT order_id,order_item_id,qty FROM packing_allocations WHERE batch_id=?',
                                 (package['batch_id'],)))
    billed = _scope(db.execute('SELECT order_id,order_item_id,qty FROM invoice_allocations WHERE invoice_id=?',
                               (invoice_id,)))
    if not selected or selected != billed:
        raise conflict('Aktualna lista pakowa nie odpowiada pozycjom faktury. Sprawdź dokumenty przed przypisaniem numeru.')
    old = db.execute('SELECT * FROM packing_batches WHERE id=?', (prior['final_batch_id'],)).fetchone()
    if not old:
        raise conflict('Brak powiązania wcześniejszej paczki z fakturą. Nie można automatycznie połączyć przesyłek.')
    original_invoice_id = old['invoice_id']
    if not original_invoice_id:
        # Invoice deletion clears live links, but retains its exact list identity.
        # Match stable list/hash values; snapshot-local batch IDs can be remapped.
        removed = []
        for proof in db.execute("SELECT payload FROM fulfillment_verifications WHERE kind LIKE 'invoice_removed:%'"):
            try:
                payload = json.loads(proof[0])
                if any(r.get('packing_list_id') == old['packing_list_id']
                       and r.get('selection_hash') == old['selection_hash']
                       for r in payload.get('packing_batches', [])):
                    removed.append(int(payload['invoice_id']))
            except (TypeError, ValueError, KeyError):
                continue
        if len(set(removed)) != 1:
            raise conflict('Nie potwierdzono zastąpienia wcześniejszej faktury. Nie zmieniono powiązania przesyłki.')
        original_invoice_id = removed[0]
    if original_invoice_id != invoice_id and db.execute('SELECT 1 FROM invoices WHERE id=?', (original_invoice_id,)).fetchone():
        raise conflict('Ten numer należy do wcześniejszej paczki z osobną fakturą. Wpisz numer aktualnej przesyłki.')
    old_ids = {int(r[0]) for r in db.execute('SELECT DISTINCT order_id FROM packing_allocations WHERE batch_id=?',
                                           (prior['final_batch_id'],))}
    members = set(package['order_ids'])
    if not old_ids or not old_ids.issubset(members):
        raise conflict('Aktualna lista nie obejmuje wszystkich zamówień tej paczki. Nie zmieniono powiązania.')
    marks = ','.join('?' for _ in members)
    orders = [dict(r) for r in db.execute(f'SELECT * FROM orders WHERE id IN ({marks})', sorted(members))]
    if len(orders) != len(members):
        raise conflict('Brakuje zamówień należących do paczki.')
    for order in orders:
        if str(order.get('status') or '').lower() in ('cancelled', 'canceled'):
            raise conflict('Anulowane zamówienie nie może zostać dopisane do przesyłki.')
        tracking = str(order.get('tracking_no') or '').strip()
        if tracking and (tracking != prior['tracking'] or str(order.get('carrier') or '').lower() != prior['carrier']):
            raise conflict('Jedno z zamówień ma już inny numer przesyłki. Sprawdź bieżącą paczkę.')
    other = [dict(r) for r in db.execute(f'''SELECT DISTINCT ps.* FROM packing_shipments ps
        JOIN packing_allocations a ON a.batch_id=ps.final_batch_id WHERE a.order_id IN ({marks})''', sorted(members))]
    for row in shipment_amendments.effective_shipments(db, other):
        if row['shipment_key'] == prior['shipment_key']:
            continue
        if (int(row['final_batch_id']) == package['batch_id']
                or str(row['confirmed_at']) >= str(prior['confirmed_at'])):
            raise conflict('Zamówienia mają nowsze albo inne nadanie. Nie można użyć numeru wcześniejszej paczki.')
    sids = {str(r.get('inpost_shipment_id')) for r in orders
            if r['id'] in old_ids and r.get('inpost_shipment_id')}
    if prior['carrier'] != 'inpost':
        sids = set()
    if len(sids) > 1:
        raise conflict('Zamówienia wskazują różne przesyłki InPost.')
    sid = next(iter(sids), '')
    original_key = prior.get('original_shipment_key') or prior['shipment_key']
    if (prior['carrier'] == 'inpost' and original_key.startswith('inpost:')
            and original_key[7:] != prior['tracking'] and sid != original_key[7:]):
        raise conflict('Identyfikator InPost nie odpowiada wcześniejszej przesyłce. Sprawdź jej zapisany wynik.')
    if sid:
        state = db.execute('SELECT sync_state,lease_until FROM inpost_tracking_state WHERE shipment_id=?', (sid,)).fetchone()
        if state and (state['sync_state'] in ('pending', 'conflict') or float(state['lease_until'] or 0) > time.time()):
            raise conflict('Trwa rozliczanie statusu tej przesyłki. Dokończ odświeżanie statusu przed przypisaniem nowej listy.')
    for row in orders:
        if row.get('inpost_shipment_id') and str(row['inpost_shipment_id']) != sid:
            raise conflict('Jedno z zamówień jest już przypisane do innej etykiety InPost.')
    # Unresolved or competing bookings must not be silently replaced, including
    # dropship bookings for another recipient belonging to the same customer.
    attempts = db.execute(f'''SELECT DISTINCT a.* FROM fulfillment_shipping_attempts a
        LEFT JOIN fulfillment_shipping_members m ON m.attempt_order_id=a.order_id
        WHERE a.order_id IN ({marks}) OR m.order_id IN ({marks})''', sorted(members) * 2)
    receivers = []
    for row in attempts:
        try:
            payload = json.loads(row['payload'] or '{}')
            provider = json.loads(row['provider_json'] or '{}')
        except (TypeError, ValueError):
            raise conflict('Nie można potwierdzić wcześniejszego nadania.')
        if row['state'] in ('FAILED', 'REJECTED'):
            continue
        if row['state'] != 'SUCCESS' or not sid or str(provider.get('id') or '') != sid:
            raise conflict('Istnieje inna lub nierozstrzygnięta próba nadania. Sprawdź jej wynik przed przypisaniem numeru.')
        if provider.get('tracking_number') and str(provider['tracking_number']) != prior['tracking']:
            raise conflict('Numer nie odpowiada zapisanej etykiecie InPost.')
        receivers.append(payload.get('receiver'))
    if receivers and any(r != receivers[0] for r in receivers):
        raise conflict('Zapisane nadania mają różnych odbiorców.')
    return sid
