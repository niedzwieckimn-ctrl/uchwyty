"""Durable inventory count over the existing warehouse count sessions/items."""
import csv
import hashlib
import io
import json
import os
import sqlite3
import uuid
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

from invoice_sales import sales_by_sku

WARSAW = ZoneInfo('Europe/Warsaw')
MONEY = Decimal('0.01')
KINDS = {'opening', 'historical_sales_manual', 'historical_sales_import',
         'historical_purchase_manual', 'historical_purchase_import',
         'unit_value_manual', 'unit_value_import', 'purchase_coverage'}


def now():
    return datetime.now(WARSAW).replace(microsecond=0).isoformat()


def money(value):
    return Decimal(str(value)).quantize(MONEY, rounding=ROUND_HALF_UP)


def initialize_schema(db):
    columns = {row['name'] for row in db.execute('PRAGMA table_info(internal_inventory_count_sessions)')}
    additions = {'inventory_year': 'INTEGER', 'remanent_no': 'TEXT',
                 'phase': "TEXT NOT NULL DEFAULT 'IN_PROGRESS'",
                 'as_of_date': 'TEXT', 'snapshot_at': 'TEXT', 'source_fingerprint': 'TEXT',
                 'company_snapshot_json': 'TEXT'}
    for name, definition in additions.items():
        if name not in columns:
            try:
                db.execute(f'ALTER TABLE internal_inventory_count_sessions ADD COLUMN {name} {definition}')
            except sqlite3.OperationalError as exc:
                if 'duplicate column name' not in str(exc).lower():
                    raise
    db.executescript((Path(__file__).parent / 'migrations' / 'remanent.sql').read_text(encoding='utf-8'))
    snapshot_columns = {row['name'] for row in db.execute('PRAGMA table_info(internal_remanent_snapshots)')}
    if 'assumed_zero' not in snapshot_columns:
        db.execute('ALTER TABLE internal_remanent_snapshots ADD COLUMN assumed_zero INTEGER NOT NULL DEFAULT 0')
    db.execute('CREATE UNIQUE INDEX IF NOT EXISTS uq_remanent_no ON internal_inventory_count_sessions(remanent_no) WHERE remanent_no IS NOT NULL')
    db.execute('''CREATE UNIQUE INDEX IF NOT EXISTS uq_remanent_open_owner ON internal_inventory_count_sessions(created_by)
                  WHERE status='OPEN' AND inventory_year IS NOT NULL''')
    db.commit()


def _session(db, session_id, owner=None, *, phase=None):
    row = db.execute('SELECT * FROM internal_inventory_count_sessions WHERE session_id=? AND inventory_year IS NOT NULL',
                     (session_id,)).fetchone()
    if row is None or (owner is not None and row['created_by'] != owner):
        raise ValueError('Nie znaleziono remanentu lub brak dostępu')
    if phase is not None and row['phase'] != phase:
        raise ValueError('Nieprawidłowy etap remanentu')
    return row


def create_draft(db, owner, year):
    year = int(year)
    if not 2000 <= year <= 2100:
        raise ValueError('Nieprawidłowy rok')
    session_id = str(uuid.uuid4())
    db.execute('BEGIN IMMEDIATE')
    try:
        db.execute('''INSERT INTO internal_inventory_count_sessions
            (session_id,status,created_by,conversation_id,created_at,inventory_year,remanent_no,phase)
            VALUES(?,'OPEN',?,'',?,?,?,'DRAFT')''',
            (session_id, owner, now(), year, f'REM/{year}/{session_id[:8].upper()}'))
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        raise ValueError('Najpierw zamknij istniejący otwarty remanent') from None
    return session_id


def _validate_date(value, year):
    parsed = date.fromisoformat(str(value))
    if parsed.year != int(year):
        raise ValueError('Data musi należeć do roku remanentu')
    return parsed.isoformat()


def _overlap(db, product_id, start, end):
    _quantities, _problems, dates = sales_by_sku(db, start, end)
    return dates.get(product_id, [])


