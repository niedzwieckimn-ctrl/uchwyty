"""Preview and approve existing evidence; never create or cancel shipments."""
import json
import hashlib
from decimal import Decimal
from pathlib import Path


def _cache(b, data):
    directory = Path(b.DATA_DIR) / 'fulfillment-cache'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (hashlib.sha256(data).hexdigest() + '.pdf')
    path.write_bytes(data)
    return str(path)


def document_proof(f, oid):
    b = f.b; s = f.snapshot(oid)
    conflicts = []
    if len(s['invoices']) != 1:
        return {'status': 'CONFLICT', 'conflicts': ['Wymagana jest jedna jednoznaczna faktura dla zamówienia.'], 'paths': {}}
    inv = s['invoices'][0]
    iid = inv['id']
    c = b.conn()
    try:
        allocations = [dict(r) for r in c.execute('SELECT * FROM invoice_allocations WHERE invoice_id=? ORDER BY order_id,order_item_id', (iid,))]
        source_ids = sorted({r['order_id'] for r in allocations})
        related = [f.snapshot(member) for member in source_ids]
    finally:
        c.close()
    if oid not in source_ids:
        conflicts.append('Brak allocations potwierdzających zawartość zamówienia.')
    for order in related:
        expected = {i['id']: (i['product_id'], i['qty']) for i in order['items']}
        actual = {i['order_item_id']: (i['product_id'], i['qty']) for i in allocations if i['order_id'] == order['order']['id']}
        if expected != actual:
            conflicts.append('Zawartość faktury nie odpowiada bieżącym pozycjom zamówienia.')
    meta = next((m for m in s['metas'] if m['invoice_id'] == iid), {})
    try:
        lines = json.loads(meta.get('invoice_items_json') or '[]')
        line_map = {int(i.get('order_item_id') or i.get('id') or 0): int(i.get('qty') or 0) for i in lines}
        if line_map != {i['order_item_id']: i['qty'] for i in allocations}:
            conflicts.append('Metadata pozycji faktury nie zgadza się z allocations.')
        indexed = {int(i.get('order_item_id') or i.get('id') or 0): i for i in lines}
        for order in related:
            if str(inv.get('currency') or 'PLN').upper() != str(order['order'].get('currency') or 'PLN').upper():
                conflicts.append('Waluta faktury różni się od waluty zamówienia.')
            if str(inv.get('buyer_email') or '').strip().casefold() != str(order['order'].get('customer_email') or '').strip().casefold():
                conflicts.append('Odbiorca faktury różni się od klienta zamówienia.')
            for item in order['items']:
                line = indexed.get(item['id'], {})
                if line.get('sku') != item.get('sku') or int(line.get('product_id') or 0) != item['product_id']:
                    conflicts.append('Produkt w metadanych faktury różni się od zamówienia.')
                if item.get('unit_net_price') is not None and (line.get('unit_net_price') is None or Decimal(str(line['unit_net_price'])) != Decimal(str(item['unit_net_price']))):
                    conflicts.append('Cena pozycji zamówienia zmieniła się względem faktury.')
                if 'source_order_note' in line and str(line['source_order_note'] or '') != str(order['order'].get('note') or ''):
                    conflicts.append('Uwagi zamówienia różnią się od zapisanego źródła dokumentu.')
    except (ValueError, TypeError):
        conflicts.append('Nie można zweryfikować pozycji faktury.')
    if inv.get('publication_state', 'complete') != 'complete':
        conflicts.append('Publikacja faktury nie została ukończona; najpierw wznów istniejący dokument.')
    paths = {}
    exists, path = b.invoice_pdf_exists(meta.get('pdf_path', ''), inv.get('invoice_no', ''))
    if exists:
        paths['invoice'] = path
    elif b.parse_supabase_storage_ref(meta.get('pdf_path', '')):
        try:
            content, _ = b.supabase_storage_download_bytes(meta['pdf_path'])
            paths['invoice'] = _cache(b, content)
        except Exception:
            pass
    packing = b._existing_packing_list_source(iid, include_content=True)
    if packing:
        paths['packing_list'] = packing['path'] if packing['kind'] == 'local' else _cache(b, packing['content'])
    if set(paths) != {'invoice', 'packing_list'}:
        conflicts.append('Brakuje istniejącego PDF faktury lub listy pakowej. Adopcja nie regeneruje dokumentów.')
    hashes = {kind: hashlib.sha256(Path(path).read_bytes()).hexdigest() for kind, path in paths.items()}
    basis = {'invoice_id': iid, 'allocations': allocations, 'meta': meta,
             'orders': [(o['order']['id'], o['content_hash']) for o in related], 'hashes': hashes}
    return {'status': 'CONFLICT' if conflicts else 'SAFE', 'conflicts': conflicts,
            'invoice_id': iid, 'invoice_number': inv['invoice_no'], 'order_ids': source_ids,
            'fingerprint': f._hash(basis), 'content_hash': s['content_hash'], 'paths': paths, 'hashes': hashes}


