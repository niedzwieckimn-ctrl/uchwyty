import logging
import sqlite3
from datetime import datetime
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
