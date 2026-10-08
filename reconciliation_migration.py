"""One-at-a-time, resumable conversion. No business rows or PDFs are deleted."""
import hashlib
import json
from pathlib import Path
import threading
import time
from contextlib import ExitStack

import reconciliation_documents as documents

_lock = threading.Lock()
_worker = None
_state = {'state': 'idle', 'converted': 0, 'unchanged': 0, 'conflicts': 0}


def convert(b, oid):
    rows = b.supabase_request('/rest/v1/fulfillment_reconciliation',
        params={'order_id': 'eq.' + str(int(oid)), 'select': 'revision,payload'})
    if not rows:
        return 'unchanged'
    record = rows[0]
    payload = record['payload']
    if not any(d.get('pdf_base64') for section in documents.SECTIONS for d in payload.get(section, [])):
        return 'unchanged'
    if not documents.enabled(b):
        raise ValueError('Magazyn dokumentów nie jest włączony.')
    wire = documents.externalize(b, payload)
    # Verify the complete semantic round trip, not just the presence of objects.
    if documents.hydrate(b, wire) != documents.hydrate(b, payload):
        raise ValueError('Konwersja zmieniła treść danych realizacji.')
    raw = json.dumps(record, ensure_ascii=False, sort_keys=True).encode('utf-8')
    digest = hashlib.sha256(raw).hexdigest()
    object_path = f'fulfillment/backups/{oid}/{record["revision"]}-{digest}.json'
    path = Path(b.DATA_DIR) / 'fulfillment-backups' / (digest + '.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    ref = b.supabase_storage_upload_file(str(path), object_path, content_type='application/json')
    if ref != b.supabase_storage_ref(object_path) or hashlib.sha256(b.supabase_storage_download_bytes(ref)[0]).hexdigest() != digest:
        raise ValueError('Nie potwierdzono kopii bezpieczeństwa danych realizacji.')
    result = b.supabase_request('/rest/v1/rpc/save_fulfillment_reconciliation', method='POST', payload={
        'p_order_id': int(oid), 'p_expected_revision': record['revision'], 'p_payload': wire})
    if not result.get('saved'):
        return 'conflicts'  # A user's concurrent write always wins; reread next run.
    b.app.logger.info('RECONCILIATION_STORAGE_MIGRATED order_id=%s revision=%s before_bytes=%s after_bytes=%s backup=%s',
        oid, result['revision'], len(json.dumps(payload).encode()), len(json.dumps(wire).encode()), object_path)
    return 'converted'


def status():
    with _lock:
        return dict(_state)


def start(b):
    global _worker
    with _lock:
        if _worker and _worker.is_alive():
            return
        _state.update(state='running', converted=0, unchanged=0, conflicts=0, error='')
        def run():
            try:
                with b.app.app_context():
                    rows = b.supabase_request('/rest/v1/fulfillment_reconciliation', params={
                        'select': 'order_id', 'order': 'order_id.asc', 'limit': '1000'})
                    for row in rows:
                        import packing_versions
                        from fulfillment_operations import ui_write
                        from werkzeug.exceptions import Conflict
                        c = b.conn()
                        try:
                            members = packing_versions.evidence_members(c, [int(row['order_id'])])
                        finally:
                            c.close()
                        try:
                            with ExitStack() as locks:
                                for oid in sorted(members):
                                    locks.enter_context(ui_write(oid))
                                c = b.conn()
                                try:
                                    pending = any(c.execute('SELECT 1 FROM fulfillment_reconciliation_pending WHERE order_id=?', (oid,)).fetchone() for oid in members)
                                finally:
                                    c.close()
                                outcome = 'conflicts' if pending else convert(b, row['order_id'])
                        except Conflict:
                            outcome = 'conflicts'
                        with _lock:
                            _state[outcome] += 1
                        time.sleep(3)  # Bounded migration, separate from invoice requests.
                with _lock:
                    _state['state'] = 'complete'
            except Exception:
                b.app.logger.exception('RECONCILIATION_STORAGE_MIGRATION_STOPPED')
                with _lock:
                    _state.update(state='stopped', error='Przerwano konwersję. Dane źródłowe i kopie pozostają zachowane.')
        _worker = threading.Thread(target=run, name='packing-storage-migration', daemon=True)
        _worker.start()
