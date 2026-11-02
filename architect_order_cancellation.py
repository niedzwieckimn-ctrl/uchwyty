"""Admin cancellation of an unfulfilled promotion order; cloud owns the commit."""
import re

from flask import request, session, redirect, url_for, render_template_string


PAGE = '''{% extends "base.html" %}{% block content %}
<div class="card" style="max-width:850px"><h1>Anulowanie zamówienia</h1>
<p><strong>{{ order['order_no'] }}</strong></p>
{% if error %}<p role="alert" style="color:#b42318">{{ error }}</p>{% endif %}
{% if result %}
<p role="status"><strong>Zamówienie anulowane. Historia została zachowana.</strong></p>
<p>Kod: <strong>{{ result['code'] }}</strong>.
{% if result['code_status']=='active' %}Można go ponownie użyć u przypisanego dystrybutora, w dotychczasowym terminie ważności.
{% elif result['code_status']=='expired' %}Termin ważności kodu upłynął. Architekt może wygenerować nowy kod.
{% else %}Aktualny status kodu: {{ result['code_status'] }}.{% endif %}</p>
{% else %}
<p>Anulowanie zachowa pozycje, ceny i historię wykorzystania kodu. Kod zostanie zwolniony do nowego zamówienia. Jego termin ważności pozostanie bez zmian.</p>
<p>Powiązaną fakturę, wydanie magazynowe lub przesyłkę trzeba wcześniej wycofać właściwą operacją. Anulowanie nie wysyła e-maila.</p>
<form method="post"><label for="cancellation-reason">Powód anulowania</label>
<textarea id="cancellation-reason" name="reason" minlength="5" maxlength="1000" required style="display:block;width:100%;margin:12px 0">{{ reason }}</textarea>
<button class="btn danger" type="submit">Anuluj zamówienie i zwolnij kod</button></form>
{% endif %}
<p><a class="btn" href="{{ url_for('order_view',order_id=order['id']) }}">Wróć do zamówienia</a>
<a class="btn" href="{{ url_for('orders') }}">Lista zamówień</a></p></div>
{% endblock %}'''


def _redemption(b, oid):
    rows = b.supabase_request('/rest/v1/architect_code_redemptions', params={
        'select': 'id,code_id,cancelled_at', 'order_id': f'eq.{oid}', 'limit': 1}, timeout=10) or []
    return rows[0] if rows else None


def _result(b, oid):
    """Reconcile an ambiguous POST by reading durable state, never resending it."""
    row = _redemption(b, oid)
    if not row or not row.get('cancelled_at'):
        return None
    orders = b.supabase_request('/rest/v1/orders', params={
        'select': 'id,status', 'id': f'eq.{oid}', 'limit': 1}, timeout=10) or []
    codes = b.supabase_request('/rest/v1/architect_codes', params={
        'select': 'code,status', 'id': f"eq.{row['code_id']}", 'limit': 1}, timeout=10) or []
    if not orders or orders[0]['status'] != 'cancelled' or not codes:
        return None
    return dict(ok=True, order_id=oid, status='cancelled', cancelled_at=row['cancelled_at'],
                code=codes[0]['code'], code_status=codes[0]['status'])


def _cache_cancelled(b, oid):
    db = b.conn()
    try:
        db.execute("UPDATE orders SET status='cancelled' WHERE id=?", (oid,))
        db.commit()
    finally:
        db.close()


def _local_preflight(b, oid):
    import fulfillment_operations
    state = fulfillment_operations.snapshot(oid)
    order = state['order']
    if any(int(member) != oid for member in state.get('package_ids', [oid])):
        raise ValueError('Zamówienie jest na wspólnej liście pakowej. Najpierw wycofaj lub popraw tę listę.')
    if state['invoices'] or state['allocations']:
        raise ValueError('Zamówienie ma fakturę. Najpierw wycofaj ją właściwą operacją.')
    if order.get('warehouse_issued'):
        raise ValueError('Najpierw wycofaj wydanie magazynowe. Kod nie został zwolniony.')
    if order.get('tracking_no') or order.get('inpost_shipment_id') or state['attempts']:
        raise ValueError('Najpierw rozlicz lub wycofaj przesyłkę i jej rozpoczęte operacje.')
    if state['intents'] or state['persistence']['pending']:
        raise ValueError('Trwa zapis dokumentów lub synchronizacja. Dokończ je przed anulowaniem.')


