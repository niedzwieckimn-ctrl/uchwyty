"""Recipient choice through the real panel service; carrier/cloud IO is fake."""
import copy
import json

import pytest
import app as b
import fulfillment_operations as f
from test_fulfillment_orchestrator import flow, docs, state, run
from test_agent_shipping_draft47 import parcel, ORDER_IDS

CUSTOM = dict(name='Odbiorca dropshipping', street='Długa 10/2', post_code='30-001',
              city='Kraków', phone='+48 600 700 800', email='odbiorca@example.invalid')
FORM = dict(service='inpost_courier_standard', quantity='1', length='40', width='30',
            height='20', weight='5', insurance='0', cod='0')


def context(oid=702):
    with b.app.test_request_context():
        return f.shipment_recipient_context(oid)


def form(mode='custom', oid=702, **overrides):
    return {**FORM, 'recipient_mode': mode, 'recipient_fingerprint': context(oid)['fingerprint'],
            **{'recipient_' + k: v for k, v in CUSTOM.items()}, **overrides}


def request(data=None, oid=702):
    with b.app.test_request_context('/orders/%s/inpost' % oid,
                                   method='POST' if data is not None else 'GET', data=data):
        b._refresh_domain_route_context()
        return b.order_inpost_create_service(oid, request=b.request, session={})


@pytest.fixture
def ready(flow):
    docs()
    return flow


def test_form_shows_default_and_custom_fields(ready):
    html = request()
    assert 'Odbiorca przesyłki' in html and 'Inny odbiorca — dropshipping' in html
    assert 'Testowa 1' in html and '00-001 Warszawa' in html
    assert 'hidden disabled' in html
    assert '>Nadaj przesyłkę</button>' in html
    assert not ready['calls']


def test_custom_booking_preserves_customer_invoice_and_documents(ready):
    db = b.conn()
    before = {t: [tuple(r) for r in db.execute('SELECT * FROM ' + t)] for t in
              ('invoices', 'invoice_meta', 'invoice_allocations', 'order_shipping_requirements', 'stock')}
    customer = tuple(db.execute('SELECT customer_name,customer_address,customer_email,customer_phone FROM orders WHERE id=702').fetchone())
    db.close()
    assert request(form()).status_code == 302
    assert ready['calls'][0][0] == CUSTOM
    assert context()['saved']['payload']['receiver'] == CUSTOM
    assert state()['requirements']['recipient'] == CUSTOM
    assert state()['shipment']['parameters_need_review'] is False
    assert run('shipping.requirements.update', recipient_phone='500500500').error_code == 'RECIPIENT_LOCKED'
    db = b.conn()
    assert customer == tuple(db.execute('SELECT customer_name,customer_address,customer_email,customer_phone FROM orders WHERE id=702').fetchone())
    for table, rows in before.items():
        assert [tuple(r) for r in db.execute('SELECT * FROM ' + table)] == rows
    db.close()
    html = request()
    assert 'Odbiorca zapisany' in html and CUSTOM['street'] in html
    assert 'Przygotuj kolejne nadanie dla tej listy' not in html
    assert 'name="recipient_mode"' not in html
    request(form(recipient_street='Inna 99'))
    assert len(ready['calls']) == 1


def test_client_choice_ignores_custom_fields(ready):
    expected = context()['default']
    assert request(form('client')).status_code == 302
    assert ready['calls'][0][0] == expected


@pytest.mark.parametrize('key,value', [('name',''), ('street','Bez numeru'), ('post_code','30001'),
                                    ('phone','123'), ('email','invalid'), ('city','x'*201)])
def test_invalid_recipient_retains_input_and_never_calls_carrier(ready, key, value):
    html = request(form(**{'recipient_' + key: value, 'weight':'7', 'sms':'1',
                          'service':'inpost_courier_express_1200'}))
    assert 'name="recipient_mode"' in html
    assert 'name="weight" value="7"' in html
    assert ('Odbiorca dropshipping' in html) if key != 'name' else ('name="recipient_name" type="text" value=""' in html)
    assert 'inpost_courier_express_1200" selected' in html
    assert not ready['calls'] and context()['saved'] is None


def test_stale_form_blocks_booking(ready, monkeypatch):
    data = form()
    monkeypatch.setattr(b, '_client_profile_for_email', lambda email: {'address':'Nowa 2, 00-002 Warszawa','phone':'501502503'})
    assert 'Dane klienta lub paczki zmieniły się' in request(data)
    assert not ready['calls']


def test_lost_carrier_response_freezes_recipient_and_blocks_retry(ready, monkeypatch):
    calls = []
    def create(*args):
        calls.append(args)
        raise TimeoutError('lost carrier response')
    monkeypatch.setattr(b, 'create_courier_shipment', create)
    html = request(form())
    assert 'Dane odbiorcy są zablokowane' in html
    assert 'name="recipient_mode"' not in html
    assert context()['saved']['state'] == 'UNKNOWN'
    assert context()['saved']['payload']['receiver'] == CUSTOM
    request(form(recipient_street='Zmiana 99'))
    assert len(calls) == 1 and calls[0][0] == CUSTOM


