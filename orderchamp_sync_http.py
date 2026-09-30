"""Admin controls + a bounded server worker; independent of the browser lifetime."""
import hashlib
import hmac
import os
import threading
import time
from flask import Blueprint, jsonify, render_template, request
from orderchamp_http import _admin_only
from orderchamp_client import OrderchampError
from orderchamp_incremental_client import IncrementalClient
from orderchamp_sync_engine import SyncEngine


def mirror_import_rows(db,result,protect_orders=lambda db,rows:rows):
    """One SQLite transaction; do not call the legacy helper that closes its DB."""
    order_ids=set(result['order_ids'])
    if order_ids!={r['id'] for r in result.get('orders',[])}:
        raise OrderchampError('INVALID_IMPORT_RESULT')
    def upsert(table,rows):
        columns=[r[1] for r in db.execute(f'PRAGMA table_info({table})')]
        for row in rows:
            cols=[c for c in columns if c in row]
            changes=','.join(f'{c}=excluded.{c}' for c in cols if c!='id')
            statement=f"INSERT INTO {table}({','.join(cols)}) VALUES({','.join('?' for _ in cols)}) ON CONFLICT(id) DO UPDATE SET {changes}"
            db.execute(statement,[row[c] for c in cols])
    db.execute('BEGIN IMMEDIATE')
    try:
        orders=protect_orders(db,result.get('orders',[]))
        upsert('customers',result.get('customers',[]))
        upsert('orders',orders)
        for oid in order_ids:
            incoming={r['id'] for r in result.get('order_items',[]) if r['order_id']==oid}
            if not incoming: raise OrderchampError('INVALID_IMPORT_RESULT')
            old={r[0] for r in db.execute('SELECT id FROM order_items WHERE order_id=?',(oid,))}
            for removed in old-incoming:
                if db.execute('SELECT 1 FROM invoice_allocations WHERE order_item_id=? LIMIT 1',(removed,)).fetchone():
                    raise OrderchampError('LOCAL_IMPORT_CONFLICT')
                db.execute('DELETE FROM order_items WHERE id=?',(removed,))
        if any(r['order_id'] not in order_ids for r in result.get('order_items',[])):
            raise OrderchampError('INVALID_IMPORT_RESULT')
        upsert('order_items',result.get('order_items',[]))
        db.commit()
    except Exception:
        db.rollback();raise


class SupabaseStore:
    def __init__(self,request_fn): self.request=request_fn

    def rpc(self,name,payload,timeout=8):
        try:
            return self.request('/rest/v1/rpc/'+name,method='POST',payload=payload,timeout=timeout)
        except Exception as exc:
            # Categorise known local RPC errors without exposing SQL/PII/credentials.
            code=next((c for c in ('ORDER_ALREADY_IN_FULFILLMENT','LEASE_LOST','JOB_PAUSED',
                'ORDERCHAMP_REVIEW_REQUIRED','WAIT_BEFORE_RECONCILIATION','JOB_RUNNING','NO_UNCERTAIN_WRITE',
                'PAUSE_BEFORE_SKU_TEST','ORDER_SKU_NOT_FOUND') if c in str(exc)),'SYNC_DATABASE_UNAVAILABLE')
            raise OrderchampError(code) from None

    def control(self,action,token=None,data=None):
        return self.rpc('orderchamp_job_v2',{'p_action':action,'p_token':token,'p_data':data or {}},timeout=3 if action=='wake' else 8)

    def availability(self):
        value=self.rpc('orderchamp_availability_v2',{})
        if not isinstance(value,dict) or not isinstance(value.get('rows'),list):
            raise OrderchampError('INVALID_AVAILABILITY_SNAPSHOT')
        return value

    def import_orders(self,token,orders):
        value=self.rpc('orderchamp_import_v2',{'p_token':token,'p_orders':orders})
        if not isinstance(value,dict) or not isinstance(value.get('order_ids'),list):
            raise OrderchampError('INVALID_IMPORT_RESULT')
        return value


class Worker:
    def __init__(self,engine,enabled,logger):
        self.engine,self.enabled,self.logger=engine,enabled,logger
        self.lock=threading.Lock()
        self.thread=None
        self.pid=None
        self.wake_event=threading.Event()
        self.retry_at=0

    def start(self):
        if not self.enabled(): return
        with self.lock:
            if self.pid==os.getpid() and self.thread and self.thread.is_alive(): return
            self.pid=os.getpid()
            self.thread=threading.Thread(target=self.loop,name='orderchamp-sync',daemon=True)
            self.thread.start()

    def wake(self):
        self.start()
        self.wake_event.set()

    def loop(self):
        while self.enabled():
            # Repeated browser/webhook wakeups never bypass outage backoff.
            delay=max(0,self.retry_at-time.monotonic())
            if delay:
                self.wake_event.wait(min(delay,60)); self.wake_event.clear()
                continue
            try:
                result=self.engine.run()
                failed=bool(result.get('error'))
            except Exception:
                failed=True
            if failed:
                self.logger.warning('ORDERCHAMP_SYNC_PAUSED_AFTER_ERROR')
                self.retry_at=time.monotonic()+300
            self.wake_event.wait(60)
            self.wake_event.clear()


