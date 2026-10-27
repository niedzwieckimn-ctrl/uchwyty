"""Manual catalogue creation, separate from stock receipts and existing prices."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
import re


def money(raw):
    try:
        value = Decimal(str(raw).strip().replace(',', '.'))
        if not value.is_finite() or value < 0 or value > 99999999:
            raise ValueError()
        return value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        raise ValueError('Podaj poprawną, nieujemną cenę (maksymalnie 99 999 999).')


def validate(form):
    data = {key: str(form.get(key, '')).strip() for key in ('sku', 'ean', 'model', 'name')}
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,79}', data['sku']):
        raise ValueError('SKU: od 1 do 80 znaków, litery, cyfry oraz . _ / - (bez spacji).')
    if not data['model'] or len(data['model']) > 120 or len(data['name']) > 300:
        raise ValueError('Podaj model (do 120 znaków). Nazwa może mieć do 300 znaków.')
    if data['ean']:
        ean = data['ean']
        if not ean.isascii() or not ean.isdigit() or len(ean) not in (8, 12, 13, 14):
            raise ValueError('EAN/GTIN musi mieć 8, 12, 13 lub 14 cyfr. Możesz pozostawić go pusty.')
        checksum = sum(int(ch) * (3 if i % 2 == 0 else 1) for i, ch in enumerate(reversed(ean[:-1])))
        if (checksum + int(ean[-1])) % 10:
            raise ValueError('Nieprawidłowa cyfra kontrolna EAN/GTIN.')
    net = money(form.get('net_price', ''))
    gross = money(form.get('gross_price') or net * Decimal('1.23'))
    eur = (net / Decimal('4.30') * Decimal('1.45')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    # UVP remains independent of the agreed PLN -> B2B EUR calculation.
    uvp = money(form.get('uvp_eur') or eur * Decimal('1.45'))
    data.update(net_price=str(net), gross_price=str(gross), price_eur=str(eur), uvp_eur=str(uvp))
    return data


def local_create(db, request_id, data, stamp):
    """Offline installations only. The whole product is committed once."""
    db.execute('BEGIN IMMEDIATE')
    db.execute('CREATE TABLE IF NOT EXISTS product_creation_receipts '
               '(request_id TEXT PRIMARY KEY, payload TEXT NOT NULL, result TEXT NOT NULL)')
    payload = json.dumps(data, sort_keys=True)
    old = db.execute('SELECT payload,result FROM product_creation_receipts WHERE request_id=?', (request_id,)).fetchone()
    if old:
        if old['payload'] != payload:
            raise ValueError('Ten formularz został już zapisany z innymi danymi. Otwórz nowy formularz.')
        return json.loads(old['result'])
    if db.execute('SELECT 1 FROM products WHERE lower(trim(sku))=lower(?)', (data['sku'],)).fetchone():
        raise ValueError('Produkt o tym SKU już istnieje (również w archiwum).')
    if data['ean'] and db.execute('SELECT 1 FROM products WHERE ean=?', (data['ean'],)).fetchone():
        raise ValueError('Ten EAN jest już przypisany do produktu.')
    if db.execute('SELECT 1 FROM pricing WHERE lower(trim(model))=lower(?)', (data['sku'],)).fetchone() or db.execute('SELECT 1 FROM pricing_eur WHERE lower(trim(sku))=lower(?)', (data['sku'],)).fetchone():
        raise ValueError('SKU ma już zapisany cennik. Użyj importu katalogu lub innego SKU.')
    model_price = db.execute('SELECT net_price,gross_price FROM pricing WHERE lower(trim(model))=lower(?)', (data['model'],)).fetchone()
    if model_price and (money(model_price['net_price']) != money(data['net_price']) or money(model_price['gross_price']) != money(data['gross_price'])):
        raise ValueError('Ten model ma wspólny cennik z inną ceną. Podaj osobny model wariantu lub cenę zgodną z cennikiem.')
    pid = db.execute('INSERT INTO products(sku,model,ean,name,archived,created_at) VALUES(?,?,?,?,0,?)',
                     (data['sku'], data['model'], data['ean'], data['name'], stamp)).lastrowid
    db.execute('INSERT INTO stock(product_id,qty) VALUES(?,0)', (pid,))
    db.execute('INSERT INTO pricing(model,net_price,gross_price,created_at) VALUES(?,?,?,?)',
               (data['sku'], data['net_price'], data['gross_price'], stamp))
    db.execute('INSERT INTO pricing_eur(sku,ean,price_eur,uvp_eur,created_at,updated_at) VALUES(?,?,?,?,?,?)',
               (data['sku'], data['ean'], data['price_eur'], data['uvp_eur'], stamp, stamp))
    if data.get('image'):
        image = data['image']
        db.execute('INSERT OR IGNORE INTO product_images(stored_path,filename,created_at) VALUES(?,?,?)',
                   (image['stored_path'], image['filename'], stamp))
        iid = db.execute('SELECT id FROM product_images WHERE stored_path=?', (image['stored_path'],)).fetchone()['id']
        db.execute('INSERT INTO product_image_assignments(product_id,image_id,created_at) VALUES(?,?,?)', (pid, iid, stamp))
    result = {'product_id': pid, 'sku': data['sku']}
    db.execute('INSERT INTO product_creation_receipts VALUES(?,?,?)', (request_id, payload, json.dumps(result)))
    db.commit()
    return result


def cache_receipt(db, result):
    """Insert only: replay must never reset stock or overwrite later price edits."""
    db.execute('BEGIN IMMEDIATE')
    for table in ('products', 'stock', 'pricing', 'pricing_eur', 'product_images', 'product_image_assignments'):
        row = result.get(table)
        if not row:
            continue
        columns = [r[1] for r in db.execute('PRAGMA table_info(' + table + ')')]
        keys = [key for key in columns if key in row]
        db.execute('INSERT OR IGNORE INTO ' + table + '(' + ','.join(keys) + ') VALUES(' + ','.join('?' for _ in keys) + ')', [row[key] for key in keys])
    db.commit()
