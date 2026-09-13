"""Monotonic invoice numbers, shared by the existing administrative issuer."""
import re
from datetime import datetime


def initialize(c):
    c.execute('CREATE TABLE IF NOT EXISTS invoice_number_counters(period TEXT PRIMARY KEY, last_number INTEGER NOT NULL)')
    c.execute('CREATE TABLE IF NOT EXISTS invoice_number_claims(invoice_no TEXT PRIMARY KEY, created_at TEXT NOT NULL)')
    c.execute("INSERT OR IGNORE INTO invoice_number_claims SELECT invoice_no,COALESCE(created_at,'') FROM invoices WHERE invoice_no IS NOT NULL")
    c.execute("""CREATE TRIGGER IF NOT EXISTS retain_invoice_number AFTER INSERT ON invoices BEGIN
        INSERT OR IGNORE INTO invoice_number_claims VALUES(NEW.invoice_no,COALESCE(NEW.created_at,'')); END""")


def _period(issue_date):
    return datetime.strptime(issue_date, '%Y-%m-%d').strftime('%m/%Y')


def _highest(c, period):
    row = c.execute('SELECT last_number FROM invoice_number_counters WHERE period=?', (period,)).fetchone()
    highest = row[0] if row else 0
    for row in c.execute('SELECT invoice_no FROM invoices UNION SELECT invoice_no FROM invoice_number_claims'):
        match = re.fullmatch(r'FVAT (\d+)/' + re.escape(period), str(row[0]).strip(), re.I)
        if match:
            highest = max(highest, int(match[1]))
    return highest


def preview(b, issue_date):
    c = b.conn()
    try:
        return f'FVAT {_highest(c, _period(issue_date)) + 1}/{_period(issue_date)}'
    finally:
        c.close()


def reserve(b, issue_date, requested=''):
    period = _period(issue_date)
    # The standard series is allocated at POST, never from a stale GET value.
    custom = requested.strip() if requested and not re.fullmatch(r'FVAT \d+/\d{2}/\d{4}', requested.strip(), re.I) else ''
    selected = re.fullmatch(r'FVAT (\d+)/' + re.escape(period), requested.strip(), re.I) if requested else None
    requested_min = int(selected[1]) if selected else 0
    if b.supabase_enabled():
        result = b.supabase_request('/rest/v1/rpc/reserve_invoice_number', method='POST',
                                    payload={'p_period': period, 'p_custom': custom, 'p_requested_min': requested_min})
        if not isinstance(result, str) or not result:
            raise ValueError('Nie potwierdzono rezerwacji numeru faktury. Sprawdź migrację numeracji.')
        number = result
    else:
        number = None
    c = b.conn()
    try:
        c.execute('BEGIN IMMEDIATE')
        if number is None:
            number = custom or f'FVAT {max(_highest(c, period) + 1, requested_min)}/{period}'
        if c.execute('SELECT 1 FROM invoices WHERE lower(trim(invoice_no))=lower(?) UNION ALL SELECT 1 FROM invoice_number_claims WHERE lower(trim(invoice_no))=lower(?)', (number, number)).fetchone():
            raise ValueError('Numer faktury został już wykorzystany.')
        c.execute('INSERT INTO invoice_number_claims VALUES(?,?)', (number, b.now_iso()))
        match = re.fullmatch(r'FVAT (\d+)/' + re.escape(period), number, re.I)
        if match:
            c.execute('INSERT INTO invoice_number_counters VALUES(?,?) ON CONFLICT(period) DO UPDATE SET last_number=MAX(last_number,excluded.last_number)', (period, int(match[1])))
        c.commit()
        return number
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()
