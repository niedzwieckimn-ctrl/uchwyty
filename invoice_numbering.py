"""Shared invoice numbering for the UI and fulfillment business operation."""
import re
import urllib.error
from datetime import datetime


_STANDARD_NUMBER = r"FVAT (\d+)/{}"
_CLAIM_TTL_MINUTES = 10


def initialize(c):
    """Keep only short-lived race claims; issued invoices are the sequence truth."""
    # The counter table remains for backwards-compatible database startup, but
    # numbering no longer reads or writes it.
    c.execute('CREATE TABLE IF NOT EXISTS invoice_number_counters(period TEXT PRIMARY KEY, last_number INTEGER NOT NULL)')
    c.execute('CREATE TABLE IF NOT EXISTS invoice_number_claims(invoice_no TEXT PRIMARY KEY, created_at TEXT NOT NULL)')

    legacy_permanent_claims = c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name IN "
        "('retain_invoice_number','retain_updated_invoice_number',"
        "'retain_final_invoice_number_insert','retain_final_invoice_number_update') LIMIT 1"
    ).fetchone() is not None

    for trigger in (
        'retain_invoice_number', 'retain_updated_invoice_number',
        'consume_reserved_invoice_number', 'consume_changed_invoice_number',
        'maintain_changed_invoice_number', 'release_deleted_draft_number',
        'retain_final_invoice_number_insert', 'retain_final_invoice_number_update',
    ):
        c.execute(f'DROP TRIGGER IF EXISTS {trigger}')

    # A successful insert/update consumes its transient reservation. The real
    # invoice row then protects the number through invoices.invoice_no UNIQUE.
    c.execute('''CREATE TRIGGER consume_reserved_invoice_number AFTER INSERT ON invoices BEGIN
        DELETE FROM invoice_number_claims
         WHERE lower(trim(invoice_no))=lower(trim(NEW.invoice_no));
    END''')
    c.execute('''CREATE TRIGGER consume_changed_invoice_number AFTER UPDATE OF invoice_no ON invoices
    WHEN lower(trim(OLD.invoice_no))<>lower(trim(NEW.invoice_no)) BEGIN
        DELETE FROM invoice_number_claims
         WHERE lower(trim(invoice_no))=lower(trim(NEW.invoice_no));
    END''')

    # Remove records that cannot be active reservations. Fresh orphan claims are
    # retained briefly because another worker may be between reserve and insert.
    c.execute('DELETE FROM invoice_number_counters')
    if legacy_permanent_claims:
        c.execute('DELETE FROM invoice_number_claims')
    c.execute('''DELETE FROM invoice_number_claims
                  WHERE EXISTS(SELECT 1 FROM invoices
                                WHERE lower(trim(invoices.invoice_no))=lower(trim(invoice_number_claims.invoice_no)))
                     OR datetime(created_at) IS NULL
                     OR datetime(created_at) < datetime('now','localtime',?)''',
              (f'-{_CLAIM_TTL_MINUTES} minutes',))


def _period(issue_date):
    return datetime.strptime(issue_date, '%Y-%m-%d').strftime('%m/%Y')


def _real_invoice_exists(c, number):
    return c.execute(
        'SELECT 1 FROM invoices WHERE lower(trim(invoice_no))=lower(trim(?)) LIMIT 1',
        (number,),
    ).fetchone() is not None


def _last_real_sequence(c, period):
    highest = 0
    rows = c.execute(
        "SELECT invoice_no FROM invoices WHERE lower(trim(invoice_no)) LIKE lower(?)",
        (f'FVAT %/{period}',),
    ).fetchall()
    pattern = re.compile(_STANDARD_NUMBER.format(re.escape(period)), re.I)
    for row in rows:
        match = pattern.fullmatch(str(row[0] or '').strip())
        if match:
            highest = max(highest, int(match[1]))
    return highest


