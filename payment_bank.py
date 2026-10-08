"""One bank-selection rule for foreign invoices and EUR proformas."""
import os
import re

from invoice_types import resolve_invoice_type


class BankConfigurationError(ValueError):
    pass


def foreign_bank():
    iban = re.sub(r'\s+', '', os.environ.get('FOREIGN_INVOICE_IBAN', '')).upper()
    bic = re.sub(r'\s+', '', os.environ.get('FOREIGN_INVOICE_BIC', '')).upper()
    name = os.environ.get('FOREIGN_INVOICE_BANK', '').strip()
    valid = bool(re.fullmatch(r'[A-Z]{2}\d{2}[A-Z0-9]{11,30}', iban))
    if valid:
        digits = ''.join(str(ord(c) - 55) if c.isalpha() else c for c in iban[4:] + iban[:4])
        valid = int(digits) % 97 == 1
    if not valid or not re.fullmatch(r'[A-Z]{6}[A-Z0-9]{2}([A-Z0-9]{3})?', bic) or not name:
        raise BankConfigurationError(
            'Brak prawidłowego konta do faktur zagranicznych i proform EUR. '
            'Uzupełnij IBAN, BIC i nazwę banku przed wystawieniem dokumentu.')
    return dict(iban=iban, bic=bic, bank_name=name, foreign=True)


def for_invoice(company, invoice, items=None):
    if resolve_invoice_type(invoice, items) in {'wdt', 'export'}:
        return foreign_bank()
    company = dict(company or {})
    return dict(iban=company.get('bank_account') or '', bic=company.get('bank_swift') or '',
                bank_name='', foreign=False)


def display_iban(value):
    value = str(value or '').replace(' ', '')
    return ' '.join(value[i:i + 4] for i in range(0, len(value), 4))
