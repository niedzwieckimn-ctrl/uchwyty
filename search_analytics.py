"""Search analytics v3: immutable events, deterministic intent projection, no stock writes."""
import csv
import hashlib
import io
import json
import re
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from difflib import get_close_matches
from zoneinfo import ZoneInfo

WINDOW = 300
WARSAW = ZoneInfo('Europe/Warsaw')


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
        for p in self.products:
            name = str(p.get('name') or '').strip()
            # Empty names and SKU-looking names are not a reliable product family.
            if not name or re.match(r'^CH\d+[-\d]', name, re.I):
                p['family_id'] = None
                continue
            p['family_id'] = family_id(name)
            self.families.setdefault(p['family_id'], name)
        self.rules = {r['phrase']: r for r in rules}

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
        same_edit = prev and not prev['model_id'] and not e.get('model_id') and (
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


def register(b):
    from flask import request, jsonify, g, render_template, redirect, url_for, Response
    store = Store(b)

    def catalog():
        if b.supabase_enabled():
            products = b.supabase_select_rows('products', extra_params={'select':'id,sku,model,name', 'archived':'eq.0'})
        else:
            c = b.conn()
            try:
                products = [dict(r) for r in c.execute('SELECT id,sku,model,name FROM products WHERE COALESCE(archived,0)=0')]
            finally:
                c.close()
        return Catalog(products, store.rows('rule'))

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
        now = datetime.now(timezone.utc)
        days = int(request.args.get('days', '30')) if request.args.get('days', '30') in {'7','30','90'} else 30
        start = datetime.combine(now.astimezone(WARSAW).date() - timedelta(days=days-1), datetime.min.time(), WARSAW)
        error = ''
        try:
            cat = catalog()
            # Fetch full new event history before filtering: a boundary must not create an extra intent.
            events = store.rows('event')
            intents = project(events, now)
        except Exception:
            b.app.logger.exception('Search analytics read failed')
            cat, events, intents = Catalog([]), [], []
            error = 'Nie udało się pobrać statystyk. Sprawdź wdrożenie migracji i połączenie z bazą. Odśwież stronę.'
        clients_all = sorted({(i['customer_id'], i['customer_name']) for i in intents}, key=lambda x:x[1])
        q, customer, result = key(request.args.get('q')), request.args.get('customer',''), request.args.get('result','all')
        filtered = [i for i in intents if stamp(i['started_at']) >= start and not i['superseded']
                    and (not customer or i['customer_id'] == customer)
                    and (not q or q in key(' '.join(i['raw_phrases']) + ' ' + i['model_name'] + ' ' + i['customer_name']))
                    and (result == 'all' or result == 'yes' and i['results_count'] > 0 or result == 'no' and i['no_result'])]
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
        models, clients, missing = {}, {}, {}
        trend = Counter(stamp(i['started_at']).astimezone(WARSAW).date() for i in filtered)
        for i in filtered:
            c = clients.setdefault(i['customer_id'], {'name':i['customer_name'], 'count':0, 'missing':0})
            c['count'] += 1
            c['missing'] += int(i['no_result'])
            if i['model_id']:
                m = models.setdefault(i['model_id'], {'id':i['model_id'], 'name':i['model_name'], 'count':0, 'clients':set(), 'last':'', 'phrases':Counter(), 'skus':Counter()})
                m['count'] += 1
                m['clients'].add(i['customer_id'])
                m['last'] = max(m['last'], i['last_activity_at'])
                m['phrases'].update(e['query'] for e in i['events'])
                # SKU drilldown counts explicit selections only, never treats all search matches as clicks.
                m['skus'].update(e['selected_sku'] for e in i['events'] if e.get('selected_sku'))
            if i['no_result']:
                phrase = key(i['query'])
                r = cat.rules.get(phrase, {})
                m = missing.setdefault(phrase, {'phrase':phrase, 'count':0, 'last':'', 'ignored':r.get('ignored',False), 'purchase':r.get('purchase',False), 'assigned':cat.families.get(r.get('model_id'),'')})
                m['count'] += 1
                m['last'] = max(m['last'], i['last_activity_at'])
        for c in clients.values():
            c['percent'] = round(c['missing'] / c['count'] * 100, 1)
        sort = request.args.get('sort','count')
        if sort not in {'count','missing','percent'}:
            sort = 'count'
        model_rows = sorted(models.values(), key=lambda m:(-m['count'],m['name']))
        for m in missing.values():
            suggestion = get_close_matches(m['phrase'], [key(n) for n in cat.families.values()], n=1, cutoff=.65)
            m['suggestion'] = next((n for n in cat.families.values() if suggestion and key(n) == suggestion[0]), '')
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
            missing=sorted(missing.values(),key=lambda m:-m['count']), families=cat.families, rules=cat.rules,
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