def shipment_proof(f, oid):
    s = f.snapshot(oid); o = s['order']
    sid = o.get('inpost_shipment_id')
    if not sid:
        return {'status': 'CONFLICT', 'conflicts': ['Brak istniejącej przesyłki.'], 'missing_fields': []}
    try:
        remote = f.b.inpost_get_shipment(sid)
    except Exception as exc:
        raise f.error('PROVIDER_READ_FAILED', 'Nie udało się odczytać istniejącej przesyłki u przewoźnika. Nie utworzono nowej przesyłki.') from exc
    conflicts = []
    if str(remote.get('id')) != str(sid):
        conflicts.append('Provider zwrócił inną przesyłkę.')
    target = f.effective_receiver(s)
    for member in s['package_ids']:
        other = f.snapshot(member)
        if f.effective_receiver(other) != target:
            conflicts.append('Zamówienia paczki mają różnych odbiorców lub adresy.')
        if other['order'].get('inpost_shipment_id') and str(other['order']['inpost_shipment_id']) != str(sid):
            conflicts.append('Jedno z zamówień ma inną przesyłkę.')
    receiver = remote.get('receiver') or {}
    address = receiver.get('address') or {}
    street = ' '.join(str(address.get(k) or '').strip() for k in ('street', 'building_number')).strip()
    known_receiver = {'name': receiver.get('name') or receiver.get('company_name'), 'street': street,
        'post_code': address.get('post_code'), 'city': address.get('city'), 'phone': receiver.get('phone'), 'email': receiver.get('email')}
    for key, value in known_receiver.items():
        from inpost_module import normalize_polish_phone
        normalize = normalize_polish_phone if key == 'phone' else lambda text: str(text or '').strip().casefold()
        if value and normalize(value) != normalize(target.get(key)):
            conflicts.append('Dane odbiorcy u przewoźnika nie zgadzają się z zamówieniem: ' + key)
    parcels = remote.get('parcels') or []
    if len(parcels) > 1:
        conflicts.append('Przesyłka ma wiele paczek; brak jednoznacznego zestawu wymiarów.')
    parcel = parcels[0] if parcels else {}
    dimensions = parcel.get('dimensions') or {}
    weight = parcel.get('weight') or {}
    known = {}
    for key in ('length', 'width', 'height'):
        if dimensions.get(key) is not None and dimensions.get('unit', 'mm') in {'mm', 'cm'}:
            known[key] = float(dimensions[key]) / (10 if dimensions.get('unit', 'mm') == 'mm' else 1)
    if isinstance(weight, dict) and weight.get('amount') is not None and weight.get('unit', 'kg') == 'kg':
        known['weight'] = float(weight['amount'])
    fields = dict(s['requirements'])
    for key, value in known.items():
        if key in fields and float(fields[key]) != value:
            conflicts.append('Parametry paczki różnią się od danych przewoźnika: ' + key)
        fields[key] = value
    fields.update(carrier='inpost', dimension_unit='cm', weight_unit='kg', weight_source='manual')
    for key in ('sms', 'email'):
        if isinstance(remote.get('additional_services'), list):
            fields[key] = key in remote['additional_services']
    missing = [key for key in ('length', 'width', 'height', 'weight', 'sms', 'email') if key not in fields]
    missing += ['recipient_' + k for k, v in target.items() if not v]
    if not missing:
        f._validate_requirements(fields)
    proof = {'provider': 'inpost', 'shipment_id': str(sid), 'tracking': remote.get('tracking_number') or o.get('tracking_no'),
             'order_ids': s['package_ids'],
             'service': remote.get('service') or '', 'non_standard': bool(parcel.get('is_non_standard')),
             'recipient': target, 'parameters': fields, 'missing_fields': missing, 'conflicts': conflicts,
             'status': 'CONFLICT' if conflicts else 'NEEDS_HUMAN' if missing else 'SAFE'}
    proof['fingerprint'] = f._hash({'orders': [(member, f.snapshot(member)['content_hash']) for member in s['package_ids']], 'proof': proof})
    return proof


