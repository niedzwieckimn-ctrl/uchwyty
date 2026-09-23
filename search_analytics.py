"""Search analytics v3: immutable events, deterministic intent projection, no stock writes."""
import csv
import hashlib
import io
import json
import re
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from difflib import get_close_matches
from zoneinfo import ZoneInfo

WINDOW = 300
WARSAW = ZoneInfo('Europe/Warsaw')
OPERATION = 'search.analytics.read'
_backend = None


def configure(backend):
    global _backend
    _backend = backend


def initialize(db):
    """Create the local event store eagerly so controlled READs work before UI use."""
    db.execute(
        """CREATE TABLE IF NOT EXISTS search_analytics_records(
               id TEXT PRIMARY KEY,
               kind TEXT NOT NULL,
               created_at TEXT NOT NULL,
               payload TEXT NOT NULL
           )"""
    )
    db.commit()


def key(value):
    return re.sub(r'\s+', ' ', str(value or '').strip().casefold())


def stamp(value):
    result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return result if result.tzinfo else result.replace(tzinfo=timezone.utc)


def family_id(name):
    return hashlib.sha256(key(name).encode()).hexdigest()[:24]


class Catalog:
    def __init__(self, products, rules=()):
        self.products = [dict(p) for p in products]
        self.families = {}
        legacy_names = {}
        for p in self.products:
            model = str(p.get('model') or '').strip()
            display = str(p.get('name') or '').strip()
            sku = str(p.get('sku') or '').strip()
            # A base model code groups its colour/size variants. A full SKU is
            # only a variant identifier, so use a genuine catalogue name then.
            model_key = model if model and key(model) != key(sku) and not re.match(
                r'^CH\d+-', model, re.I) else ''
            name_key = display if display and key(display) != key(sku) and not re.match(
                r'^CH\d+-', display, re.I) else ''
            identity = model_key or name_key
            if not identity:
                p['family_id'] = None
                continue
            p['family_id'] = family_id(identity)
            self.families.setdefault(p['family_id'], name_key or model_key)
            if name_key:
                legacy_names.setdefault(family_id(name_key), set()).add(p['family_id'])
        # Existing alias rules used a hash of the display name. Keep them
        # usable when that name unambiguously maps to a model code.
        legacy_ids = {old: next(iter(ids)) for old, ids in legacy_names.items() if len(ids) == 1}
        self.rules = {}
        for rule in rules:
            current = dict(rule)
            current['model_id'] = legacy_ids.get(current.get('model_id'), current.get('model_id'))
            self.rules[current['phrase']] = current

    def resolve(self, query, selected=None):
        q = key(query)
        if selected is not None:
            rows = [p for p in self.products if str(p['id']) == str(selected)]
            if not rows:
                raise ValueError('Wybrany produkt nie istnieje w katalogu.')
            method = 'selection'
        elif self.rules.get(q, {}).get('model_id') in self.families:
            fid = self.rules[q]['model_id']
            rows = [p for p in self.products if p['family_id'] == fid]
            method = 'alias'
        else:
            rows = [p for p in self.products if q and any(q in key(p.get(f)) for f in ('sku', 'model', 'name'))]
            method = 'query'
        families = {p['family_id'] for p in rows}
        fid = next(iter(families)) if len(families) == 1 and None not in families else None
        return {'model_id': fid, 'model_name': self.families.get(fid, ''),
                'resolution': method if fid else ('ambiguous' if rows else 'unresolved'),
                'catalog_results': len(rows), 'skus': [p['sku'] for p in rows] if fid else [],
                'selected_sku': rows[0]['sku'] if selected is not None else ''}


