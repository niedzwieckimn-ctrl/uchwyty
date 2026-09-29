"""Paged invoice metadata for the admin list; no PDF or snapshot downloads."""
import re
from datetime import timedelta


def _group_key(name, tax_no):
    tax_no = re.sub(r'\D', '', str(tax_no or '').strip())
    return ('nip:' + tax_no if tax_no else
            'name:' + re.sub(r'[\W_]+', '', (name or '').casefold(), flags=re.UNICODE))


def read_invoice_list(db, args, today, *, norm, normalize_currency, resolve_type, to_int):
    owns_snapshot = not db.in_transaction
    if owns_snapshot:
        db.execute('BEGIN')
    try:
        return _read_invoice_list(db, args, today, norm=norm, normalize_currency=normalize_currency,
                                  resolve_type=resolve_type, to_int=to_int)
    finally:
        if owns_snapshot:
            db.rollback()  # End our read snapshot, never the caller's transaction.


def _read_invoice_list(db, args, today, *, norm, normalize_currency, resolve_type, to_int):
    # Preserve Python's Unicode search and the existing document-type rules.
    db.create_function('inv_norm', 1, norm, deterministic=True)
    db.create_function('inv_fold', 1, lambda value: norm(value).casefold(), deterministic=True)
    db.create_function('inv_currency', 1, normalize_currency, deterministic=True)
    db.create_function('inv_type', 2, lambda value, currency: resolve_type(
        {'invoice_type': value, 'currency': normalize_currency(currency)}), deterministic=True)
    db.create_function('inv_group', 2, _group_key, deterministic=True)
    cte = '''WITH metadata AS (
        SELECT i.id, i.invoice_no, i.issue_date, i.payment_to, i.buyer_name,
               i.buyer_tax_no, i.total_net, i.total_gross,
               inv_currency(i.currency) AS currency,
               inv_type(i.invoice_type,i.currency) AS document_type,
               COALESCE(NULLIF(i.buyer_name,''),NULLIF(o.customer_name,''),'Bez klienta') AS customer_display,
               inv_norm(COALESCE(NULLIF(i.publication_state,''),'complete'))='complete' AS publication_complete,
               COALESCE(m.paid,0) AS paid, COALESCE(m.sent_to_client,0) AS sent_to_client,
               COALESCE(m.seen_by_client,0) AS seen_by_client,
               COALESCE(m.paid_at,'') AS paid_at, COALESCE(m.seen_at,'') AS seen_at,
               CASE WHEN COALESCE(m.pdf_path,'')<>'' OR COALESCE(m.invoice_items_json,'')<>'' THEN 1 ELSE 0 END AS pdf_ok,
               COALESCE(k.status,'draft') AS ksef_status,
               COALESCE(k.ksef_number,'') AS ksef_number,
               COALESCE(k.last_error,'') AS ksef_error, COALESCE(k.sent_at,'') AS ksef_sent_at,
               o.id AS source_order_id, o.order_no AS source_order_no,
               o.created_at AS source_order_created_at, o.note AS source_order_note,
               o.customer_name AS order_customer_name
        FROM invoices i
        LEFT JOIN invoice_meta m ON m.invoice_id=i.id
        LEFT JOIN ksef_documents k ON k.invoice_id=i.id
        LEFT JOIN orders o ON o.id=i.order_id
    ), entries AS (
        SELECT *, CASE WHEN NOT publication_complete THEN 'publication_incomplete'
                       WHEN paid THEN 'paid'
                       WHEN substr(inv_norm(payment_to),1,10)<>'' AND substr(inv_norm(payment_to),1,10)<:today
                       THEN 'overdue' ELSE 'unpaid' END AS payment_status
        FROM metadata
    ) '''
    params = {'today': today.isoformat()}
    summary = dict(db.execute(cte + '''SELECT COUNT(*) AS 'all',
        COALESCE(SUM(publication_complete AND NOT paid),0) AS unpaid,
        COALESCE(SUM(payment_status='overdue'),0) AS overdue,
        COALESCE(SUM(publication_complete AND paid),0) AS paid,
        COALESCE(SUM(ksef_status='sent'),0) AS ksef,
        COALESCE(SUM(NOT sent_to_client),0) AS unsent FROM entries''', params).fetchone())
    params['month_now'] = today.isoformat()[:7]
    month_totals = [dict(row) for row in db.execute(cte + '''SELECT currency,
        SUM(total_net) AS net,SUM(total_gross) AS gross FROM entries
        WHERE publication_complete AND substr(inv_norm(issue_date),1,7)=:month_now GROUP BY currency''', params)]
    customers = sorted((r[0] for r in db.execute(cte + 'SELECT DISTINCT customer_display FROM entries', params)), key=str.casefold)
    months = [r[0] for r in db.execute(cte + '''SELECT DISTINCT substr(inv_norm(issue_date),1,7) AS month
        FROM entries WHERE month<>'' ORDER BY month DESC''', params)]
    currencies = [r[0] for r in db.execute(cte + 'SELECT DISTINCT currency FROM entries ORDER BY currency', params)]
    conditions = []
    query = norm(args.get('q')).casefold()
    if query:
        params['q'] = query
        conditions.append('(' + ' OR '.join(f'instr(inv_fold({field}),:q)>0' for field in
                          ('invoice_no','customer_display','source_order_no','source_order_note')) + ')')
    for arg, field in (('customer','customer_display'),('month','substr(inv_norm(issue_date),1,7)'),
                       ('document_type','document_type'),('currency','currency')):
        value = norm(args.get(arg))
        if value:
            params[arg] = value.upper() if arg == 'currency' else value
            conditions.append(f'{field}=:{arg}')
    payment = norm(args.get('payment'))
    if payment:
        params['payment'] = payment
        conditions.append("((:payment='open' AND publication_complete AND NOT paid) OR payment_status=:payment)")
    ksef = norm(args.get('ksef'))
    if ksef:
        params['ksef'] = ksef
        conditions.append("((:ksef='none' AND ksef_status NOT IN ('sent','ready','error')) OR ksef_status=:ksef)")
    sent = norm(args.get('sent'))
    if sent:
        params['sent'] = sent
        conditions.append("((:sent='sent' AND sent_to_client) OR (:sent='unsent' AND NOT sent_to_client))")
    if norm(args.get('period')) == '30d':
        params['cutoff'] = (today - timedelta(days=30)).isoformat()
        conditions.append('substr(inv_norm(issue_date),1,10)>=:cutoff')
    where = (' WHERE ' + ' AND '.join(conditions)) if conditions else ''
    total = db.execute(cte + 'SELECT COUNT(*) FROM entries' + where, params).fetchone()[0]
    pages = max(1, (total + 49) // 50)
    page = min(max(1, to_int(args.get('page'), 1)), pages)
    params['offset'] = (page - 1) * 50
    rows = [dict(row) for row in db.execute(cte + 'SELECT * FROM entries' + where +
             ' ORDER BY inv_norm(issue_date) DESC,id DESC LIMIT 50 OFFSET :offset', params)]
    group_totals = {}
    if norm(args.get('view')) == 'customers':
        for row in db.execute(cte + '''SELECT inv_group(customer_display,buyer_tax_no) AS group_key,
                currency, COUNT(*) AS count,
                SUM(CASE WHEN publication_complete THEN total_net ELSE 0 END) AS total_net,
                SUM(CASE WHEN publication_complete THEN total_gross ELSE 0 END) AS total_gross
                FROM entries''' + where + ' GROUP BY group_key,currency', params):
            group_totals.setdefault(row['group_key'], []).append(dict(row))
    for row in rows:
        row['document_type_label'] = {'domestic':'KRAJOWA','wdt':'WDT','export':'EKSPORT'}[row['document_type']]
        row['payment_status_label'] = {'publication_incomplete':'Dokument niedokończony',
            'paid':'Zapłacona','overdue':'Po terminie','unpaid':'Nieopłacona'}[row['payment_status']]
        row['group_key'] = _group_key(row['customer_display'], row['buyer_tax_no'])
    return dict(rows=rows, summary=summary, month_totals=month_totals, customers=customers,
                months=months, currencies=currencies, total_filtered=total, page=page,
                page_count=pages, group_totals=group_totals)
