"""Durable automatic courier pickup with an at-most-once uncertain POST.

ShipX has asynchronous dispatch creation. Persist intent BEFORE the POST and
reconcile its result by GET after a crash/timeout, without blind resubmission.
Supabase is authoritative when connected; SQLite is used by standalone tests.
"""
import json
import os
import re
import threading
import time
import uuid

SCHEMA = '''CREATE TABLE IF NOT EXISTS inpost_pickup_jobs(
 shipment_id TEXT PRIMARY KEY,pickup_json TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending',
 dispatch_id TEXT,external_id TEXT,error TEXT NOT NULL DEFAULT '',attempted INTEGER NOT NULL DEFAULT 0,
 next_check REAL NOT NULL DEFAULT 0,lease_until REAL NOT NULL DEFAULT 0,claim_token TEXT,
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL)'''

LABELS = {
 'pending':'Podjazd oczekuje na gotowość przesyłki',
 'new':'Zlecenie podjazdu utworzone — trwa weryfikacja InPost',
 'sent':'Zlecenie podjazdu przekazane do realizacji',
 'unknown':'Wynik zamówienia podjazdu wymaga sprawdzenia',
 'rejected':'InPost odrzucił podjazd — wymagana interwencja',
 'configuration_error':'Uzupełnij dane miejsca odbioru',
 'collected':'Kurier odebrał przesyłkę',
}

def initialize(c):
    c.execute(SCHEMA);c.commit()

def enabled():
    return os.environ.get('INPOST_AUTO_PICKUP','1').lower() not in {'0','false','no','off'}

def company_pickup(b):
    c=b.conn()
    try:
        row=c.execute('SELECT * FROM company_profile WHERE id=1').fetchone()
        company=dict(row) if row else {}
    finally:c.close()
    street,post_code,city=b.split_address(company.get('address') or '')
    defaults={'name':company.get('company_name') or 'Magazyn','street':street,
              'post_code':post_code,'city':city,'phone':company.get('phone') or '',
              'email':company.get('email') or '',
              'comment':'Odbiór przesyłek z magazynu.'}
    return {k:os.environ.get('INPOST_PICKUP_'+k.upper(),str(v)).strip() for k,v in defaults.items()}

def validate(pickup):
    if not pickup.get('name') or not re.search(r'\d',pickup.get('street','')) or not pickup.get('city'):
        raise ValueError('Uzupełnij nazwę, ulicę z numerem i miasto miejsca odbioru.')
    if not re.fullmatch(r'\d{2}-\d{3}',pickup.get('post_code','')):
        raise ValueError('Podaj kod pocztowy miejsca odbioru w formacie 00-000.')
    phone=re.sub(r'\D','',pickup.get('phone',''))
    if len(phone)==11 and phone.startswith('48'):phone=phone[2:]
    if len(phone)!=9:raise ValueError('Podaj polski numer telefonu kontaktowego miejsca odbioru.')
    if not pickup.get('comment','').strip():
        raise ValueError('Wpisz krótką uwagę dla kuriera, np. „Odbiór przesyłek z magazynu”.')

def _validation_error(error):
    text=str(error or '').lower()
    return 'http 400' in text and ('validation error' in text or '"required"' in text)

class Store:
    def __init__(self,b):self.b=b
    def enqueue(self,sid,pickup):
        sid=str(sid);now=self.b.now_iso()
        data={'shipment_id':sid,'pickup_json':json.dumps(pickup,ensure_ascii=False),'created_at':now,'updated_at':now}
        if self.b.supabase_enabled():
            self.b.supabase_request('/rest/v1/inpost_pickup_jobs',method='POST',params={'on_conflict':'shipment_id'},payload=data,prefer='resolution=ignore-duplicates')
        else:
            c=self.b.conn()
            try:c.execute('INSERT OR IGNORE INTO inpost_pickup_jobs(shipment_id,pickup_json,created_at,updated_at) VALUES(?,?,?,?)',tuple(data.values()));c.commit()
            finally:c.close()
    def rows(self,sid=None):
        if self.b.supabase_enabled():
            params={'select':'*','order':'created_at.asc','limit':1000}
            if sid is not None:params['shipment_id']='eq.'+str(sid)
            return self.b.supabase_request('/rest/v1/inpost_pickup_jobs',params=params) or []
        c=self.b.conn()
        try:return [dict(r) for r in c.execute('SELECT * FROM inpost_pickup_jobs'+(' WHERE shipment_id=?' if sid is not None else '')+' ORDER BY created_at', (str(sid),) if sid is not None else ())]
        finally:c.close()
    def claim(self,sid):
        token=str(uuid.uuid4());now=time.time()
        if self.b.supabase_enabled():
            return self.b.supabase_request('/rest/v1/rpc/claim_inpost_pickup',method='POST',payload={'p_shipment':str(sid),'p_token':token})
        c=self.b.conn()
        try:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute("SELECT * FROM inpost_pickup_jobs WHERE shipment_id=? AND lease_until<=? AND next_check<=? AND state NOT IN ('rejected','configuration_error','collected')",(str(sid),now,now)).fetchone()
            if not row:return None
            c.execute('UPDATE inpost_pickup_jobs SET claim_token=?,lease_until=? WHERE shipment_id=?',(token,now+180,str(sid)));c.commit()
            return dict(row,claim_token=token)
        finally:c.close()
    def correct_address(self,sid,pickup):
        validate(pickup)
        payload=json.dumps(pickup,ensure_ascii=False)
        if self.b.supabase_enabled():
            return self.b.supabase_request('/rest/v1/rpc/correct_inpost_pickup_address',method='POST',payload={'p_shipment':str(sid),'p_pickup':payload})
        c=self.b.conn()
        try:
            cur=c.execute("UPDATE inpost_pickup_jobs SET pickup_json=?,state='pending',error='',next_check=0 WHERE shipment_id=? AND attempted=0 AND claim_token IS NULL AND state IN ('configuration_error','pending')",(payload,str(sid)));c.commit()
            return cur.rowcount==1
        finally:c.close()
    def update(self,job,release=True,**values):
        values['updated_at']=self.b.now_iso()
        if release:values.update(claim_token=None,lease_until=0,next_check=time.time()+60)
        if self.b.supabase_enabled():
            result=self.b.supabase_request('/rest/v1/rpc/update_inpost_pickup',method='POST',payload={'p_shipment':job['shipment_id'],'p_token':job['claim_token'],'p_values':values})
            if not result:raise RuntimeError('Utracono blokadę obsługi podjazdu')
            return
        c=self.b.conn()
        try:
            keys=list(values)
            cur=c.execute('UPDATE inpost_pickup_jobs SET '+','.join(k+'=?' for k in keys)+' WHERE shipment_id=? AND claim_token=?',tuple(values[k] for k in keys)+(job['shipment_id'],job['claim_token']))
            if cur.rowcount!=1:raise RuntimeError('Utracono blokadę obsługi podjazdu')
            c.commit()
        finally:c.close()

