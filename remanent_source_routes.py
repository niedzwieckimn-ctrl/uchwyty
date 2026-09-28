"""Owner-scoped forms for reviewing versioned annual cost files."""
import io
import sqlite3

import remanent
import remanent_sources as sources

MAX_UPLOAD = 5 * 1024 * 1024
STATUS_LABELS = {
    'UNKNOWN_SKU':'Nieznane lub niejednoznaczne SKU', 'OPENING_BALANCE':'Stan początkowy',
    'MATCHED':'Istniejące powiązanie', 'QUANTITY_CONFLICT':'Sprzeczne ilości',
    'AMBIGUOUS':'Kilka możliwych przyjęć', 'SUGGESTED_MATCH':'Sugestia dopasowania',
    'ONLY_IN_ANNUAL':'Tylko w Rocznym Rozliczeniu',
}
BASIS_LABELS = {'goods_net':'Towar netto', 'goods_transport':'Towar + transport',
                'landed_cost':'Pełny koszt'}
ACTION_LABELS = {'exclude':'Wyłącz z uzgodnienia', 'link':'Koszty istniejących przyjęć',
                 'history':'Zakup historyczny bez zmiany magazynu', 'opening':'Stan początkowy'}


def register_routes(app, deps):
    from flask import abort, redirect, render_template, request, send_file, url_for
    from flask import session as browser_session
    from internal_rbac import current_actor_context, require_permission

    @app.url_value_preprocessor
    def source_form_limits(endpoint, values):
        # Runs before the shared CSRF guard reads multipart form fields.
        if endpoint in {'remanent_sources_preview', 'remanent_sources_commit'}:
            request.max_content_length = 12 * 1024 * 1024
            request.max_form_memory_size = 2 * 1024 * 1024
            request.max_form_parts = 30050

    def owner():
        actor = current_actor_context()
        if actor is None:
            abort(401)
        return actor.actor_id

    def scoped_session(db, session_id, owner_id):
        try:
            return dict(remanent._session(db, session_id, owner_id))
        except ValueError:
            abort(404)

    def upload():
        uploaded = request.files.get('file')
        if uploaded is None or not uploaded.filename:
            raise ValueError('Wybierz plik JSON z Rocznego Rozliczenia.')
        data = uploaded.read(MAX_UPLOAD + 1)
        if len(data) > MAX_UPLOAD:
            raise ValueError('Plik przekracza 5 MB.')
        return data

    def show(db, selected, *, plan=None, error='', saved=False):
        settings = db.execute('SELECT * FROM remanent_valuation_settings WHERE session_id=?',
                              (selected['session_id'],)).fetchone()
        return render_template('remanent_sources.html', title='Uzgodnienie kosztów i dostaw',
            base_url=deps['BASE_URL'], db_path=deps['DB_PATH'], remanent=selected,
            settings=dict(settings) if settings else None, plan=plan, error=error, saved=saved,
            status_labels=STATUS_LABELS, basis_labels=BASIS_LABELS, action_labels=ACTION_LABELS)

    def decisions_from_form(plan):
        decisions = []
        for row in plan['rows']:
            key = row['key']
            action = request.form.get('action_' + key, '')
            if action not in ACTION_LABELS:
                raise ValueError('Wybierz działanie dla każdej pozycji, również dla wykluczanych.')
            choice = {'line_key':key, 'action':action}
            if action == 'history':
                day = request.form.get('received_' + key, '').strip()
                if not day or request.form.get('history_confirm_' + key) != 'yes':
                    raise ValueError('Zakup historyczny wymaga jawnej daty przyjęcia i potwierdzenia, że nie zmieni magazynu.')
                choice['received_date'] = day
            elif action == 'opening':
                if request.form.get('opening_confirm_' + key) != 'yes':
                    raise ValueError('Potwierdź źródło, ilość i wartość stanu początkowego.')
            elif action == 'link':
                allocations = []
                for index, receipt in enumerate(row['candidates']):
                    field = key + '_' + str(index)
                    if request.form.get('receipt_' + field) != 'yes':
                        continue
                    raw = request.form.get('quantity_' + field, '').strip()
                    if not raw.isdecimal() or len(raw) > 10:
                        raise ValueError('Podaj całkowitą liczbę sztuk dla każdego wybranego przyjęcia.')
                    allocations.append({'receipt_key':receipt['receipt_key'], 'quantity':int(raw)})
                choice['allocations'] = allocations
            decisions.append(choice)
        return decisions

    @app.get('/remanent/<session_id>/sources')
    @require_permission('inventory.remanent_manage')
    def remanent_sources(session_id):
        db = deps['conn']()
        try:
            selected = scoped_session(db, session_id, owner())
            return show(db, selected, saved=browser_session.pop('remanent_sources_saved',None) == session_id)
        finally:
            db.close()

    @app.post('/remanent/<session_id>/sources/preview')
    @require_permission('inventory.remanent_manage')
    def remanent_sources_preview(session_id):
        db = deps['conn']()
        try:
            owner_id = owner()
            selected = scoped_session(db, session_id, owner_id)
            try:
                plan = sources.preview(db, session_id, owner_id, upload())
                return show(db, selected, plan=plan)
            except (ValueError, TypeError, KeyError, AttributeError) as exc:
                return show(db, selected, error=str(exc) if isinstance(exc, ValueError)
                            else 'Plik zawiera nieprawidłowe pola kontraktu JSON.'), 400
        finally:
            db.close()

    @app.post('/remanent/<session_id>/sources/commit')
    @require_permission('inventory.remanent_manage')
    def remanent_sources_commit(session_id):
        db = deps['conn']()
        try:
            owner_id = owner()
            selected = scoped_session(db, session_id, owner_id)
            try:
                data = upload()
                plan = sources.preview(db, session_id, owner_id, data)
                fingerprint = request.form.get('fingerprint', '')
                decisions = decisions_from_form(plan)
                sources.commit(db, session_id, owner_id, data, decisions, fingerprint)
                browser_session['remanent_sources_saved'] = session_id
                return redirect(url_for('remanent_sources', session_id=session_id))
            except (ValueError, TypeError, KeyError, AttributeError, sqlite3.IntegrityError) as exc:
                db.rollback()
                message = str(exc) if isinstance(exc, ValueError) else 'Nie zapisano uzgodnienia. Sprawdź plik i ponownie otwórz podgląd.'
                return show(db, selected, error=message), 409
        finally:
            db.close()

    @app.post('/remanent/<session_id>/sources/method')
    @require_permission('inventory.remanent_manage')
    def remanent_sources_method(session_id):
        db = deps['conn']()
        try:
            owner_id = owner()
            selected = scoped_session(db, session_id, owner_id)
            try:
                if request.form.get('confirmed') != 'yes':
                    raise ValueError('Potwierdź wybraną metodę i podstawę kosztów.')
                sources.select_method(db, session_id, owner_id, request.form.get('method'), request.form.get('basis'),
                    confirm_manual_basis=request.form.get('confirm_manual_basis') == 'yes')
                db.commit()
                browser_session['remanent_sources_saved'] = session_id
                return redirect(url_for('remanent_sources', session_id=session_id))
            except ValueError as exc:
                db.rollback()
                return show(db, selected, error=str(exc)), 409
        finally:
            db.close()

    @app.get('/remanent/<session_id>/sources/result.json')
    @require_permission('inventory.remanent_manage')
    def remanent_sources_export(session_id):
        db = deps['conn']()
        try:
            owner_id = owner()
            selected = scoped_session(db, session_id, owner_id)
            try:
                data = sources.export_result(db, session_id, owner_id)
            except ValueError as exc:
                return show(db, selected, error=str(exc)), 409
            return send_file(io.BytesIO(data), mimetype='application/json', as_attachment=True,
                             download_name=f'remanent-{selected["inventory_year"]}-{session_id}.json')
        finally:
            db.close()
