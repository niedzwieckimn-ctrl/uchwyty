"""Invoice numbering with an explicit, user-controlled cursor per period."""
import re
import urllib.error
from datetime import datetime


_STANDARD_NUMBER = r"FVAT (\d+)/{}"


def initialize(c):
    c.execute('CREATE TABLE IF NOT EXISTS invoice_number_counters(period TEXT PRIMARY KEY, last_number INTEGER NOT NULL)')
    c.execute('CREATE TABLE IF NOT EXISTS invoice_number_claims(invoice_no TEXT PRIMARY KEY, created_at TEXT NOT NULL)')

    # Convert the former permanent-history model once. Its claims included every
    # deleted draft and its counters could only grow.
    legacy = c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name IN "
        "('retain_invoice_number','retain_updated_invoice_number') LIMIT 1"
    ).fetchone()
    c.execute('DROP TRIGGER IF EXISTS retain_invoice_number')
    c.execute('DROP TRIGGER IF EXISTS retain_updated_invoice_number')
    if legacy:
        c.execute('DELETE FROM invoice_number_claims')
        c.execute('DELETE FROM invoice_number_counters')
        invoices = c.execute("SELECT invoice_no FROM invoices WHERE invoice_no IS NOT NULL").fetchall()
        for row in invoices:
            match = re.fullmatch(r'FVAT (\d+)/((?:0[1-9]|1[0-2])/[0-9]{4})', str(row[0]).strip(), re.I)
            if match:
                c.execute(
                    'INSERT INTO invoice_number_counters VALUES(?,?) '
                    'ON CONFLICT(period) DO UPDATE SET last_number=MAX(last_number,excluded.last_number)',
                    (match[2], int(match[1])),
                )

    # Claims protect only reserve -> insert/update. Direct inserts from bootstrap
    # advance the cursor; reserved manual numbers have already set it exactly.
    c.execute('DROP TRIGGER IF EXISTS consume_reserved_invoice_number')
    c.execute('''CREATE TRIGGER consume_reserved_invoice_number AFTER INSERT ON invoices BEGIN
        INSERT INTO invoice_number_counters(period,last_number)
        SELECT substr(NEW.invoice_no,instr(NEW.invoice_no,'/')+1),
               CAST(substr(NEW.invoice_no,6,instr(substr(NEW.invoice_no,6),'/')-1) AS INTEGER)
        WHERE NEW.invoice_no GLOB 'FVAT [0-9]*/[0-1][0-9]/[0-9][0-9][0-9][0-9]'
          AND NOT EXISTS(SELECT 1 FROM invoice_number_claims WHERE lower(trim(invoice_no))=lower(trim(NEW.invoice_no)))
        ON CONFLICT(period) DO UPDATE SET last_number=MAX(last_number,excluded.last_number);
        DELETE FROM invoice_number_claims
         WHERE lower(trim(invoice_no))=lower(trim(NEW.invoice_no))
           AND NOT EXISTS(SELECT 1 FROM ksef_documents k WHERE k.invoice_id=NEW.id
             AND (COALESCE(k.ksef_number,'')<>'' OR COALESCE(k.sent_at,'')<>''
               OR lower(COALESCE(k.status,'')) IN ('sending','processing','unknown','sent','accepted')));
    END''')
    c.execute('DROP TRIGGER IF EXISTS maintain_changed_invoice_number')
    c.execute('''CREATE TRIGGER maintain_changed_invoice_number AFTER UPDATE OF invoice_no ON invoices
    WHEN lower(trim(OLD.invoice_no))<>lower(trim(NEW.invoice_no)) BEGIN
        UPDATE invoice_number_counters
           SET last_number=CAST(substr(OLD.invoice_no,6,instr(substr(OLD.invoice_no,6),'/')-1) AS INTEGER)-1
         WHERE OLD.invoice_no GLOB 'FVAT [0-9]*/[0-1][0-9]/[0-9][0-9][0-9][0-9]'
           AND period=substr(OLD.invoice_no,instr(OLD.invoice_no,'/')+1)
           AND last_number=CAST(substr(OLD.invoice_no,6,instr(substr(OLD.invoice_no,6),'/')-1) AS INTEGER);
        INSERT INTO invoice_number_counters(period,last_number)
        SELECT substr(NEW.invoice_no,instr(NEW.invoice_no,'/')+1),
               CAST(substr(NEW.invoice_no,6,instr(substr(NEW.invoice_no,6),'/')-1) AS INTEGER)
        WHERE NEW.invoice_no GLOB 'FVAT [0-9]*/[0-1][0-9]/[0-9][0-9][0-9][0-9]'
          AND NOT EXISTS(SELECT 1 FROM invoice_number_claims WHERE lower(trim(invoice_no))=lower(trim(NEW.invoice_no)))
        ON CONFLICT(period) DO UPDATE SET last_number=MAX(last_number,excluded.last_number);
        DELETE FROM invoice_number_claims
         WHERE lower(trim(invoice_no))=lower(trim(NEW.invoice_no))
           AND NOT EXISTS(SELECT 1 FROM ksef_documents k WHERE k.invoice_id=NEW.id
             AND (COALESCE(k.ksef_number,'')<>'' OR COALESCE(k.sent_at,'')<>''
               OR lower(COALESCE(k.status,'')) IN ('sending','processing','unknown','sent','accepted')));
    END''')
    c.execute('DROP TRIGGER IF EXISTS release_deleted_draft_number')
    c.execute('''CREATE TRIGGER release_deleted_draft_number AFTER DELETE ON invoices BEGIN
        UPDATE invoice_number_counters
           SET last_number=CAST(substr(OLD.invoice_no,6,instr(substr(OLD.invoice_no,6),'/')-1) AS INTEGER)-1
         WHERE OLD.invoice_no GLOB 'FVAT [0-9]*/[0-1][0-9]/[0-9][0-9][0-9][0-9]'
           AND period=substr(OLD.invoice_no,instr(OLD.invoice_no,'/')+1)
           AND last_number=CAST(substr(OLD.invoice_no,6,instr(substr(OLD.invoice_no,6),'/')-1) AS INTEGER);
    END''')

    # A finalized KSeF document keeps a permanent claim even if its invoice row
    # is later removed outside the protected application flow.
    c.execute('DROP TRIGGER IF EXISTS retain_final_invoice_number_insert')
    c.execute('''CREATE TRIGGER retain_final_invoice_number_insert AFTER INSERT ON ksef_documents
    WHEN COALESCE(NEW.ksef_number,'')<>'' OR COALESCE(NEW.sent_at,'')<>''
      OR lower(COALESCE(NEW.status,'')) IN ('sending','processing','unknown','sent','accepted') BEGIN
        INSERT OR IGNORE INTO invoice_number_claims(invoice_no,created_at)
        SELECT invoice_no,COALESCE(NEW.updated_at,'') FROM invoices WHERE id=NEW.invoice_id;
    END''')
    c.execute('DROP TRIGGER IF EXISTS retain_final_invoice_number_update')
    c.execute('''CREATE TRIGGER retain_final_invoice_number_update AFTER UPDATE ON ksef_documents
    WHEN COALESCE(NEW.ksef_number,'')<>'' OR COALESCE(NEW.sent_at,'')<>''
      OR lower(COALESCE(NEW.status,'')) IN ('sending','processing','unknown','sent','accepted') BEGIN
        INSERT OR IGNORE INTO invoice_number_claims(invoice_no,created_at)
        SELECT invoice_no,COALESCE(NEW.updated_at,'') FROM invoices WHERE id=NEW.invoice_id;
    END''')
    c.execute('''INSERT OR IGNORE INTO invoice_number_claims(invoice_no,created_at)
        SELECT i.invoice_no,COALESCE(k.updated_at,i.created_at,'')
          FROM invoices i JOIN ksef_documents k ON k.invoice_id=i.id
         WHERE COALESCE(k.ksef_number,'')<>'' OR COALESCE(k.sent_at,'')<>''
            OR lower(COALESCE(k.status,'')) IN ('sending','processing','unknown','sent','accepted')''')


