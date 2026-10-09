"""Shipment overview over existing packing evidence; no parallel business writes."""
import json
import threading
from urllib.parse import quote

from flask import abort, redirect, render_template, request, url_for
from werkzeug.exceptions import HTTPException

SECTIONS = ('packing_lists', 'packing_batches', 'packing_allocations', 'packing_shipments',
            'fulfillment_shipping_attempts')
# Existing current document kinds: packing_list, invoice, label. Read only
# their identities, including for legacy snapshots that still contain PDF bytes.
DOC_FIELDS = ('kind', 'document_id')
PROJECTION = 'order_id,revision,' + ','.join(
    ['payload->' + name for name in SECTIONS] +
    [f'doc{i}_{field}:payload->fulfillment_documents->{i}->{field}'
     for i in range(3) for field in DOC_FIELDS])
_cache = {}
_lock = threading.Lock()


def records(b):
    if not b.supabase_enabled():
        db = b.conn()
        try:
            payload = {name: [dict(row) for row in db.execute('SELECT * FROM ' + name)] for name in SECTIONS}
            payload['current_documents'] = [dict(row) for row in db.execute(
                'SELECT kind,document_id FROM fulfillment_documents')]
            return [dict(order_id=0, revision=0, **payload)], False
        finally:
            db.close()
    # Cold reads omit all PDF fields. Warm reads transfer only a tiny revision index.
    index = b.supabase_request('/rest/v1/fulfillment_reconciliation',
        params={'select': 'order_id,revision', 'order': 'order_id.desc', 'limit': 200}, timeout=15)
    if not isinstance(index, list):
        raise ValueError('Invalid shipment index')
    context = (str(b.DB_PATH), b.SUPABASE_URL)
    with _lock:
        cached = dict(_cache.get(context, {}))
    wanted = {int(row['order_id']): row['revision'] for row in index}
    changed = [oid for oid, rev in wanted.items() if cached.get(oid, {}).get('revision') != rev]
    for offset in range(0, len(changed), 25):
        ids = changed[offset:offset + 25]
        fresh = b.supabase_request('/rest/v1/fulfillment_reconciliation', params={
            'select': PROJECTION, 'order_id': 'in.(' + ','.join(map(str, ids)) + ')'}, timeout=20)
        if not isinstance(fresh, list) or {int(row['order_id']) for row in fresh} != set(ids):
            raise ValueError('Incomplete shipment snapshot')
        cached.update({int(row['order_id']): row for row in fresh})
    result = {oid: cached[oid] for oid in wanted}
    with _lock:
        # One active deployment context; tests and changed credentials cannot share it.
        _cache.clear()
        _cache[context] = result
    return list(result.values()), len(index) == 200


def cards_from_records(rows):
    sources = {}
    current = set()
    for record in rows:
        documents = record.get('current_documents', [
            {field: record.get(f'doc{i}_{field}') for field in DOC_FIELDS} for i in range(3)])
        for document in documents:
            if document.get('kind') == 'packing_list':
                batch = next((r for r in record.get('packing_batches') or []
                              if r['id'] == document.get('document_id')), None)
                if batch:
                    current.add((batch.get('packing_list_id'), batch['created_at'], batch.get('selection_hash') or ''))
        for packing in record.get('packing_lists') or []:
            key = packing['packing_list_id']
            rank = (int(record['order_id']) == int(packing['root_order_id']), int(record.get('revision') or 0))
            if key not in sources or rank > sources[key][0]:
                sources[key] = (rank, packing, record)
    cards = []
    for key, (_, packing, record) in sources.items():
        batch_id = packing.get('current_batch_id')
        if not batch_id:  # Withdrawn drafts stay in the existing audit history.
            continue
        batch = next((r for r in record.get('packing_batches') or [] if r['id'] == batch_id), None)
        items = [r for r in record.get('packing_allocations') or [] if r['batch_id'] == batch_id]
        if not batch or not items:
            continue
        finals = sorted((r for r in record.get('packing_shipments') or []
                         if r['packing_list_id'] == key and r['final_batch_id'] == batch_id),
                        key=lambda r: r['confirmed_at'])
        final = finals[-1] if finals else {}
        if not final and (key, batch['created_at'], batch.get('selection_hash') or '') not in current:
            continue  # An old draft is not a new parcel after its pointer was replaced.
        receiver, booked_tracking, booked_carrier = {}, '', ''
        for attempt in record.get('fulfillment_shipping_attempts') or []:
            try:
                payload = json.loads(attempt.get('payload') or '{}')
                scope = payload.get('recipient_selection', {}).get('scope', {})
                if scope != {'packing_list': key, 'selection': batch.get('selection_hash') or ''}:
                    continue
                receiver = payload.get('receiver') or {}
                provider = json.loads(attempt.get('provider_json') or '{}')
                booked_tracking = provider.get('tracking_number') or ''
                booked_carrier = 'inpost' if booked_tracking else ''
            except (ValueError, TypeError):
                continue
        tracking = final.get('tracking') or booked_tracking
        carrier = final.get('carrier') or booked_carrier
        invoice_id = batch.get('invoice_id') or packing.get('invoice_id')
        cards.append(dict(key=key, root=int(packing['root_order_id']), batch=batch,
            created=batch['created_at'], sent=bool(final), tracking=tracking, carrier=carrier,
            receiver=receiver, invoice_id=invoice_id, invoice=None,
            customer=items[0].get('customer_name_snapshot') or 'Klient',
            total=sum(int(i['qty']) for i in items),
            orders=sorted({str(i.get('order_number_snapshot') or '') for i in items}),
            history_count=max(0, len(finals) - 1),
            status='Wysłana' if final else ('Nadana' if tracking else ('Do nadania' if invoice_id else 'Do faktury')),
            tracking_url=('https://inpost.pl/sledzenie-przesylek?number=' + quote(tracking, safe=''))
                if carrier.lower() == 'inpost' and tracking else ''))
    return sorted(cards, key=lambda card: card['created'], reverse=True)


