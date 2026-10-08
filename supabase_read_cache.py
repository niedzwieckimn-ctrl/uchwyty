"""Bounded, page-scoped refresh of an already bootstrapped local database.

This is a GET cache only. Cold bootstraps and blocking refreshes before writes
continue to use the complete authoritative snapshot and existing write guards.
No remote schema, timestamp contract, or deletion inference is required.
"""
import json
import re
import threading
import time


FLOW = {'orders', 'order_items', 'invoices', 'invoice_meta', 'invoice_allocations'}
CATALOG = {'products', 'stock', 'pricing', 'pricing_eur'}
INCOMING = {'china_packages', 'china_items'}
FINANCE = {'orders', 'invoices', 'invoice_meta', 'ksef_documents'}
IMAGES = {'product_images', 'product_image_assignments'}


def plan(request):
    """Return table names and optional row filters; None is a safe fallback."""
    path = request.path.rstrip('/') or '/'
    exact = {
        '/': FLOW | CATALOG | INCOMING | {'cash_flow_settings'},
        '/company': {'company_profile'},
        '/customers': {'customers'},
        '/pricing': {'pricing', 'pricing_eur'},
        '/products': CATALOG,
        '/stock': FLOW | CATALOG | INCOMING | IMAGES | {'cash_flow_settings'},
        '/invoices': FINANCE,
        '/ksef': FINANCE,
        '/payments/overdue': FINANCE | {'company_profile', 'customers'},
        '/api/client_invoices': FINANCE,
        '/api/order_lookup': FLOW | CATALOG | INCOMING,
        '/orders': FLOW | CATALOG | INCOMING | {'customers'},
        '/orders/new': CATALOG | {'customers'},
        '/orders/stock-issue-audit': FLOW | {'products', 'stock'},
    }
    if path in exact:
        return exact[path], {}
    label = re.fullmatch(r'/orders/(\d+)/inpost/label', path)
    if label:
        return {'orders'}, {'orders': {'id': 'eq.' + label[1]}}
    if re.fullmatch(r'/api/invoices/\d+/download', path):
        return {'orders', 'invoices', 'invoice_meta'}, {}
    if re.fullmatch(r'/api/client/orders/\d+/pdf(?:-retail)?', path):
        return CATALOG | {'orders', 'order_items', 'customers', 'company_profile'}, {}
    if path.startswith('/orders/by-code/'):
        return {'orders'}, {}
    if re.fullmatch(r'/orders/\d+/(?:packing-correction|packing-withdraw|packing-documents)/[0-9a-f]{64}(?:/print)?', path):
        return FLOW | {'customers', 'company_profile'}, {}
    order = re.fullmatch(r'/orders/\d+(?:/(invoice|inpost|packing-list|proforma))?', path)
    if order:
        # Packing/invoicing can combine several orders: never filter their
        # membership down to just the order in the URL.
        tables = FLOW | CATALOG | {'customers', 'company_profile'}
        if order[1] is None:
            tables |= INCOMING | {'ksef_documents'}
        return tables, {}
    return None, {}


def _state(b):
    """Caller holds the backend sync lock; state never survives DB replacement."""
    context = b._scoped_context()
    state = b._supabase_sync_state.get('read_cache')
    if state is None or state['context'] != context:
        state = {'context': context, 'finished': {}, 'failures': {},
                 'retry_after': {}, 'queue': {}, 'running': False}
        b._supabase_sync_state['read_cache'] = state
    return state


def _key(table, filters):
    return table, json.dumps(filters or {}, sort_keys=True, separators=(',', ':'))


def _due(b, state, table, filters, now):
    key = _key(table, filters)
    if now < state['retry_after'].get(key, 0):
        return False
    # A full table satisfies a narrower read, never the other way around.
    finished = max(state['finished'].get(key, 0),
                   state['finished'].get(_key(table, {}), 0))
    ttl = max(1, b.SUPABASE_BACKGROUND_PULL_INTERVAL_SEC)
    return not finished or now - finished >= ttl


