"""FA(3) generator for WDT and export invoices.

This module is intentionally pure: it builds/validates XML and never touches
the database, stock, allocations, order status or delivery status.
"""

from decimal import Decimal, ROUND_HALF_UP
from xml.dom import minidom
from xml.etree.ElementTree import Element, SubElement, tostring
import re

from invoice_types import require_invoice_type
from ksef_module import (
    _add, _buyer_address, _company_address, _date, _dec, _invoice_payment_type,
    _limit, _money, _name_for_item, _nip, _now_utc, _payment_code,
    _payment_due_date, _pln_bank_account, _price, _qty, _swift, _tag, _text,
    MONEY_Q, PRICE_Q,
)


EU_CODES = frozenset({
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE",
    "EL", "GR", "HU", "IE", "IT", "LV", "LT", "LU", "MT", "NL", "PT",
    "RO", "SK", "SI", "ES", "SE",
})


def _foreign_tax_id(value):
    return re.sub(r"[\s.\-]+", "", _text(value).upper())


def validate(invoice, company, items):
    problems = []
    try:
        invoice_type = require_invoice_type(invoice.get("invoice_type"))
    except ValueError as exc:
        return [str(exc)]
    if invoice_type not in {"wdt", "export"}:
        problems.append("Nieobsługiwany typ faktury zagranicznej.")
    country = _text(invoice.get("buyer_country")).upper()
    tax_id = _foreign_tax_id(invoice.get("buyer_tax_no"))
    currency = _text(invoice.get("currency")).upper()
    if not _text(invoice.get("invoice_no")):
        problems.append("Brak numeru faktury.")
    if not _date(invoice.get("issue_date")) or not _date(invoice.get("sell_date")):
        problems.append("Brak daty wystawienia lub sprzedaży.")
    if not _text(company.get("company_name")) or len(_nip(company.get("nip"))) != 10:
        problems.append("Brak poprawnych danych sprzedawcy.")
    if not _text(invoice.get("buyer_name")) or not country:
        problems.append("Brak nazwy lub kraju nabywcy.")
    if not currency or currency == "PLN":
        problems.append("Faktura zagraniczna wymaga jawnej waluty obcej.")
    if invoice_type == "wdt":
        prefix = tax_id[:2]
        if country == "PL" or country not in EU_CODES:
            problems.append("WDT wymaga kraju UE innego niż PL.")
        if len(tax_id) < 8 or prefix not in EU_CODES:
            problems.append("WDT wymaga pełnego numeru VAT UE z prefiksem kraju.")
        elif prefix != ("EL" if country == "GR" else country):
            problems.append("Prefiks VAT UE nie odpowiada krajowi nabywcy.")
    elif invoice_type == "export":
        if country in EU_CODES or country == "PL":
            problems.append("Eksport wymaga kraju spoza UE.")
        if not tax_id:
            problems.append("Eksport wymaga zagranicznego Tax ID.")
    if not items:
        problems.append("Brak pozycji faktury.")
    for idx, item in enumerate(items, 1):
        if _dec(item.get("qty")) <= 0:
            problems.append(f"Pozycja {idx}: ilość musi być większa od 0.")
        if _dec(item.get("net_price")) < 0:
            problems.append(f"Pozycja {idx}: cena netto nie może być ujemna.")
    return problems