def project(events, now=None):
    """Per-customer consecutive model runs; unresolved editing chains never inherit a model."""
    now = now or datetime.now(timezone.utc)
    intents, current = [], {}
    for event in sorted(events, key=lambda e: (e['created_at'], e.get('sequence', 0), e['id'])):
        e = dict(event)
        at = stamp(e['created_at'])
        cid = e['customer_id']
        prev = current.get(cid)
        gap = (at - stamp(prev['last_activity_at'])).total_seconds() if prev else WINDOW + 1
        same_model = prev and e.get('model_id') and prev['model_id'] == e['model_id']
        # Prefix typing can settle into a result, but ambiguous phrases keep their own unresolved record.
        same_edit = prev and (not prev['model_id'] or prev['model_id'] == e.get('model_id')) and (
            prev['events'][-1].get('edit_id') == e.get('edit_id') and e.get('edit_id') and
            (key(e['query']).startswith(key(prev['query'])) or key(prev['query']).startswith(key(e['query'])))
        )
        if prev and gap <= WINDOW and (same_model or same_edit):
            intent = prev
        else:
            if prev:
                prev['closed'] = True
                last = prev['events'][-1]
                # A no-result prefix corrected to a result is not an unanswered demand.
                if gap <= WINDOW and not prev['model_id'] and last.get('resolution') == 'unresolved' and e.get('results_count', 0) > 0 and e.get('edit_id') == last.get('edit_id') and key(e['query']).startswith(key(last['query'])):
                    prev['superseded'] = True
            intent = {'id': e['id'], 'customer_id': cid, 'customer_name': e['customer_name'],
                      'model_id': e.get('model_id'), 'model_name': e.get('model_name', ''),
                      'started_at': e['created_at'], 'events': [], 'closed': False, 'superseded': False}
            intents.append(intent)
            current[cid] = intent
        intent['events'].append(e)
        if e.get('model_id') and not intent['model_id']:
            intent['model_id'], intent['model_name'] = e['model_id'], e['model_name']
        intent.update(last_activity_at=e['created_at'], query=e['query'], results_count=e['results_count'], resolution=e['resolution'])
    for intent in intents:
        intent['raw_phrases'] = list(dict.fromkeys(e['query'] for e in intent['events']))
        intent['raw_count'] = len(intent['events'])
        intent['skus'] = list(dict.fromkeys(s for e in intent['events'] for s in e.get('skus', [])))
        intent['final'] = intent['closed'] or (now - stamp(intent['last_activity_at'])).total_seconds() >= WINDOW
        intent['no_result'] = intent['results_count'] == 0 and intent['final'] and not intent['superseded']
        intent['status'] = ('Doprecyzowane' if intent['superseded'] else 'Bez wyników' if intent['no_result'] else
                            'Oczekuje' if intent['results_count'] == 0 else 'Niejednoznaczne' if not intent['model_id'] else
                            'Alias' if intent['resolution'] == 'alias' else 'Scalone' if intent['raw_count'] > 1 else 'Z wynikami')
    return intents


class Store:
    def __init__(self, b):
        self.b = b

    def local(self):
        c = self.b.conn()
        c.execute('CREATE TABLE IF NOT EXISTS search_analytics_records(id TEXT PRIMARY KEY, kind TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL)')
        c.commit()
        return c

    def put(self, rid, kind, payload, replace=False):
        row = {'id': rid, 'kind': kind, 'created_at': datetime.now(timezone.utc).isoformat(), 'payload': payload}
        if self.b.supabase_enabled():
            self.b.supabase_request('/rest/v1/search_analytics_records', method='POST', params={'on_conflict':'id'},
                payload=row, prefer='resolution=merge-duplicates' if replace else 'resolution=ignore-duplicates')
        else:
            c = self.local()
            try:
                c.execute('INSERT OR REPLACE INTO search_analytics_records VALUES(?,?,?,?)' if replace else
                          'INSERT OR IGNORE INTO search_analytics_records VALUES(?,?,?,?)',
                          (rid, kind, row['created_at'], json.dumps(payload, ensure_ascii=False)))
                c.commit()
            finally:
                c.close()

    def rows(self, kind, since=None):
        if self.b.supabase_enabled():
            params = {'kind': 'eq.' + kind}
            if since:
                params['created_at'] = 'gte.' + since.isoformat()
            raw = self.b.supabase_select_rows('search_analytics_records', order_by='created_at,id', extra_params=params)
        else:
            c = self.local()
            try:
                raw = [dict(r) for r in c.execute('SELECT * FROM search_analytics_records WHERE kind=?' + (' AND created_at>=?' if since else '') + ' ORDER BY created_at,id',
                      (kind, since.isoformat()) if since else (kind,))]
            finally:
                c.close()
        return [json.loads(r['payload']) if isinstance(r['payload'], str) else r['payload'] for r in raw]


