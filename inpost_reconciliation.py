"""Shipment work receipts and immutable sync intents on the existing SQLite DB.

Business writes, packing evidence and stage() share one transaction. Cloud work
publishes that snapshot, never a fresh selection of arbitrary current members.
Payment outbox order snapshots are advanced in that same transaction. Packing
evidence still uses reconciliation_store's existing revision/ack protocol.
"""
import json
from pathlib import Path
import time
import uuid

FIELDS = ('status', 'tracking_no', 'inpost_shipment_id', 'carrier', 'shipped_at')


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def initialize(db):
    sql = (Path(__file__).parent / 'migrations/inpost_reconciliation_sqlite.sql').read_text(encoding='utf-8')
    for statement in sql.split(';'):
        if statement.strip():
            db.execute(statement)


def read(db, sid):
    row = db.execute('SELECT * FROM inpost_reconciliation WHERE shipment_id=?', (sid,)).fetchone()
    return dict(row) if row else None


def observe(db, sid, root, observation):
    import inpost_tracking as tracking
    if tracking.normalized(observation['status']) not in tracking.COLLECTED:
        return
    payload = encoded(observation)
    old = read(db, sid)
    if not old or old['observation'] != payload:
        db.execute('''INSERT INTO inpost_reconciliation(shipment_id,root_order_id,revision,observation,apply_state)
            VALUES(?,?,1,?,'pending') ON CONFLICT(shipment_id) DO UPDATE SET
            root_order_id=excluded.root_order_id,revision=revision+1,
            observation=excluded.observation,apply_state='pending',retry_at=0''', (sid, root, payload))


def require_claim(db, shipment):
    claim = shipment.get('_local_claim')
    if not claim:
        return  # Existing direct, authenticated business callers.
    row = db.execute('''SELECT 1 FROM inpost_tracking_state s JOIN inpost_reconciliation j
        ON j.shipment_id=s.shipment_id WHERE s.shipment_id=? AND s.lease_token=?
        AND s.lease_until>? AND j.revision=?''',
        (claim['sid'], claim['token'], time.time(), claim['revision'])).fetchone()
    if not row:
        raise ValueError('Utracona dzierżawa lub nowsza wersja odczytu InPost; pominięto stary zapis')


def historical_member(db, oid, sid, tracking):
    """Caller must additionally prove membership of the immutable final batch."""
    row = db.execute('SELECT snapshot_json FROM inpost_shipment_history WHERE order_id=? AND shipment_id=?',
                     (oid, sid)).fetchone()
    if not row:
        return None
    try:
        saved = json.loads(row[0])
        if (int(saved['id']) == int(oid) and str(saved['inpost_shipment_id']) == sid
                and ''.join(str(saved.get('tracking_no') or '').split()) == tracking
                and str(saved.get('carrier') or '').lower() == 'inpost'):
            return saved
    except (TypeError, ValueError, KeyError):
        pass
    return None


def stage(b, db, sid, root, batch, members, current, before, mail_orders, shipment):
    import invoice_payment_sync
    import inpost_tracking as tracking
    if not db.in_transaction:
        raise ValueError('InPost intent requires the business transaction')
    require_claim(db, shipment)
    db.execute('INSERT OR IGNORE INTO inpost_reconciliation(shipment_id,root_order_id) VALUES(?,?)', (sid, root))
    old = read(db, sid)
    payload = {'shipment_id': sid, 'final_batch_id': batch, 'member_ids': sorted(members), 'orders': []}
    # Stable preconditions on retries; never replace a pending intent by live data.
    previous = json.loads(old['sync_payload'])
    old_orders = {r['id']: r for r in previous.get('orders', [])}
    for row in current:
        values = {field: row[field] for field in FIELDS}
        prior = old_orders.get(row['id'])
        expected = prior['expected'] if prior and prior['values'] == values else {
            'status': before[row['id']]['status'],
            'inpost_shipment_id': before[row['id']]['inpost_shipment_id'] or '',
            'warehouse_issued': int(before[row['id']]['warehouse_issued'] or 0),
        }
        payload['orders'].append({'id': row['id'], 'values': values, 'expected': expected})
        invoice_payment_sync.refresh_order_snapshot(db, row['id'])
    data = encoded(payload)
    changed = data != old['sync_payload']
    db.execute('''UPDATE inpost_reconciliation SET apply_state='applied',applied_revision=revision,
        applied_at=?,retry_at=0,sync_revision=sync_revision+?,sync_payload=?,
        notification_payload=CASE WHEN notification_payload='{}' THEN ? ELSE notification_payload END
        WHERE shipment_id=?''', (tracking._stamp(), int(changed), data,
        encoded({'batch_id': batch, 'tracking_no': shipment.get('tracking_number') or '', 'orders': mail_orders}), sid))
    db.execute('INSERT OR IGNORE INTO inpost_tracking_state(shipment_id) VALUES(?)', (sid,))
    if b.supabase_enabled() and changed:
        db.execute('''UPDATE inpost_tracking_state SET sync_state=CASE WHEN sync_state='conflict'
            THEN sync_state ELSE 'pending' END WHERE shipment_id=?''', (sid,))


