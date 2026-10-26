"""InPost address parsing and form regressions; all carrier IO is mocked."""
import pytest

import inpost_module
from test_shipping_recipient import b, context, form, request, ready, flow


@pytest.mark.parametrize('address,street,number', [
    ('Bartycka 24/26/PAW.13', 'Bartycka', '24/26/PAW.13'),
    ('Bartycka 24/26/paw.13', 'Bartycka', '24/26/paw.13'),
    ('Testowa 10', 'Testowa', '10'),
    ('Testowa 10A', 'Testowa', '10A'),
    ('Długa 10/2', 'Długa', '10/2'),
    ('Testowa 24-26', 'Testowa', '24-26'),
    ('3 Maja 10', '3 Maja', '10'),
    ('  Bartycka, 24/26/PAW.13,  ', 'Bartycka', '24/26/PAW.13'),
])
def test_address_reaches_carrier_payload_without_losing_building_number(monkeypatch, address, street, number):
    captured = []
    monkeypatch.setattr(inpost_module, 'organization_id', lambda: '123456')
    monkeypatch.setattr(inpost_module, '_request',
                        lambda path, method='GET', payload=None, **kw: captured.append(payload) or {'id': 987})
    inpost_module.create_courier_shipment(
        dict(name='Odbiorca', street=address, city='Warszawa', post_code='00-001',
             phone='500600700', email='test@example.invalid'),
        dict(length=400, width=300, height=200, weight=5, quantity=1), 'ZAM-TEST')
    assert captured[0]['receiver']['address']['street'] == street
    assert captured[0]['receiver']['address']['building_number'] == number
    assert inpost_module.split_street_building(address, strict=True) == (street, number)


@pytest.mark.parametrize('address', ['', 'Bez numeru', '24/26/PAW.13', ', 13', 'Testowa numer 1?'])
def test_strict_address_parser_rejects_missing_or_invalid_parts(address):
    with pytest.raises(inpost_module.InPostError, match='Podaj ulicę i numer'):
        inpost_module.split_street_building(address, strict=True)


@pytest.mark.parametrize('mode', ['client', 'custom'])
def test_pavilion_address_can_be_booked_once_without_changing_customer(ready, monkeypatch, mode):
    address = 'Bartycka 24/26/PAW.13'
    if mode == 'client':
        monkeypatch.setattr(b, '_client_profile_for_email',
                            lambda email: {'address': address + ', 00-001 Warszawa', 'phone': '500600700'})
    db = b.conn()
    tables = ('orders', 'invoices', 'invoice_meta', 'invoice_allocations', 'stock')
    customer = tuple(db.execute('SELECT customer_address,customer_name FROM orders WHERE id=702').fetchone())
    before = {t: [tuple(r) for r in db.execute('SELECT * FROM ' + t)] for t in tables[1:]}
    db.close()
    data = form(mode, recipient_street=address)
    assert request(data).status_code == 302
    assert len(ready['calls']) == 1
    assert ready['calls'][0][0]['street'] == address
    assert context()['saved']['payload']['receiver']['street'] == address
    assert request(data).status_code == 302
    assert len(ready['calls']) == 1
    db = b.conn()
    assert tuple(db.execute('SELECT customer_address,customer_name FROM orders WHERE id=702').fetchone()) == customer
    for table, rows in before.items():
        assert [tuple(r) for r in db.execute('SELECT * FROM ' + table)] == rows
    db.close()


@pytest.mark.parametrize('address', ['Bez numeru', '24/26/PAW.13', ', 13'])
def test_invalid_address_does_not_reserve_or_send_a_shipment(ready, address):
    html = request(form(recipient_street=address, weight='7'))
    assert 'Podaj ulicę i numer' in html
    assert 'name="weight" value="7"' in html
    assert not ready['calls']
    assert context()['saved'] is None
