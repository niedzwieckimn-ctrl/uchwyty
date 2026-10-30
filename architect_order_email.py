"""Durable remote order confirmations; delivery never depends on SQLite cache.

Migration 010 enqueues immutable snapshots in the order transaction. Only the
post-fork worker may deliver them. Missing configuration leaves the jobs queued;
ambiguous provider outcomes require reconciliation and are never blindly resent.
"""
from __future__ import annotations

import threading
import uuid

import email_module

_worker = None
_worker_lock = threading.Lock()


def order_status(remote, order_id: int) -> dict:
    """Report confirmed durable state, including unavailable/absent queues."""
    try:
        rows = remote('/rest/v1/architect_order_email_outbox', params={
            'select': 'status,last_error_code', 'order_id': f'eq.{int(order_id)}',
            'limit': 1,
        }, timeout=5)
    except Exception:
        return {'ok': False, 'queued': None, 'pending_retry': False, 'status': 'unavailable'}
    if not isinstance(rows, list) or not rows:
        return {'ok': False, 'queued': False, 'pending_retry': False, 'status': 'not_queued'}
    state = rows[0].get('status')
    pending = state in {'PENDING', 'RETRY', 'DELIVERING'}
    status = str(state or 'UNKNOWN').lower()
    if state in {'PENDING', 'RETRY'} and not email_module.email_config_summary().get('configured'):
        status = 'awaiting_configuration'
    return {'ok': state == 'DELIVERED', 'queued': True, 'pending_retry': pending,
            'status': status, 'requires_review': state in {'UNKNOWN', 'BLOCKED'}}


def _outcome(result: dict) -> tuple[str, str, str]:
    """Persist bounded codes only, never raw provider bodies, headers or secrets."""
    if result.get('ok'):
        body = result.get('body')
        provider_id = str(body.get('id') or '')[:128] if isinstance(body, dict) else ''
        return 'DELIVERED', '', provider_id
    if result.get('delivery_outcome') == 'unknown':
        return 'UNKNOWN', 'PROVIDER_OUTCOME_UNKNOWN', ''
    if result.get('status') == 429:
        return 'RETRY', 'PROVIDER_RATE_LIMITED', ''
    if result.get('skipped'):
        return 'RETRY', 'PROVIDER_NOT_READY', ''
    if not result.get('status'):
        return 'UNKNOWN', 'PROVIDER_OUTCOME_UNKNOWN', ''
    return 'BLOCKED', 'PROVIDER_REJECTED', ''


def process_batch(backend, *, limit: int = 2) -> dict:
    """One bounded batch; claiming is serialized in PostgreSQL, across workers."""
    summary = {'claimed': 0, 'accepted': 0, 'status': 'ready'}
    if not email_module.email_config_summary().get('configured'):
        return {**summary, 'status': 'awaiting_configuration'}
    for _ in range(max(0, min(int(limit), 5))):
        owner = str(uuid.uuid4())
        job = backend.supabase_request('/rest/v1/rpc/architect_email_claim', method='POST',
            payload={'p_owner': owner}, timeout=10)
        if not job:
            break
        summary['claimed'] += 1
        snapshot = job.get('payload') or {}
        try:
            result = email_module.send_order_confirmation(
                snapshot['order'], snapshot['items'],
                idempotency_key=job['idempotency_key'],
                recipients_override=[job['recipient']],
            )
            if not isinstance(result, dict):
                result = {'delivery_outcome': 'unknown'}
        except Exception:
            # The exception could occur after the provider accepted the message.
            result = {'delivery_outcome': 'unknown'}
        outcome, error_code, provider_id = _outcome(result)
        finished = backend.supabase_request('/rest/v1/rpc/architect_email_finish', method='POST',
            payload={'p_id': job['id'], 'p_owner': owner, 'p_status': outcome,
                     'p_error_code': error_code, 'p_provider_id': provider_id}, timeout=10)
        if finished is not True:
            raise RuntimeError('Email outcome was not durably acknowledged')
        if outcome == 'DELIVERED':
            summary['accepted'] += 1
    return summary


def start_worker(backend) -> bool:
    """Called by Gunicorn post_worker_init only, never during app import."""
    global _worker
    if not backend.supabase_enabled() or not email_module.email_config_summary().get('configured'):
        return False
    with _worker_lock:
        if _worker is not None and _worker.is_alive():
            return False
        def run():
            stop = threading.Event()
            # At most two messages per minute; configuration is rechecked each
            # batch. No tight polling or HTTP work on the request thread.
            while not stop.wait(60):
                try:
                    process_batch(backend)
                except Exception as exc:
                    backend.app.logger.warning('ARCHITECT_EMAIL_WORKER unavailable type=%s', type(exc).__name__)
        _worker = threading.Thread(target=run, name='architect-order-email', daemon=True)
        _worker.start()
        return True
