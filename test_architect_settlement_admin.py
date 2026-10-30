from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import pytest
import app as b
import routes.architects as a
from test_auth_resilience import admin_client
from test_internal_rbac import _create_actor


def test_today_payout_uses_actual_time_including_before_noon():
    now = datetime(2026,10,28,8,15,tzinfo=ZoneInfo('Europe/Warsaw'))
    assert datetime.fromisoformat(a.payout_timestamp('2026-10-28',now)) == now


def test_past_payout_uses_timezone_for_selected_date():
    now = datetime(2026,10,28,8,15,tzinfo=ZoneInfo('Europe/Warsaw'))
    paid = datetime.fromisoformat(a.payout_timestamp('2026-07-01',now))
    assert paid.utcoffset() == timedelta(hours=2)
    assert paid.date().isoformat() == '2026-07-01'


@pytest.mark.parametrize('value',['2026-10-29','','2026-02-30'])
def test_invalid_payout_date_is_rejected(value):
    with pytest.raises(ValueError):
        a.payout_timestamp(value,datetime(2026,10,28,8,tzinfo=timezone.utc))


def test_payout_route_sends_current_timestamp(admin_client,monkeypatch):
    monkeypatch.setattr(b,'_rate_hits',{})
    admin_client.post('/login',data={'username':'admin','password':'correct-test-only'})
    with admin_client.session_transaction() as state:
        token=state['csrf_token']
    calls=[]
    now=datetime(2026,10,28,8,15,tzinfo=ZoneInfo('Europe/Warsaw'))
    monkeypatch.setattr(a,'app_now',lambda:now)
    monkeypatch.setattr(a,'supabase_request',lambda *args,**kwargs:calls.append((args,kwargs)))
    result=admin_client.post('/architects',data={'csrf_token':token,'action':'record_payout',
        'settlement_id':'11111111-1111-4111-8111-111111111111',
        'paid_at':'2026-10-28','payment_reference':'SIMULATED-TEST'})
    assert result.status_code==302
    assert datetime.fromisoformat(calls[0][1]['payload']['p_paid_at'])==now


def test_warehouse_actor_cannot_manage_architect_program(admin_client,monkeypatch):
    monkeypatch.setattr(b,'_rate_hits',{})
    admin_client.post('/login',data={'username':'admin','password':'correct-test-only'})
    actor=_create_actor('HUMAN','MAGAZYN')
    with admin_client.session_transaction() as state:
        state['internal_actor_id']=actor
    assert admin_client.get('/architects').status_code==403
    assert admin_client.get('/architects/11111111-1111-4111-8111-111111111111').status_code==403


def test_external_role_cannot_manage_architect_program():
    response=b.app.test_client().get('/architects',headers={'Authorization':'Bearer architect-or-distributor'})
    assert response.status_code==302
    assert '/login' in response.headers['Location']


def test_admin_renders_existing_sample_items_without_dict_method_collision(admin_client, monkeypatch):
    monkeypatch.setattr(b, '_rate_hits', {})
    admin_client.post('/login', data={'username': 'admin', 'password': 'correct-test-only'})
    sample_id = '11111111-1111-4111-8111-111111111111'
    def read(path, **kwargs):
        if path.endswith('/architect_sample_orders'):
            return [{'id': sample_id, 'architect_id': 'test-architect',
                     'shipping_address': 'STAGING', 'status': 'requested',
                     'tracking_no': '', 'created_at': '2026-10-28'}]
        if path.endswith('/architect_sample_items'):
            return [{'sample_order_id': sample_id, 'product_id': 503, 'qty': 1}]
        if path.endswith('/client_stock_catalog_v'):
            return [{'product_id': 503, 'sku': 'STAGING-MOS-003', 'model': 'TEST', 'name': 'Uchwyt'}]
        return []
    monkeypatch.setattr(a, 'supabase_request', read)
    response = admin_client.get('/architects?view=settings')
    assert response.status_code == 200
    assert 'Warianty: STAGING-MOS-003' in response.get_data(as_text=True)
    assert 'Rozliczenia miesięczne' in response.get_data(as_text=True)