def register(b):
    app = b.app

    @app.before_request
    def architect_cancelled_order_guard():
        match = re.fullmatch(r'/orders/(\d+)(?:/(.*))?', request.path)
        if not match or not session.get('admin_authenticated') or not b.supabase_enabled():
            return None
        oid, tail = int(match[1]), match[2] or ''
        if tail == 'cancel':
            return None
        mutation = request.method not in {'GET', 'HEAD', 'OPTIONS'}
        document_write = request.method == 'GET' and (
            tail in {'invoice', 'packing-list', 'issue'} or tail.startswith('inpost/create'))
        if not mutation and not document_write:
            return None
        try:
            row = _redemption(b, oid)
            if row and (tail == 'delete' or (tail == 'status' and request.form.get('status') == 'cancelled')):
                return redirect(url_for('architect_order_cancel', order_id=oid), code=303)
            if row and row.get('cancelled_at'):
                return render_template_string('''{% extends "base.html" %}{% block content %}
                    <div class="card"><h1>Zamówienie anulowane</h1><p>Nie można ponownie go realizować.</p>
                    <a class="btn" href="{{ url_for('architect_order_cancel',order_id=oid) }}">Sprawdź stan kodu</a></div>
                    {% endblock %}''', oid=oid), 409
        except Exception:
            app.logger.exception('ARCHITECT_CANCEL_GUARD order_id=%s', oid)
            return 'Nie można potwierdzić stanu zamówienia. Operacja nie została rozpoczęta.', 503
        return None

    @app.route('/orders/<int:order_id>/cancel', methods=['GET', 'POST'])
    def architect_order_cancel(order_id):
        # Existing application admin gate and CSRF check run before this handler.
        if not session.get('admin_authenticated'):
            return 'Wymagane uprawnienia administratora.', 403
        db = b.conn()
        try:
            row = db.execute('SELECT * FROM orders WHERE id=?', (order_id,)).fetchone()
            order = dict(row) if row else None
        finally:
            db.close()
        if order is None:
            return 'Nie znaleziono zamówienia.', 404
        error, result, status = '', None, 200
        reason = request.form.get('reason', '').strip()
        try:
            if not b.supabase_enabled():
                raise ValueError('Anulowanie z kodem wymaga połączenia z bazą programu.')
            if not _redemption(b, order_id):
                raise ValueError('To zamówienie nie jest powiązane z kodem architekta.')
            result = _result(b, order_id)
            if request.method == 'POST' and not result:
                if not 5 <= len(reason) <= 1000:
                    raise ValueError('Podaj powód anulowania (5–1000 znaków).')
                from fulfillment_operations import ui_write
                with ui_write(order_id):
                    _local_preflight(b, order_id)
                    try:
                        result = b.supabase_request('/rest/v1/rpc/architect_cancel_order', method='POST',
                            payload={'p_order_id': order_id, 'p_reason': reason, 'p_actor': 'admin'}, timeout=20)
                    except Exception:
                        result = _result(b, order_id)
                        if not result:
                            raise
                    if not isinstance(result, dict) or result.get('ok') is not True:
                        raise ValueError('Nie potwierdzono anulowania. Odśwież stronę przed dalszą pracą.')
            if result:
                _cache_cancelled(b, order_id)
                order['status'] = 'cancelled'
        except ValueError as exc:
            error, status = str(exc), 409
        except Exception as exc:
            app.logger.exception('ARCHITECT_CANCEL order_id=%s', order_id)
            # HTTP errors expose provider JSON only in server logs, not in UI.
            detail = ''
            if hasattr(exc, 'read'):
                try:
                    import json
                    detail = json.loads(exc.read().decode('utf-8')).get('message', '')
                except Exception:
                    pass
            error = detail or 'Nie potwierdzono anulowania. Odśwież stronę, aby sprawdzić aktualny stan. Kod nie został zwolniony bez potwierdzenia w bazie.'
            status = 409 if detail else 503
        return render_template_string(PAGE, order=order, result=result, reason=reason, error=error), status