def add_entry(db, session_id, owner, *, product_id, kind, period_start, period_end,
              quantity, source, unit_value_pln=None, document_no='', note='',
              import_id=None, import_row=None):
    session = _session(db, session_id, owner, phase='DRAFT')
    if kind not in KINDS:
        raise ValueError('Nieprawidłowe źródło danych')
    start = _validate_date(period_start, session['inventory_year'])
    end = _validate_date(period_end, session['inventory_year'])
    if start > end:
        raise ValueError('Błędny zakres dat')
    product_id = int(product_id)
    if not db.execute('SELECT 1 FROM products WHERE id=?', (product_id,)).fetchone():
        raise ValueError('Nie znaleziono SKU')
    quantity = int(quantity)
    if quantity < 0:
        raise ValueError('Ilość nie może być ujemna; korektę wpisu wyjaśnij w historii')
    if kind in {'opening', 'unit_value_manual', 'unit_value_import', 'purchase_coverage'} and quantity and kind != 'opening':
        raise ValueError('Ten typ wpisu nie może zawierać ilości')
    if kind == 'opening' and (start != f"{session['inventory_year']}-01-01" or end != start):
        raise ValueError('Stan początkowy dotyczy 1 stycznia')
    if kind.startswith('historical_sales') and _overlap(db, product_id, start, end):
        raise ValueError('Okres historyczny pokrywa się z fakturą aplikacji dla tego SKU; rozdziel okresy')
    value = None
    if unit_value_pln not in (None, ''):
        try:
            value = Decimal(str(unit_value_pln).replace(',', '.'))
            if value < 0 or not value.is_finite():
                raise ValueError()
            value = str(value)
        except (InvalidOperation, ValueError):
            raise ValueError('Nieprawidłowa cena jednostkowa PLN') from None
    if kind.startswith('unit_value') and value is None:
        raise ValueError('Podaj cenę jednostkową')
    if not str(source).strip():
        raise ValueError('Podaj źródło danych')
    cur = db.execute('''INSERT INTO internal_remanent_entries
        (inventory_year,product_id,kind,period_start,period_end,quantity,unit_value_pln,
         source,document_no,note,import_id,import_row,created_by,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (session['inventory_year'], product_id, kind, start, end, quantity, value,
         str(source).strip()[:200], str(document_no).strip()[:100], str(note).strip()[:500],
         import_id, import_row, owner, now()))
    return cur.lastrowid


def void_entry(db, session_id, owner, entry_id, reason):
    session = _session(db, session_id, owner, phase='DRAFT')
    if not reason.strip():
        raise ValueError('Podaj powód wycofania wpisu')
    changed = db.execute('''UPDATE internal_remanent_entries SET voided_by=?,voided_at=?,void_reason=?
        WHERE id=? AND inventory_year=? AND voided_at IS NULL''',
        (owner, now(), reason[:500], int(entry_id), session['inventory_year'])).rowcount
    if not changed:
        raise ValueError('Wpis nie istnieje lub został wycofany')


def preview_csv(db, session_id, owner, data, kind, start, end):
    session = _session(db, session_id, owner, phase='DRAFT')
    if kind not in {'historical_purchase_import', 'historical_sales_import'}:
        raise ValueError('Import dotyczy zakupów albo sprzedaży historycznej')
    start = _validate_date(start, session['inventory_year'])
    end = _validate_date(end, session['inventory_year'])
    if start > end or len(data) > 5 * 1024 * 1024:
        raise ValueError('Błędny zakres lub plik większy niż 5 MB')
    digest = hashlib.sha256(data).hexdigest()
    duplicate_file = bool(db.execute('''SELECT 1 FROM internal_remanent_imports
        WHERE file_sha256=? AND kind=?''', (digest, kind)).fetchone())
    try:
        reader = csv.DictReader(io.StringIO(data.decode('utf-8-sig'), newline=''))
        if not reader.fieldnames or not {'sku','quantity','date'}.issubset(set(reader.fieldnames)):
            raise ValueError('Wymagane nagłówki: sku,quantity,date')
        products = {str(row['sku']).casefold(): int(row['id']) for row in db.execute('SELECT id,sku FROM products')}
        rows, problems, seen = [], [], set()
        for line_number, row in enumerate(reader, 2):
            sku = str(row.get('sku') or '').strip()
            product_id = products.get(sku.casefold())
            try:
                row_date = _validate_date(row['date'], session['inventory_year'])
                if not start <= row_date <= end:
                    raise ValueError('Data poza zakresem importu')
                raw_quantity = str(row.get('quantity') or '').strip()
                quantity = int(raw_quantity)
                if raw_quantity != str(quantity) or quantity < 0:
                    raise ValueError('Ilość musi być nieujemną liczbą całkowitą')
                raw_price = (row.get('unit_value_pln') or '').strip().replace(',', '.')
                if kind == 'historical_purchase_import' and not raw_price:
                    raise ValueError('Zakup wymaga unit_value_pln')
                price = Decimal(raw_price) if raw_price else None
                if price is not None and (price < 0 or not price.is_finite()):
                    raise ValueError('Nieprawidłowa cena PLN')
                if row.get('line_value_pln'):
                    line_value = Decimal(row['line_value_pln'].replace(',', '.'))
                    if price is None or money(price * quantity) != money(line_value):
                        raise ValueError('Wartość pozycji nie zgadza się z ilością i ceną')
                key = (sku.casefold(), row_date, str(row.get('document_no') or '').casefold(), quantity, str(price))
                if key in seen:
                    raise ValueError('Powtórzony wiersz CSV')
                seen.add(key)
                if not product_id:
                    raise ValueError('Nieznane SKU')
                if kind == 'historical_sales_import' and _overlap(db, product_id, row_date, row_date):
                    raise ValueError('Sprzedaż pokrywa się z fakturą aplikacji')
                if db.execute('''SELECT 1 FROM internal_remanent_entries WHERE product_id=? AND kind=?
                    AND period_start=? AND period_end=? AND quantity=? AND document_no=?
                    AND voided_at IS NULL LIMIT 1''',
                    (product_id,kind,row_date,row_date,quantity,str(row.get('document_no') or '').strip())).fetchone():
                    raise ValueError('Pozycja została już zaimportowana; wycofaj ją przed ponownym importem')
                rows.append({'line': line_number, 'product_id': product_id, 'sku': sku,
                             'date': row_date, 'quantity': quantity, 'unit_value_pln': str(price) if price is not None else None,
                             'document_no': str(row.get('document_no') or '').strip()})
            except (ValueError, InvalidOperation, TypeError) as exc:
                problems.append({'line': line_number, 'sku': sku, 'error': str(exc)})
        if not rows and not problems:
            problems.append({'line': 0, 'error': 'Pusty plik CSV'})
    except UnicodeDecodeError:
        raise ValueError('Plik CSV musi być zakodowany w UTF-8') from None
    return {'sha256': digest, 'kind': kind, 'start': start, 'end': end,
            'rows': rows, 'problems': problems, 'duplicates': int(duplicate_file),
            'matched': len(rows), 'unmatched': sum(p['error']=='Nieznane SKU' for p in problems),
            'quantity': sum(r['quantity'] for r in rows),
            'value_pln': str(money(sum(Decimal(r['unit_value_pln'] or '0') * r['quantity'] for r in rows)))}


def import_csv(db, session_id, owner, data, kind, start, end, expected_hash):
    preview = preview_csv(db, session_id, owner, data, kind, start, end)
    if preview['sha256'] != expected_hash or preview['problems'] or preview['duplicates']:
        raise ValueError('Import zmienił się, zawiera błędy lub już istnieje; ponów podgląd')
    import_id = str(uuid.uuid4())
    db.execute('BEGIN IMMEDIATE')
    try:
        db.execute('''INSERT INTO internal_remanent_imports
            (import_id,file_sha256,kind,period_start,period_end,row_count,created_by,created_at)
            VALUES(?,?,?,?,?,?,?,?)''',
            (import_id, preview['sha256'], kind, preview['start'], preview['end'], len(preview['rows']), owner, now()))
        for row in preview['rows']:
            add_entry(db, session_id, owner, product_id=row['product_id'], kind=kind,
                      period_start=row['date'], period_end=row['date'], quantity=row['quantity'],
                      unit_value_pln=row['unit_value_pln'], source='CSV', document_no=row['document_no'],
                      import_id=import_id, import_row=row['line'])
        db.commit()
    except Exception:
        db.rollback()
        raise
    return import_id


def confirm_purchase_coverage(db, session_id, owner, as_of_date):
    session = _session(db, session_id, owner, phase='DRAFT')
    end = _validate_date(as_of_date, session['inventory_year'])
    start = f"{session['inventory_year']}-01-01"
    for row in db.execute('SELECT id FROM products').fetchall():
        existing = db.execute('''SELECT 1 FROM internal_remanent_entries WHERE inventory_year=? AND product_id=?
            AND kind='purchase_coverage' AND period_start=? AND period_end=? AND voided_at IS NULL''',
            (session['inventory_year'], row['id'], start, end)).fetchone()
        if not existing:
            add_entry(db, session_id, owner, product_id=row['id'], kind='purchase_coverage',
                      period_start=start, period_end=end, quantity=0,
                      source='Potwierdzenie kompletności zakupów',
                      note='Operator potwierdził kompletność danych zakupowych, w tym zerowe zakupy')


def _source_fingerprint(db, start, end):
    from invoice_sales import invoice_rows
    documents = [(row['id'], row['issue_date'], row['invoice_items_json'], row['publication_state'])
                 for row in invoice_rows(db, start, end)]
    receipts = [(row['package_id'], row['received_at'], row['quantities_json']) for row in
                db.execute('''SELECT package_id,received_at,quantities_json FROM china_stock_receipts
                    WHERE substr(received_at,1,10) BETWEEN ? AND ? ORDER BY package_id''', (start, end))]
    return hashlib.sha256(json.dumps([documents, receipts], ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _app_purchases(db, start, end):
    quantities = defaultdict(int)
    incomplete = []
    for row in db.execute('''SELECT * FROM china_stock_receipts WHERE substr(received_at,1,10) BETWEEN ? AND ?''',
                          (start, end)):
        try:
            payload = json.loads(row['quantities_json'] or '[]')
        except ValueError:
            payload = []
        if not payload:
            incomplete.append(row['package_id'])
        for item in payload:
            quantities[int(item['product_id'])] += int(item['qty'])
    return quantities, incomplete


def start_count(db, session_id, owner, as_of_date):
    session = _session(db, session_id, owner, phase='DRAFT')
    as_of = _validate_date(as_of_date, session['inventory_year'])
    start = f"{session['inventory_year']}-01-01"
    db.execute('BEGIN IMMEDIATE')
    try:
        session = _session(db,session_id,owner,phase='DRAFT')
        app_sales, unresolved, sale_dates = sales_by_sku(db, start, as_of)
        if unresolved:
            raise ValueError(f'Faktury bez wiarygodnego SKU/ilości: {unresolved[:5]}. Wyjaśnij je przed startem.')
        app_purchases, incomplete_receipts = _app_purchases(db, start, as_of)
        if incomplete_receipts:
            raise ValueError(f'Przyjęcia zakupowe bez pozycji: {incomplete_receipts[:5]}. Wyjaśnij je przed startem.')
        entries = db.execute('''SELECT * FROM internal_remanent_entries
            WHERE inventory_year=? AND voided_at IS NULL AND period_start<=? ORDER BY id''',
            (session['inventory_year'], as_of)).fetchall()
        sources = defaultdict(list)
        for entry in entries:
            if entry['period_end'] > as_of:
                raise ValueError('Wpis historyczny wykracza poza datę stanu; podziel okres')
            if entry['kind'].startswith('historical_sales') and any(
                    entry['period_start'] <= day <= entry['period_end']
                    for day in sale_dates.get(entry['product_id'], [])):
                raise ValueError(f"Nakładanie okresów sprzedaży dla SKU produktu {entry['product_id']}")
            sources[entry['product_id']].append(entry)
        products = db.execute('''SELECT p.id,p.sku,p.model,p.name,COALESCE(s.qty,0) stock_qty
            FROM products p LEFT JOIN stock s ON s.product_id=p.id ORDER BY p.sku''').fetchall()
        if not products:
            raise ValueError('Brak produktów do spisu')
        for product in products:
            pid = int(product['id'])
            record = sources[pid]
            def total(kind):
                return sum(int(r['quantity']) for r in record if r['kind']==kind)
            opening = total('opening')
            purchase_manual = total('historical_purchase_manual')
            purchase_import = total('historical_purchase_import')
            historical_purchases = purchase_manual + purchase_import
            purchase_app = app_purchases[pid]
            coverage = any(r['kind']=='purchase_coverage' and r['period_start']==start
                           and r['period_end']==as_of for r in record)
            sale_manual = total('historical_sales_manual')
            sale_import = total('historical_sales_import')
            sale_app = app_sales.get(pid,0)
            document_qty = opening + historical_purchases + purchase_app - sale_manual - sale_import - sale_app if coverage else None
            explicit = [r['unit_value_pln'] for r in record if r['kind'] in ('unit_value_manual','unit_value_import')]
            prices = [r['unit_value_pln'] for r in record if r['unit_value_pln'] is not None]
            if explicit and len({Decimal(v) for v in explicit}) > 1:
                raise ValueError(f"Sprzeczne zatwierdzone ceny dla {product['sku']}")
            # Multiple purchase prices need an explicit valuation decision.
            value = explicit[-1] if explicit else (prices[-1] if len({Decimal(v) for v in prices}) == 1 else None)
            previous = db.execute('''SELECT sn.counted_final FROM internal_remanent_snapshots sn
                 JOIN internal_inventory_count_sessions s ON s.session_id=sn.session_id
                 WHERE s.status='COMPLETED' AND sn.product_id=? AND sn.counted_final IS NOT NULL
                 ORDER BY s.completed_at DESC LIMIT 1''', (pid,)).fetchone()
            model = str(product['model'] or '').strip()
            sku = str(product['sku'])
            variant = sku[len(model):].lstrip('- ') if model and sku.casefold().startswith(model.casefold()) else ''
            db.execute('''INSERT INTO internal_remanent_snapshots
                 (snapshot_id,session_id,product_id,sku,name,model,variant,system_stock_at_start,last_inventory_count,
                  opening_stock,historical_purchases,purchases_from_app,purchases_known,
                  historical_sales_manual,historical_sales_import,sales_from_app,document_stock,unit_value_pln,source_json)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (f'{session_id}:{pid}',session_id,pid,sku,product['name'] or '',model,variant,
                 int(product['stock_qty']),int(previous['counted_final']) if previous else None,
                 opening,historical_purchases,purchase_app,int(bool(coverage)),sale_manual,sale_import,sale_app,
                 document_qty,value,json.dumps({'entry_ids':[r['id'] for r in record],
                                                'sales_dates':sale_dates.get(pid,[])},ensure_ascii=False)))
        changed = db.execute('''UPDATE internal_inventory_count_sessions SET phase='IN_PROGRESS',
            as_of_date=?,snapshot_at=?,source_fingerprint=?,company_snapshot_json=?
            WHERE session_id=? AND phase='DRAFT' AND status='OPEN' ''',
            (as_of, now(), _source_fingerprint(db,start,as_of),
             json.dumps(dict(db.execute('SELECT * FROM company_profile WHERE id=1').fetchone() or {}),ensure_ascii=False),
             session_id)).rowcount
        if changed != 1:
            raise ValueError('Remanent został rozpoczęty w innym żądaniu')
        db.commit()
    except Exception:
        db.rollback()
        raise
    return session_id


