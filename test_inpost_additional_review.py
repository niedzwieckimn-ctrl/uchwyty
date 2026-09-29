"""Independent acceptance assertions for the InPost patch; all external IO is fake."""
import json
import sqlite3
import time
import urllib.request

import pytest
import app as b
import inpost_tracking as tracking
import email_module
import packing_versions
import inpost_history
from test_fulfillment_orchestrator import flow
from test_agent_shipping_draft47 import parcel, ORDER_IDS
from test_inpost_tracking47 import tracked, _order

_REAL_SHIPPED_EMAIL = b._send_orders_shipped_email


def _tracking_state():
    db=b.conn()
    try:return tracking.read(db,'123456')
    finally:db.close()


def test_real_mail_adapter_timeout_remains_unknown(tracked, monkeypatch):
    monkeypatch.setattr(b,'_send_orders_shipped_email',_REAL_SHIPPED_EMAIL)
    monkeypatch.setattr(b,'send_email',email_module.send_email)
    monkeypatch.setattr(email_module,'email_config_summary',lambda: {
        'enabled':True,'configured':True,'from':'review@example.invalid','missing':[]})
    def timeout(*args,**kwargs): raise TimeoutError('review: response lost after possible acceptance')
    monkeypatch.setattr(urllib.request,'urlopen',timeout)
    result=tracking.process(b,_order(702),source='webhook')
    db=b.conn()
    rows=[json.loads(row[0]) for row in db.execute("SELECT result_json FROM email_events WHERE event_type='order_shipped'")]
    notice=tracking.notification(db,'123456')
    db.close()
    assert rows and all(row.get('delivery_outcome')=='unknown' for row in rows),rows
    assert notice['state']=='unknown', {'notice':notice,'result':result}


def test_sync_conflict_is_not_overwritten_by_next_refresh(tracked, monkeypatch):
    monkeypatch.setattr(b,'supabase_enabled',lambda:True)
    monkeypatch.setattr(packing_versions,'sync_evidence',lambda *args:None)
    def cloud_down(*args,**kwargs): raise ConnectionError('review cloud unavailable')
    monkeypatch.setattr(b,'supabase_update_rows',cloud_down)
    first=tracking.process(b,_order(702),source='webhook')
    assert first['sync_state']=='pending'
    b.sqlite_upsert_rows('orders',[{**_order(702),'inpost_shipment_id':'NEWER-REMOTE-SHIPMENT','tracking_no':'NEW-TRACK'}],'id')
    assert _tracking_state()['sync_state']=='conflict'
    pushed=[]
    monkeypatch.setattr(b,'supabase_update_rows',lambda *args,**kwargs:pushed.append((args,kwargs)))
    second=tracking.process(b,_order(702),source='webhook')
    assert not pushed, {'overwrote_remote_with':pushed,'result':second,'state':_tracking_state()}
    assert _tracking_state()['sync_state']=='conflict'


def test_crash_after_local_commit_preserves_durable_sync_intent(tracked, monkeypatch):
    monkeypatch.setattr(b,'supabase_enabled',lambda:True)
    class SimulatedProcessDeath(BaseException): pass
    original=tracking.mark_result
    def die_before_pending(backend,sid,result,error='',sync_state=None):
        if result=='applied' and sync_state=='pending':
            raise SimulatedProcessDeath('after commit, before sync marker')
        return original(backend,sid,result,error,sync_state)
    monkeypatch.setattr(tracking,'mark_result',die_before_pending)
    with pytest.raises(SimulatedProcessDeath):
        tracking.process(b,_order(702),source='webhook')
    assert _order(702)['status']=='partially_shipped'
    # A fresh connection pulls the old remote value after the interrupted write.
    b.sqlite_upsert_rows('orders',[{**_order(702),'status':'packed_partial'}],'id')
    assert _order(702)['status']=='partially_shipped', _tracking_state()


def test_terminal_status_does_not_abandon_unapplied_orders(tracked,monkeypatch):
    monkeypatch.setattr(b,'inpost_get_shipment',lambda sid: {
        'id':sid,'tracking_number':'TRACK-TEST','status':'delivered'})
    original=b.apply_verified_inpost_status
    def transient_failure(*args,**kwargs): raise sqlite3.OperationalError('review temporary write failure')
    monkeypatch.setattr(b,'apply_verified_inpost_status',transient_failure)
    first=tracking.process(b,_order(702),source='poll')
    assert not first['ok'] and _order(702)['status']=='packed_partial'
    monkeypatch.setattr(b,'apply_verified_inpost_status',original)
    db=b.conn();db.execute("UPDATE inpost_tracking_state SET next_check_at=0 WHERE shipment_id='123456'");db.commit();db.close()
    monkeypatch.setattr(tracking.time,'sleep',lambda _:None)
    tracking.process_due(b)
    assert _order(702)['status']=='partially_shipped',_tracking_state()


def test_historical_member_does_not_block_remaining_current_members(tracked,monkeypatch):
    assert tracking.process(b,_order(702),source='webhook')['ok']
    inpost_history.prepare_next(b,[704])
    db=b.conn()
    db.execute("UPDATE orders SET inpost_shipment_id='NEW-PARCEL',tracking_no='NEW-TRACK',status='packed_partial' WHERE id=704")
    db.commit();db.close()
    monkeypatch.setattr(b,'inpost_get_shipment',lambda sid: {
        'id':sid,'tracking_number':'TRACK-TEST','status':'adopted_at_sorting_center'})
    result=tracking.process(b,_order(702),source='webhook')
    assert result['ok'],result
    assert _order(704)['inpost_shipment_id']=='NEW-PARCEL'
    assert _order(704)['status']=='packed_partial'
    assert len(tracked['mail'])==1


def test_notification_receipt_failure_does_not_claim_orders_unapplied(tracked,monkeypatch):
    def disk_failure(*args,**kwargs): raise sqlite3.OperationalError('review receipt write failed')
    monkeypatch.setattr(tracking,'notification_finish',disk_failure)
    result=tracking.process(b,_order(702),source='webhook')
    assert len(tracked['mail'])==1
    assert _order(702)['status']=='partially_shipped'
    assert result.get('orders_applied') is True,result


def test_official_newer_branch_scan_is_not_discarded_as_older():
    # Consecutive events in the provider's official Tracking example:
    # https://dokumentacja-inpost.atlassian.net/wiki/spaces/PL/pages/18153479
    old={'status_code':'sent_from_source_branch','status_event_at':'2023-12-17T19:10:51+01:00'}
    assert tracking._older(old,'adopted_at_source_branch','2023-12-18T05:56:59+01:00') is False