def process_one(b,sid):
    store=Store(b);job=store.claim(sid)
    if not job:return
    try:
        # HTTP 400 is a definite rejection: it is safe to correct the data and
        # submit again. It is not an uncertain timeout, which must stay blocked.
        if job['attempted'] and _validation_error(job.get('error')):
            store.update(job,state='configuration_error',attempted=0,
                         error='InPost odrzucił dane podjazdu. Uzupełnij formularz i zleć ponownie.')
            return
        if job.get('dispatch_id'):
            result=b.inpost_get_dispatch_order(job['dispatch_id'])
        elif job['attempted']:
            result=b.inpost_find_dispatch_order(sid)
            if not result:
                store.update(job,state='unknown',error='Nie potwierdzono wyniku poprzedniej próby. Nie ponowiono zamówienia kuriera. Sprawdź historię w InPost.')
                return
        else:
            pickup=json.loads(job['pickup_json'])
            try:validate(pickup)
            except ValueError as exc:
                store.update(job,state='configuration_error',error=str(exc));return
            shipment=b.inpost_get_shipment(sid)
            status=str(shipment.get('status') or '').lower()
            if status!='confirmed':
                store.update(job,state='pending',error='InPost przygotowuje przesyłkę; podjazd zostanie zamówiony po jej potwierdzeniu.')
                return
            # A restart after this commit is uncertain, even if POST never ran.
            # Such a job is reconciled with GET, never blindly resubmitted.
            store.update(job,release=False,attempted=1,state='unknown')
            job['attempted']=1
            result=b.inpost_create_dispatch_order([str(sid)],pickup)
        dispatch_id=str(result.get('id') or '')
        if not dispatch_id:raise RuntimeError('InPost nie zwrócił identyfikatora podjazdu')
        status=str(result.get('status') or 'new').lower()
        if status not in {'new','sent','rejected'}:status='unknown'
        error=json.dumps(result.get('errors') or {},ensure_ascii=False) if status=='rejected' else ''
        store.update(job,state=status,dispatch_id=dispatch_id,external_id=str(result.get('external_id') or ''),error=error)
    except Exception as exc:
        # Preserve durable intent; failure of this update leaves the lease and
        # attempted flag for a future worker to reconcile safely.
        if _validation_error(exc):
            store.update(job,state='configuration_error',attempted=0,error=str(exc))
        else:
            store.update(job,state='unknown' if job['attempted'] or job.get('dispatch_id') else 'pending',error=str(exc))

def process_due(b):
    if not enabled():return
    now=time.time()
    for job in Store(b).rows():
        if float(job.get('next_check') or 0)<=now and job['state'] not in {'rejected','configuration_error','collected'}:
            try:process_one(b,job['shipment_id'])
            except Exception:b.app.logger.exception('Nie udało się utrwalić wyniku podjazdu InPost')

_worker_lock=threading.Lock()
_worker=None
def start_worker(b):
    global _worker
    if not enabled() or os.environ.get('INPOST_PICKUP_WORKER','1')=='0' or not b.inpost_config_summary()['configured']:return
    with _worker_lock:
        if _worker and _worker.is_alive():return
        def run():
            while True:
                try:
                    with b.app.app_context():process_due(b)
                except Exception:b.app.logger.exception('Kolejka podjazdów InPost wymaga sprawdzenia')
                time.sleep(60)
        _worker=threading.Thread(target=run,name='inpost-pickups',daemon=True);_worker.start()
