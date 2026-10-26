import json
import logging

import pytest
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import inpost_pickups as pickups


class Backend:
    def __init__(self,path):
        self.path=str(path)
        self.created=[]
        self.dispatches={}
        self.app=SimpleNamespace(logger=logging.getLogger(__name__))

    def conn(self):
        db=sqlite3.connect(self.path)
        db.row_factory=sqlite3.Row
        return db

    def now_iso(self):
        return '2026-10-09T10:00:00+02:00'

    def supabase_enabled(self):
        return False

    def inpost_get_shipment(self,sid):
        return {'id':str(sid),'status':'confirmed'}

    def inpost_create_dispatch_order(self,sids,pickup):
        result={'id':str(900+len(self.created)),'external_id':str(700+len(self.created)),'status':'sent'}
        self.created.append(list(sids));self.dispatches[result['id']]=result
        return result

    def inpost_get_dispatch_order(self,dispatch_id):
        return self.dispatches[str(dispatch_id)]

    def inpost_find_dispatch_order(self,sid):
        for dispatch_id,result in self.dispatches.items():
            if any(str(sid) in group for group in self.created):return result
        return None


def at(hour,minute,day=9):
    return datetime(2026,10,day,hour,minute,tzinfo=ZoneInfo('Europe/Warsaw'))


def setup_backend(tmp_path,monkeypatch,when):
    backend=Backend(tmp_path/'pickup.db')
    db=backend.conn();pickups.initialize(db);db.close()
    monkeypatch.setattr(pickups,'_local_now',lambda:when)
    return backend,pickups.Store(backend)


def pickup_data():
    return {'name':'Magazyn','street':'Testowa 1','post_code':'00-001','city':'Warszawa',
            'phone':'500600700','email':'test@example.com','comment':'Odbiór z magazynu'}


def test_manual_order_groups_today_and_later_parcel_goes_next_day(tmp_path,monkeypatch):
    backend,store=setup_backend(tmp_path,monkeypatch,at(10,0))
    store.enqueue('11',pickup_data(),2);store.enqueue('12',pickup_data(),1)

    before=pickups.dashboard_state(backend)
    assert before['today']['parcel_count']==3 and before['can_order']
    assert pickups.order_today(backend)['ok']
    assert backend.created==[['11','12']]
    assert {row['dispatch_id'] for row in store.rows()}=={'900'}

    store.enqueue('13',pickup_data(),1)
    state=pickups.dashboard_state(backend)
    assert state['today']['ordered'] is True
    assert state['next']['date']=='2026-10-12' # Friday -> Monday
    assert state['next']['parcel_count']==1


def test_worker_waits_until_cutoff_and_creates_one_dispatch(tmp_path,monkeypatch):
    backend,store=setup_backend(tmp_path,monkeypatch,at(12,19))
    store.enqueue('21',pickup_data());store.enqueue('22',pickup_data())
    pickups.process_due(backend)
    assert backend.created==[]

    monkeypatch.setattr(pickups,'_local_now',lambda:at(12,20))
    pickups.process_due(backend)
    assert backend.created==[['21','22']]


def test_new_shipment_after_cutoff_is_planned_for_next_business_day(tmp_path,monkeypatch):
    backend,store=setup_backend(tmp_path,monkeypatch,at(12,21))
    store.enqueue('31',pickup_data(),3)
    state=pickups.dashboard_state(backend)
    assert state['today'] is None
    assert state['next']['date']=='2026-10-12'
    assert state['next']['parcel_count']==3
    assert pickups.order_today(backend)['ok'] is False


def test_address_correction_keeps_daily_assignment(tmp_path,monkeypatch):
    backend,store=setup_backend(tmp_path,monkeypatch,at(10,0))
    store.enqueue('41',pickup_data(),2)
    corrected=dict(pickup_data(),street='Nowa 2')
    assert store.correct_address('41',corrected)
    row=store.rows('41')[0]
    assert pickups._job_date(row)=='2026-10-09'
    assert pickups._parcel_count(row)==2


def clock(monkeypatch,when):
    value=[when]
    monkeypatch.setattr(pickups,'_local_now',lambda:value[0])
    monkeypatch.setattr(pickups.time,'time',lambda:value[0].timestamp())
    return value


