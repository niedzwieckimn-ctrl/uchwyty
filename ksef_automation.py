"""KSeF worker liveness and owner-only diagnostic view; never submits invoices."""
from datetime import datetime, timedelta
from functools import wraps
import hmac
import os

from flask import jsonify, render_template_string, request, session
from internal_rbac import ACTOR_HUMAN, ALLOW, current_actor_context
import ksef_scheduler as scheduler
from run_ksef_batch import candidates, run_batch

VERSION = '2026-09-29-recovery1'


def register_routes(backend):
    app = backend.app

    def owner_only(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            actor = current_actor_context() if session.get('admin_authenticated') else None
            if actor is None:
                return jsonify(ok=False, error='ADMIN_LOGIN_REQUIRED'), 401
            if actor.actor_type != ACTOR_HUMAN or 'OWNER' not in actor.roles or actor.permission_decision('system.jobs') != ALLOW:
                return jsonify(ok=False, error='ADMIN_ROLE_REQUIRED'), 403
            if request.method == 'POST':
                origin = request.headers.get('Origin', '').rstrip('/')
                supplied = request.form.get('csrf_token') or request.headers.get('X-CSRF-Token', '')
                expected = session.get('csrf_token', '')
                if ((origin and origin != request.host_url.rstrip('/')) or not supplied or not expected
                        or not hmac.compare_digest(supplied.encode(), expected.encode())):
                    return jsonify(ok=False, error='CSRF_REQUIRED'), 403
            return fn(*args, **kwargs)
        return wrapped

    @app.get('/health/ksef-worker')
    def ksef_worker_health():
        alive = bool(scheduler._worker and scheduler._worker.is_alive())
        response = jsonify(ok=alive, version=VERSION, worker_alive=alive)
        response.status_code = 200 if alive else 503
        response.headers['Cache-Control'] = 'no-store'
        return response

    def state():
        now = datetime.now(scheduler.WARSAW)
        run_date = (now.date() if now.hour >= 17 else now.date()-timedelta(days=1)).isoformat()
        result = dict(version=VERSION, local_time=now.isoformat(), run_date=run_date,
                      worker_alive=bool(scheduler._worker and scheduler._worker.is_alive()),
                      automation_start_date=os.environ.get('KSEF_AUTOMATION_START_DATE', ''),
                      configuration=backend.ksef_config_summary())
        try:
            result['run'] = scheduler.Store(backend).state(run_date)
            if result['run']:
                result['run'].pop('lease_token', None)
            result['eligible_invoice_ids_from_cache'] = candidates(now, backend)
        except Exception as exc:
            result['error'] = str(exc)[:500]
        return result

    @app.get('/api/admin/ksef/automation')
    @owner_only
    def ksef_automation_state():
        response = jsonify(state())
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.route('/admin/ksef/automation', methods=['GET', 'POST'])
    @owner_only
    def ksef_automation_page():
        report = state()
        if request.method == 'POST':
            try:
                report['dry_run'] = run_batch(backend, send=False)
            except Exception as exc:
                report['dry_run'] = {'ok': False, 'error': str(exc)[:500]}
            report.update(state())
        response = app.make_response(render_template_string('''
        {% extends "base.html" %}{% block content %}
        <div class="card"><h1>Automatyczna wysyłka KSeF</h1>
        <p>Nowe faktury: od 17:00 czasu polskiego. Zaległe: po uruchomieniu aplikacji.
        Kolejna kontrola po zakończonym przebiegu: po 5 minutach.</p>
        <p>Proces automatu: <b>{{ 'działa' if report.worker_alive else 'NIE DZIAŁA' }}</b>.</p>
        <form method="post"><input type="hidden" name="csrf_token" value="{{ csrf }}">
        <button type="submit">Sprawdź aktualne dane i kolejkę — bez wysyłania faktur</button></form>
        <p>Kontrola pobiera dane z Supabase i wyświetla faktury zakwalifikowane do wysyłki.
        Nie wysyła XML ani e-maili. Wynik ostatniego przebiegu automatu jest poniżej.</p>
        <pre style="white-space:pre-wrap;overflow-wrap:anywhere">{{ report|tojson(indent=2) }}</pre>
        <a href="/ksef">Wróć do faktur KSeF</a></div>{% endblock %}
        ''', report=report, csrf=session.get('csrf_token', '')))
        response.headers['Cache-Control'] = 'no-store'
        return response