def catalog_for(b):
    store = Store(b)
    if b.supabase_enabled():
        products = b.supabase_select_rows(
            'products', extra_params={'select':'id,sku,model,name', 'archived':'eq.0'})
    else:
        c = b.conn()
        try:
            products = [dict(r) for r in c.execute(
                'SELECT id,sku,model,name FROM products WHERE COALESCE(archived,0)=0')]
        finally:
            c.close()
    return Catalog(products, store.rows('rule'))


def analytics_snapshot(b, *, days=30, query='', customer='', result='all', now=None):
    """Shared projection and aggregation used by the panel and agent READ."""
    now = now or datetime.now(timezone.utc)
    days = int(days)
    if days not in {7, 30, 90}:
        raise ValueError('Nieobsługiwany okres statystyk wyszukiwania.')
    result = result if result in {'all', 'yes', 'no'} else 'all'
    start = datetime.combine(
        now.astimezone(WARSAW).date() - timedelta(days=days - 1),
        datetime.min.time(), WARSAW)
    end = datetime.combine(now.astimezone(WARSAW).date() + timedelta(days=1), datetime.min.time(), WARSAW)
    store = Store(b)
    cat = catalog_for(b)
    # Full history is required before the date filter so a boundary cannot
    # split one typing/editing chain into two intents.
    events = store.rows('event')
    # Reproject immutable historical events against the current catalog. This
    # repairs earlier unresolved records without inserting another event or
    # changing the intent count.
    for event in events:
        selected_sku = key(event.get('selected_sku'))
        selected = next((p['id'] for p in cat.products if selected_sku and
                         key(p.get('sku')) == selected_sku), None)
        event.update(cat.resolve(event.get('query'), selected))
    intents = project(events, now)
    normalized_query = key(query)
    filtered = [
        intent for intent in intents
        if start <= stamp(intent['started_at']) < end and not intent['superseded']
        and (not customer or intent['customer_id'] == customer)
        and (not normalized_query or normalized_query in key(
            ' '.join(intent['raw_phrases']) + ' ' + intent['model_name'] + ' ' + intent['customer_name']))
        and (result == 'all' or result == 'yes' and intent['results_count'] > 0
             or result == 'no' and intent['no_result'])
    ]
    models, clients, missing = {}, {}, {}
    trend = Counter(stamp(intent['started_at']).astimezone(WARSAW).date()
                    for intent in filtered)
    for intent in filtered:
        client = clients.setdefault(
            intent['customer_id'], {'name': intent['customer_name'], 'count': 0, 'missing': 0})
        client['count'] += 1
        client['missing'] += int(intent['no_result'])
        if intent['model_id']:
            model = models.setdefault(intent['model_id'], {
                'id': intent['model_id'], 'name': intent['model_name'], 'count': 0,
                'clients': set(), 'last': '', 'phrases': Counter(), 'skus': Counter()})
            model['count'] += 1
            model['clients'].add(intent['customer_id'])
            model['last'] = max(model['last'], intent['last_activity_at'])
            model['phrases'].update(event['query'] for event in intent['events'])
            model['skus'].update(
                event['selected_sku'] for event in intent['events'] if event.get('selected_sku'))
        if intent['no_result']:
            phrase = key(intent['query'])
            rule = cat.rules.get(phrase, {})
            row = missing.setdefault(phrase, {
                'phrase': phrase, 'count': 0, 'last': '',
                'ignored': rule.get('ignored', False),
                'purchase': rule.get('purchase', False),
                'assigned': cat.families.get(rule.get('model_id'), '')})
            row['count'] += 1
            row['last'] = max(row['last'], intent['last_activity_at'])
    for client in clients.values():
        client['percent'] = round(client['missing'] / client['count'] * 100, 1)
    model_rows = sorted(models.values(), key=lambda model: (-model['count'], model['name']))
    for row in missing.values():
        suggestion = get_close_matches(
            row['phrase'], [key(name) for name in cat.families.values()], n=1, cutoff=.65)
        row['suggestion'] = next(
            (name for name in cat.families.values() if suggestion and key(name) == suggestion[0]), '')
    return {
        'days': days, 'start': start, 'end': end, 'events': events, 'intents': intents,
        'filtered': filtered, 'catalog': cat,
        'clients_all': sorted({(i['customer_id'], i['customer_name']) for i in intents}, key=lambda x:x[1]),
        'models': model_rows, 'clients': clients,
        'missing': sorted(missing.values(), key=lambda row: (-row['count'], row['phrase'])),
        'trend': trend,
    }