def record(b, result, filters=None):
    """Only committed upserts establish freshness, including empty results."""
    filters = filters or {}
    now = time.time()
    with b._supabase_sync_lock:
        state = _state(b)
        for table, item in result.get('tables', {}).items():
            key = _key(table, filters.get(table))
            if item.get('status') == 'ok':
                state['finished'][key] = now
                state['failures'].pop(key, None)
                state['retry_after'].pop(key, None)
            elif item.get('status') == 'error':
                failures = min(5, state['failures'].get(key, 0) + 1)
                state['failures'][key] = failures
                state['retry_after'][key] = now + min(900, 60 * 2 ** (failures - 1))


def schedule(b, request):
    if not b.supabase_enabled():
        return False, 'not_configured'
    tables, filters = plan(request)
    if tables is None:
        # Unknown routes retain the old completeness guarantee, but share the
        # per-table cache. Logging makes new unscoped callers discoverable.
        tables = {table for table, _ in b.SUPABASE_PULL_TABLES}
        b.app.logger.warning('SUPABASE_READ_SCOPE_FALLBACK endpoint=%s', request.endpoint)
    specs = tuple((table, key) for table, key in b.SUPABASE_PULL_TABLES if table in tables)
    reason = 'GET ' + request.path
    queue_key = (specs, json.dumps(filters, sort_keys=True))
    with b._supabase_sync_lock:
        state = _state(b)
        if not any(_due(b, state, table, filters.get(table), time.time()) for table, _ in specs):
            return False, 'throttled'
        state['queue'][queue_key] = (specs, filters, reason)
        if state['running']:
            return False, 'already_running'
        state['running'] = True

    def job():
        try:
            while True:
                with b._supabase_sync_lock:
                    if _state(b) is not state:
                        return
                    if not state['queue']:
                        state['running'] = False
                        return
                    _, (wanted, row_filters, why) = state['queue'].popitem()
                started = time.perf_counter()
                with b._supabase_full_io_lock:
                    # A preceding bootstrap/write/read may already have fetched
                    # these rows while this job waited. Recheck under the lock.
                    with b._supabase_sync_lock:
                        if _state(b) is not state:
                            return
                        due = tuple((table, key) for table, key in wanted
                                    if _due(b, state, table, row_filters.get(table), time.time()))
                    if not due:
                        continue
                    try:
                        result = b.pull_shared_tables_from_supabase(
                            force=True, delete_missing=False, tables=due, filters=row_filters)
                        if result.get('ok'):
                            # Global reconciliation can WRITE statuses. Do not
                            # run it after refreshing only an invoice or label.
                            with b._supabase_sync_lock:
                                now = time.time()
                                flow_fresh = all(
                                    state['finished'].get(_key(table, {}), 0) > 0
                                    and now - state['finished'][_key(table, {})]
                                    < max(1, b.SUPABASE_BACKGROUND_PULL_INTERVAL_SEC)
                                    and _key(table, {}) not in state['failures']
                                    for table in FLOW)
                            if FLOW <= {table for table, _ in wanted} and not row_filters and flow_fresh:
                                b._run_post_pull_reconciliation()
                            b.invoice_payment_sync.flush_pending(b, limit=3)
                            import inpost_tracking
                            inpost_tracking.flush_pending(b, limit=1)
                    except Exception as exc:
                        result = {'ok': False, 'error_type': type(exc).__name__,
                                  'tables': {table: {'status': 'error'} for table, _ in due}}
                        record(b, result, row_filters)
                    result.update(reason=why, scoped=True,
                                  total_ms=round((time.perf_counter() - started) * 1000, 2))
                    b.app.logger.info('SUPABASE_SCOPED_READ %s', json.dumps(result, sort_keys=True))
        finally:
            with b._supabase_sync_lock:
                # A new worker may start after the queue was drained. Do not
                # clobber its running flag in this old worker's finally block.
                if _state(b) is state and state.get('worker') is worker:
                    state['running'] = False

    worker = threading.Thread(target=job, daemon=True)
    with b._supabase_sync_lock:
        state['worker'] = worker
    worker.start()
    return True, 'started'