def _period(issue_date):
    return datetime.strptime(issue_date, '%Y-%m-%d').strftime('%m/%Y')


def _cursor(c, period):
    row = c.execute('SELECT last_number FROM invoice_number_counters WHERE period=?', (period,)).fetchone()
    return max(0, int(row[0])) if row else 0


def _is_used(c, number):
    return c.execute(
        '''SELECT 1 FROM invoices WHERE lower(trim(invoice_no))=lower(trim(?))
           UNION ALL
           SELECT 1 FROM invoice_number_claims WHERE lower(trim(invoice_no))=lower(trim(?)) LIMIT 1''',
        (number, number),
    ).fetchone() is not None


def _next_available(c, period, start=0):
    candidate = max(_cursor(c, period) + 1, int(start or 0), 1)
    while _is_used(c, f'FVAT {candidate}/{period}'):
        candidate += 1
    return candidate


def preview(b, issue_date):
    period = _period(issue_date)
    c = b.conn()
    try:
        return f'FVAT {_next_available(c, period)}/{period}'
    finally:
        c.close()


def _remote_collision(exc):
    if not isinstance(exc, urllib.error.HTTPError) or exc.code not in (400, 409):
        return False
    body = exc.read().decode('utf-8', errors='replace').lower()
    return any(marker in body for marker in (
        'invoice number was already used', 'invoice_number_claims_pkey', 'duplicate key value',
    ))