def detail(db, session_id, owner=None):
    session = dict(_session(db,session_id,owner))
    snapshots = [dict(row) for row in db.execute('''SELECT * FROM internal_remanent_snapshots
        WHERE session_id=? ORDER BY sku''', (session_id,))]
    counts = {row['product_id']: row for row in db.execute('''SELECT * FROM internal_inventory_count_items
        WHERE session_id=? AND status<>'SUPERSEDED' ''', (session_id,))}
    items=[]
    for row in snapshots:
        count = row['counted_final'] if session['status']=='COMPLETED' else (
            counts[row['product_id']]['counted_quantity'] if row['product_id'] in counts else None)
        price = Decimal(row['unit_value_pln']) if row['unit_value_pln'] is not None else None
        document = row['document_stock']
        if price is None and document == 0 and count == 0:
            price = Decimal('0')  # zero-stock SKU has no valuation to guess
            row['unit_value_pln'] = '0.00'
        row['counted_qty'] = count
        row['assumed_zero'] = bool(row['assumed_zero']) if session['status']=='COMPLETED' else False
        row['purchases_total'] = row['historical_purchases'] + row['purchases_from_app'] if row['purchases_known'] else None
        row['sales_total'] = row['historical_sales_manual']+row['historical_sales_import']+row['sales_from_app']
        row['difference_document_vs_count'] = count-document if count is not None and document is not None else None
        row['difference_system_vs_count'] = count-row['system_stock_at_start'] if count is not None else None
        row['document_stock_value'] = str(money(document*price)) if document is not None and price is not None else None
        row['counted_stock_value'] = str(money(count*price)) if count is not None and price is not None else None
        row['difference_value'] = str(money((count-document)*price)) if count is not None and document is not None and price is not None else None
        items.append(row)
    def sum_field(field):
        values=[item[field] for item in items]
        return sum(values) if all(value is not None for value in values) else None
    def sum_money(field):
        values=[item[field] for item in items]
        return str(money(sum(Decimal(value) for value in values))) if all(value is not None for value in values) else None
    total = {'opening_stock':sum_field('opening_stock'), 'purchases_total':sum_field('purchases_total'),
             'sales_total':sum_field('sales_total'),'document_stock':sum_field('document_stock'),
             'document_value':sum_money('document_stock_value'),'counted_qty':sum_field('counted_qty'),
             'counted_value':sum_money('counted_stock_value'),
             'difference_qty':sum_field('difference_document_vs_count'),
             'difference_value':sum_money('difference_value'),
             'shortage_qty':sum(max(0,-i['difference_document_vs_count']) for i in items if i['difference_document_vs_count'] is not None),
             'surplus_qty':sum(max(0,i['difference_document_vs_count']) for i in items if i['difference_document_vs_count'] is not None),
             'shortage_value':str(money(sum(max(Decimal('0'),-Decimal(i['difference_value'])) for i in items if i['difference_value'] is not None))),
             'surplus_value':str(money(sum(max(Decimal('0'),Decimal(i['difference_value'])) for i in items if i['difference_value'] is not None))),
             'sku_count':len(items),'counted_count':sum(i['counted_qty'] is not None and not i['assumed_zero'] for i in items),
             'uncounted_count':sum(i['counted_qty'] is None or i['assumed_zero'] for i in items)}
    return session,items,total


