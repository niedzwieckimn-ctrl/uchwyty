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
from datetime import datetime, time as clock_time, timedelta
from zoneinfo import ZoneInfo

WARSAW = ZoneInfo('Europe/Warsaw')
DAILY_META = '_daily_pickup'

SCHEMA = '''CREATE TABLE IF NOT EXISTS inpost_pickup_jobs(
 shipment_id TEXT PRIMARY KEY,pickup_json TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending',
 dispatch_id TEXT,external_id TEXT,error TEXT NOT NULL DEFAULT '',attempted INTEGER NOT NULL DEFAULT 0,
 next_check REAL NOT NULL DEFAULT 0,lease_until REAL NOT NULL DEFAULT 0,claim_token TEXT,
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL)'''

LABELS = {
 'pending':'Przesyłka czeka na dzienny podjazd',
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

def _local_now():
    return datetime.now(WARSAW)

def _parse_clock(name, default):
    raw=os.environ.get(name,default).strip()
    try:
        hour,minute=(int(value) for value in raw.split(':',1))
        return clock_time(hour,minute)
    except (TypeError,ValueError):
        return clock_time(*(int(value) for value in default.split(':',1)))

def cutoff_time():
    return _parse_clock('INPOST_DAILY_PICKUP_CUTOFF','12:20')

def next_business_day(value):
    value=value+timedelta(days=1)
    while value.weekday()>=5:
        value=value+timedelta(days=1)
    return value

def _pickup_meta(payload):
    value=(payload or {}).get(DAILY_META) or {}
    return value if isinstance(value,dict) else {}

def _job_payload(job):
    try:return json.loads(job.get('pickup_json') or '{}')
    except (TypeError,ValueError):return {}

def _job_date(job):
    return str(_pickup_meta(_job_payload(job)).get('scheduled_date') or '')

def _parcel_count(job):
    try:return max(1,int(_pickup_meta(_job_payload(job)).get('parcel_count') or 1))
    except (TypeError,ValueError):return 1

def _closed(rows,scheduled_date):
    return any(_job_date(row)==scheduled_date and
               (int(row.get('attempted') or 0) or row.get('dispatch_id') or
                row.get('state') in {'new','sent','unknown','collected'}) for row in rows)

def scheduled_date(rows=None,now=None):
    now=now or _local_now();today=now.date()
    if today.weekday()>=5:
        while today.weekday()>=5:today=next_business_day(today)
        return today.isoformat()
    cutoff=cutoff_time()
    after_cutoff=now.time() > clock_time(cutoff.hour,cutoff.minute,59,999999)
    if after_cutoff or _closed(rows or [],today.isoformat()):
        return next_business_day(today).isoformat()
    return today.isoformat()

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
    def enqueue(self,sid,pickup,parcel_count=1):
        sid=str(sid);now=self.b.now_iso()
        # The existing JSON column keeps the daily plan without a database
        # migration. Old jobs without this marker retain their legacy handling.
        payload=dict(pickup)
        rows=self.rows()
        payload[DAILY_META]={
            'scheduled_date':scheduled_date(rows),
            'parcel_count':max(1,min(99,int(parcel_count or 1))),
            'cutoff':cutoff_time().strftime('%H:%M'),
        }
        data={'shipment_id':sid,'pickup_json':json.dumps(payload,ensure_ascii=False),'created_at':now,'updated_at':now}
        if self.b.supabase_enabled():
            self.b.supabase_request('/rest/v1/inpost_pickup_jobs',method='POST',params={'on_conflict':'shipment_id'},payload=data,prefer='resolution=ignore-duplicates')
        else:
            c=self.b.conn()
            try:c.execute('INSERT OR IGNORE INTO inpost_pickup_jobs(shipment_id,pickup_json,created_at,updated_at) VALUES(?,?,?,?)',tuple(data.values()));c.commit()
            finally:c.close()
    def rows(self,sid=None):
        if self.b.supabase_enabled():
            params={'select':'*','order':'created_at.desc','limit':1000}
            if sid is not None:params['shipment_id']='eq.'+str(sid)
            return self.b.supabase_request('/rest/v1/inpost_pickup_jobs',params=params) or []
        c=self.b.conn()
        try:return [dict(r) for r in c.execute('SELECT * FROM inpost_pickup_jobs'+(' WHERE shipment_id=?' if sid is not None else '')+' ORDER BY created_at DESC', (str(sid),) if sid is not None else ())]
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
        current=next(iter(self.rows(sid)),None)
        payload=dict(pickup)
        meta=_pickup_meta(_job_payload(current or {}))
        if meta:payload[DAILY_META]=meta
        payload=json.dumps(payload,ensure_ascii=False)
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

    def replace_payload(self,job,payload):
        claimed=self.claim(job['shipment_id'])
        if not claimed:return False
        self.update(claimed,pickup_json=json.dumps(payload,ensure_ascii=False),state='pending',error='')
        return True

def _result_values(result):
    dispatch_id=str(result.get('id') or '')
    if not dispatch_id:raise RuntimeError('InPost nie zwrócił identyfikatora podjazdu')
    status=str(result.get('status') or 'new').lower()
    if status not in {'new','sent','rejected'}:status='unknown'
    error=json.dumps(result.get('errors') or {},ensure_ascii=False) if status=='rejected' else ''
    return dict(state=status,dispatch_id=dispatch_id,
                external_id=str(result.get('external_id') or ''),error=error)

def _update_group(store,rows,values,leader=None):
    for row in rows:
        if leader and str(row['shipment_id'])==str(leader['shipment_id']):continue
        claimed=store.claim(row['shipment_id'])
        if claimed:
            store.update(claimed,**values)

def process_date(b,day):
    """Create one DispatchOrder for every parcel planned for one workday."""
    store=Store(b)
    rows=sorted((row for row in store.rows() if _job_date(row)==day),
                key=lambda row:(row.get('created_at') or '',str(row['shipment_id'])))
    if not rows:return {'ok':False,'message':'Brak przesyłek zaplanowanych na ten podjazd.'}
    leader=store.claim(rows[0]['shipment_id'])
    if not leader:
        return {'ok':False,'message':'Podjazd jest już sprawdzany. Odśwież widok za chwilę.'}
    try:
        if leader.get('dispatch_id'):
            result=b.inpost_get_dispatch_order(leader['dispatch_id'])
        elif int(leader.get('attempted') or 0):
            result=b.inpost_find_dispatch_order(leader['shipment_id'])
            if not result:
                values=dict(state='unknown',error='Nie potwierdzono wyniku poprzedniej próby. Nie ponowiono zamówienia kuriera. Sprawdź historię w InPost.')
                store.update(leader,**values);_update_group(store,rows,values,leader)
                return {'ok':False,'message':values['error']}
        else:
            pickup=_job_payload(leader)
            try:validate(pickup)
            except ValueError as exc:
                values=dict(state='configuration_error',error=str(exc))
                store.update(leader,**values);_update_group(store,rows,values,leader)
                return {'ok':False,'message':str(exc)}
            shipment_ids=[]
            for row in rows:
                shipment=b.inpost_get_shipment(row['shipment_id'])
                if str(shipment.get('status') or '').lower()!='confirmed':
                    store.update(leader,state='pending',error='InPost przygotowuje jedną z przesyłek; podjazd zostanie zamówiony po jej potwierdzeniu.')
                    return {'ok':False,'message':'Jedna z przesyłek nie jest jeszcze potwierdzona przez InPost.'}
                shipment_ids.append(str(row['shipment_id']))
            # The leader is the durable at-most-once marker for the whole day.
            # After this write a timeout is reconciled by GET, never re-POSTed.
            store.update(leader,release=False,attempted=1,state='unknown')
            leader['attempted']=1
            result=b.inpost_create_dispatch_order(shipment_ids,pickup)
        values=_result_values(result)
        store.update(leader,**values)
        _update_group(store,rows,values,leader)
        return {'ok':values['state'] in {'new','sent'},'message':LABELS.get(values['state'],values['state']),
                'dispatch_id':values['dispatch_id']}
    except Exception as exc:
        if _validation_error(exc):
            values=dict(state='configuration_error',attempted=0,error=str(exc))
            store.update(leader,**values);_update_group(store,rows,values,leader)
        else:
            state='unknown' if int(leader.get('attempted') or 0) or leader.get('dispatch_id') else 'pending'
            store.update(leader,state=state,error=str(exc))
        return {'ok':False,'message':str(exc)}

def process_one(b,sid):
    store=Store(b)
    current=next(iter(store.rows(sid)),None)
    if current and _job_date(current):
        return process_date(b,_job_date(current))
    job=store.claim(sid)
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
        store.update(job,**_result_values(result))
    except Exception as exc:
        # Preserve durable intent; failure of this update leaves the lease and
        # attempted flag for a future worker to reconcile safely.
        if _validation_error(exc):
            store.update(job,state='configuration_error',attempted=0,error=str(exc))
        else:
            store.update(job,state='unknown' if job['attempted'] or job.get('dispatch_id') else 'pending',error=str(exc))

def _reschedule_expired(store,rows,now):
    target=scheduled_date([],now)
    changed=False
    for row in rows:
        if int(row.get('attempted') or 0) or row.get('dispatch_id'):continue
        payload=_job_payload(row);meta=_pickup_meta(payload)
        if not meta:continue
        meta['scheduled_date']=target;payload[DAILY_META]=meta
        changed=store.replace_payload(row,payload) or changed
    return changed

def order_today(b,now=None):
    now=now or _local_now();cutoff=cutoff_time()
    if now.weekday()>=5 or now.time()>clock_time(cutoff.hour,cutoff.minute,59,999999):
        return {'ok':False,'message':'Dzisiejszy termin 12:20 minął. Nowe przesyłki są zaplanowane na następny dzień roboczy.'}
    return process_date(b,now.date().isoformat())

def dashboard_state(b,now=None):
    now=now or _local_now();rows=Store(b).rows();today=now.date().isoformat()
    groups={}
    for row in rows:
        day=_job_date(row)
        if not day:continue
        group=groups.setdefault(day,[]);group.append(row)
    def summary(day,items):
        attempted=any(int(row.get('attempted') or 0) or row.get('dispatch_id') for row in items)
        states={str(row.get('state') or '') for row in items}
        return {'date':day,'parcel_count':sum(_parcel_count(row) for row in items),
                'shipment_count':len(items),'ordered':attempted,
                'state':('problem' if states & {'unknown','rejected','configuration_error'} else
                         'ordered' if attempted else 'planned'),
                'label':next((LABELS.get(row.get('state'),row.get('state')) for row in items
                              if row.get('state')!='pending'),LABELS['pending'])}
    future=sorted(day for day in groups if day>today)
    cutoff=cutoff_time()
    return {'today':summary(today,groups[today]) if groups.get(today) else None,
            'next':summary(future[0],groups[future[0]]) if future else None,
            'cutoff':cutoff.strftime('%H:%M'),
            'can_order':bool(groups.get(today)) and not _closed(rows,today)
                and now.weekday()<5 and now.time()<=clock_time(cutoff.hour,cutoff.minute,59,999999)}

def process_due(b):
    if not enabled():return
    epoch=time.time();now=_local_now();store=Store(b);rows=store.rows()
    # Legacy rows keep their original immediate/reconciliation path.
    for job in rows:
        if _job_date(job):continue
        if float(job.get('next_check') or 0)<=epoch and job['state'] not in {'rejected','configuration_error','collected'}:
            try:process_one(b,job['shipment_id'])
            except Exception:b.app.logger.exception('Nie udało się utrwalić wyniku podjazdu InPost')
    groups={}
    for job in rows:
        day=_job_date(job)
        if day:groups.setdefault(day,[]).append(job)
    today=now.date().isoformat();cutoff=cutoff_time()
    for day,jobs in sorted(groups.items()):
        needs_reconciliation=any(job.get('state') in {'unknown','new'} for job in jobs)
        if needs_reconciliation and any(float(job.get('next_check') or 0)<=epoch for job in jobs):
            try:process_date(b,day)
            except Exception:b.app.logger.exception('Nie udało się uzgodnić dziennego podjazdu InPost')
            continue
        if day<today and not _closed(rows,day):
            _reschedule_expired(store,jobs,now);continue
        if day!=today:continue
        due=now.time()>=cutoff
        still_open=now.time()<=clock_time(cutoff.hour,cutoff.minute,59,999999)
        if due and still_open and any(float(job.get('next_check') or 0)<=epoch for job in jobs):
            try:process_date(b,day)
            except Exception:b.app.logger.exception('Nie udało się zamówić dziennego podjazdu InPost')
        elif not still_open and not _closed(rows,day):
            _reschedule_expired(store,jobs,now)

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