def test_success_retry_uses_saved_recipient_and_parcel_without_carrier_post(ready):
    request(form())
    db = b.conn()
    db.execute("UPDATE orders SET inpost_shipment_id='',tracking_no='' WHERE id=702")
    db.commit(); db.close()
    # The recovery button does not submit the disabled parcel fields.
    assert request({}).status_code == 302
    assert len(ready['calls']) == 1
    assert context()['saved']['payload']['receiver'] == CUSTOM


def test_confirmed_receipt_shows_label_without_recovery_post(ready, monkeypatch):
    request(form())
    db = b.conn()
    db.execute("UPDATE orders SET inpost_shipment_id='',tracking_no='' WHERE id=702")
    db.commit(); db.close()
    html = request()
    assert '>Pokaż etykietę</a>' in html
    assert 'Odczytaj zapisany wynik' not in html and '>Nadaj przesyłkę</button>' not in html
    labels = []
    monkeypatch.setattr(b, 'inpost_get_label', lambda sid, *a: labels.append(sid) or b'%PDF-1.4\nlabel')
    with b.app.test_request_context('/orders/702/inpost/label'):
        b._refresh_domain_route_context()
        response = b.order_inpost_label(702)
        assert response.status_code == 200 and response.mimetype == 'application/pdf'
        response.close()
    assert labels == ['123456']
    assert len(ready['calls']) == 1 and len(ready['pickup']) == 1
    db = b.conn()
    assert db.execute('SELECT inpost_shipment_id FROM orders WHERE id=702').fetchone()[0] == ''
    db.close()


def test_next_package_defaults_to_client_without_overwriting_requirements(ready):
    request(form())
    db = b.conn()
    # Simulate a different current logical package; old claims remain as evidence.
    db.execute("UPDATE packing_batches SET packing_list_id='new-package',selection_hash='new-selection'")
    db.commit(); db.close()
    assert context()['saved'] is None
    assert context()['default']['street'] == 'Testowa 1'
    assert f.effective_receiver(f.snapshot(702))['street'] == 'Testowa 1'


def test_same_recipient_visible_from_every_combined_package_member(parcel):
    # This fixture deliberately has no invoice for the current parcel; the
    # carrier reservation itself is tested here, without changing invoice rules.
    ctx = context()
    with b.app.test_request_context():
        f.safe_create_shipment(702, CUSTOM, {'length':400,'width':300,'height':200,'weight':5}, '',
                               'inpost_courier_standard', {},
                               recipient_selection={'mode':'custom','scope':ctx['scope']})
    for oid in ORDER_IDS:
        assert context(oid)['saved']['payload']['receiver'] == CUSTOM
        assert f.effective_receiver(f.snapshot(oid)) == CUSTOM
        assert f.shipment_content(f.snapshot(oid)) == f.shipment_content(f.snapshot(702))
    assert len(parcel['calls']) == 1


def test_cloud_claim_restores_recipient_and_success_after_local_loss(ready, monkeypatch):
    request(form())
    db = b.conn()
    saved = dict(db.execute('SELECT * FROM fulfillment_shipping_attempts').fetchone())
    saved['payload'] = json.loads(saved['payload'])
    saved['provider_json'] = json.loads(saved['provider_json'])
    db.execute('DELETE FROM fulfillment_shipping_members')
    db.execute('DELETE FROM fulfillment_shipping_attempts')
    db.execute("UPDATE orders SET inpost_shipment_id='',tracking_no='' WHERE id=702")
    db.commit(); db.close()
    def remote(path, method='GET', **kw):
        if path.endswith('/fulfillment_reconciliation'):
            return []
        if path.endswith('/fulfillment_shipping_claims'):
            return [copy.deepcopy(saved)]
        if path.endswith('/claim_fulfillment_shipment'):
            return {'acquired':False, 'claim':copy.deepcopy(saved)}
        raise AssertionError(path)
    monkeypatch.setattr(b, 'supabase_enabled', lambda: True)
    monkeypatch.setattr(b, 'supabase_request', remote)
    monkeypatch.setattr(b, 'sync_local_rows_to_supabase', lambda *a: None)
    assert context()['saved']['payload']['receiver'] == CUSTOM
    assert request(form(recipient_street='Zmiana 99')).status_code == 302
    assert len(ready['calls']) == 1


def test_output_escapes_recipient_text(ready):
    html = request(form(recipient_name='<script>alert(1)</script>', recipient_email='invalid'))
    assert '<script>alert(1)</script>' not in html
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in html