def business_read(data, actor=None, correlation_id='', transaction_connection=None):
    del actor, correlation_id, transaction_connection
    if _backend is None:
        raise RuntimeError('Źródło statystyk wyszukiwania nie jest skonfigurowane.')
    snapshot = analytics_snapshot(
        _backend, days=int(data.get('days') or 30), query=data.get('query') or '',
        customer=data.get('customer_id') or '', result=data.get('result') or 'all')
    limit = int(data.get('limit') or 100)
    intents = sorted(snapshot['filtered'], key=lambda row: (row['last_activity_at'], row['id']), reverse=True)
    phrase_counts = Counter()
    for row in intents:
        phrase_counts.update({key(phrase) for phrase in row['raw_phrases'] if key(phrase)})
    phrase_ranking = [{'phrase':phrase,'intent_count':count}
                      for phrase,count in sorted(phrase_counts.items(), key=lambda item:(-item[1],item[0]))]
    public_models = [{
        'model_id': row['id'], 'model_name': row['name'], 'intent_count': row['count'],
        'customer_count': len(row['clients']), 'last_at': row['last'],
        'phrases': [{'phrase': phrase, 'count': count}
                    for phrase, count in row['phrases'].most_common()],
        'explicit_sku_selections': [{'sku': sku, 'count': count}
                                    for sku, count in row['skus'].most_common()],
    } for row in snapshot['models']]
    public_intents = [{
        'intent_id': row['id'], 'customer_id': row['customer_id'],
        'customer_name': row['customer_name'], 'model_id': row['model_id'],
        'model_name': row['model_name'], 'started_at': row['started_at'],
        'last_activity_at': row['last_activity_at'], 'phrases': row['raw_phrases'],
        'results_count': row['results_count'], 'no_result': row['no_result'],
        'status': row['status'], 'raw_event_count': row['raw_count'],
        'explicit_sku_selections': [event['selected_sku'] for event in row['events']
                                    if event.get('selected_sku')],
    } for row in intents[:limit]] if data.get('include_intents') else []
    snapshot_id = hashlib.sha256(json.dumps({
        'start':snapshot['start'].isoformat(), 'end':snapshot['end'].isoformat(),
        'intents':intents, 'rules':snapshot['catalog'].rules,
    },ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()
    return {
        'ok': True,
        'scope': {'kind': 'projected_analytics', 'entity_existence_authoritative': True,
                  'empty_means': 'no_matching_projected_search_intents'},
        'complete': not data.get('include_intents') or len(intents) <= limit,
        'truncated': bool(data.get('include_intents')) and len(intents) > limit,
        'snapshot_id': snapshot_id, 'rankings_complete': True,
        'phrase_ranking': phrase_ranking,
        'phrase_count_basis': 'one_intent_per_distinct_normalized_phrase',
        'period': {'days': snapshot['days'], 'date_from': snapshot['start'].date().isoformat(),
                   'date_to': (snapshot['end'].date() - timedelta(days=1)).isoformat()},
        'totals': {'intents': len(snapshot['filtered']),
                   'active_customers': len(snapshot['clients']),
                   'no_result_intents': sum(row['no_result'] for row in snapshot['filtered'])},
        'models': public_models, 'missing': snapshot['missing'],
        'intents': public_intents,
        'aliases': {phrase: snapshot['catalog'].families[rule['model_id']]
                    for phrase, rule in snapshot['catalog'].rules.items()
                    if rule.get('model_id') in snapshot['catalog'].families},
    }


def register(b):
    from flask import request, jsonify, g, render_template, redirect, url_for, Response
    configure(b)
    store = Store(b)

    def catalog():
        return catalog_for(b)

    old_log = b.app.view_functions['api_client_search_log']
    old_dashboard = b.app.view_functions['client_searches']

    def log():
        data = request.get_json(silent=True) or {}
        if request.method == 'OPTIONS' or data.get('version') != 3:
            return old_log()
        try:
            if not b._rate_limit('search_intents', 240, 60):
                return jsonify(ok=False, error='Za dużo zdarzeń wyszukiwania.'), 429
            query = str(data.get('query') or '').strip()[:120]
            if len(query) < 2:
                return jsonify(ok=True, skipped=True)
            eid = str(uuid.UUID(str(data.get('event_id'))))
            edit_id = str(uuid.UUID(str(data.get('edit_id'))))
            user = g.client_user
            cid = str(user.get('id') or key(user.get('email')))
            if not cid:
                raise ValueError('Brak tożsamości klienta.')
            cat = catalog()
            resolution = cat.resolve(query, data.get('selected_product_id'))
            count = max(0, min(1000000, int(data.get('results_count', 0))))
            # Count is the visible client result count; identity and family come from server catalog.
            event = dict(resolution, id=hashlib.sha256((cid + eid).encode()).hexdigest(), customer_id=cid,
                         customer_name=str(user.get('email') or cid), query=query, edit_id=edit_id,
                         sequence=max(0, int(data.get('sequence', 0))), results_count=count,
                         created_at=datetime.now(timezone.utc).isoformat())
            c = b.conn()
            try:
                row = c.execute('SELECT name FROM customers WHERE lower(email)=? LIMIT 1', (key(user.get('email')),)).fetchone()
                if row and row['name']:
                    event['customer_name'] = row['name']
            finally:
                c.close()
            store.put(event['id'], 'event', event)
            return jsonify(ok=True, event_id=eid, model_id=event['model_id'], resolution=event['resolution'])
        except (ValueError, TypeError):
            return jsonify(ok=False, error='Nieprawidłowe zdarzenie wyszukiwania.'), 400
        except Exception:
            b.app.logger.exception('Search analytics event save failed')
            return jsonify(ok=False, error='Nie udało się zapisać statystyki wyszukiwania.'), 503

    def aliases():
        if request.method == 'OPTIONS':
            return '', 204
        try:
            cat = catalog()
            return jsonify(ok=True, aliases={q:cat.families[r['model_id']] for q,r in cat.rules.items() if r.get('model_id') in cat.families})
        except Exception:
            return jsonify(ok=False, aliases={}), 503

    def action():
        try:
            cat = catalog()
            phrase = key(request.form.get('phrase'))[:120]
            operation = request.form.get('operation')
            if not phrase or operation not in {'alias', 'assign', 'purchase', 'ignore', 'restore', 'remove_alias'}:
                raise ValueError('Nieprawidłowa akcja.')
            rule = dict(cat.rules.get(phrase, {'phrase': phrase}))
            if operation in {'alias', 'assign'}:
                fid = request.form.get('model_id')
                if fid not in cat.families:
                    raise ValueError('Wybierz istniejącą rodzinę produktu.')
                rule['model_id'] = fid
            elif operation == 'remove_alias':
                rule.pop('model_id', None)
            else:
                rule['ignored' if operation in {'ignore', 'restore'} else 'purchase'] = operation != 'restore'
            store.put('rule:' + hashlib.sha256(phrase.encode()).hexdigest(), 'rule', rule, replace=True)
            return redirect(url_for('client_searches', message='Zapisano. Alias działa dla nowych wyszukiwań po odświeżeniu panelu klienta.'))
        except ValueError as exc:
            return str(exc), 400
        except Exception:
            return 'Nie udało się zapisać zmiany. Spróbuj ponownie.', 503

    def dashboard():
        if request.args.get('legacy') == '1':
            return old_dashboard()
        days = int(request.args.get('days', '30')) if request.args.get('days', '30') in {'7','30','90'} else 30
        q, customer, result = key(request.args.get('q')), request.args.get('customer',''), request.args.get('result','all')
        error = ''
        try:
            projection = analytics_snapshot(
                b, days=days, query=q, customer=customer, result=result)
        except Exception:
            b.app.logger.exception('Search analytics read failed')
            empty = Catalog([])
            start = datetime.combine(
                datetime.now(WARSAW).date() - timedelta(days=days-1),
                datetime.min.time(), WARSAW)
            projection = {
                'days': days, 'start': start, 'events': [], 'intents': [],
                'filtered': [], 'catalog': empty, 'clients_all': [], 'models': [],
                'clients': {}, 'missing': [], 'trend': Counter(),
            }
            error = 'Nie udało się pobrać statystyk. Sprawdź wdrożenie migracji i połączenie z bazą. Odśwież stronę.'
        start, events = projection['start'], projection['events']
        filtered, cat = projection['filtered'], projection['catalog']
        clients_all = projection['clients_all']
        if request.args.get('export') == '1':
            if error:
                return error, 503
            out = io.StringIO()
            writer = csv.writer(out, delimiter=';')
            writer.writerow(['Start', 'Ostatnia aktywność', 'Klient', 'Model', 'Frazy', 'Wyników', 'Status', 'Surowych wpisów'])
            def safe(value):
                s = str(value)
                return "'" + s if s.lstrip().startswith(('=', '+', '-', '@')) else s
            for i in filtered:
                writer.writerow(map(safe, [i['started_at'], i['last_activity_at'], i['customer_name'], i['model_name'], ' | '.join(i['raw_phrases']), i['results_count'], i['status'], i['raw_count']]))
            return Response('\ufeff' + out.getvalue(), mimetype='text/csv', headers={'Content-Disposition':'attachment; filename=wyszukiwania.csv'})
        clients = projection['clients']
        sort = request.args.get('sort','count')
        if sort not in {'count','missing','percent'}:
            sort = 'count'
        model_rows = projection['models']
        missing_rows = projection['missing']
        trend = projection['trend']
        bars = [(start.date()+timedelta(days=d), trend[start.date()+timedelta(days=d)]) for d in range(days)]
        pagesize = 10
        try: page = max(1, int(request.args.get('page',1)))
        except ValueError: page = 1
        ordered = sorted(filtered, key=lambda i:i['last_activity_at'], reverse=True)
        page = min(page, max(1,(len(ordered)+pagesize-1)//pagesize))
        def link(**updates):
            args = request.args.to_dict()
            args.update(updates)
            return url_for('client_searches', **args)
        return render_template('search_analytics.html', title='Statystyki wyszukiwań', base_url=b.BASE_URL, db_path=b.DB_PATH,
            days=days, q=request.args.get('q',''), customer=customer, result=result, clients_all=clients_all,
            total=len(filtered), active=len(clients), no_results=sum(i['no_result'] for i in filtered),
            top=model_rows[0] if model_rows else None, models=model_rows, clients=sorted(clients.values(),key=lambda c:-c[sort]),
            missing=missing_rows, families=cat.families, rules=cat.rules,
            bars=bars, maxbar=max([v for _,v in bars]+[1]), latest=ordered[(page-1)*pagesize:page*pagesize],
            page=page, more=page*pagesize<len(ordered), link=link, error=error, sort=sort,
            raw_latest=sorted([e for e in events if stamp(e['created_at']) >= start
                and (not customer or e['customer_id'] == customer)
                and (not q or q in key(e['query']+' '+e.get('model_name','')+' '+e['customer_name']))],
                key=lambda e:e['created_at'], reverse=True)[:100],
            datefmt=lambda x:stamp(x).astimezone(WARSAW).strftime('%d.%m.%Y %H:%M'))

    b.app.view_functions['api_client_search_log'] = log
    b.app.view_functions['client_searches'] = dashboard
    b.app.add_url_rule('/api/client/search-aliases', 'search_aliases', aliases, methods=['GET','OPTIONS'])
    b.app.add_url_rule('/searches/action', 'search_action', action, methods=['POST'])
