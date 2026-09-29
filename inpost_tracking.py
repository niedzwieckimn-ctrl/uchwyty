"""Durable, shipment-scoped ShipX observations and bounded polling.

The order and packing-list transaction remains in app.apply_verified_inpost_status.
This module never creates a shipment, pickup, invoice, or stock movement.
"""
from __future__ import annotations

import os
import json
import hashlib
import re
import threading
import time
import uuid
from datetime import datetime, timezone


COLLECTED = {
    'collected_from_sender', 'taken_by_courier_from_pok', 'collected_by_courier',
    'taken_by_courier', 'adopted_at_source_branch', 'sent_from_source_branch',
    'adopted_at_sorting_center', 'sent_from_sorting_center', 'adopted_at_target_branch',
    'out_for_delivery_to_address', 'out_for_delivery', 'ready_to_pickup',
    'pickup_reminder_sent', 'delivered', 'returned_to_sender',
}
TERMINAL = {'delivered', 'returned_to_sender', 'cancelled'}
RANK = {
    'created': 0, 'offers_prepared': 0, 'offer_selected': 0, 'confirmed': 1,
    'collected_from_sender': 2, 'taken_by_courier_from_pok': 2,
    'collected_by_courier': 2, 'taken_by_courier': 2,
    'adopted_at_source_branch': 3, 'sent_from_source_branch': 4,
    'adopted_at_sorting_center': 5, 'sent_from_sorting_center': 6,
    'adopted_at_target_branch': 7, 'out_for_delivery_to_address': 8,
    'out_for_delivery': 8, 'ready_to_pickup': 9,
    'pickup_reminder_sent': 9, 'delivered': 10, 'returned_to_sender': 10,
}
LABELS = {
    'created': 'Utworzona', 'confirmed': 'Przygotowana przez nadawcę',
    'collected_from_sender': 'Odebrana od nadawcy',
    'taken_by_courier': 'Odebrana przez kuriera',
    'taken_by_courier_from_pok': 'Odebrana przez kuriera',
    'collected_by_courier': 'Odebrana przez kuriera',
    'adopted_at_source_branch': 'Przyjęta w oddziale nadawczym',
    'sent_from_source_branch': 'W drodze z oddziału nadawczego',
    'adopted_at_sorting_center': 'W sortowni',
    'sent_from_sorting_center': 'W drodze z sortowni',
    'adopted_at_target_branch': 'W oddziale docelowym',
    'out_for_delivery_to_address': 'W doręczeniu',
    'out_for_delivery': 'W doręczeniu', 'ready_to_pickup': 'Gotowa do odbioru',
    'pickup_reminder_sent': 'Oczekuje na odbiór', 'delivered': 'Doręczona',
    'returned_to_sender': 'Zwrócona do nadawcy', 'cancelled': 'Anulowana',
}
NOTIFICATION_LABELS = {
    'not_prepared': 'Nieprzygotowane', 'pending': 'Oczekuje',
    'sending': 'Wysyłka trwa; wynik niepotwierdzony',
    'accepted': 'Przyjęte przez dostawcę e-mail',
    'failed': 'Wysyłka odrzucona lub nieudana', 'unknown': 'Wynik wysyłki niepewny',
    'skipped': 'Pominięte — wiadomości nie wysłano',
}
RESULT_LABELS = {
    'never': 'Jeszcze nie sprawdzano', 'checking': 'Trwa sprawdzanie',
    'verified': 'Status odczytany', 'applied': 'Status zastosowany',
    'unchanged': 'Bez zmiany statusu', 'older_ignored': 'Pominięto starsze zdarzenie',
    'api_error': 'Błąd odczytu InPost', 'apply_error': 'Błąd zastosowania statusu',
    'applied_with_error': 'Zamówienia zapisane; dalszy etap wymaga uwagi',
    'stale_shipment': 'Zamówienie ma już inną przesyłkę',
    'scope_conflict': 'Sprzeczne dane przesyłki',
}


def normalized(value):
    return re.sub(r'[^a-z0-9]+', '_', str(value or '').strip().lower()).strip('_')


def status_label(value):
    code = normalized(value)
    return LABELS.get(code, 'Nieznany status InPost' if code else 'Jeszcze nieodświeżony')


def notification_label(value):
    return NOTIFICATION_LABELS.get(str(value or ''), NOTIFICATION_LABELS['not_prepared'])


