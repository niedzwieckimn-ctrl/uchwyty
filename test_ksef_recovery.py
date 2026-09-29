from datetime import datetime, timedelta, timezone
import contextlib
import json
import logging
import sqlite3
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
import pytest
from flask import Flask, redirect
import ksef_scheduler as scheduler
import run_ksef_batch as batch


@pytest.fixture
def backend(tmp_path, monkeypatch):
    path=tmp_path/'ksef.db'
    def conn():
        c=sqlite3.connect(path,timeout=10)
        c.row_factory=sqlite3.Row
        return c
    with contextlib.closing(conn()) as c:
        scheduler.initialize(c)
        c.executescript('''CREATE TABLE invoices(id INTEGER PRIMARY KEY,publication_state TEXT,created_at TEXT);
        CREATE TABLE ksef_documents(invoice_id INTEGER PRIMARY KEY,ksef_number TEXT,status TEXT);
        CREATE TABLE invoice_meta(invoice_id INTEGER PRIMARY KEY,sent_to_client INTEGER);''')
    app=Flask(__name__)
    b=SimpleNamespace(conn=conn,app=app,supabase_enabled=lambda:False,_refresh_domain_route_context=lambda:None)
    monkeypatch.setenv('KSEF_AUTOMATION_START_DATE','2026-09-16')
    return b


def at(day=28,hour=17,minute=0):
    return datetime(2026,9,day,hour,minute,tzinfo=scheduler.WARSAW)


def invoice(b,iid=100,created='2026-09-28 09:12:59',number='',mailed=0,state='draft',publication='complete'):
    with contextlib.closing(b.conn()) as c:
        c.execute('INSERT INTO invoices VALUES(?,?,?)',(iid,publication,created))
        c.execute('INSERT INTO ksef_documents VALUES(?,?,?)',(iid,number,state))
        c.execute('INSERT INTO invoice_meta VALUES(?,?)',(iid,mailed))
        c.commit()


def successful_sender(b):
    calls=[]
    def send(iid):
        calls.append(iid)
        with contextlib.closing(b.conn()) as c:
            c.execute("UPDATE ksef_documents SET ksef_number='TEST',status='sent' WHERE invoice_id=?",(iid,))
            c.execute('UPDATE invoice_meta SET sent_to_client=1 WHERE invoice_id=?',(iid,))
            c.commit()
        return redirect('/ksef')
    b.app.view_functions['invoice_ksef_send']=send
    def doc(iid):
        with contextlib.closing(b.conn()) as c:return dict(c.execute('SELECT * FROM ksef_documents WHERE invoice_id=?',(iid,)).fetchone())
    def meta(iid):
        with contextlib.closing(b.conn()) as c:return dict(c.execute('SELECT * FROM invoice_meta WHERE invoice_id=?',(iid,)).fetchone())
    b.load_ksef_doc=doc;b.load_invoice_meta=meta
    return calls


def tick(b,monkeypatch,now):
    monkeypatch.setattr(scheduler,'_utc_now',lambda:now.astimezone(timezone.utc))
    return scheduler.check_once(b,now=now)


def test_1700_full_batch_and_retries_do_not_resend(backend,monkeypatch):
    invoice(backend)
    calls=successful_sender(backend)
    assert tick(backend,monkeypatch,at(hour=16,minute=59))['summary']['invoice_ids']==[]
    assert tick(backend,monkeypatch,at())['status']=='completed'
    assert calls==[100]
    assert tick(backend,monkeypatch,at(minute=1))['status']=='cooldown'
    assert tick(backend,monkeypatch,at(minute=5))['summary']['invoice_ids']==[]
    assert calls==[100]
    record=scheduler.Store(backend).state('2026-09-28')
    assert json.loads(record['summary_json'])['ok'] is True


def test_empty_batch_does_not_close_the_day(backend,monkeypatch):
    calls=successful_sender(backend)
    assert tick(backend,monkeypatch,at())['status']=='completed'
    invoice(backend)
    assert tick(backend,monkeypatch,at(minute=5))['summary']['invoice_ids']==[100]
    assert calls==[100]