def reserve(b, issue_date, requested='', *, manual=False):
    period = _period(issue_date)
    requested = requested.strip() if requested else ''
    if manual and not requested:
        raise ValueError('Numer faktury jest wymagany.')
    custom = requested if manual else ''
    selected = re.fullmatch(_STANDARD_NUMBER.format(re.escape(period)), requested, re.I) if requested and not manual else None
    requested_min = int(selected[1]) if selected else 0
    if b.supabase_enabled():
        try:
            result = b.supabase_request('/rest/v1/rpc/reserve_invoice_number', method='POST',
                                        payload={'p_period': period, 'p_custom': custom, 'p_requested_min': requested_min})
        except urllib.error.HTTPError as exc:
            if _remote_collision(exc):
                raise ValueError('Numer faktury został już wykorzystany.') from None
            raise
        if not isinstance(result, str) or not result:
            raise ValueError('Nie potwierdzono rezerwacji numeru faktury. Sprawdź migrację numeracji.')
        number = result
    else:
        number = None
    c = b.conn()
    try:
        c.execute('BEGIN IMMEDIATE')
        if number is None:
            number = custom or f'FVAT {_next_available(c, period, requested_min)}/{period}'
        if _is_used(c, number):
            raise ValueError('Numer faktury został już wykorzystany.')
        c.execute('INSERT INTO invoice_number_claims VALUES(?,?)', (number, b.now_iso()))
        match = re.fullmatch(_STANDARD_NUMBER.format(re.escape(period)), number, re.I)
        if match:
            c.execute(
                'INSERT INTO invoice_number_counters VALUES(?,?) '
                'ON CONFLICT(period) DO UPDATE SET last_number=excluded.last_number',
                (period, int(match[1])),
            )
        c.commit()
        return number
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def reserve_manual_change(b, current, requested, issue_date):
    requested = requested.strip() if requested else ''
    if not requested:
        raise ValueError('Numer faktury jest wymagany.')
    if requested.casefold() == str(current or '').strip().casefold():
        return requested
    return reserve(b, issue_date, requested, manual=True)
