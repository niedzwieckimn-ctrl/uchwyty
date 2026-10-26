"""Readable carrier references retain the exact durable recovery identifier."""
import json
import re

import pytest

import inpost_module
from test_shipping_recipient import b, f, CUSTOM, ready, flow
from test_fulfillment_orchestrator import run, state


@pytest.mark.parametrize('reference', ['ZAM-2610201, ZAM-2610221, ZAM-2610241', 'ZAM-123 ' * 30, ''])
def test_readable_reference_is_identical_in_claim_and_carrier_payload(ready, monkeypatch, reference):
    captured = []
    monkeypatch.setattr(b, 'create_courier_shipment', inpost_module.create_courier_shipment)
    monkeypatch.setattr(inpost_module, 'organization_id', lambda: '123456')
    monkeypatch.setattr(inpost_module, '_request',
                        lambda path, method='GET', payload=None, **kw: captured.append(payload) or {'id': 987})
    parcel = dict(length=400, width=300, height=200, weight=5, comments='Odbior od strony bramy')
    with b.app.test_request_context():
        first = f.safe_create_shipment(702, CUSTOM, parcel, reference, 'inpost_courier_standard', {})
        assert f.safe_create_shipment(702, CUSTOM, parcel, reference, 'inpost_courier_standard', {}) == first
    assert len(captured) == 1
    db = b.conn()
    saved = dict(db.execute('SELECT * FROM fulfillment_shipping_attempts').fetchone())
    db.close()
    expected_prefix = (' '.join(reference.split()) or 'Przesylka')[:63].rstrip()
    assert saved['reference'] == captured[0]['reference']
    assert saved['reference'].startswith(expected_prefix + ' / P-')
    assert re.search(r' / P-[a-f0-9]{32}$', saved['reference'])
    assert len(saved['reference']) <= 100
    assert 'fulfillment' not in saved['reference']
    assert captured[0]['comments'] == parcel['comments']
    assert json.loads(saved['payload'])['parcel']['comments'] == parcel['comments']


@pytest.mark.parametrize('legacy', [False, True])
def test_uncertain_new_and_legacy_references_are_recovered_without_second_post(ready, monkeypatch, legacy):
    calls = []
    def lost(*args):
        calls.append(args)
        raise TimeoutError('accepted but response lost')
    monkeypatch.setattr(b, 'create_courier_shipment', lost)
    parcel = dict(length=400, width=300, height=200, weight=5)
    with b.app.test_request_context():
        with pytest.raises(TimeoutError):
            f.safe_create_shipment(702, CUSTOM, parcel, 'ZAM-2610201', 'inpost_courier_standard', {})
        with pytest.raises(inpost_module.InPostError, match='niepewny'):
            f.safe_create_shipment(702, CUSTOM, parcel, 'ZAM-2610201', 'inpost_courier_standard', {})
    reference = calls[0][2]
    if legacy:
        reference = 'fulfillment-8d4f77ae-a14e-4d52-9762-e89085939daa'
        db = b.conn()
        db.execute('UPDATE fulfillment_shipping_attempts SET reference=?', (reference,))
        db.commit(); db.close()
    reads = []
    monkeypatch.setattr(inpost_module, 'organization_id', lambda: '123456')
    def lookup(path, *args, **kwargs):
        reads.append(path)
        return {'items': [{'id': 123456, 'reference': reference, 'tracking_number': 'TRACK-TEST'}]}
    monkeypatch.setattr(inpost_module, '_request', lookup)
    assert run('shipping.shipment.refresh').status == 'SUCCESS'
    assert state()['shipment']['exists']
    assert len(calls) == 1
    assert len(reads) == 1 and '/organizations/123456/shipments?' in reads[0]