def result_label(value):
    return RESULT_LABELS.get(str(value or ''), 'Nieznany wynik sprawdzenia')


def _utc(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _stamp():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def interval_seconds():
    return max(300, min(86400, int(os.environ.get('INPOST_TRACKING_INTERVAL_SECONDS', '900'))))


def initialize(db):
    db.executescript('''
      CREATE TABLE IF NOT EXISTS inpost_tracking_state(
        shipment_id TEXT PRIMARY KEY,
        tracking_no TEXT NOT NULL DEFAULT '',
        status_code TEXT NOT NULL DEFAULT '',
        status_event_at TEXT NOT NULL DEFAULT '',
        verified_at TEXT NOT NULL DEFAULT '',
        last_attempt_at TEXT NOT NULL DEFAULT '',
        last_result TEXT NOT NULL DEFAULT 'never',
        last_error TEXT NOT NULL DEFAULT '',
        sync_state TEXT NOT NULL DEFAULT 'none',
        sync_error TEXT NOT NULL DEFAULT '',
        next_check_at REAL NOT NULL DEFAULT 0,
        failures INTEGER NOT NULL DEFAULT 0,
        lease_token TEXT,
        lease_until REAL NOT NULL DEFAULT 0,
        terminal INTEGER NOT NULL DEFAULT 0
      );
      CREATE INDEX IF NOT EXISTS idx_inpost_tracking_due
        ON inpost_tracking_state(terminal,next_check_at,lease_until);
      CREATE TABLE IF NOT EXISTS inpost_tracking_notifications(
        shipment_id TEXT PRIMARY KEY,
        state TEXT NOT NULL,
        recipient TEXT NOT NULL DEFAULT '',
        result_text TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL,
        claim_token TEXT,
        lease_until REAL NOT NULL DEFAULT 0
      );
    ''')
    import inpost_reconciliation as work
    work.initialize(db)
    # Upgrade existing verified receipts without resetting notification claims.
    for row in db.execute('''SELECT s.*,MIN(o.id) AS root_order_id FROM inpost_tracking_state s
        JOIN orders o ON o.inpost_shipment_id=s.shipment_id
        LEFT JOIN inpost_reconciliation j ON j.shipment_id=s.shipment_id
        WHERE j.shipment_id IS NULL AND s.verified_at<>'' GROUP BY s.shipment_id''').fetchall():
        work.observe(db, row['shipment_id'], row['root_order_id'], {
            'id': row['shipment_id'], 'status': row['status_code'],
            'tracking_number': row['tracking_no'], 'status_at': row['status_event_at']})


def read(db, shipment_id):
    row = db.execute('SELECT * FROM inpost_tracking_state WHERE shipment_id=?',
                     (str(shipment_id),)).fetchone()
    return dict(row) if row else None


def notification(db, shipment_id):
    row = db.execute('SELECT * FROM inpost_tracking_notifications WHERE shipment_id=?',
                     (str(shipment_id),)).fetchone()
    return dict(row) if row else None


def notification_view(db, shipment_id):
    saved = notification(db, shipment_id)
    if saved:
        return saved
    orders = [dict(row) for row in db.execute(
        'SELECT id,tracking_no FROM orders WHERE inpost_shipment_id=? ORDER BY id',
        (str(shipment_id),))]
    if not orders:
        return None
    tracking = re.sub(r'\s+', '', orders[0]['tracking_no'] or '')
    if not tracking:
        return None
    digest = hashlib.sha256(tracking.encode('utf-8')).hexdigest()[:16]
    keys = [f"order_shipped:{row['id']}:inpost:{digest}" for row in orders]
    placeholders = ','.join('?' for _ in keys)
    events = list(db.execute(
        f'SELECT ok,result_json FROM email_events WHERE event_key IN ({placeholders})', tuple(keys)))
    if not events:
        return None
    state = _legacy_notification_state(events, len(keys))
    return {'state': state, 'result_text': 'Na podstawie wcześniejszej ewidencji e-mail'}


def _legacy_notification_state(events, expected):
    import inpost_reconciliation as work
    states = []
    for item in events:
        try:
            result = json.loads(item['result_json'] or '{}')
        except (TypeError, ValueError):
            result = {}
        if 'delivery_outcome' not in result and not result.get('skipped'):
            states.append('accepted' if item['ok'] else 'unknown')
        else:
            states.append(work.notification_outcome(result))
    return states[0] if len(states) == expected and len(set(states)) == 1 else 'unknown'


def _claim(b, shipment_id, source):
    now = time.time()
    token = uuid.uuid4().hex
    db = b.conn()
    try:
        db.execute('BEGIN IMMEDIATE')
        db.execute('INSERT OR IGNORE INTO inpost_tracking_state(shipment_id) VALUES(?)', (shipment_id,))
        row = read(db, shipment_id)
        if row['lease_until'] > now:
            db.commit()
            return None, 'busy'
        if source == 'poll' and (row['terminal'] or row['next_check_at'] > now):
            db.commit()
            return None, 'not_due'
        last = _utc(row['last_attempt_at'])
        if source == 'manual' and last and now - last.timestamp() < 30:
            db.commit()
            return None, 'recent'
        db.execute('''UPDATE inpost_tracking_state SET lease_token=?,lease_until=?,
                      last_attempt_at=?,last_result='checking',last_error=''
                      WHERE shipment_id=?''', (token, now + 90, _stamp(), shipment_id))
        db.commit()
        return token, ''
    finally:
        db.close()


def _event_at(shipment, code):
    # ShipX tracking_details contains the carrier's event time, separately
    # from the moment this application reads the API.
    details = shipment.get('tracking_details') or []
    candidates = []
    if isinstance(details, list):
        for item in details:
            if isinstance(item, dict) and normalized(item.get('status')) == code:
                parsed = _utc(item.get('datetime'))
                if parsed:
                    candidates.append(parsed)
    if candidates:
        return max(candidates).isoformat(timespec='seconds')
    for key in ('status_changed_at', 'status_at'):
        parsed = _utc(shipment.get(key))
        if parsed:
            return parsed.isoformat(timespec='seconds')
    return ''


def _older(row, code, event_at):
    old = normalized(row['status_code'])
    if not old:
        return False
    old_time, new_time = _utc(row['status_event_at']), _utc(event_at)
    if old_time and new_time:
        if new_time != old_time:
            return new_time < old_time  # A later repeat scan wins over rank.
        return old != code  # Conflicting equal timestamps do not replace evidence.
    if old == code:
        return False
    if old in TERMINAL:
        return True
    if old in RANK and code in RANK and RANK[code] < RANK[old]:
        return True
    # Unknown codes stay visible as raw diagnostics but cannot roll back a
    # physical courier scan without a newer, explicit carrier timestamp.
    if old in COLLECTED and code not in RANK and not (old_time and new_time and new_time > old_time):
        return True
    return False


def _verified(b, shipment_id, token, shipment, root_order_id=0):
    code = normalized(shipment.get('status'))
    if not code:
        raise ValueError('InPost nie zwrócił kodu statusu')
    remote_id = str(shipment.get('id') or '').strip()
    if remote_id and remote_id != shipment_id:
        raise ValueError('InPost zwrócił inną przesyłkę niż żądana')
    tracking = re.sub(r'\s+', '', str(shipment.get('tracking_number') or ''))
    event_at = _event_at(shipment, code)
    db = b.conn()
    try:
        db.execute('BEGIN IMMEDIATE')
        row = read(db, shipment_id)
        if not row or row['lease_token'] != token or row['lease_until'] <= time.time():
            db.rollback()
            return None, 'lost_lease'
        older = _older(row, code, event_at)
        selected = normalized(row['status_code']) if older else code
        db.execute('''UPDATE inpost_tracking_state SET
            tracking_no=CASE WHEN ?<>'' THEN ? ELSE tracking_no END,
            status_code=?,status_event_at=?,verified_at=?,last_result=?,
            last_error='',next_check_at=?,failures=0,terminal=?
            WHERE shipment_id=?''',
            (tracking, tracking, row['status_code'] if older else code,
             row['status_event_at'] if older or (row['status_code'] == code and not event_at) else event_at,
             row['verified_at'] if older else _stamp(),
             'older_ignored' if older else 'verified',
             time.time() + interval_seconds(), int(selected in TERMINAL), shipment_id))
        if not older:
            import inpost_reconciliation as work
            work.observe(db, shipment_id, root_order_id, {
                'id': shipment_id, 'tracking_number': tracking,
                'status': code, 'status_at': event_at})
        db.commit()
        return read(db, shipment_id), 'older_ignored' if older else ('unchanged' if row['status_code'] == code else 'changed')
    finally:
        db.close()


def _release(b, shipment_id, token):
    db = b.conn()
    try:
        db.execute('''UPDATE inpost_tracking_state SET lease_token=NULL,lease_until=0
                      WHERE shipment_id=? AND lease_token=?''', (shipment_id, token))
        db.commit()
    finally:
        db.close()


def _failed(b, shipment_id, token, error, retry_after=0):
    db = b.conn()
    try:
        db.execute('BEGIN IMMEDIATE')
        row = read(db, shipment_id)
        if row and row['lease_token'] == token and row['lease_until'] > time.time():
            failures = min(8, int(row['failures']) + 1)
            delay = max(interval_seconds(), min(21600, 60 * 2 ** failures), retry_after)
            db.execute('''UPDATE inpost_tracking_state SET last_result='api_error',last_error=?,
                failures=?,next_check_at=?,lease_token=NULL,lease_until=0
                WHERE shipment_id=?''', (str(error)[:240], failures, time.time() + delay, shipment_id))
        db.commit()
    finally:
        db.close()


def mark_result(b, shipment_id, result, error='', sync_state=None, *, token=None):
    db = b.conn()
    try:
        db.execute('BEGIN IMMEDIATE')
        import inpost_reconciliation as work
        if work.read(db, shipment_id):
            # Sync state is owned exclusively by the staged intent/its scoped ack.
            sync_state = None
            if token is None:
                return
        if token and not db.execute('''SELECT 1 FROM inpost_tracking_state
                WHERE shipment_id=? AND lease_token=? AND lease_until>?''',
                (shipment_id, token, time.time())).fetchone():
            return
        db.execute('INSERT OR IGNORE INTO inpost_tracking_state(shipment_id) VALUES(?)',
                   (shipment_id,))
        db.execute('''UPDATE inpost_tracking_state SET last_result=?,last_error=?,
            sync_state=COALESCE(?,sync_state),sync_error=CASE
                WHEN ?='synced' THEN '' WHEN ?='pending' THEN ? ELSE sync_error END
            WHERE shipment_id=?''',
            (result, str(error)[:240], sync_state, sync_state, sync_state,
             str(error)[:240], shipment_id))
        db.commit()
    finally:
        db.close()


def _public_state(state):
    if not state:
        return state
    return {key: value for key, value in state.items()
            if key not in {'lease_token', 'lease_until'}}


def _authenticated_observation(b, order, sid):
    shipment = b.inpost_get_shipment(sid)
    if not isinstance(shipment, dict) or str(shipment.get('id') or '') != sid:
        raise ValueError('InPost nie potwierdził żądanego shipment ID')
    shipment = {k: v for k, v in shipment.items() if not k.startswith('_')}
    number = re.sub(r'\s+', '', str(shipment.get('tracking_number') or ''))
    expected = re.sub(r'\s+', '', str(order.get('tracking_no') or ''))
    if expected and number != expected:
        raise ValueError('Numer śledzenia InPost nie zgadza się z bieżącym zamówieniem')
    # Shipment does not promise tracking_details. History is optional enrichment,
    # requested only after the authenticated ID/tracking association is confirmed.
    if number and not shipment.get('tracking_details'):
        try:
            history = b.inpost_get_tracking(number)
            if not isinstance(history, dict) or str(history.get('tracking_number') or '') != number:
                raise ValueError('Historia wskazuje inny numer śledzenia')
            if normalized(history.get('status')) != normalized(shipment.get('status')):
                raise ValueError('Historia i zasób Shipment mają różne statusy')
            shipment['tracking_details'] = history.get('tracking_details') or []
        except Exception:
            # updated_at / fetched-at are never physical event timestamps.
            shipment['_history_unavailable'] = True
    return shipment


def process(b, order, *, source='manual'):
    import inpost_reconciliation as work
    shipment_id = str(order.get('inpost_shipment_id') or '').strip()
    if not shipment_id:
        return {'ok': False, 'error': 'Brak bieżącego identyfikatora przesyłki'}
    token, reason = _claim(b, shipment_id, source)
    if not token:
        db = b.conn()
        try:
            return {'ok': reason in {'recent', 'not_due'}, 'result': reason,
                    'state': _public_state(read(db, shipment_id))}
        finally:
            db.close()
    try:
        try:
            if source == 'reconcile':
                db = b.conn()
                try:
                    job = work.read(db, shipment_id)
                    shipment = json.loads(job['observation'])
                    state, outcome = read(db, shipment_id), 'reconciled'
                finally:
                    db.close()
            else:
                shipment = _authenticated_observation(b, order, shipment_id)
                state, outcome = _verified(b, shipment_id, token, shipment, int(order['id']))
        except Exception as exc:
            _failed(b, shipment_id, token, exc, getattr(exc, 'retry_after', 0) or 0)
            db = b.conn()
            try:
                return {'ok': False, 'stage': 'scope' if 'śledzenia' in str(exc) else 'api',
                        'orders_applied': False, 'error': str(exc)[:240],
                        'state': _public_state(read(db, shipment_id))}
            finally:
                db.close()
        if outcome == 'lost_lease':
            return {'ok': False, 'result': outcome, 'state': None}
        if outcome == 'older_ignored' or normalized(shipment.get('status')) not in COLLECTED:
            return {'ok': True, 'result': outcome, 'state': _public_state(state), 'orders_applied': False}
        db = b.conn()
        try:
            job = work.read(db, shipment_id)
            shipment['_local_claim'] = {'sid': shipment_id, 'token': token, 'revision': job['revision']}
            fresh = db.execute('SELECT * FROM orders WHERE id=?', (int(order['id']),)).fetchone()
            if not fresh or str(fresh['inpost_shipment_id'] or '') != shipment_id:
                fresh = work.historical_member(db, int(order['id']), shipment_id, state['tracking_no'])
                if not fresh:
                    return {'ok': False, 'stage': 'scope', 'orders_applied': False,
                            'error': 'Zamówienie ma już inną bieżącą przesyłkę bez zgodnej historii'}
            fresh = dict(fresh)
        finally:
            db.close()
        try:
            if source == 'reconcile' and job['apply_state'] == 'applied':
                work.finish_notification(b, shipment_id)
                result = work.receipt(b, shipment_id)
            else:
                result = b.apply_verified_inpost_status(fresh, shipment)
        except Exception as exc:
            result = work.receipt(b, shipment_id)
            result.update(ok=False, error=str(exc)[:240])
        try:
            return _complete_process(b, shipment_id, token, job, outcome, result)
        except Exception as exc:
            # The business receipt already returned by apply must survive a
            # failure of the final diagnostic write/read as well.
            return {**result, 'ok': False, 'state': _public_state(state),
                    'error': 'Nie zapisano końcowej diagnostyki: ' + type(exc).__name__}
    finally:
        try:
            _release(b, shipment_id, token)
        except Exception:
            pass  # The durable lease expires; this never authorizes another mail.


def _complete_process(b, shipment_id, token, job, outcome, result):
        mark_result(b, shipment_id,
                    ('unchanged' if outcome == 'unchanged' else 'applied') if result.get('ok') else
                    'applied_with_error' if result.get('orders_applied') else 'apply_error',
                    result.get('error') or '', token=token)
        db = b.conn()
        try:
            db.execute("""UPDATE inpost_reconciliation SET retry_at=? WHERE shipment_id=?
                AND revision=? AND EXISTS(SELECT 1 FROM inpost_tracking_state
                WHERE shipment_id=? AND lease_token=? AND lease_until>?)""",
                (time.time() + 60, shipment_id, job['revision'], shipment_id, token, time.time()))
            db.commit()
            state = read(db, shipment_id)
        finally:
            db.close()
        return {'result': outcome, 'state': _public_state(state), **result}



def notification_claim(b, shipment_id, recipient, event_keys=()):
    db = b.conn()
    token = uuid.uuid4().hex
    try:
        db.execute('BEGIN IMMEDIATE')
        row = notification(db, shipment_id)
        if row:
            if row['state'] == 'pending':
                db.execute('''UPDATE inpost_tracking_notifications SET state='sending',
                    updated_at=?,claim_token=?,lease_until=? WHERE shipment_id=?''',
                    (_stamp(), token, time.time() + 180, shipment_id))
                db.commit()
                return token
            if row['state'] == 'sending' and row['lease_until'] < time.time():
                db.execute('''UPDATE inpost_tracking_notifications SET state='unknown',
                    result_text='Niepotwierdzony wynik poprzedniej próby',updated_at=?,
                    claim_token=NULL,lease_until=0 WHERE shipment_id=?''', (_stamp(), shipment_id))
            db.commit()
            return None
        if event_keys:
            placeholders = ','.join('?' for _ in event_keys)
            previous = list(db.execute(
                f'SELECT ok,result_json FROM email_events WHERE event_key IN ({placeholders})',
                tuple(event_keys)))
            if previous:
                state = _legacy_notification_state(previous, len(event_keys))
                db.execute('''INSERT INTO inpost_tracking_notifications
                    (shipment_id,state,recipient,result_text,updated_at)
                    VALUES(?,?,?,?,?)''', (shipment_id, state, recipient or '',
                    'Wynik odziedziczony z ewidencji wcześniejszych e-maili', _stamp()))
                db.commit()
                return None
        db.execute('''INSERT INTO inpost_tracking_notifications
            (shipment_id,state,recipient,updated_at,claim_token,lease_until)
            VALUES(?,'sending',?,?,?,?)''',
            (shipment_id, recipient or '', _stamp(), token, time.time() + 180))
        db.commit()
        return token
    finally:
        db.close()


def notification_finish(b, shipment_id, token, state, message=''):
    db = b.conn()
    try:
        db.execute('''UPDATE inpost_tracking_notifications SET state=?,result_text=?,
            updated_at=?,claim_token=NULL,lease_until=0
            WHERE shipment_id=? AND claim_token=?''',
            (state, str(message)[:240], _stamp(), shipment_id, token))
        db.commit()
    finally:
        db.close()


def protect_incoming(db, rows):
    protected = []
    for row in rows:
        row = dict(row)
        local = db.execute('SELECT * FROM orders WHERE id=?', (row.get('id'),)).fetchone()
        if local and local['inpost_shipment_id']:
            state = read(db, str(local['inpost_shipment_id']))
            if state and state['sync_state'] in {'pending', 'conflict'}:
                if str(row.get('inpost_shipment_id') or '') not in {'', str(local['inpost_shipment_id'])}:
                    db.execute('''UPDATE inpost_tracking_state SET sync_state='conflict',
                        sync_error='Supabase wskazuje inną bieżącą przesyłkę'
                        WHERE shipment_id=?''', (str(local['inpost_shipment_id']),))
                for key in ('status', 'inpost_shipment_id', 'tracking_no', 'carrier',
                            'shipped_at', 'warehouse_issued'):
                    row[key] = local[key]
        protected.append(row)
    return protected


def protected_order_ids(db):
    return {int(row[0]) for row in db.execute('''SELECT o.id FROM orders o
        JOIN inpost_tracking_state s ON s.shipment_id=o.inpost_shipment_id
        WHERE s.sync_state IN ('pending','conflict')''')}


def scope_diagnostic(db, order):
    """Read the three independent identities without attaching any order."""
    import packing_versions
    sid = str(order.get('inpost_shipment_id') or '').strip()
    if not sid:
        return {'shipment_id': '', 'error': 'Zamówienie nie ma bieżącej przesyłki'}
    linked = [dict(row) for row in db.execute(
        'SELECT id,tracking_no,status FROM orders WHERE inpost_shipment_id=? ORDER BY id',
        (sid,))]
    ids = [int(row['id']) for row in linked]
    final = db.execute('SELECT final_batch_id,tracking FROM packing_shipments WHERE shipment_key=?',
                       ('inpost:' + sid,)).fetchone()
    try:
        packing = (packing_versions.batch_result(db, final['final_batch_id'], mode='final')
                   if final else packing_versions.current_for_order(db, int(order['id'])))
        expected = sorted(int(i) for i in packing['order_ids']) if packing else []
        batch_id = int(packing['batch_id']) if packing else None
        error = '' if packing else 'Brak potwierdzonej listy pakowej'
    except Exception as exc:
        expected, batch_id, error = [], None, str(exc)[:240]
    tracking_no = re.sub(r'\s+', '', str(order.get('tracking_no') or ''))
    tracking_conflicts = [row['id'] for row in linked if tracking_no and
                          re.sub(r'\s+', '', row['tracking_no'] or '') not in {'', tracking_no}]
    historical = [int(row[0]) for row in db.execute(
        'SELECT order_id FROM inpost_shipment_history WHERE shipment_id=? ORDER BY order_id',
        (sid,))]
    import inpost_reconciliation as work
    valid_history = {oid for oid in historical if final and oid in expected
                     and work.historical_member(db, oid, sid, tracking_no)}
    return {'shipment_id': sid, 'tracking_no': tracking_no,
            'order_ids': ids, 'packing_batch_id': batch_id,
            'packing_order_ids': expected, 'historical_order_ids': historical,
            'missing_shipment_id': sorted(set(expected) - set(ids) - valid_history),
            'outside_packing_list': sorted(set(ids) - set(expected)),
            'tracking_conflict_ids': tracking_conflicts,
            'final_tracking': final['tracking'] if final else '', 'error': error}


def flush_pending(b):
    import inpost_reconciliation as work
    return work.flush(b)


def read_state_result(b, shipment_id):
    db = b.conn()
    try:
        row = read(db, shipment_id)
        return row['last_result'] if row else 'never'
    finally:
        db.close()


def due_shipments(b, limit=None):
    limit = max(1, min(100, int(limit or os.environ.get('INPOST_TRACKING_BATCH_SIZE', '20'))))
    db = b.conn()
    try:
        # One shipment is queried once regardless of how many orders share it.
        return [dict(row) for row in db.execute('''SELECT MIN(o.id) AS id,
            o.inpost_shipment_id FROM orders o
            LEFT JOIN inpost_tracking_state s ON s.shipment_id=o.inpost_shipment_id
            WHERE o.inpost_shipment_id IS NOT NULL AND o.inpost_shipment_id<>''
              AND (s.shipment_id IS NULL OR (s.terminal=0 AND s.next_check_at<=?
                  AND s.lease_until<=?))
            GROUP BY o.inpost_shipment_id ORDER BY MIN(COALESCE(s.next_check_at,0)),MIN(o.id)
            LIMIT ?''', (time.time(), time.time(), limit))]
    finally:
        db.close()


def process_due(b):
    completed = []
    spacing = max(0.1, min(60, float(os.environ.get('INPOST_TRACKING_MIN_REQUEST_SPACING_SECONDS', '1'))))
    for item in due_shipments(b):
        db = b.conn()
        try:
            row = db.execute('SELECT * FROM orders WHERE id=?', (item['id'],)).fetchone()
            order = dict(row) if row else None
        finally:
            db.close()
        if order and str(order.get('inpost_shipment_id') or '') == item['inpost_shipment_id']:
            completed.append(process(b, order, source='poll'))
            time.sleep(spacing)
    # Carrier terminal flags stop network polling only. Local receipts remain
    # eligible, including when every member has progressed to a later parcel.
    db = b.conn()
    try:
        pending = [dict(row) for row in db.execute("""SELECT j.*,s.tracking_no FROM inpost_reconciliation j
            JOIN inpost_tracking_state s ON s.shipment_id=j.shipment_id
            LEFT JOIN inpost_tracking_notifications n ON n.shipment_id=j.shipment_id
            WHERE (j.apply_state='pending' OR (j.apply_state='applied' AND (n.shipment_id IS NULL OR n.state='pending')))
            AND (j.retry_at<=? OR s.next_check_at=0) AND s.lease_until<=?
            ORDER BY j.retry_at LIMIT 20""", (time.time(), time.time()))]
        db.execute("""UPDATE inpost_tracking_notifications SET state='unknown',
            result_text='Niepotwierdzony wynik poprzedniej próby; sprawdź ewidencję',
            claim_token=NULL,lease_until=0 WHERE state='sending' AND lease_until<=?""", (time.time(),))
        db.commit()
    finally:
        db.close()
    for job in pending:
        completed.append(process(b, {'id': job['root_order_id'], 'inpost_shipment_id': job['shipment_id'],
                                    'tracking_no': job['tracking_no']}, source='reconcile'))
    flush_pending(b)
    return completed


_worker_lock = threading.Lock()
_worker = None


def start_worker(b):
    global _worker
    if os.environ.get('INPOST_TRACKING_WORKER', '1') != '1' or not b.inpost_config_summary()['configured']:
        return
    with _worker_lock:
        if _worker and _worker.is_alive():
            return
        def run():
            while True:
                try:
                    with b.app.app_context():
                        process_due(b)
                except Exception:
                    b.app.logger.exception('Okresowe sprawdzanie statusów InPost nie powiodło się')
                time.sleep(60)
        _worker = threading.Thread(target=run, name='inpost-tracking', daemon=True)
        _worker.start()
