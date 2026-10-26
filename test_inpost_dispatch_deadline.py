"""The correction form cannot bypass the pickup day or daily cutoff."""
import importlib.util
import json
from pathlib import Path
import sys

from flask import Flask, render_template, render_template_string, request
from jinja2 import ChoiceLoader, DictLoader, FileSystemLoader
import pytest

import inpost_pickups as pickups
from test_inpost_daily_pickups import at, pickup_data, setup_backend


@pytest.fixture
def dispatch(tmp_path, monkeypatch):
    backend, store = setup_backend(tmp_path, monkeypatch, at(10, 0))
    app = Flask('dispatch-deadline-tests')
    app.jinja_loader = ChoiceLoader([
        DictLoader({'base.html': '{% block content %}{% endblock %}'}),
        FileSystemLoader(str(Path(__file__).parent / 'templates')),
    ])
    app.add_url_rule('/orders', 'orders', lambda: '')
    app.add_url_rule('/shipments', 'shipments', lambda: '')
    app.add_url_rule('/pickup-now', 'shipment_pickup_order_now', lambda: '')
    # Load an isolated route module; do not overwrite the real app's globals.
    spec = importlib.util.spec_from_file_location('dispatch_deadline_routes',
        Path(__file__).parent / 'routes' / 'shipping.py')
    routes = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(routes)
    routes.register_routes(dict(app=app, request=request, json=json,
        norm=lambda value: str(value or '').strip(), render_template_string=render_template_string))
    monkeypatch.setitem(sys.modules, 'app', backend)
    monkeypatch.setattr(pickups, 'company_pickup', lambda b: pickup_data())
    return app.test_client(), backend, store, app


def correct(client, sid='11'):
    return client.post('/inpost/dispatch', data=dict(pickup_data(), street='Nowa 2',
        shipment_id=sid, action='correct'))


def due_again(backend):
    db = backend.conn()
    db.execute('UPDATE inpost_pickup_jobs SET next_check=0')
    db.commit()
    db.close()


def test_manual_address_correction_can_order_today_before_cutoff(dispatch):
    client, backend, store, _ = dispatch
    store.enqueue('11', pickup_data())
    response = correct(client)
    assert response.status_code == 200
    assert backend.created == [['11']]
    assert json.loads(store.rows('11')[0]['pickup_json'])['street'] == 'Nowa 2'


@pytest.mark.parametrize('when,scheduled_day', [
    (at(12, 21), '2026-10-09'),
    (at(10, 0), '2026-10-12'),
    (at(10, 0, day=10), '2026-10-10'),
])
def test_correcting_outside_window_only_saves_address(dispatch, monkeypatch, when, scheduled_day):
    client, backend, store, _ = dispatch
    store.enqueue('11', pickup_data())
    db = backend.conn()
    payload = json.loads(store.rows('11')[0]['pickup_json'])
    payload[pickups.DAILY_META]['scheduled_date'] = scheduled_day
    db.execute('UPDATE inpost_pickup_jobs SET pickup_json=?', (json.dumps(payload),))
    db.commit()
    db.close()
    monkeypatch.setattr(pickups, '_local_now', lambda: when)
    response = correct(client)
    assert response.status_code == 200
    assert backend.created == []
    row = store.rows('11')[0]
    assert row['attempted'] == 0
    assert json.loads(row['pickup_json'])['street'] == 'Nowa 2'
    assert pickups._job_date(row) == scheduled_day
    assert 'Dane odbioru zapisane.' in response.text
    assert 'Zapisz dane odbioru' in response.text
    assert 'Zapisz dane i zamów kuriera' not in response.text


@pytest.mark.parametrize('uncertain', [False, True])
def test_existing_attempt_is_only_reconciled_after_cutoff(dispatch, monkeypatch, uncertain):
    client, backend, store, _ = dispatch
    store.enqueue('11', pickup_data())
    assert correct(client).status_code == 200
    if uncertain:
        db = backend.conn()
        db.execute("UPDATE inpost_pickup_jobs SET dispatch_id=NULL,state='unknown'")
        db.commit()
        db.close()
    due_again(backend)
    monkeypatch.setattr(pickups, '_local_now', lambda: at(13, 0))
    reads = []
    original = backend.inpost_find_dispatch_order if uncertain else backend.inpost_get_dispatch_order
    def observed(value):
        reads.append(value)
        return original(value)
    monkeypatch.setattr(backend, 'inpost_find_dispatch_order' if uncertain else 'inpost_get_dispatch_order', observed)
    response = client.post('/inpost/dispatch', data={'shipment_id': '11'})
    assert response.status_code == 200
    assert reads and backend.created == [['11']]
    assert store.rows('11')[0]['dispatch_id'] == '900'


def test_unknown_shipment_cannot_process_entire_queue(dispatch):
    client, backend, store, _ = dispatch
    store.enqueue('11', pickup_data())
    for sid in ('', 'unknown'):
        assert client.post('/inpost/dispatch', data={'shipment_id': sid}).status_code == 404
    assert not backend.created


def test_pickup_error_is_visible_once_without_false_deadline_promise(dispatch):
    _, _, _, app = dispatch
    error = 'Nie zlecono podjazdu do 12:20. Kurier nie jest zamówiony.'
    summary = dict(parcel_count=2, date='2026-10-09', ordered=False, label='Podjazd oczekuje', error=error)
    with app.test_request_context():
        html = render_template('shipments.html', pickup=dict(today=summary, next=summary,
            cutoff='12:20', can_order=False), active=[], history=[], q='', message='', pickup_notice='')
    assert html.count(error) == 1
    assert 'role="alert"' in html and 'Sprawdź podjazd' in html
    assert 'automat najpóźniej' not in html


def test_unknown_attempt_does_not_claim_confirmed_pickup(dispatch):
    _, _, _, app = dispatch
    summary = dict(parcel_count=2, date='2026-10-09', ordered=True,
                   confirmed=False, label='Wynik zamówienia podjazdu wymaga sprawdzenia', error='')
    with app.test_request_context():
        html = render_template('shipments.html', pickup=dict(today=summary, next=None,
            cutoff='12:20', can_order=False), active=[], history=[], q='', message='', pickup_notice='')
    assert 'wynik podjazdu wymaga sprawdzenia' in html
    assert 'podjazd zamówiony' not in html
    assert 'automat najpóźniej' not in html