def legacy_job(backend,sid,*,dispatch_id=None,attempted=0,state='pending'):
    db=backend.conn()
    try:
        db.execute("INSERT INTO inpost_pickup_jobs(shipment_id,pickup_json,dispatch_id,attempted,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                   (sid,json.dumps(pickup_data()),dispatch_id,attempted,state,backend.now_iso(),backend.now_iso()))
        db.commit()
    finally:db.close()


def test_slow_legacy_refreshes_clock_before_daily_deadline(monkeypatch,tmp_path):
    when=at(12,19).replace(second=57)
    backend,store=setup_backend(tmp_path,monkeypatch,when)
    now=clock(monkeypatch,when)
    store.enqueue('51',pickup_data())
    legacy_job(backend,'legacy',dispatch_id='800',attempted=1,state='new')
    def slow_get(dispatch_id):
        assert dispatch_id=='800'
        now[0]=at(12,20).replace(second=3)
        return {'id':'800','status':'sent','external_id':'600'}
    monkeypatch.setattr(backend,'inpost_get_dispatch_order',slow_get)

    pickups.process_due(backend)

    assert backend.created==[['51']]
    assert store.rows('51')[0]['dispatch_id']=='900'


def test_worker_wait_reaches_cutoff_instead_of_skipping_minute(monkeypatch,tmp_path):
    when=at(12,19).replace(second=57)
    backend,store=setup_backend(tmp_path,monkeypatch,when)
    now=clock(monkeypatch,when)
    store.enqueue('52',pickup_data())
    pickups.process_due(backend)
    assert backend.created==[]
    delay=pickups._worker_sleep_seconds()
    assert delay==3
    now[0]+=timedelta(seconds=delay)
    pickups.process_due(backend)
    assert backend.created==[['52']]


@pytest.mark.parametrize('when,expected',[
    (at(11,0),60),
    (at(12,19).replace(second=30),10),
    (at(12,20),10),
    (at(12,20).replace(second=58),2),
    (at(12,21),60),
    (at(12,20,day=10),60),
])
def test_worker_only_polls_frequently_near_business_cutoff(when,expected):
    assert pickups._worker_sleep_seconds(when)==expected


@pytest.mark.parametrize('not_ready',['created','read_error'])
def test_unattempted_preflight_gets_short_retry_in_open_window(monkeypatch,tmp_path,not_ready):
    backend,store=setup_backend(tmp_path,monkeypatch,at(12,20))
    now=clock(monkeypatch,at(12,20))
    store.enqueue('53',pickup_data())
    calls=[]
    def readiness(sid):
        calls.append(sid)
        if len(calls)==1:
            if not_ready=='read_error':raise TimeoutError('synthetic read failure')
            return {'id':sid,'status':'created'}
        return {'id':sid,'status':'confirmed'}
    monkeypatch.setattr(backend,'inpost_get_shipment',readiness)

    pickups.process_due(backend)
    first=store.rows('53')[0]
    assert backend.created==[] and first['attempted']==0
    assert first['next_check']==now[0].timestamp()+10
    now[0]+=timedelta(seconds=10)
    pickups.process_due(backend)
    assert backend.created==[['53']]


@pytest.mark.parametrize('when',[at(12,19),at(12,21)])
def test_automatic_first_post_is_only_inside_cutoff_minute(monkeypatch,tmp_path,when):
    backend,store=setup_backend(tmp_path,monkeypatch,at(10,0))
    store.enqueue('54',pickup_data())
    clock(monkeypatch,when)
    result=pickups.process_date(backend,'2026-10-09',automatic=True)
    assert result['ok'] is False and backend.created==[]
    assert store.rows('54')[0]['attempted']==0


@pytest.mark.parametrize('automatic',[False,True])
def test_slow_preflight_does_not_start_post_after_cutoff(monkeypatch,tmp_path,automatic):
    when=at(12,20).replace(second=58)
    backend,store=setup_backend(tmp_path,monkeypatch,when)
    now=clock(monkeypatch,when)
    store.enqueue('55',pickup_data())
    def slow_read(sid):
        now[0]=at(12,21)
        return {'id':sid,'status':'confirmed'}
    monkeypatch.setattr(backend,'inpost_get_shipment',slow_read)
    result=pickups.process_date(backend,'2026-10-09',automatic=automatic)
    assert result['ok'] is False and backend.created==[]
    assert store.rows('55')[0]['attempted']==0


@pytest.mark.parametrize('automatic',[False,True])
def test_slow_intent_commit_preserves_marker_without_late_post(monkeypatch,tmp_path,automatic):
    when=at(12,20).replace(second=58)
    backend,store=setup_backend(tmp_path,monkeypatch,when)
    now=clock(monkeypatch,when)
    store.enqueue('56',pickup_data())
    original=pickups.Store.update
    def slow_commit(self,job,release=True,**values):
        result=original(self,job,release=release,**values)
        if not release and values.get('attempted')==1:now[0]=at(12,21)
        return result
    monkeypatch.setattr(pickups.Store,'update',slow_commit)
    result=pickups.process_date(backend,'2026-10-09',automatic=automatic)
    assert result['ok'] is False and backend.created==[]
    row=store.rows('56')[0]
    assert row['attempted']==1 and row['state']=='unknown'
    now[0]+=timedelta(seconds=61)
    pickups.process_due(backend)
    assert backend.created==[] and store.rows('56')[0]['attempted']==1


def test_uncertain_post_is_not_repeated_by_frequent_ticks(monkeypatch,tmp_path):
    backend,store=setup_backend(tmp_path,monkeypatch,at(12,20))
    now=clock(monkeypatch,at(12,20))
    store.enqueue('57',pickup_data())
    attempts=[]
    def timeout_post(sids,pickup):
        attempts.append(list(sids))
        raise TimeoutError('synthetic post timeout')
    monkeypatch.setattr(backend,'inpost_create_dispatch_order',timeout_post)
    pickups.process_due(backend)
    row=store.rows('57')[0]
    assert row['attempted']==1 and row['state']=='unknown'
    assert row['next_check']==now[0].timestamp()+60
    for _ in range(7):
        now[0]+=timedelta(seconds=10)
        pickups.process_due(backend)
    assert attempts==[['57']]
    assert store.rows('57')[0]['attempted']==1


@pytest.mark.parametrize('legacy',[False,True])
def test_reconciliation_only_cannot_create_first_pickup(monkeypatch,tmp_path,legacy):
    backend,store=setup_backend(tmp_path,monkeypatch,at(10,0))
    clock(monkeypatch,at(10,0))
    if legacy:legacy_job(backend,'58')
    else:store.enqueue('58',pickup_data())
    result=pickups.process_one(backend,'58',allow_create=False)
    row=store.rows('58')[0]
    assert result['ok'] is False and backend.created==[]
    assert row['attempted']==0 and row['claim_token'] is None


@pytest.mark.parametrize('legacy',[False,True])
def test_reconciliation_only_recovers_existing_dispatch_after_cutoff(monkeypatch,tmp_path,legacy):
    backend,store=setup_backend(tmp_path,monkeypatch,at(10,0))
    now=clock(monkeypatch,at(10,0))
    if legacy:legacy_job(backend,'59')
    else:store.enqueue('59',pickup_data())
    pickups.process_one(backend,'59')
    assert backend.created==[['59']]
    now[0]=at(13,0)
    pickups.process_one(backend,'59',allow_create=False)
    assert backend.created==[['59']]
    assert store.rows('59')[0]['state']=='sent'


@pytest.mark.parametrize('other_attempt',[
    {'attempted':1},
    {'dispatch_id':'900'},
])
def test_pending_leader_cannot_duplicate_another_members_attempt(monkeypatch,tmp_path,other_attempt):
    backend,store=setup_backend(tmp_path,monkeypatch,at(10,0))
    clock(monkeypatch,at(10,0))
    store.enqueue('61',pickup_data());store.enqueue('62',pickup_data())
    db=backend.conn()
    try:
        field,value=next(iter(other_attempt.items()))
        db.execute('UPDATE inpost_pickup_jobs SET '+field+'=? WHERE shipment_id=?',(value,'62'))
        db.commit()
    finally:db.close()
    result=pickups.process_one(backend,'61')
    assert result['ok'] is False and backend.created==[]
    assert store.rows('61')[0]['attempted']==0


@pytest.mark.parametrize('when',[at(12,21),at(10,0,day=10)])
def test_legacy_first_post_cannot_bypass_cutoff_or_weekend(monkeypatch,tmp_path,when):
    backend,store=setup_backend(tmp_path,monkeypatch,when)
    clock(monkeypatch,when)
    legacy_job(backend,'71')
    result=pickups.process_one(backend,'71')
    row=store.rows('71')[0]
    assert result['ok'] is False and backend.created==[]
    assert row['attempted']==0 and row['claim_token'] is None


@pytest.mark.parametrize('slow_phase',['readiness','intent'])
def test_legacy_slow_operation_does_not_create_late_pickup(monkeypatch,tmp_path,slow_phase):
    when=at(12,20).replace(second=58)
    backend,store=setup_backend(tmp_path,monkeypatch,when)
    now=clock(monkeypatch,when)
    legacy_job(backend,'72')
    if slow_phase=='readiness':
        def slow_read(sid):
            now[0]=at(12,21)
            return {'id':sid,'status':'confirmed'}
        monkeypatch.setattr(backend,'inpost_get_shipment',slow_read)
    else:
        original=pickups.Store.update
        def slow_commit(self,job,release=True,**values):
            result=original(self,job,release=release,**values)
            if not release and values.get('attempted')==1:now[0]=at(12,21)
            return result
        monkeypatch.setattr(pickups.Store,'update',slow_commit)
    result=pickups.process_one(backend,'72')
    row=store.rows('72')[0]
    assert result['ok'] is False and backend.created==[]
    assert row['attempted']==int(slow_phase=='intent')
    assert row['state']==('unknown' if slow_phase=='intent' else 'pending')

@pytest.mark.parametrize('collected_sid',['11','12'])
@pytest.mark.parametrize('status',['collected_from_sender','delivered','adopted_at_sorting_center'])
def test_collected_parcel_does_not_block_remaining(tmp_path,monkeypatch,collected_sid,status):
    backend,store=setup_backend(tmp_path,monkeypatch,at(10,0))
    store.enqueue('11',pickup_data());store.enqueue('12',pickup_data())
    monkeypatch.setattr(backend,'inpost_get_shipment',lambda sid:{'status':status if sid==collected_sid else 'confirmed'})
    assert pickups.order_today(backend)['ok']
    assert backend.created==[[sid for sid in ['11','12'] if sid!=collected_sid]]
    assert store.rows(collected_sid)[0]['state']=='collected'
    assert store.rows(collected_sid)[0]['attempted']==0
    assert pickups.dashboard_state(backend)['today']['parcel_count']==1


def test_after_cutoff_parcel_collected_before_tomorrow_never_orders(tmp_path,monkeypatch):
    backend,store=setup_backend(tmp_path,monkeypatch,at(13,0))
    now=clock(monkeypatch,at(13,0))
    store.enqueue('11',pickup_data())
    monkeypatch.setattr(backend,'inpost_get_shipment',lambda sid:{'status':'taken_by_courier'})
    now[0]=at(12,20,12)
    pickups.process_due(backend)
    assert backend.created==[]
    assert store.rows('11')[0]['state']=='collected'
    assert pickups.dashboard_state(backend)['today'] is None
    now[0]=at(12,20,13)
    pickups.process_due(backend)
    assert backend.created==[]
    store.enqueue('12',pickup_data())
    assert pickups._job_date(store.rows('12')[0])=='2026-10-13'


def test_unknown_status_or_read_failure_never_becomes_collected(tmp_path,monkeypatch):
    backend,store=setup_backend(tmp_path,monkeypatch,at(10,0))
    store.enqueue('11',pickup_data())
    monkeypatch.setattr(backend,'inpost_get_shipment',lambda sid:{'status':'mystery'})
    assert not pickups.order_today(backend)['ok']
    assert store.rows('11')[0]['state']=='pending'
    assert backend.created==[]


def test_collection_does_not_erase_uncertain_dispatch(tmp_path,monkeypatch):
    backend,store=setup_backend(tmp_path,monkeypatch,at(10,0))
    store.enqueue('11',pickup_data())
    job=store.claim('11');store.update(job,state='unknown',attempted=1,next_check=0)
    monkeypatch.setattr(backend,'inpost_get_shipment',lambda sid:{'status':'delivered'})
    assert not pickups.order_today(backend)['ok']
    assert store.rows('11')[0]['attempted']==1
    assert store.rows('11')[0]['state']=='unknown'
    assert backend.created==[]
