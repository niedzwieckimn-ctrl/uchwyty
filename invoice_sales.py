"""One invoice source for Cash Flow, sales reports and inventory counts.

Dates are inclusive issue dates. A stock count must always supply both bounds.
The invoice snapshot is authoritative; allocations are a fallback for old rows.
"""
import json
from collections import defaultdict
from datetime import date


def invoice_rows(db, start_date=None, end_date=None):
    if (start_date is None) != (end_date is None):
        raise ValueError('Podaj obie granice okresu faktur')
    if start_date is not None:
        start_date = date.fromisoformat(str(start_date)).isoformat()
        end_date = date.fromisoformat(str(end_date)).isoformat()
        if start_date > end_date:
            raise ValueError('Początek okresu jest po końcu')
    where = "WHERE COALESCE(i.publication_state,'complete')='complete'"
    if start_date:
        where += ' AND substr(trim(i.issue_date),1,10) BETWEEN ? AND ?'
    return db.execute(f'''SELECT i.*, COALESCE(m.paid,0) AS paid, m.paid_at,
            COALESCE(m.payment_reminder,0) AS payment_reminder,
            m.invoice_items_json, COALESCE(o.currency,'PLN') AS order_currency,
            COALESCE(NULLIF(TRIM(i.buyer_name),''),o.customer_name,'-') AS sales_customer
        FROM invoices i LEFT JOIN invoice_meta m ON m.invoice_id=i.id
        LEFT JOIN orders o ON o.id=i.order_id {where}
        ORDER BY COALESCE(i.payment_to,i.issue_date) ASC,i.id DESC''',
        (start_date, end_date) if start_date else ()).fetchall()


def invoice_lines(db, invoice, *, product_ids=None, resolve_skus=True):
    """Return (product_id, sku, qty) lines and an explicit unresolved list.

    Never combine snapshot and allocations: that would count a single invoice twice.
    Preserve invoice-only lines for financial/whole-invoice unit reporting; an
    unmapped SKU is an error for a physical stock count, not a silent omission.
    """
    try:
        parsed = json.loads(invoice['invoice_items_json'] or '[]')
    except (ValueError, TypeError):
        parsed = []
    if not isinstance(parsed, list):
        parsed = []
    lines, unresolved = [], []
    if resolve_skus and product_ids is None:
        product_ids = {str(row['sku']).casefold(): int(row['id']) for row in
                       db.execute('SELECT id,sku FROM products')}
    product_ids = product_ids or {}
    if parsed:
        for index, item in enumerate(parsed, 1):
            if not isinstance(item, dict):
                unresolved.append((invoice['id'], index, 'invalid_line'))
                continue
            raw_qty = item.get('qty')
            if raw_qty is None:
                raw_qty = item.get('invoice_qty')
            if raw_qty is None:
                raw_qty = item.get('current_invoice_qty')
            try:
                qty = int(raw_qty)
                if qty < 0 or str(raw_qty).strip() not in {str(qty), str(float(qty))}:
                    raise ValueError()
            except (ValueError, TypeError):
                unresolved.append((invoice['id'], index, 'invalid_quantity'))
                continue
            sku = str(item.get('sku') or '').strip()
            product_id = product_ids.get(sku.casefold())
            if resolve_skus and sku and not product_id:
                unresolved.append((invoice['id'], index, 'unknown_sku'))
            elif resolve_skus and not sku:
                unresolved.append((invoice['id'], index, 'missing_sku'))
            lines.append((product_id, sku, qty))
        return lines, unresolved
    for allocation in db.execute('''SELECT product_id,sku,qty FROM invoice_allocations
                                    WHERE invoice_id=? ORDER BY id''', (invoice['id'],)):
        sku = str(allocation['sku'] or '').strip()
        product_id = product_ids.get(sku.casefold()) if sku else None
        if resolve_skus and not product_id:
            unresolved.append((invoice['id'], len(lines)+1, 'unknown_sku'))
        lines.append((product_id, sku, int(allocation['qty'])))
    if not lines:
        unresolved.append((invoice['id'], 0, 'missing_lines'))
    return lines, unresolved


def sales_by_sku(db, start_date, end_date):
    result = defaultdict(int)
    unresolved = []
    dates = defaultdict(set)
    invoices = invoice_rows(db, start_date, end_date)
    product_ids = {str(row['sku']).casefold(): int(row['id']) for row in
                   db.execute('SELECT id,sku FROM products')}
    for invoice in invoices:
        # A staged invoice may have provisional metadata; it is not published.
        if 'publication_state' in invoice.keys() and invoice['publication_state'] != 'complete':
            continue
        lines, problems = invoice_lines(db, invoice, product_ids=product_ids)
        unresolved.extend(problems)
        for product_id, _sku, qty in lines:
            if product_id is not None:
                result[product_id] += qty
                dates[product_id].add(str(invoice['issue_date'])[:10])
    return dict(result), unresolved, {key: sorted(value) for key, value in dates.items()}