def _claim_sync(b, sid):
    db = b.conn()
    try:
        db.execute('BEGIN IMMEDIATE')
        job = read(db, sid)
        state = db.execute('SELECT sync_state FROM inpost_tracking_state WHERE shipment_id=?', (sid,)).fetchone()
        if not job or not state or state[0] != 'pending' or job['sync_until'] > time.time():
            return None
        token = uuid.uuid4().hex
        db.execute('UPDATE inpost_reconciliation SET sync_token=?,sync_until=? WHERE shipment_id=?',
                   (token, time.time() + 180, sid))
        db.commit()
        return {**job, 'sync_token': token}
    finally:
        db.close()


def _owns_sync(db, claim):
    return db.execute('''SELECT 1 FROM inpost_reconciliation j JOIN inpost_tracking_state s
        ON s.shipment_id=j.shipment_id WHERE j.shipment_id=? AND sync_revision=? AND sync_payload=?
        AND sync_token=? AND sync_until>? AND s.sync_state='pending' ''',
        (claim['shipment_id'], claim['sync_revision'], claim['sync_payload'], claim['sync_token'], time.time())).fetchone()


def _finish_sync(b, claim, error='', conflict=False):
    db = b.conn()
    try:
        db.execute('BEGIN IMMEDIATE')
        owned = _owns_sync(db, claim)
        if owned:
            db.execute('UPDATE inpost_tracking_state SET sync_state=?,sync_error=? WHERE shipment_id=?',
                ('conflict' if conflict else 'pending' if error else 'synced', error[:240], claim['shipment_id']))
        db.execute('''UPDATE inpost_reconciliation SET sync_token=NULL,sync_until=0
            WHERE shipment_id=? AND sync_token=?''', (claim['shipment_id'], claim['sync_token']))
        db.commit()
        return bool(owned and not error)
    finally:
        db.close()


class SyncConflict(ValueError):
    pass


def publish(b, claim):
    import packing_versions
    payload = json.loads(claim['sync_payload'])
    sid = claim['shipment_id']
    db = b.conn()
    try:
        if not _owns_sync(db, claim):
            raise ValueError('Nieaktualna wersja synchronizacji')
    finally:
        db.close()
    # Publish the evidence already staged with the business write first.
    packing_versions.sync_evidence(b, payload['member_ids'])
    for item in payload['orders']:
        db = b.conn()
        try:
            # Serialize each bounded PATCH with local writers and lease takeover.
            # No lock spans the entire group or evidence publication.
            db.execute('BEGIN IMMEDIATE')
            if not _owns_sync(db, claim):
                raise ValueError('Nieaktualna wersja synchronizacji')
            local = db.execute('SELECT * FROM orders WHERE id=?', (item['id'],)).fetchone()
            if local and str(local['inpost_shipment_id'] or '') != sid and historical_member(
                    db, item['id'], sid, item['values']['tracking_no']):
                db.commit()
                continue  # Proven later parcel: never write the old parcel back.
            if not local or any(local[k] != v for k, v in item['values'].items()):
                raise SyncConflict('Bieżące zamówienie zmieniono po zapisaniu obowiązku synchronizacji')
            expected = item['expected']
            b.supabase_update_rows('orders', item['values'], {
                'id': item['id'],
                'inpost_shipment_id': list(dict.fromkeys([expected['inpost_shipment_id'], sid])),
                'status': list(dict.fromkeys([expected['status'], item['values']['status']])),
                'warehouse_issued': expected['warehouse_issued'],
            })
            db.commit()
        finally:
            db.close()


def flush(b, shipment_id=None):
    if not b.supabase_enabled():
        return 0
    db = b.conn()
    try:
        sids = [row[0] for row in db.execute('''SELECT s.shipment_id FROM inpost_tracking_state s
            JOIN inpost_reconciliation j ON j.shipment_id=s.shipment_id
            WHERE s.sync_state='pending' AND (? IS NULL OR s.shipment_id=?)
            ORDER BY s.verified_at LIMIT 20''', (shipment_id, shipment_id))]
    finally:
        db.close()
    done = 0
    for sid in sids:
        claim = _claim_sync(b, sid)
        if not claim:
            continue
        try:
            publish(b, claim)
        except Exception as exc:
            _finish_sync(b, claim, str(exc), isinstance(exc, SyncConflict))
        else:
            done += int(_finish_sync(b, claim))
    return done


def notification_outcome(result):
    if result.get('skipped') or result.get('delivery_outcome') == 'skipped':
        return 'skipped'
    return {'accepted': 'accepted', 'unknown': 'unknown', 'rejected': 'failed'}.get(
        result.get('delivery_outcome'), 'accepted' if result.get('ok') else 'failed')


def persist_notification(b, order_ids):
    import reconciliation_store
    import packing_versions
    # Retry the exact pending payload / lost acknowledgement before staging a
    # new receipt. This preserves the revision protocol on a network timeout.
    for oid in order_ids:
        reconciliation_store.retry_pending(b, oid)
    db = b.conn()
    try:
        db.execute('BEGIN IMMEDIATE')
        packing_versions.stage_evidence(b, db, order_ids)
        db.commit()
    finally:
        db.close()
    packing_versions.sync_evidence(b, order_ids)


