"""Versioned file reconciliation. Never changes physical stock or invoice data."""
import hashlib
import json
import uuid
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext

BASES = {'goods_net','goods_transport','landed_cost'}
METHOD = 'periodic_weighted_average'


def canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def initialize(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS remanent_source_installation(id INTEGER PRIMARY KEY CHECK(id=1),installation_id TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS remanent_source_heads(installation_id TEXT NOT NULL,import_id TEXT NOT NULL,version INTEGER NOT NULL,
        PRIMARY KEY(installation_id,import_id));
    CREATE TABLE IF NOT EXISTS remanent_source_bundles(bundle_id TEXT PRIMARY KEY,installation_id TEXT NOT NULL,import_id TEXT NOT NULL,
        version INTEGER NOT NULL,basis TEXT NOT NULL,payload_hash TEXT NOT NULL,created_at TEXT NOT NULL,
        UNIQUE(installation_id,import_id,version,basis));
    CREATE TABLE IF NOT EXISTS remanent_source_lines(bundle_id TEXT NOT NULL,line_id TEXT NOT NULL,product_id INTEGER NOT NULL,
        payload_json TEXT NOT NULL,decision_json TEXT NOT NULL,created_by TEXT NOT NULL,
        PRIMARY KEY(bundle_id,line_id));
    CREATE TABLE IF NOT EXISTS remanent_source_commits(commit_key TEXT PRIMARY KEY,session_id TEXT NOT NULL,
        payload_hash TEXT NOT NULL,decisions_json TEXT NOT NULL,created_by TEXT NOT NULL,created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS remanent_source_decisions(id INTEGER PRIMARY KEY AUTOINCREMENT,installation_id TEXT NOT NULL,
        import_id TEXT NOT NULL,version INTEGER NOT NULL,line_id TEXT NOT NULL,decision_json TEXT NOT NULL,
        created_by TEXT NOT NULL,created_at TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS remanent_source_decision_lookup ON remanent_source_decisions(installation_id,import_id,version,line_id,id);
    CREATE TRIGGER IF NOT EXISTS remanent_source_decisions_no_update BEFORE UPDATE ON remanent_source_decisions
        BEGIN SELECT RAISE(ABORT,'RECONCILIATION_HISTORY_IMMUTABLE'); END;
    CREATE TRIGGER IF NOT EXISTS remanent_source_decisions_no_delete BEFORE DELETE ON remanent_source_decisions
        BEGIN SELECT RAISE(ABORT,'RECONCILIATION_HISTORY_IMMUTABLE'); END;
    CREATE TABLE IF NOT EXISTS remanent_valuation_settings(session_id TEXT PRIMARY KEY,method TEXT NOT NULL,basis TEXT NOT NULL,
        confirmed_by TEXT NOT NULL,confirmed_at TEXT NOT NULL);
    CREATE TRIGGER IF NOT EXISTS remanent_source_lines_immutable_update BEFORE UPDATE ON remanent_source_lines
        BEGIN SELECT RAISE(ABORT,'SOURCE_VERSION_IMMUTABLE'); END;
    CREATE TRIGGER IF NOT EXISTS remanent_source_lines_immutable_delete BEFORE DELETE ON remanent_source_lines
        BEGIN SELECT RAISE(ABORT,'SOURCE_VERSION_IMMUTABLE'); END;
    CREATE TRIGGER IF NOT EXISTS remanent_source_bundles_immutable_update BEFORE UPDATE ON remanent_source_bundles
        BEGIN SELECT RAISE(ABORT,'SOURCE_VERSION_IMMUTABLE'); END;
    CREATE TRIGGER IF NOT EXISTS remanent_source_bundles_immutable_delete BEFORE DELETE ON remanent_source_bundles
        BEGIN SELECT RAISE(ABORT,'SOURCE_VERSION_IMMUTABLE'); END;
    ''')
    columns = {row['name'] for row in db.execute('PRAGMA table_info(remanent_valuation_settings)')}
    if 'manual_basis_confirmed' not in columns:
        db.execute('ALTER TABLE remanent_valuation_settings ADD COLUMN manual_basis_confirmed INTEGER NOT NULL DEFAULT 0')
    db.executescript('''
    CREATE TRIGGER IF NOT EXISTS remanent_valuation_settings_no_started_insert BEFORE INSERT ON remanent_valuation_settings
    WHEN NOT EXISTS(SELECT 1 FROM internal_inventory_count_sessions WHERE session_id=NEW.session_id AND phase='DRAFT' AND status='OPEN')
    BEGIN SELECT RAISE(ABORT,'VALUATION_SETTINGS_FROZEN'); END;
    CREATE TRIGGER IF NOT EXISTS remanent_valuation_settings_no_started_update BEFORE UPDATE ON remanent_valuation_settings
    WHEN NOT EXISTS(SELECT 1 FROM internal_inventory_count_sessions WHERE session_id=OLD.session_id AND phase='DRAFT' AND status='OPEN')
      OR NOT EXISTS(SELECT 1 FROM internal_inventory_count_sessions WHERE session_id=NEW.session_id AND phase='DRAFT' AND status='OPEN')
    BEGIN SELECT RAISE(ABORT,'VALUATION_SETTINGS_FROZEN'); END;
    CREATE TRIGGER IF NOT EXISTS remanent_valuation_settings_no_started_delete BEFORE DELETE ON remanent_valuation_settings
    WHEN NOT EXISTS(SELECT 1 FROM internal_inventory_count_sessions WHERE session_id=OLD.session_id AND phase='DRAFT' AND status='OPEN')
    BEGIN SELECT RAISE(ABORT,'VALUATION_SETTINGS_FROZEN'); END;
    ''')
    db.execute('INSERT OR IGNORE INTO remanent_source_installation VALUES(1,?)',(str(uuid.uuid4()),))


def _decimal(value):
    if value is None: return None
    try:
        if not isinstance(value,str) or not value or len(value)>100: raise ValueError()
        result = Decimal(value)
        if not result.is_finite() or result < 0 or abs(result.as_tuple().exponent)>100: raise ValueError()
        return result
    except (InvalidOperation,ValueError):
        raise ValueError('Koszt musi być nieujemną liczbą dziesiętną lub jawnym brakiem.') from None


def _date(value, field, *, nullable=False):
    if value is None and nullable: return None
    try:
        if not isinstance(value,str) or len(value)!=10: raise ValueError()
        parsed=date.fromisoformat(value)
        if parsed.isoformat()!=value: raise ValueError()
        return value
    except (ValueError,TypeError):
        raise ValueError('Nieprawidłowa data '+field+'; wymagany format RRRR-MM-DD.') from None


def parse(data):
    if len(data) > 5*1024*1024: raise ValueError('Plik przekracza 5 MB.')
    try: payload = json.loads(data.decode('utf-8-sig'))
    except (ValueError,UnicodeError): raise ValueError('Oczekiwany jest plik JSON mostu w wersji 2.') from None
    if (not isinstance(payload,dict) or payload.get('schema') != 'annual_inventory_bridge' or payload.get('version') != 2
            or payload.get('export_scope') != 'complete_source_versions'):
        raise ValueError('Wymagany kontrakt annual_inventory_bridge v2 i kompletne wersje źródeł.')
    installation = str(payload.get('source_installation_id') or '')
    try: payload['source_installation_id']=str(uuid.UUID(installation))
    except ValueError: raise ValueError('Brak stabilnego identyfikatora instalacji źródłowej.') from None
    lines = payload.get('lines')
    if not isinstance(lines,list) or not lines or len(lines)>10000: raise ValueError('Brak pozycji lub zbyt wiele pozycji.')
    seen = set()
    versions = {}
    for row in lines:
        if not isinstance(row,dict): raise ValueError('Nieprawidłowa pozycja źródłowa.')
        for field in ('source_import_id','line_id','sku','document_no','supplier','valuation_basis'):
            if not isinstance(row.get(field),str) or len(row[field])>300: raise ValueError('Nieprawidłowe pole '+field)
        if not row['source_import_id'] or not row['line_id'] or not row['sku'].strip(): raise ValueError('Brak tożsamości dostawy/pozycji/SKU.')
        if not isinstance(row.get('kind'),str) or row['kind'] not in {'purchase','opening'} or row['valuation_basis'] not in BASES: raise ValueError('Nieznany rodzaj pozycji lub podstawa kosztów.')
        if type(row.get('source_version')) is not int or not 1<=row['source_version']<=9223372036854775807: raise ValueError('Nieprawidłowa wersja źródła.')
        if type(row.get('quantity')) is not int or not 0<=row['quantity']<=9223372036854775807: raise ValueError('Nieprawidłowa ilość źródła.')
        if type(row.get('inventory_year')) is not int or not 2000<=row['inventory_year']<=2100: raise ValueError('Nieprawidłowy rok.')
        _date(row.get('document_date'),'document_date')
        _date(row.get('received_date'),'received_date',nullable=True)
        total,unit=(_decimal(row.get(field)) for field in ('line_value_pln','unit_value_pln'))
        if row['quantity']==0 and total not in (None,Decimal('0')): raise ValueError('Zerowa ilość nie może mieć dodatniej wartości.')
        if total is not None and unit is not None:
            with localcontext() as context:
                context.prec=240
                expected=(unit*row['quantity']).quantize(Decimal('0.01'),rounding=ROUND_HALF_UP)
                if total!=expected: raise ValueError('Wartość pozycji nie zgadza się z ilością i ceną jednostkową.')
        key = (row['source_import_id'],row['source_version'],row['valuation_basis'],row['line_id'])
        if key in seen: raise ValueError('Powtórzony line_id w tej samej wersji i podstawie kosztów.')
        seen.add(key)
        prior = versions.setdefault(row['source_import_id'],row['source_version'])
        if prior != row['source_version']: raise ValueError('Plik zawiera dwie wersje tej samej dostawy.')
    return payload


def line_key(payload,row):
    return digest([payload['source_installation_id'],row['source_import_id'],row['source_version'],row['valuation_basis'],row['line_id']])


def receipts(db):
    """One stable identity per package/product; JSON position is not an identity."""
    result = []
    for saved in db.execute('''SELECT r.*,p.package_no,p.cost_document_no,p.supplier FROM china_stock_receipts r
        LEFT JOIN china_packages p ON p.id=r.package_id ORDER BY r.package_id'''):
        try: lines = json.loads(saved['quantities_json'] or '[]')
        except (ValueError,TypeError): lines = []
        if not isinstance(lines,list): lines = []
        grouped={}
        if not lines: grouped[None]=None
        for item in lines:
            pid = item.get('product_id') if isinstance(item,dict) else None
            product = db.execute('SELECT sku FROM products WHERE id=?',(pid,)).fetchone() if type(pid) is int and pid>0 else None
            if not product: pid=None
            qty = item.get('qty') if isinstance(item,dict) else None
            if type(qty) is not int or qty<0: qty = None
            grouped[pid]=None if qty is None or (pid in grouped and grouped[pid] is None) else grouped.get(pid,0)+qty
        for pid,qty in sorted(grouped.items(),key=lambda pair:pair[0] or 0):
            product=db.execute('SELECT sku FROM products WHERE id=?',(pid,)).fetchone() if pid else None
            row={'receipt_key':str(saved['package_id'])+(':product:'+str(pid) if pid else ':unknown'),'product_id':pid,
                'sku':product['sku'] if product else '', 'quantity':qty,'received_date':str(saved['received_at'])[:10],
                'document_no':saved['cost_document_no'] or saved['package_no'] or '', 'supplier':saved['supplier'] or ''}
            row['fingerprint']=digest(row)
            result.append(row)
    return result


def active_rows(db):
    result=[]
    for row in db.execute('''SELECT b.*,l.line_id,l.product_id,l.payload_json,l.decision_json
        FROM remanent_source_bundles b JOIN remanent_source_heads h
        ON h.installation_id=b.installation_id AND h.import_id=b.import_id AND h.version=b.version
        JOIN remanent_source_lines l ON l.bundle_id=b.bundle_id ORDER BY b.bundle_id,l.line_id'''):
        latest=db.execute('''SELECT id,decision_json FROM remanent_source_decisions WHERE installation_id=? AND import_id=?
            AND version=? AND line_id=? ORDER BY id DESC LIMIT 1''',(row['installation_id'],row['import_id'],row['version'],row['line_id'])).fetchone()
        decision=json.loads(latest['decision_json'] if latest else row['decision_json'])
        result.append({**dict(row),'source':json.loads(row['payload_json']),
            'product_id':decision.get('product_id',row['product_id']), 'decision':decision,
            'decision_revision':latest['id'] if latest else None})
    return result


def _session(db,session_id,owner):
    import remanent
    session=remanent._session(db,session_id,owner,phase='DRAFT')
    if session['status']!='OPEN': raise ValueError('Remanent jest zamknięty.')
    return session


def _valid_allocation(allocation,receipt,product_id):
    return bool(receipt and receipt['product_id']==product_id and receipt['quantity'] is not None
        and type(allocation.get('quantity')) is int and 0<allocation['quantity']<=receipt['quantity']
        and allocation.get('receipt_fingerprint')==receipt['fingerprint'])


def preview(db,session_id,owner,data):
    session = _session(db,session_id,owner)
    payload = parse(data)
    existing = active_rows(db)
    physical = receipts(db)
    products = [dict(row) for row in db.execute('SELECT id,sku FROM products ORDER BY id')]
    rows=[]
    used={a['receipt_key'] for row in existing for a in row['decision'].get('allocations',[])}
    for source in payload['lines']:
        matches=[p for p in products if p['sku'].casefold()==source['sku'].casefold()]
        pid=matches[0]['id'] if len(matches)==1 else None
        candidates=[r for r in physical if pid and r['product_id']==pid]
        previous=next((r for r in existing if r['installation_id']==payload['source_installation_id'] and r['import_id']==source['source_import_id'] and r['line_id']==source['line_id']),None)
        decision=previous['decision'] if previous else None
        linked=[a['receipt_key'] for a in (decision or {}).get('allocations',[])]
        used.update(linked)
        if pid is None: status='UNKNOWN_SKU'
        elif source['kind']=='opening': status='OPENING_BALANCE'
        elif linked:
            by_key={r['receipt_key']:r for r in candidates}
            status='MATCHED' if (all(_valid_allocation(a,by_key.get(a['receipt_key']),pid) for a in decision['allocations'])
                and sum(a['quantity'] for a in decision['allocations'])<=source['quantity']) else 'QUANTITY_CONFLICT'
        elif len(candidates)>1: status='AMBIGUOUS'
        elif candidates and candidates[0]['quantity']!=source['quantity']: status='QUANTITY_CONFLICT'
        elif candidates: status='SUGGESTED_MATCH'
        else: status='ONLY_IN_ANNUAL'
        rows.append({'key':line_key(payload,source),'source':source,'product_id':pid,'status':status,
                     'candidates':candidates,'previous_decision':decision})
    fingerprint=digest({'payload':payload,'receipts':physical,'sources':existing,'products':products,'year':session['inventory_year']})
    return {'schema':2,'payload_hash':digest(payload),'fingerprint':fingerprint,'rows':rows,
        'warehouse_only':[r for r in physical if r['receipt_key'] not in used],
        'message':'SKU identyfikuje produkt. Zgodność ilości lub dokumentu jest sugestią; dopasowanie wymaga decyzji.'}


def commit(db,session_id,owner,data,decisions,expected_fingerprint):
    import inventory_count_lifecycle
    inventory_count_lifecycle.assert_storage(db)
    payload=parse(data)
    key=digest([session_id,payload,decisions,expected_fingerprint])
    db.execute('BEGIN IMMEDIATE')
    try:
        _session(db,session_id,owner)
        if db.execute('SELECT 1 FROM remanent_source_commits WHERE commit_key=?',(key,)).fetchone():
            db.rollback()
            return {'ok':True,'already_imported':True,'commit_key':key}
        plan=preview(db,session_id,owner,data)
        if plan['fingerprint']!=expected_fingerprint: raise ValueError('Źródła zmieniły się od podglądu. Otwórz nowy podgląd.')
        if not isinstance(decisions,list) or any(not isinstance(choice,dict) or not isinstance(choice.get('line_key'),str) for choice in decisions):
            raise ValueError('Nieprawidłowa lista decyzji.')
        choices={choice['line_key']:choice for choice in decisions}
        if len(choices)!=len(decisions) or set(choices)!={r['key'] for r in plan['rows']}: raise ValueError('Rozstrzygnij każdą pozycję dokładnie raz.')
        physical={r['receipt_key']:r for r in receipts(db)}
        bundle_rows=defaultdict(list)
        normalized=[]
        installation=payload['source_installation_id']
        replacing={(installation,row['source_import_id']) for row in payload['lines']}
        prior=active_rows(db)
        for item in plan['rows']:
            source=item['source']; choice=choices[item['key']]
            action=choice.get('action')
            if not isinstance(action,str) or action not in {'exclude','link','history','opening'}: raise ValueError('Wybierz działanie dla '+source['sku'])
            if not item['product_id'] and action!='exclude': raise ValueError('Najpierw wyjaśnij SKU '+source['sku'])
            decision={'action':action,'allocations':[],'received_date':None,'product_id':item['product_id']}
            if action=='opening':
                if source['kind']!='opening': raise ValueError('To nie jest stan początkowy.')
            elif action=='history':
                if source['kind']!='purchase': raise ValueError('Stan początkowy wymaga osobnego wyboru.')
                day=choice.get('received_date') or source.get('received_date')
                if not day: raise ValueError('Podaj datę fizycznego przyjęcia dla zakupu historycznego.')
                decision['received_date']=_date(day,'received_date')
            elif action=='link':
                if source['kind']!='purchase': raise ValueError('Nie można przypisać stanu początkowego do dostawy.')
                seen=set(); quantity=0
                allocations=choice.get('allocations',[])
                if not isinstance(allocations,list) or any(not isinstance(a,dict) for a in allocations): raise ValueError('Nieprawidłowe powiązanie przyjęcia.')
                for allocation in allocations:
                    if not isinstance(allocation.get('receipt_key'),str): raise ValueError('Nieprawidłowy identyfikator przyjęcia.')
                    receipt=physical.get(allocation.get('receipt_key'))
                    qty=allocation.get('quantity')
                    if not receipt or receipt['product_id']!=item['product_id'] or receipt['quantity'] is None: raise ValueError('Nieprawidłowe powiązanie przyjęcia.')
                    if type(qty) is not int or qty<=0 or qty>receipt['quantity']: raise ValueError('Nieprawidłowa ilość powiązania.')
                    if receipt['receipt_key'] in seen: raise ValueError('Powtórzone przyjęcie w pozycji.')
                    seen.add(receipt['receipt_key']); quantity+=qty
                    decision['allocations'].append({'receipt_key':receipt['receipt_key'],'quantity':qty,'receipt_fingerprint':receipt['fingerprint']})
                decision['allocations'].sort(key=lambda r:r['receipt_key'])
                if not quantity or quantity>source['quantity']: raise ValueError('Suma powiązań musi być dodatnia i nie przekraczać ilości źródła.')
            record={'source':source,'decision':decision,'product_id':item['product_id'],
                    'installation_id':installation,'import_id':source['source_import_id'],'version':source['source_version'],'basis':source['valuation_basis'],'line_id':source['line_id']}
            normalized.append(record)
            bundle_rows[(source['source_import_id'],source['source_version'],source['valuation_basis'])].append(record)
        # Physical identity and operator decisions must agree between cost bases.
        combined=[r for r in prior if (r['installation_id'],r['import_id']) not in replacing]+normalized
        for old in prior:
            if (old['installation_id'],old['import_id']) in replacing and any(old['version']==r['version'] and old['import_id']==r['import_id'] and old['basis']!=r['basis'] for r in normalized):
                revised=next((r for r in normalized if r['installation_id']==old['installation_id'] and r['import_id']==old['import_id'] and r['version']==old['version'] and r['line_id']==old['line_id']),None)
                combined.append({**old,'decision':revised['decision'],'product_id':revised['product_id']} if revised else old)
        basis_lines=defaultdict(lambda:defaultdict(set))
        for row in combined:
            basis_lines[(row['installation_id'],row['import_id'],row['version'])][row['basis']].add(row['line_id'])
        for variants in basis_lines.values():
            line_sets=list(variants.values())
            if any(ids!=line_sets[0] for ids in line_sets[1:]): raise ValueError('Każda podstawa kosztów musi zawierać komplet tych samych pozycji wersji źródła.')
        identities={}; allocated=defaultdict(int)
        for row in combined:
            identity=(row['installation_id'],row['import_id'],row['version'],row['line_id'])
            physical_fields={k:v for k,v in row['source'].items() if k not in {'valuation_basis','line_value_pln','unit_value_pln'}}
            evidence=digest([physical_fields,row['decision']])
            if identity in identities:
                if identities[identity]!=evidence: raise ValueError('Podstawy kosztów wskazują różne ilości lub uzgodnienia tego samego źródła.')
                continue
            identities[identity]=evidence
            for allocation in row['decision'].get('allocations',[]): allocated[allocation['receipt_key']]+=allocation['quantity']
        for receipt_key,qty in allocated.items():
            if receipt_key not in physical or physical[receipt_key]['quantity'] is None or qty>physical[receipt_key]['quantity']: raise ValueError('Koszty przekraczają ilość przyjęcia '+receipt_key)
        stamp=datetime.now(timezone.utc).isoformat()
        for (import_id,version,basis),rows in bundle_rows.items():
            head=db.execute('SELECT version FROM remanent_source_heads WHERE installation_id=? AND import_id=?',(installation,import_id)).fetchone()
            if head and version<head['version']: raise ValueError('Starsza wersja dostawy nie może zastąpić nowszej.')
            bundle_id=digest([installation,import_id,version,basis])
            payload_hash=digest(sorted([r['source'] for r in rows],key=lambda r:r['line_id']))
            old=db.execute('SELECT payload_hash FROM remanent_source_bundles WHERE bundle_id=?',(bundle_id,)).fetchone()
            if old and old['payload_hash']!=payload_hash: raise ValueError('Ta wersja źródła ma już inną treść. Wymagana nowa wersja źródła.')
            if not old:
                db.execute('INSERT INTO remanent_source_bundles VALUES(?,?,?,?,?,?,?)',(bundle_id,installation,import_id,version,basis,payload_hash,stamp))
                for row in rows:
                    db.execute('INSERT INTO remanent_source_lines VALUES(?,?,?,?,?,?)',(bundle_id,row['line_id'],row['product_id'] or 0,canonical(row['source']),canonical(row['decision']),owner))
            db.execute('INSERT INTO remanent_source_heads VALUES(?,?,?) ON CONFLICT(installation_id,import_id) DO UPDATE SET version=MAX(version,excluded.version)',(installation,import_id,version))
        for row in normalized:
            identity=(row['installation_id'],row['import_id'],row['version'],row['line_id'])
            previous=db.execute('''SELECT decision_json FROM remanent_source_decisions WHERE installation_id=? AND import_id=?
                AND version=? AND line_id=? ORDER BY id DESC LIMIT 1''',identity).fetchone()
            if not previous or previous['decision_json']!=canonical(row['decision']):
                db.execute('''INSERT INTO remanent_source_decisions(installation_id,import_id,version,line_id,decision_json,created_by,created_at)
                    VALUES(?,?,?,?,?,?,?)''',(*identity,canonical(row['decision']),owner,stamp))
        db.execute('INSERT INTO remanent_source_commits VALUES(?,?,?,?,?,?)',(key,session_id,digest(payload),canonical(decisions),owner,stamp))
        db.commit()
        return {'ok':True,'already_imported':False,'commit_key':key}
    except Exception:
        db.rollback(); raise


def select_method(db,session_id,owner,method,basis,confirm_manual_basis=False):
    import inventory_count_lifecycle
    inventory_count_lifecycle.assert_storage(db)
    session=_session(db,session_id,owner)
    if method!=METHOD or basis not in BASES: raise ValueError('Wybierz metodę i podstawę kosztów.')
    manual=db.execute('''SELECT 1 FROM internal_remanent_entries WHERE inventory_year=? AND voided_at IS NULL
        AND kind IN ('opening','historical_purchase_manual','historical_purchase_import') AND unit_value_pln IS NOT NULL LIMIT 1''',
        (session['inventory_year'],)).fetchone()
    if manual and confirm_manual_basis is not True:
        raise ValueError('Potwierdź, że wartości ręczne stanu początkowego i zakupów używają wybranej podstawy kosztów.')
    db.execute('''INSERT INTO remanent_valuation_settings(session_id,method,basis,confirmed_by,confirmed_at,manual_basis_confirmed)
        VALUES(?,?,?,?,?,?) ON CONFLICT(session_id)
        DO UPDATE SET method=excluded.method,basis=excluded.basis,confirmed_by=excluded.confirmed_by,
        confirmed_at=excluded.confirmed_at,manual_basis_confirmed=excluded.manual_basis_confirmed''',
        (session_id,method,basis,owner,datetime.now(timezone.utc).isoformat(),int(confirm_manual_basis is True)))


def valuation(db,year,as_of,basis):
    """Weighted-average inputs, not a second purchase-cost allocation engine."""
    result=defaultdict(lambda: {'opening_quantity':0,'opening_value':Decimal('0'),'opening_known':False,
        'history_quantity':0,'purchase_value':Decimal('0'),'receipt_quantity':0,'problems':[],'sources':[]})
    grouped=defaultdict(list)
    for row in active_rows(db): grouped[(row['installation_id'],row['import_id'],row['version'],row['line_id'])].append(row)
    links=defaultdict(list)
    physical={r['receipt_key']:r for r in receipts(db)}
    products={row['id']:row['sku'] for row in db.execute('SELECT id,sku FROM products')}
    for variants in grouped.values():
        row=next((r for r in variants if r['basis']==basis),variants[0])
        source,decision=row['source'],row['decision']; action=decision['action']
        if action=='exclude': continue
        if not row['product_id'] or products.get(row['product_id'],'').casefold()!=source['sku'].casefold():
            result[None]['problems'].append('Nieaktualne przypisanie SKU '+source['sku']+'; uzgodnij źródło ponownie')
            continue
        value=_decimal(source.get('line_value_pln')) if row['basis']==basis else None
        trace={k:row[k] for k in ('installation_id','import_id','version','basis','line_id','bundle_id','decision_revision') if k in row}
        trace.update(valuation_basis=source['valuation_basis'],document_no=source['document_no'],supplier=source['supplier'],document_date=source['document_date'],decision=decision)
        target=result[row['product_id']]
        if action=='opening' and source['inventory_year']==year:
            if target['opening_known']: target['problems'].append('Sprzeczne źródła stanu początkowego')
            target['opening_known']=True; target['opening_quantity']+=source['quantity']
            if value is None: target['problems'].append('Brak wartości stanu początkowego w wybranej podstawie')
            else: target['opening_value']+=value
            target['sources'].append(trace)
        if action=='history' and f'{year}-01-01'<=decision['received_date']<=as_of:
            target['history_quantity']+=source['quantity']; target['sources'].append(trace)
            if value is None: target['problems'].append('Brak kosztu zakupu historycznego w wybranej podstawie')
            else: target['purchase_value']+=value
        if action=='link':
            for allocation in decision['allocations']:
                receipt=physical.get(allocation['receipt_key'])
                if not _valid_allocation(allocation,receipt,row['product_id']):
                    target['problems'].append('Zmienione lub nieaktualne przyjęcie '+allocation['receipt_key']+'; uzgodnij źródło ponownie')
                    continue
                links[allocation['receipt_key']].append((allocation['quantity'],None if value is None else value*Decimal(allocation['quantity'])/Decimal(source['quantity']),trace))
    for receipt in physical.values():
        try: _date(receipt['received_date'],'data przyjęcia')
        except ValueError:
            result[None]['problems'].append('Przyjęcie bez wiarygodnej daty '+receipt['receipt_key']); continue
        if not f'{year}-01-01'<=receipt['received_date']<=as_of: continue
        target=result[receipt['product_id']]
        if receipt['quantity'] is None or not receipt['product_id']:
            target['problems'].append('Przyjęcie bez wiarygodnych pozycji '+receipt['receipt_key']); continue
        target['receipt_quantity']+=receipt['quantity']
        matched=links[receipt['receipt_key']]
        if sum(q for q,_v,_t in matched)!=receipt['quantity']: target['problems'].append('Niepełne przypisanie kosztów przyjęcia '+receipt['receipt_key'])
        for qty,value,trace in matched:
            target['sources'].append({**trace,'receipt':receipt,'allocated_quantity':qty})
            if value is None: target['problems'].append('Brak kosztu przyjęcia '+receipt['receipt_key'])
            else: target['purchase_value']+=value
    return dict(result)


def export_result(db,session_id,owner):
    import remanent
    session,items,_total=remanent.detail(db,session_id,owner)
    if session['status']!='COMPLETED': raise ValueError('Eksport wyniku wymaga zamkniętego spisu.')
    settings=db.execute('SELECT * FROM remanent_valuation_settings WHERE session_id=?',(session_id,)).fetchone()
    if not settings: raise ValueError('Brak utrwalonej metody wyceny.')
    body={'schema':'warehouse_inventory_result','version':1,
        'source_installation_id':db.execute('SELECT installation_id FROM remanent_source_installation WHERE id=1').fetchone()[0],
        'session_id':session_id,'inventory_year':session['inventory_year'],'as_of_date':session['as_of_date'],
        'closed_at':session['completed_at'],'valuation_method':settings['method'],'valuation_basis':settings['basis'],
        'items':[{'sku':item['sku'],'counted_quantity':item['counted_final'],'unit_value_pln':item['unit_value_pln'],
            'line_value_pln':item['counted_stock_value'],'source_versions':json.loads(item['source_json']).get('valuation_sources',[])} for item in items]}
    body['snapshot_hash']=digest(body)
    return canonical(body).encode('utf-8')
