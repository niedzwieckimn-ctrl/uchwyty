"""Edit catalogue identity in place, preserving prices and product references."""
FIELDS = ('sku', 'model', 'ean', 'name')


def snapshot(row):
    return {field: row[field] or '' for field in FIELDS}


class EditConflict(ValueError):
    pass


def local_product(db, product_id):
    return db.execute('SELECT * FROM products WHERE id=? AND COALESCE(archived,0)=0',
                      (product_id,)).fetchone()


def _price(db, table, key, value):
    rows = db.execute(f'SELECT * FROM {table} WHERE lower(trim({key}))=lower(trim(?))',
                      (value,)).fetchall()
    if len(rows) > 1:
        raise EditConflict('Cennik ma kilka wpisów dla tego kodu. Sprawdź cennik.')
    return rows[0] if rows else None


def _amounts(row):
    return (row['net_price'], row['gross_price']) if row else None


def _update_prices(db, product_id, old, new):
    old_sku, new_sku = old['sku'], new['sku']
    sku_changed = old_sku != new_sku
    rekey = old_sku.strip().lower() != new_sku.strip().lower()
    if sku_changed or old['model'] != new['model']:
        old_rate = _price(db, 'pricing', 'model', old_sku)
        rate = _price(db, 'pricing', 'model', old['model']) or old_rate
        model_rate = _price(db, 'pricing', 'model', new['model'])
        dest = _price(db, 'pricing', 'model', new_sku)
        if ((rekey and dest) or (model_rate and _amounts(model_rate) != _amounts(rate)) or
                (not model_rate and dest and _amounts(dest) != _amounts(rate))):
            raise EditConflict('Nowy kod lub model koliduje z cennikiem PLN.')
        # A SKU price can also be the shared model price for other handles.
        if rate and not model_rate and not dest:
            if db.execute('SELECT 1 FROM products WHERE id<>? AND lower(trim(model))=lower(?)',
                          (product_id, new_sku)).fetchone():
                raise EditConflict('Ten kod jest modelem innych produktów. Wybierz osobny SKU.')
            db.execute('INSERT INTO pricing(model,net_price,gross_price,created_at) VALUES(?,?,?,?)',
                       (new_sku, rate['net_price'], rate['gross_price'], rate['created_at']))
        if rekey and old_rate and old_sku.strip().lower() != new['model'].strip().lower():
            shared = db.execute('SELECT 1 FROM products WHERE id<>? AND '
                                '(lower(trim(model))=lower(trim(?)) OR lower(trim(sku))=lower(trim(?)))',
                                (product_id, old_sku, old_sku)).fetchone()
            if not shared:
                db.execute('DELETE FROM pricing WHERE model=?', (old_rate['model'],))
    if sku_changed or old['ean'] != new['ean']:
        euro = _price(db, 'pricing_eur', 'sku', old_sku)
        if rekey and _price(db, 'pricing_eur', 'sku', new_sku):
            raise EditConflict('Nowy SKU ma już cennik EUR.')
        if euro:
            db.execute('UPDATE pricing_eur SET sku=?,ean=? WHERE sku=?',
                       (new_sku, new['ean'], euro['sku']))


def local_save(db, product_id, expected, new):
    with db:
        db.execute('BEGIN IMMEDIATE')
        row = local_product(db, product_id)
        if row is None:
            raise EditConflict('Produkt usunięto lub przeniesiono do archiwum.')
        current = snapshot(row)
        if current == new:
            return False
        if current != expected:
            raise EditConflict('Dane uchwytu zmieniły się. Otwórz aktualne dane przed zapisem.')
        for field in ('sku', 'ean'):
            if current[field] != new[field] and new[field] and db.execute(
                    f'SELECT 1 FROM products WHERE id<>? AND lower(trim({field}))=lower(?)',
                    (product_id, new[field])).fetchone():
                raise EditConflict(f'{field.upper()} jest już przypisany do innego produktu.')
        _update_prices(db, product_id, current, new)
        db.execute('UPDATE products SET sku=?, model=?, ean=?, name=? WHERE id=?',
                   tuple(new[k] if current[k] != new[k] else row[k] for k in FIELDS) + (product_id,))
        return True


def cache_result(db, before, after, result):
    # Only the two affected price keys and the four editable fields are refreshed.
    with db:
        db.execute('BEGIN IMMEDIATE')
        row = result['products']
        if not db.execute('SELECT 1 FROM products WHERE id=?', (row['id'],)).fetchone():
            db.execute('INSERT INTO products(id,sku,model,ean,name,created_at,archived) VALUES(?,?,?,?,?,?,?)',
                       tuple(row[k] for k in ('id', *FIELDS, 'created_at', 'archived')))
        else:
            db.execute('UPDATE products SET sku=?,model=?,ean=?,name=? WHERE id=?',
                       tuple(row[k] for k in FIELDS) + (row['id'],))
        for table, key in [('pricing','model'), ('pricing_eur','sku')]:
            columns = [col[1] for col in db.execute(f'PRAGMA table_info({table})')]
            db.execute(f'DELETE FROM {table} WHERE lower(trim({key})) IN (lower(trim(?)),lower(trim(?)))',
                       (before['sku'], after['sku']))
            for price in result[table]:
                keys = [k for k in columns if k in price]
                placeholders = ','.join('?' for _ in keys)
                db.execute(f"INSERT INTO {table} ({','.join(keys)}) VALUES ({placeholders})",
                           [price[k] for k in keys])