def invoice_labels(b, cards):
    ids = sorted({int(c['invoice_id']) for c in cards if c['invoice_id']})
    invoices = {}
    if ids:
        if b.supabase_enabled():
            for offset in range(0, len(ids), 100):
                rows = b.supabase_request('/rest/v1/invoices', params={
                    'select': 'id,invoice_no,publication_state',
                    'id': 'in.(' + ','.join(map(str, ids[offset:offset + 100])) + ')'}, timeout=15)
                invoices.update({int(row['id']): row for row in rows})
        else:
            db = b.conn()
            try:
                invoices = {int(r['id']): dict(r) for r in db.execute(
                    'SELECT id,invoice_no,publication_state FROM invoices WHERE id IN (' +
                    ','.join('?' for _ in ids) + ')', ids)}
            finally:
                db.close()
    for card in cards:
        card['invoice'] = invoices.get(card['invoice_id'])
        if card['invoice_id'] and not card['sent'] and not card['tracking'] and (
                not card['invoice'] or card['invoice'].get('publication_state') != 'complete'):
            card['status'] = 'Dokończ fakturę'


def register_routes(app, b):
    def selected_card(key):
        try:
            rows, _ = records(b)
            card = next((c for c in cards_from_records(rows) if c['key'] == key), None)
            if not card:
                abort(404)
            if b.supabase_enabled():
                import reconciliation_store
                reconciliation_store.restore(b, card['root'])
            return card
        except HTTPException:
            raise
        except Exception:
            app.logger.exception('SHIPMENT_READ_FAILED')
            abort(503, description='Nie udało się odczytać przesyłki. Spróbuj ponownie.')

    @app.get('/shipments/<key>/continue')
    def shipment_continue(key):
        card = selected_card(key)
        db = b.conn()
        try:
            import packing_versions
            current = packing_versions.current_for_order(db, card['root'])
        finally:
            db.close()
        if not current or current['packing_list_key'] != key or current['shipment_confirmed']:
            abort(409, description='Ta paczka jest już w historii lub zmieniła się. Wróć do Przesyłek i odśwież widok.')
        invoice_labels(b, [card])
        ready = card['invoice'] and card['invoice'].get('publication_state') == 'complete'
        target = 'order_packing_list_download_admin' if ready else 'order_invoice'
        return redirect(url_for(target, order_id=card['root']))

    @app.get('/shipments')
    def shipments():
        message = ''
        limited = False
        try:
            rows, limited = records(b)
            cards = cards_from_records(rows)
            invoice_labels(b, cards)
        except Exception:
            app.logger.exception('SHIPMENTS_READ_FAILED')
            cards = []
            message = 'Nie udało się odczytać przesyłek. Odśwież stronę za chwilę.'
        query = request.args.get('q', '').strip()[:160]
        if query:
            needle = query.casefold()
            cards = [c for c in cards if needle in ' '.join([c['customer'], c['tracking'],
                ' '.join(c['orders']), str(c.get('receiver', {}))]).casefold()]
        try:
            import inpost_pickups
            pickup=inpost_pickups.dashboard_state(b)
        except Exception:
            app.logger.exception('INPOST_DAILY_PICKUP_READ_FAILED')
            pickup={'unavailable':True,'cutoff':'12:20','today':None,'next':None,'can_order':False}
        pickup_notice={
            'ordered':'Podjazd został przekazany do InPost.',
            'waiting':'Podjazd jest już sprawdzany. Odśwież widok za chwilę.',
            'failed':'Nie potwierdzono zamówienia podjazdu. Sprawdź komunikat w kafelku.',
        }.get(request.args.get('pickup'),'')
        return render_template('shipments.html', title='Przesyłki', base_url=b.BASE_URL,
            db_path=b.DB_PATH, active=[c for c in cards if not c['sent']],
            history=[c for c in cards if c['sent']], q=query, message=message, limited=limited,
            pickup=pickup,pickup_notice=pickup_notice), (503 if message else 200)

    @app.post('/shipments/pickup/order-now')
    def shipment_pickup_order_now():
        try:
            import inpost_pickups
            result=inpost_pickups.order_today(b)
            state='ordered' if result.get('ok') else ('waiting' if 'sprawdzany' in result.get('message','') else 'failed')
        except Exception:
            app.logger.exception('INPOST_DAILY_PICKUP_ORDER_FAILED')
            state='failed'
        return redirect(url_for('shipments',pickup=state))

    @app.get('/shipments/<key>/packing-list')
    def shipment_packing_pdf(key):
        # Restore only the requested shipment on demand. The overview never reads PDFs.
        try:
            card = selected_card(key)
            db = b.conn()
            try:
                doc = db.execute('''SELECT d.file_hash,d.order_id FROM fulfillment_document_history d
                    JOIN packing_batches pb ON pb.id=d.document_id
                    WHERE d.kind='packing_list' AND pb.packing_list_id=? AND pb.created_at=?
                    AND COALESCE(pb.selection_hash,'')=? ORDER BY d.order_id LIMIT 1''',
                    (key, card['batch']['created_at'], card['batch'].get('selection_hash') or '')).fetchone()
            finally:
                db.close()
        except (ValueError, OSError):
            abort(503, description='Nie udało się odczytać listy. Spróbuj ponownie.')
        if not doc or not doc['file_hash']:
            abort(409, description='Ta lista wymaga odczytu w szczegółach zamówienia.')
        return redirect('/orders/' + str(doc['order_id']) + '/packing-documents/' + doc['file_hash'])
