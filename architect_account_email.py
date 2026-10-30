"""Bounded immediate wake-up of the EXISTING worker; the DB lease owns sending.

No provider credentials or HTTP are used at import time. The durable outbox and
its scheduler remain the fallback if the process restarts or the wake-up fails.
"""
from concurrent.futures import ThreadPoolExecutor
import threading
import uuid
import os
import json
import urllib.request

_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='architect-email-now')
_slots = threading.BoundedSemaphore(4)
_lock = threading.Lock()
_running = set()


def _wake(remote, job_id):
    # Optional server-only override for installations whose existing Edge worker
    # bearer differs from the general PostgREST service credential.
    bearer = os.environ.get('PORTAL_EMAIL_WORKER_BEARER', '').strip()
    if not bearer:
        return remote('/functions/v1/portal-email', method='POST', payload={'job_id':job_id}, timeout=12)
    base = os.environ.get('SUPABASE_URL', '').rstrip('/')
    if not base.startswith('https://'):
        raise RuntimeError('WORKER_CONFIGURATION')
    request = urllib.request.Request(base+'/functions/v1/portal-email',
        data=json.dumps({'job_id':job_id}).encode('utf-8'), method='POST',
        headers={'Authorization':'Bearer '+bearer,'apikey':bearer,'Content-Type':'application/json'})
    with urllib.request.urlopen(request, timeout=12) as response:
        response.read(4096)


def delivery_state(row):
    row = row or {}
    provider = row.get('provider_status')
    status = row.get('status')
    if provider == 'delivered':
        state, label = 'delivered', 'Dostarczono'
    elif provider in {'bounced','complained','failed','suppressed'}:
        state, label = 'error', 'Błąd wysyłki'
    elif status == 'DELIVERED' and row.get('provider_id'):
        state, label = 'sent', 'Wysłano'
    elif status == 'DELIVERING':
        state, label = 'sending', 'Wysyłanie'
    elif status in {'RETRY','BLOCKED','UNKNOWN'}:
        state, label = 'error', 'Błąd wysyłki'
    else:
        state, label = 'queued', 'W kolejce'
    return {'state':state,'label':label,'provider_id':row.get('provider_id') or None,
            'queued':status in {'PENDING','RETRY'},'requires_review':status=='UNKNOWN'}


def read_delivery(remote, architect_id, job_id=None):
    params={'select':'id,status,provider_status,provider_id,last_error_code',
            'architect_id':'eq.'+str(uuid.UUID(str(architect_id))),
            'notification_type':'in.(architect_approved,architect_welcome_resend)',
            'order':'created_at.desc','limit':1}
    if job_id:
        params['id']='eq.'+str(uuid.UUID(str(job_id)))
    rows=remote('/rest/v1/architect_order_email_outbox',params=params,timeout=5) or []
    return rows[0] if rows else None


def request_attempt(remote, job_id):
    """Schedule one targeted attempt without blocking a Flask request thread.

    In-process coalescing is an optimisation only. PostgreSQL row leases and
    the original Resend idempotency key protect across processes and cron.
    """
    job_id=str(uuid.UUID(str(job_id)))
    with _lock:
        if job_id in _running:
            return True
        if not _slots.acquire(blocking=False):
            return False
        _running.add(job_id)
    def attempt():
        try:
            _wake(remote,job_id)
        except Exception:
            # Never log upstream exception bodies or credentials. A failed
            # wake-up is not proof that Resend rejected/accepted the message.
            try:
                remote('/rest/v1/rpc/portal_email_dispatch_error',method='POST',
                       payload={'p_id':job_id},timeout=5)
            except Exception:
                pass
        finally:
            with _lock:
                _running.discard(job_id)
                _slots.release()
    try:
        _pool.submit(attempt)
    except Exception:
        with _lock:
            _running.discard(job_id)
            _slots.release()
        return False
    return True