def finish_notification(b, sid):
    import hashlib
    import inpost_tracking as tracking
    import packing_versions
    db = b.conn()
    try:
        job = read(db, sid)
    finally:
        db.close()
    if not job or job['apply_state'] != 'applied':
        return
    # A durable final proof must exist before a potentially irreversible mail.
    # After loss of SQLite it also prevents treating an unknown old send as new.
    if b.supabase_enabled() and receipt(b, sid)['sync_state'] != 'synced':
        return
    payload = json.loads(job['notification_payload'])
    orders = payload.get('orders', [])
    if not orders:
        return
    number = payload['tracking_no']
    digest = hashlib.sha256(number.encode('utf-8')).hexdigest()[:16]
    keys = [f"order_shipped:{row['id']}:inpost:{digest}" for row in orders]
    token = tracking.notification_claim(b, sid, orders[0].get('customer_email'), keys)
    if not token:
        if b.supabase_enabled() and job['receipt_error']:
            persist_notification(b, [row['id'] for row in orders])
            db = b.conn()
            try:
                db.execute("UPDATE inpost_reconciliation SET receipt_error='' WHERE shipment_id=?", (sid,))
                db.commit()
            finally:
                db.close()
        return
    attempted = False
    try:
        path, _ = packing_versions.document(b, {'batch_id': payload['batch_id'], 'mode': 'historical'})
        attachment = {'filename': 'lista_pakowa.pdf', 'content': Path(path).read_bytes()}
        attempted = True
        result = b._send_orders_shipped_email(orders, number, 'inpost', attachment)
    except Exception as exc:
        if not attempted:
            tracking.notification_finish(b, sid, token, 'pending',
                                         'Nie przygotowano załącznika; automat ponowi przygotowanie')
            return
        result = {'ok': False, 'error': type(exc).__name__,
                  'delivery_outcome': 'unknown' if attempted else 'skipped', 'skipped': not attempted}
    outcome = notification_outcome(result)
    result = dict(result, ok=outcome == 'accepted', attempted=outcome != 'skipped',
                  delivery_outcome={'failed': 'rejected'}.get(outcome, outcome))
    # Persist adapter receipts before finalizing the reservation. A receipt DB
    # failure keeps the reservation; no second send is authorized by that failure.
    errors = []
    for row, key in zip(orders, keys):
        try:
            if b._record_email_event(key, 'order_shipped', row['id'], row.get('customer_email'), result) is False:
                errors.append('email_events_write_failed')
        except Exception as exc:
            errors.append(type(exc).__name__)
    try:
        tracking.notification_finish(b, sid, token, outcome, result.get('error') or '')
    except Exception as exc:
        errors.append(type(exc).__name__)
        # Independent best-effort receipt. If storage is fully unavailable the
        # pre-send 'sending' reservation itself remains durable and blocks retry.
        db = b.conn()
        try:
            db.execute("UPDATE inpost_tracking_notifications SET state='unknown',result_text=?,lease_until=0 WHERE shipment_id=? AND claim_token=?",
                ('Wiadomość mogła zostać przyjęta; nie zapisano potwierdzenia próby', sid, token))
            db.commit()
        finally:
            db.close()
    if b.supabase_enabled():
        try:
            persist_notification(b, [row['id'] for row in orders])
        except Exception as exc:
            # Final shipment proof was already published. If this receipt cannot
            # be saved, a cold restart restores unknown and never sends again.
            errors.append('notification_durability_' + type(exc).__name__)
    if errors:
        db = b.conn()
        try:
            db.execute('UPDATE inpost_reconciliation SET receipt_error=? WHERE shipment_id=?',
                       ('Nie zapisano kompletnego potwierdzenia e-mail: ' + ','.join(errors), sid))
            db.commit()
        finally:
            db.close()


def receipt(b, sid):
    import inpost_tracking as tracking
    db = b.conn()
    try:
        job = read(db, sid)
        state = tracking.read(db, sid) or {}
        notice = tracking.notification(db, sid) or {}
    finally:
        db.close()
    applied = bool(job and job['apply_state'] == 'applied' and job['applied_revision'] == job['revision'])
    sync = state.get('sync_state', 'none')
    notified = notice.get('state', 'pending')
    receipt_error = (job or {}).get('receipt_error', '')
    stage = ('apply' if not applied else 'sync' if sync in {'pending', 'conflict'} else
             'notification' if notified != 'accepted' or receipt_error else 'complete')
    if stage == 'complete':
        error = ''
    elif stage == 'sync':
        error = state.get('sync_error') or 'supabase_sync_' + sync
    else:
        error = receipt_error or ('apply_pending' if stage == 'apply' else 'notification_' + notified)
    return {'ok': stage == 'complete', 'orders_applied': applied, 'sync_state': sync,
            'notification_state': notified, 'stage': stage,
            'error': error}