def register_routes(app,backend,*,store=None,client_factory=None):
    store=store or SupabaseStore(backend['supabase_request'])
    configured=lambda: os.environ.get('ORDERCHAMP_SYNC_V2_ENABLED','0')=='1'

    def cache_import(result):
        if not result['order_ids']: return
        # Only the imported rows are mirrored locally, inside one short transaction.
        # Never pull a table or start the application's global synchronisation here.
        with backend['_supabase_full_io_lock']:
            db=backend['conn']()
            try:
                import inpost_tracking, invoice_payment_sync
                def protect_orders(db,rows):
                    rows=invoice_payment_sync.protect_incoming(db,'orders',rows)
                    return inpost_tracking.protect_incoming(db,rows)
                mirror_import_rows(db,result,protect_orders)
            finally: db.close()

    engine=SyncEngine(store,client_factory or IncrementalClient.from_env,cache_import=cache_import)
    worker=Worker(engine,configured,app.logger)
    app.extensions['orderchamp_sync_v2']={'engine':engine,'worker':worker,'store':store}
    bp=Blueprint('orderchamp_sync_v2',__name__)

    @app.before_request
    def start_serving_worker():
        worker.start()  # no network/DB work on the request thread

    @bp.after_request
    def no_cache(response):
        response.headers['Cache-Control']='no-store'
        return response

    @bp.get('/admin/orderchamp')
    @_admin_only
    def page():
        return render_template('orderchamp_sync.html',configured=configured())

    @bp.get('/api/admin/orderchamp/status')
    @_admin_only
    def status():
        if not configured(): return jsonify(configured=False,enabled=False)
        try: return jsonify(configured=True,**store.control('status'))
        except OrderchampError as exc: return jsonify(ok=False,error=exc.code),503

    @bp.post('/api/admin/orderchamp/control')
    @_admin_only
    def control():
        if not configured(): return jsonify(ok=False,error='SYNC_NOT_CONFIGURED'),503
        if not request.is_json or request.content_length is None or request.content_length>1024:
            return jsonify(ok=False,error='INVALID_BODY'),400
        body=request.get_json(silent=True)
        if not isinstance(body,dict) or body.get('action') not in ('queue','enable','pause','reconcile','compare'):
            return jsonify(ok=False,error='INVALID_ACTION'),400
        allowed={'action','sku'} if body['action']=='queue' else {'action'}
        if set(body)-allowed: return jsonify(ok=False,error='INVALID_BODY'),400
        sku=body.get('sku')
        if sku is not None and (not isinstance(sku,str) or not sku.strip() or len(sku)>128 or sku!=sku.strip()):
            return jsonify(ok=False,error='INVALID_SKU'),400
        try:
            result=store.control(body['action'],data={'only_sku':sku})
            if body['action']!='pause': worker.wake()
            return jsonify(result),202
        except OrderchampError as exc: return jsonify(ok=False,error=exc.code),503

    @bp.post('/api/internal/orderchamp/tick')
    def tick():
        secret=os.environ.get('ORDERCHAMP_SYNC_TRIGGER_TOKEN','')
        supplied=request.headers.get('Authorization','')
        if len(secret)<32 or not hmac.compare_digest(supplied.encode(),('Bearer '+secret).encode()):
            return jsonify(ok=False),403
        if not configured(): return jsonify(ok=False,error='SYNC_NOT_CONFIGURED'),503
        worker.wake()
        return jsonify(ok=True),202

    @bp.post('/webhooks/orderchamp')
    def webhook():
        secret=os.environ.get('ORDERCHAMP_WEBHOOK_SECRET','')
        account=os.environ.get('ORDERCHAMP_ACCOUNT_ID','')
        if not configured() or not secret or not account: return jsonify(ok=False),503
        if request.content_length is None or request.content_length>65536: return jsonify(ok=False),413
        raw=request.get_data()
        expected=hmac.new(secret.encode(),raw,hashlib.sha256).hexdigest()
        supplied=request.headers.get('X-Orderchamp-Signature','')
        if (not hmac.compare_digest(expected.encode(),supplied.encode()) or
            not hmac.compare_digest(account.encode(),request.headers.get('X-Orderchamp-Account-Id','').encode())):
            return jsonify(ok=False),403
        if request.headers.get('X-Orderchamp-Event') not in ('ORDER_CREATED','ORDER_UPDATED','ORDER_CANCELLED'):
            return jsonify(ok=True),200
        # The webhook is only a hint. All data is obtained from authenticated API
        # reads with overlap, so replayed/out-of-order payloads cannot alter orders.
        try:
            store.control('wake')
            worker.wake()
            return jsonify(ok=True),200
        except OrderchampError: return jsonify(ok=False),503

    app.register_blueprint(bp)
