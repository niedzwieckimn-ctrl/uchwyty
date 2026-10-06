"""Keep prior labels when a partly fulfilled order is packed again."""
import json
from datetime import datetime, timezone
SCHEMA='''CREATE TABLE IF NOT EXISTS inpost_shipment_history(
 order_id INTEGER NOT NULL,shipment_id TEXT NOT NULL,snapshot_json TEXT NOT NULL,created_at TEXT NOT NULL,
 PRIMARY KEY(order_id,shipment_id))'''
COLLECTED={'collected_from_sender','taken_by_courier_from_pok','collected_by_courier','taken_by_courier','adopted_at_source_branch','sent_from_source_branch','adopted_at_sorting_center','sent_from_sorting_center','adopted_at_target_branch','out_for_delivery_to_address','out_for_delivery','ready_to_pickup','pickup_reminder_sent','delivered','returned_to_sender'}

def prepare_next(b,order_ids, *, claim_evidence=None):
    if not order_ids:
        return
    c=b.conn()
    try:rows=[dict(x) for x in c.execute('SELECT * FROM orders WHERE id IN ('+','.join('?' for _ in order_ids)+')',tuple(order_ids))]
    finally:c.close()
    existing=[x for x in rows if x.get('inpost_shipment_id')]
    verified = {}
    for row in existing:
        sid = str(row['inpost_shipment_id'])
        shipment = verified.get(sid)
        if shipment is None:
            shipment=b.inpost_get_shipment(sid)
            if str(shipment.get('id') or '') != sid:
                raise ValueError('InPost nie potwierdził identyfikatora poprzedniej przesyłki.')
            verified[sid] = shipment
        if str(shipment.get('status') or '').lower() not in COLLECTED:
            raise ValueError('To zamówienie ma aktywną przesyłkę InPost. Kolejną paczkę przygotuj po potwierdzeniu odbioru poprzedniej przez kuriera.')
    for row in existing:
        archived = dict(row)
        archived['verified_shipment'] = verified[str(row['inpost_shipment_id'])]
        if claim_evidence and row['id'] in claim_evidence:
            archived['shipping_claim'] = claim_evidence[row['id']]
        history={'order_id':row['id'],'shipment_id':str(row['inpost_shipment_id']),'snapshot_json':json.dumps(archived,ensure_ascii=False),'created_at':b.now_iso()}
        cleared={'inpost_shipment_id':'','tracking_no':'','inpost_label_format':'','shipped_at':''}
        if b.supabase_enabled():
            b.supabase_request('/rest/v1/inpost_shipment_history',method='POST',params={'on_conflict':'order_id,shipment_id'},payload=history,prefer='resolution=ignore-duplicates')
            # Accept our own completed retry, never clear a newer shipment.
            b.supabase_update_rows('orders',cleared,{'id':row['id'], 'inpost_shipment_id':[str(row['inpost_shipment_id']), '']})
        c=b.conn()
        try:
            c.execute('INSERT OR IGNORE INTO inpost_shipment_history VALUES(?,?,?,?)',tuple(history.values()))
            c.execute("UPDATE orders SET inpost_shipment_id='',tracking_no='',inpost_label_format='',shipped_at='' WHERE id=? AND inpost_shipment_id=?",(row['id'],str(row['inpost_shipment_id'])))
            c.commit()
        finally:c.close()