def _next_real_number(c, period, start=0):
    candidate = max(_last_real_sequence(c, period) + 1, int(start or 0), 1)
    while _real_invoice_exists(c, f'FVAT {candidate}/{period}'):
        candidate += 1
    return candidate


def _claim_is_live(c, number):
    return c.execute(
        '''SELECT 1 FROM invoice_number_claims
            WHERE lower(trim(invoice_no))=lower(trim(?))
              AND datetime(created_at) >= datetime('now','localtime',?) LIMIT 1''',
        (number, f'-{_CLAIM_TTL_MINUTES} minutes'),
    ).fetchone() is not None


def _drop_stale_claim(c, number):
    c.execute(
        '''DELETE FROM invoice_number_claims
            WHERE lower(trim(invoice_no))=lower(trim(?))
              AND (datetime(created_at) IS NULL
                   OR datetime(created_at) < datetime('now','localtime',?))''',
        (number, f'-{_CLAIM_TTL_MINUTES} minutes'),
    )


def preview(b, issue_date):
    """Return max(real issued invoice in the period) + 1, ignoring claims."""
    period = _period(issue_date)
    c = b.conn()
    try:
        return f'FVAT {_next_real_number(c, period)}/{period}'
    finally:
        c.close()


def _remote_collision(exc):
    if not isinstance(exc, urllib.error.HTTPError) or exc.code not in (400, 409):
        return False
    body = exc.read().decode('utf-8', errors='replace').lower()
    return any(marker in body for marker in (
        'invoice number was already used', 'invoice number is currently reserved',
        'invoice_number_claims_pkey', 'duplicate key value',
    ))


def _reserve_local(c, b, period, custom):
    if custom:
        number = custom
        if _real_invoice_exists(c, number):
            raise ValueError('Numer faktury został już wykorzystany.')
        _drop_stale_claim(c, number)
        if _claim_is_live(c, number):
            raise ValueError('Numer faktury jest obecnie rezerwowany. Spróbuj ponownie.')
    else:
        candidate = _next_real_number(c, period)
        number = f'FVAT {candidate}/{period}'
        _drop_stale_claim(c, number)
        if _claim_is_live(c, number):
            # Do not skip to a higher number: if the competing request fails,
            # that would turn its technical claim into a permanent sequence gap.
            raise ValueError('Numer faktury jest obecnie rezerwowany. Spróbuj ponownie.')
    c.execute('INSERT INTO invoice_number_claims(invoice_no,created_at) VALUES(?,?)',
              (number, b.now_iso()))
    return number


def reserve(b, issue_date, requested='', *, manual=False):
    period = _period(issue_date)
    requested = requested.strip() if requested else ''
    if manual and not requested:
        raise ValueError('Numer faktury jest wymagany.')
    custom = requested if manual else ''

    if b.supabase_enabled():
        try:
            number = b.supabase_request(
                '/rest/v1/rpc/reserve_invoice_number', method='POST',
                # Keep the deployed RPC signature compatible. Automatic
                # numbering deliberately ignores a stale form suggestion.
                payload={'p_period': period, 'p_custom': custom, 'p_requested_min': 0},
            )
        except urllib.error.HTTPError as exc:
            if _remote_collision(exc):
                raise ValueError('Numer faktury został już wykorzystany lub jest właśnie rezerwowany.') from None
            raise
        if not isinstance(number, str) or not number:
            raise ValueError('Nie potwierdzono rezerwacji numeru faktury. Sprawdź migrację numeracji.')
    else:
        number = None

    c = b.conn()
    try:
        c.execute('BEGIN IMMEDIATE')
        if number is None:
            number = _reserve_local(c, b, period, custom)
        else:
            # Supabase serialized the global reservation. Mirror it locally so
            # the insert trigger can consume it; local history cannot override it.
            if _real_invoice_exists(c, number):
                raise ValueError('Numer faktury został już wykorzystany.')
            c.execute('INSERT OR REPLACE INTO invoice_number_claims(invoice_no,created_at) VALUES(?,?)',
                      (number, b.now_iso()))
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