def close_count(db, session_id, owner, *, confirm_uncounted=False):
    session = _session(db,session_id,owner,phase='IN_PROGRESS')
    if session['status'] != 'OPEN':
        raise ValueError('Remanent już zamknięto')
    start = f"{session['inventory_year']}-01-01"
    # Changes to invoice/receipt documents since start require a new snapshot.
    # The frozen count must never silently acquire backdated documents.
    db.execute('BEGIN IMMEDIATE')
    try:
        if _source_fingerprint(db,start,session['as_of_date']) != session['source_fingerprint']:
            raise ValueError('Faktury lub przyjęcia zmieniły się od rozpoczęcia spisu; zweryfikuj dokumenty')
        _s,items,total = detail(db,session_id,owner)
        if total['uncounted_count'] and not confirm_uncounted:
            raise ValueError('Niepoliczone SKU wymagają jawnego potwierdzenia przyjęcia 0 szt.')
        if any(not item['purchases_known'] for item in items):
            raise ValueError('Niepotwierdzona kompletność zakupów; nie wolno przyjąć braków za 0')
        if any(item['unit_value_pln'] is None and (item['document_stock'] or (item['counted_qty'] or 0))
               for item in items):
            raise ValueError('Brak potwierdzonej ceny jednostkowej dla części SKU')
        for item in items:
            db.execute('''UPDATE internal_remanent_snapshots SET counted_final=?,assumed_zero=?
                WHERE session_id=? AND product_id=?''',
                (item['counted_qty'] if item['counted_qty'] is not None else 0,
                 int(item['counted_qty'] is None),session_id,item['product_id']))
        db.execute('''UPDATE internal_inventory_count_sessions SET status='COMPLETED',completed_at=?,active_product_id=NULL
                      WHERE session_id=? AND status='OPEN' ''',(now(),session_id))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return detail(db,session_id,owner)