def generate(invoice, company, items):
    invoice_type = require_invoice_type(invoice.get("invoice_type"))
    if invoice_type not in {"wdt", "export"}:
        raise ValueError("Nieobsługiwany typ faktury zagranicznej")
    problems = validate(invoice, company, items)
    if problems:
        raise ValueError("; ".join(problems))

    root = Element(_tag("Faktura"))
    header = SubElement(root, _tag("Naglowek"))
    _add(header, "KodFormularza", "FA", attrs={"kodSystemowy": "FA (3)", "wersjaSchemy": "1-0E"})
    _add(header, "WariantFormularza", "3")
    _add(header, "DataWytworzeniaFa", _now_utc())
    _add(header, "SystemInfo", "Niedzwieccy Orders")

    seller = SubElement(root, _tag("Podmiot1"))
    seller_id = SubElement(seller, _tag("DaneIdentyfikacyjne"))
    _add(seller_id, "NIP", _nip(company.get("nip")))
    _add(seller_id, "Nazwa", _limit(company.get("company_name"), 512))
    addr1, addr2 = _company_address(company)
    seller_addr = SubElement(seller, _tag("Adres"))
    _add(seller_addr, "KodKraju", "PL")
    _add(seller_addr, "AdresL1", addr1)
    if addr2:
        _add(seller_addr, "AdresL2", addr2)

    country = _text(invoice.get("buyer_country")).upper()
    tax_id = _foreign_tax_id(invoice.get("buyer_tax_no"))
    buyer = SubElement(root, _tag("Podmiot2"))
    buyer_id = SubElement(buyer, _tag("DaneIdentyfikacyjne"))
    if invoice_type == "wdt":
        _add(buyer_id, "KodUE", tax_id[:2])
        _add(buyer_id, "NrVatUE", tax_id[2:])
    else:
        _add(buyer_id, "KodKraju", country)
        _add(buyer_id, "NrID", tax_id)
    _add(buyer_id, "Nazwa", _limit(invoice.get("buyer_name"), 512))
    buyer_addr1, buyer_addr2 = _buyer_address(invoice)
    buyer_addr = SubElement(buyer, _tag("Adres"))
    _add(buyer_addr, "KodKraju", country)
    _add(buyer_addr, "AdresL1", buyer_addr1)
    if buyer_addr2:
        _add(buyer_addr, "AdresL2", buyer_addr2)
    _add(buyer, "JST", "2")
    _add(buyer, "GV", "2")

    fa = SubElement(root, _tag("Fa"))
    _add(fa, "KodWaluty", _text(invoice.get("currency")).upper())
    _add(fa, "P_1", _date(invoice.get("issue_date")))
    _add(fa, "P_1M", _limit(invoice.get("place") or company.get("city") or "Kotusów", 256))
    _add(fa, "P_2", _limit(invoice.get("invoice_no"), 256))
    _add(fa, "P_6", _date(invoice.get("sell_date")))

    rows = []
    total_net = Decimal("0.00")
    for item in items:
        qty = _dec(item.get("qty"))
        unit_net = _dec(item.get("net_price")).quantize(PRICE_Q, rounding=ROUND_HALF_UP)
        line_net = _dec(item.get("line_value_net"), str(unit_net * qty)).quantize(MONEY_Q, rounding=ROUND_HALF_UP)
        rows.append((item, qty, unit_net, line_net))
        total_net += line_net
    total_net = total_net.quantize(MONEY_Q, rounding=ROUND_HALF_UP)
    _add(fa, "P_13_6_2" if invoice_type == "wdt" else "P_13_6_3", _money(total_net))
    _add(fa, "P_15", _money(total_net))

    annotations = SubElement(fa, _tag("Adnotacje"))
    for tag in ("P_16", "P_17", "P_18", "P_18A"):
        _add(annotations, tag, "2")
    exempt = SubElement(annotations, _tag("Zwolnienie")); _add(exempt, "P_19N", "1")
    transport = SubElement(annotations, _tag("NoweSrodkiTransportu")); _add(transport, "P_22N", "1")
    _add(annotations, "P_23", "2")
    margin = SubElement(annotations, _tag("PMarzy")); _add(margin, "P_PMarzyN", "1")
    _add(fa, "RodzajFaktury", "VAT")

    vat_code = "0 WDT" if invoice_type == "wdt" else "0 EX"
    for idx, (item, qty, unit_net, line_net) in enumerate(rows, 1):
        row = SubElement(fa, _tag("FaWiersz"))
        _add(row, "NrWierszaFa", idx)
        _add(row, "P_7", _name_for_item(item))
        _add(row, "P_8A", "szt.")
        _add(row, "P_8B", _qty(qty))
        _add(row, "P_9A", _price(unit_net))
        _add(row, "P_11", _money(line_net))
        _add(row, "P_12", vat_code)

    if not int(invoice.get("paid") or 0):
        payment = SubElement(fa, _tag("Platnosc"))
        due = _payment_due_date(invoice)
        if due:
            due_node = SubElement(payment, _tag("TerminPlatnosci")); _add(due_node, "Termin", due)
        payment_code = _payment_code(_invoice_payment_type(invoice))
        _add(payment, "FormaPlatnosci", payment_code)
        account = _pln_bank_account(company.get("bank_account"))
        if payment_code == "6" and account:
            bank = SubElement(payment, _tag("RachunekBankowy")); _add(bank, "NrRB", account)
            swift = _swift(company.get("bank_swift"))
            if swift:
                _add(bank, "SWIFT", swift)

    rough = tostring(root, encoding="utf-8")
    return minidom.parseString(rough).toprettyxml(indent="  ", encoding="utf-8").decode("utf-8")