def prepare_current_package(b, order_id):
    """Explicit panel action: keep the current LP/invoice and close older labels.

    No carrier booking, PDF generation or customer notification is performed.
    A provider-confirmed collection AND a newer saved packing batch are required.
    """
    import packing_versions
    from fulfillment_operations import release_archived_shipping_attempts
    packing_versions.prepare_write_evidence(b, [order_id])
    c = b.conn()
    try:
        package = packing_versions.current_for_order(c, order_id)
        if (not package or not package.get('complete') or not package.get('document_available')
                or c.execute('SELECT 1 FROM packing_shipments WHERE packing_list_id=?',
                             (package['packing_list_key'],)).fetchone()):
            raise ValueError('Najpierw musi istnieć poprawna, jeszcze niewysłana lista tej paczki.')
        members = sorted(package['order_ids'])
        marks = ','.join('?' for _ in members)
        rows = [dict(r) for r in c.execute(f'SELECT * FROM orders WHERE id IN ({marks})', members)]
        pending = c.execute(f'SELECT 1 FROM fulfillment_reconciliation_pending WHERE order_id IN ({marks})', members).fetchone()
        if pending:
            raise ValueError('Najpierw dokończ zapis aktualnej listy pakowej.')
        attempts = [dict(r) for r in c.execute(f'''SELECT DISTINCT a.* FROM fulfillment_shipping_attempts a
            WHERE a.order_id IN ({marks}) OR a.order_id IN (
                SELECT attempt_order_id FROM fulfillment_shipping_members WHERE order_id IN ({marks}))''', members + members)]
    finally:
        c.close()
    claims = {}
    if b.supabase_enabled():
        remote = b.supabase_request('/rest/v1/fulfillment_shipping_claims', params={
            'order_id': 'in.(' + ','.join(map(str, members)) + ')',
            'select': 'order_id,reference,payload,content_hash,state,provider_json,created_at'})
        if not isinstance(remote, list):
            raise ValueError('Nie można potwierdzić wcześniejszych nadań.')
        claims = {int(r['order_id']): r for r in remote}
    else:
        for attempt in attempts:
            saved = dict(attempt, payload=json.loads(attempt['payload']),
                         provider_json=json.loads(attempt['provider_json'] or '{}'))
            for member in saved['payload'].get('order_ids') or [saved['order_id']]:
                claims[int(member)] = saved
    def timestamp(value):
        try:
            parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=b.app_now().tzinfo or timezone.utc)
        except (ValueError, TypeError):
            raise ValueError('Brak pewnej daty poprzedniego nadania; wymagana kontrola przesyłki.')
    relevant = [row for row in rows if row.get('inpost_shipment_id')]
    if not relevant:
        return members
    for row in relevant:
        saved = claims.get(row['id'])
        if (not saved or saved['state'] != 'SUCCESS'
                or str((saved.get('provider_json') or {}).get('id') or '') != str(row['inpost_shipment_id'])
                or row['id'] not in saved['payload'].get('order_ids', [])
                or timestamp(package['created_at']) <= timestamp(saved['created_at'])):
            raise ValueError('Nie potwierdzono, że poprzednie nadanie dotyczy starszej paczki. Nie zmieniono listy.')
    # A copied cloud SUCCESS was previously changed to UNKNOWN on a validation
    # error. Only the SAME durable reference/provider ID can prove that case.
    for attempt in attempts:
        matching = next((r for r in claims.values() if r['reference'] == attempt['reference']), None)
        if attempt['state'] != 'SUCCESS' and (not matching or matching['state'] != 'SUCCESS'
                or json.loads(attempt['provider_json'] or '{}') != matching.get('provider_json')
                or json.loads(attempt['payload']) != matching.get('payload')
                or attempt['content_hash'] != matching.get('content_hash')):
            raise ValueError('Istnieje niepewna próba nadania. Najpierw sprawdź jej wynik w InPost.')
    prepare_next(b, members, claim_evidence=claims)
    c = b.conn()
    try:
        c.execute('BEGIN IMMEDIATE')
        for attempt in attempts:
            if attempt['state'] != 'SUCCESS':
                c.execute("UPDATE fulfillment_shipping_attempts SET state='SUCCESS' WHERE order_id=? AND reference=? AND provider_json=?",
                          (attempt['order_id'], attempt['reference'], attempt['provider_json']))
        release_archived_shipping_attempts(c, members)
        packing_versions.stage_evidence(b, c, members)
        c.commit()
    finally:
        c.close()
    packing_versions.sync_evidence(b, members)
    return members