def register_routes(app, deps):
    from flask import abort, redirect, render_template, request, send_file, url_for
    from internal_rbac import current_actor_context, require_permission
    from business_operations import execute_business_operation
    from remanent_pdf import render_pdf

    @app.before_request
    def require_durable_remanent_storage():
        if (request.path.startswith('/remanent') and request.method == 'POST'
                and os.environ.get('RENDER')
                and (not os.environ.get('APP_DATA_DIR')
                     or os.environ.get('REMANENT_PERSISTENCE_READY') != '1')):
            return ('Remanent jest zablokowany: najpierw potwierdź trwały dysk '
                    'APP_DATA_DIR i ustaw REMANENT_PERSISTENCE_READY=1.', 503)

    def owner():
        actor = current_actor_context()
        if actor is None:
            abort(401)
        return actor

    def db_open():
        return deps['conn']()

    def show(session_id, *, error='', preview=None):
        db = db_open()
        try:
            session,items,total = detail(db,session_id,owner().actor_id)
            entries=[dict(row) for row in db.execute('''SELECT e.*,p.sku FROM internal_remanent_entries e
              JOIN products p ON p.id=e.product_id WHERE e.inventory_year=? ORDER BY e.id DESC''',
              (session['inventory_year'],))]
            products=[dict(row) for row in db.execute('SELECT id,sku,name FROM products ORDER BY sku')]
        finally:
            db.close()
        return render_template('remanent.html', title='Remanent',base_url=deps['BASE_URL'],db_path=deps['DB_PATH'],
                               remanent=session,items=items,totals=total,entries=entries,products=products,
                               error=error,preview=preview,today=date.today().isoformat())

    @app.get('/remanent')
    @require_permission('inventory.read')
    def remanent_index():
        db=db_open()
        try:
            sessions=[dict(row) for row in db.execute('''SELECT s.*,
                (SELECT COALESCE(SUM(counted_final),0) FROM internal_remanent_snapshots
                 WHERE session_id=s.session_id) counted_units
                FROM internal_inventory_count_sessions s WHERE inventory_year IS NOT NULL
                AND created_by=? ORDER BY created_at DESC''',(owner().actor_id,))]
            for session in sessions:
                if session['status']=='COMPLETED':
                    values=db.execute('''SELECT counted_final,unit_value_pln FROM internal_remanent_snapshots
                        WHERE session_id=?''',(session['session_id'],)).fetchall()
                    session['counted_value']=str(money(sum((Decimal(row['unit_value_pln'] or '0') *
                        row['counted_final'] for row in values),Decimal('0'))))
        finally: db.close()
        return render_template('remanent.html', title='Remanent',base_url=deps['BASE_URL'],db_path=deps['DB_PATH'],
                               sessions=sessions,remanent=None,error='',today=date.today().isoformat())

    @app.post('/remanent/new')
    @require_permission('inventory.discrepancy_report')
    def remanent_new():
        db=db_open()
        try:
            sid=create_draft(db,owner().actor_id,request.form.get('year') or date.today().year)
            return redirect(url_for('remanent_detail',session_id=sid))
        except ValueError as exc:
            db.rollback()
            return str(exc),409
        finally: db.close()

    @app.get('/remanent/<session_id>')
    @require_permission('inventory.read')
    def remanent_detail(session_id):
        try: return show(session_id)
        except ValueError: abort(404)

    @app.post('/remanent/<session_id>/entries')
    @require_permission('inventory.remanent_manage')
    def remanent_entry(session_id):
        db=db_open()
        try:
            sku=str(request.form.get('sku') or '').strip()
            product=db.execute('SELECT id FROM products WHERE sku=? COLLATE NOCASE',(sku,)).fetchone()
            if not product: raise ValueError('Nie znaleziono dokładnego SKU')
            kind=request.form.get('kind') or ''
            entry_date=request.form.get('period_start') or ''
            add_entry(db,session_id,owner().actor_id,product_id=product['id'],kind=kind,
                      period_start=entry_date,period_end=request.form.get('period_end') or entry_date,
                      quantity=request.form.get('quantity') or 0,source=request.form.get('source') or '',
                      unit_value_pln=request.form.get('unit_value_pln'),
                      document_no=request.form.get('document_no') or '',note=request.form.get('note') or '')
            db.commit()
            return redirect(url_for('remanent_detail',session_id=session_id))
        except (ValueError,sqlite3.Error) as exc:
            db.rollback()
            return show(session_id,error=str(exc)),409
        finally: db.close()

    @app.post('/remanent/<session_id>/entries/<int:entry_id>/void')
    @require_permission('inventory.remanent_manage')
    def remanent_void(session_id,entry_id):
        db=db_open()
        try:
            void_entry(db,session_id,owner().actor_id,entry_id,request.form.get('reason') or '')
            db.commit()
            return redirect(url_for('remanent_detail',session_id=session_id))
        except ValueError as exc:
            db.rollback()
            return show(session_id,error=str(exc)),409
        finally: db.close()

    @app.post('/remanent/<session_id>/coverage')
    @require_permission('inventory.remanent_manage')
    def remanent_coverage(session_id):
        db=db_open()
        try:
            if request.form.get('confirmed') != 'yes':
                raise ValueError('Wymagane świadome potwierdzenie kompletności zakupów')
            confirm_purchase_coverage(db,session_id,owner().actor_id,request.form.get('as_of_date'))
            db.commit()
            return redirect(url_for('remanent_detail',session_id=session_id))
        except ValueError as exc:
            db.rollback()
            return show(session_id,error=str(exc)),409
        finally: db.close()

    @app.post('/remanent/<session_id>/csv/preview')
    @require_permission('inventory.remanent_manage')
    def remanent_csv_preview(session_id):
        db=db_open()
        try:
            uploaded=request.files.get('file')
            if not uploaded: raise ValueError('Wybierz plik CSV')
            preview=preview_csv(db,session_id,owner().actor_id,uploaded.read(5*1024*1024+1),
                request.form.get('kind'),request.form.get('period_start'),request.form.get('period_end'))
            return show(session_id,preview=preview)
        except ValueError as exc:
            return show(session_id,error=str(exc)),400
        finally: db.close()

    @app.post('/remanent/<session_id>/csv/confirm')
    @require_permission('inventory.remanent_manage')
    def remanent_csv_confirm(session_id):
        db=db_open()
        try:
            uploaded=request.files.get('file')
            if not uploaded: raise ValueError('Wybierz ponownie ten sam plik CSV')
            import_csv(db,session_id,owner().actor_id,uploaded.read(5*1024*1024+1),
                request.form.get('kind'),request.form.get('period_start'),request.form.get('period_end'),
                request.form.get('sha256'))
            return redirect(url_for('remanent_detail',session_id=session_id))
        except (ValueError,sqlite3.IntegrityError) as exc:
            db.rollback()
            return show(session_id,error=str(exc)),409
        finally: db.close()

    @app.post('/remanent/<session_id>/start')
    @require_permission('inventory.remanent_manage')
    def remanent_start(session_id):
        db=db_open()
        try:
            start_count(db,session_id,owner().actor_id,request.form.get('as_of_date'))
            return redirect(url_for('remanent_detail',session_id=session_id))
        except ValueError as exc:
            db.rollback()
            return show(session_id,error=str(exc)),409
        finally: db.close()

    @app.post('/remanent/<session_id>/count')
    @require_permission('inventory.discrepancy_report')
    def remanent_count(session_id):
        db=db_open()
        try:
            _session(db,session_id,owner().actor_id,phase='IN_PROGRESS')
            product=db.execute('SELECT id FROM products WHERE sku=? COLLATE NOCASE',
                               (str(request.form.get('sku') or '').strip(),)).fetchone()
            if not product: raise ValueError('Nieznane SKU')
            expected=execute_business_operation(owner(),'inventory.count.get_expected',{'product_id':product['id']})
            if expected.status!='SUCCESS': raise ValueError(expected.safe_error_message or expected.error_code)
            counted=int(request.form.get('counted_qty'))
            result=execute_business_operation(owner(),'inventory.count.record',
                {'product_id':product['id'],'count_session_id':session_id,'counted_quantity':counted,
                 'expected_version':expected.data['version'],'idempotency_key':str(uuid.uuid4())})
            if result.status!='SUCCESS': raise ValueError(result.safe_error_message or result.error_code)
            return redirect(url_for('remanent_detail',session_id=session_id))
        except (ValueError,TypeError) as exc:
            return show(session_id,error=str(exc)),409
        finally: db.close()

    @app.post('/remanent/<session_id>/close')
    @require_permission('inventory.remanent_manage')
    def remanent_close(session_id):
        db=db_open()
        try:
            if request.form.get('confirmed')!='yes':
                raise ValueError('Zamknięcie wymaga jawnego potwierdzenia')
            close_count(db,session_id,owner().actor_id,
                        confirm_uncounted=request.form.get('uncounted_zero')=='yes')
            return redirect(url_for('remanent_detail',session_id=session_id))
        except ValueError as exc:
            db.rollback()
            return show(session_id,error=str(exc)),409
        finally: db.close()

    @app.get('/remanent/<session_id>/pdf/<kind>')
    @require_permission('inventory.read')
    def remanent_pdf(session_id,kind):
        db=db_open()
        try:
            session,items,total=detail(db,session_id,owner().actor_id)
            company=json.loads(session['company_snapshot_json'] or '{}')
            if kind=='sheet' and not company.get('company_name'):
                raise ValueError('Uzupełnij nazwę firmy przed wygenerowaniem arkusza')
            pdf=render_pdf(session,items,total,kind,company)
            return send_file(pdf,mimetype='application/pdf',as_attachment=True,
                             download_name=f"{session['remanent_no'].replace('/','-')}-{kind}.pdf")
        except ValueError as exc:
            return str(exc),409
        finally: db.close()