def test_next_morning_catches_old_only(backend,monkeypatch):
    invoice(backend)
    invoice(backend,101,created='2026-09-29 08:00:00')
    calls=successful_sender(backend)
    report=tick(backend,monkeypatch,at(day=29,hour=10))
    assert report['run_date']=='2026-09-28' and calls==[100]


@pytest.mark.parametrize('pulled',[None,{'ok':False,'tables':{'invoices':{'status':'error'}}},{}])
def test_failed_pull_never_reports_success_or_sends(backend,pulled):
    invoice(backend)
    calls=successful_sender(backend)
    backend.supabase_enabled=lambda:True
    backend.pull_shared_tables_from_supabase=lambda **kw:pulled
    with pytest.raises(RuntimeError,match='KSEF_SYNC_FAILED'):batch.run_batch(backend,now=at())
    assert calls==[]


def test_failed_batch_persists_reason_and_retries_after_backoff(backend,monkeypatch):
    invoice(backend)
    calls=successful_sender(backend)
    good=batch.run_batch
    monkeypatch.setattr(batch,'run_batch',lambda *a,**kw:(_ for _ in ()).throw(RuntimeError('KSEF_SYNC_FAILED')))
    result=tick(backend,monkeypatch,at())
    assert result['status']=='failed' and calls==[]
    assert 'KSEF_SYNC_FAILED' in scheduler.Store(backend).state('2026-09-28')['last_error']
    monkeypatch.setattr(batch,'run_batch',good)
    assert tick(backend,monkeypatch,at(minute=5))['status']=='completed'
    assert calls==[100]


def test_only_one_worker_claims_even_across_dates(backend):
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims=list(pool.map(lambda day:scheduler.Store(backend).claim(day,now=at()),
                             ['2026-09-27','2026-09-28','2026-09-27','2026-09-28']))
    assert sum(bool(c) for c in claims)==1


def test_expired_lease_recovers_and_old_holder_cannot_complete(backend):
    store=scheduler.Store(backend)
    first=store.claim('2026-09-28',now=at())
    second=store.claim('2026-09-28',now=at()+timedelta(minutes=31))
    assert second and second['lease_token']!=first['lease_token']
    assert not store.complete(first)
    assert store.complete(second)


def test_accepted_and_mailed_invoice100_is_excluded(backend):
    invoice(backend,number='8661754935-20260929-46B85DC00003-55',mailed=1,state='sent')
    assert batch.candidates(at(day=29,hour=12),backend)==[]


def test_draft_and_pre_start_invoices_stay_excluded(backend):
    invoice(backend,publication='staged')
    invoice(backend,101,created='2026-09-15 09:00:00')
    assert batch.candidates(at(),backend)==[]


def test_dry_run_and_http_error_are_not_successful_submission(backend):
    invoice(backend)
    calls=successful_sender(backend)
    assert batch.run_batch(backend,now=at(),send=False)['invoice_ids']==[100]
    assert calls==[]
    backend.app.view_functions['invoice_ksef_send']=lambda iid:backend.app.response_class('blocked',status=403)
    result=batch.run_batch(backend,now=at())
    assert not result['ok'] and '403' in result['results'][0]['error']


def test_missing_start_date_fails_before_claim(backend,monkeypatch):
    monkeypatch.delenv('KSEF_AUTOMATION_START_DATE')
    with pytest.raises(ValueError,match='KSEF_AUTOMATION_START_DATE'):scheduler.check_once(backend,now=at())
    assert scheduler.Store(backend).state('2026-09-28') is None


@pytest.mark.parametrize('iso,ids',[
    ('2026-10-25T15:59:00+00:00',[]),('2026-10-25T16:00:00+00:00',[100]),
    ('2026-09-28T14:59:00+00:00',[]),('2026-09-28T15:00:00+00:00',[100])])
def test_warsaw_cutoff_in_summer_and_winter(backend,iso,ids):
    invoice(backend,created=iso[:10]+' 09:00:00')
    assert batch.candidates(datetime.fromisoformat(iso),backend)==ids