def preview(f, name, data):
    proof = document_proof(f, data['order_id']) if name.startswith('orders.documents.') else shipment_proof(f, data['order_id'])
    return {'ok': True, 'state': f.state(data)['state'], 'preview': {k: v for k, v in proof.items() if k not in {'paths'}}}


def adopt(f, name, data, actor):
    document = name == 'orders.documents.adopt'
    proof = document_proof(f, data['order_id']) if document else shipment_proof(f, data['order_id'])
    if proof.get('status') != 'SAFE' or proof.get('fingerprint') != data['preview_fingerprint']:
        raise f.error('ADOPTION_CONFLICT', 'Dane lub dokumenty nie zgadzają się z zatwierdzonym podglądem. Sprawdź aktualny wynik.', 'CONFLICT')
    b = f.b; oid = data['order_id']
    shipment_hash = f.shipment_content(f.snapshot(oid)) if not document else ''
    verification = {k: v for k, v in proof.items() if k != 'paths'}
    verification.update(legacy_human_verified=True, verified_at=b.now_iso(), actor=actor.delegated_by_actor_id or actor.actor_id,
                        verified_source='invoice_allocations_and_meta' if document else 'provider_get_and_human')
    c = b.conn()
    try:
        c.execute('INSERT OR REPLACE INTO fulfillment_verifications VALUES(?,?,?)', (oid, 'documents' if document else 'shipment', json.dumps(verification)))
        if not document:
            fields = proof['parameters']
            payload = {'receiver': proof['recipient'], 'parcel': {k: fields[k] * (1 if k == 'weight' else 10) for k in ('length', 'width', 'height', 'weight')},
                       'service': proof['service'], 'options': {'additional_services': [k for k in ('sms', 'email') if fields[k]]}, 'order_ids': proof['order_ids']}
            payload['parcel']['non_standard'] = proof['non_standard']
            c.execute('INSERT OR REPLACE INTO fulfillment_shipping_attempts VALUES(?,?,?,?,?,?,?)',
                      (oid, 'adopted-' + proof['shipment_id'], json.dumps(payload), shipment_hash, 'SUCCESS',
                       json.dumps({'id': proof['shipment_id'], 'tracking_number': proof['tracking']}), b.now_iso()))
            c.executemany('INSERT OR REPLACE INTO fulfillment_shipping_members VALUES(?,?)', [(member, oid) for member in proof['order_ids']])
        c.commit()
    finally:
        c.close()
    if document:
        for kind, path in proof['paths'].items():
            f.save_document(oid, kind, proof['invoice_id'] if kind == 'invoice' else 0, path)
    else:
        b.persist_inpost_result(proof['order_ids'], proof['shipment_id'], str(proof['tracking'] or ''), enqueue_pickup=False)
