# -*- coding: utf-8 -*-
"""
Generator XML KSeF FA(3).

Buduje plik XML w strukturze FA(3) opublikowanej przez Ministerstwo Finansów
w CRD: https://crd.gov.pl/wzor/2025/06/25/13775/
"""

from __future__ import annotations

import os
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from xml.dom import minidom
from xml.etree.ElementTree import Element, SubElement, register_namespace, tostring


FA3_NS = "http://crd.gov.pl/wzor/2025/06/25/13775/"
ETD_NS = "http://crd.gov.pl/xml/schematy/dziedzinowe/mf/2022/01/05/eD/DefinicjeTypy/"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"

register_namespace("", FA3_NS)
register_namespace("etd", ETD_NS)
register_namespace("xsi", XSI_NS)

MONEY_Q = Decimal("0.01")
UNIT_Q = Decimal("0.000001")
PRICE_Q = Decimal("0.00000001")


def _text(value) -> str:
    return str(value or "").strip()


def _limit(value, max_len: int) -> str:
    return _text(value)[:max_len]


def _nip(value) -> str:
    return re.sub(r"\D+", "", _text(value))


def _date(value) -> str:
    raw = _text(value)
    if not raw:
        return ""
    match = re.search(r"\d{4}-\d{2}-\d{2}", raw)
    if match:
        return match.group(0)
    match = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", raw)
    if match:
        day, month, year = match.groups()
        return f"{year}-{month}-{day}"
    return raw[:10]


def _first_nonempty(*values) -> str:
    for value in values:
        txt = _text(value)
        if txt:
            return txt
    return ""


def _now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _dec(value, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value if value not in (None, "") else default).replace(",", "."))
    except Exception:
        return Decimal(default)


def _money(value) -> str:
    return str(_dec(value).quantize(MONEY_Q, rounding=ROUND_HALF_UP))


def _qty(value) -> str:
    q = _dec(value).quantize(UNIT_Q, rounding=ROUND_HALF_UP)
    return format(q.normalize(), "f")


def _price(value) -> str:
    p = _dec(value).quantize(PRICE_Q, rounding=ROUND_HALF_UP)
    return format(p.normalize(), "f")


def _tag(name: str) -> str:
    return f"{{{FA3_NS}}}{name}"


def _add(parent, tag: str, value=None, *, attrs: dict | None = None):
    node = SubElement(parent, _tag(tag), attrs or {})
    node.text = _text(value)
    return node


def _name_for_item(item: dict) -> str:
    name = _text(item.get("name"))
    model = _text(item.get("model"))
    sku = _text(item.get("sku"))
    parts: list[str] = []
    if name and name.lower() not in {model.lower(), sku.lower()}:
        parts.append(name)
    if model and model.lower() != sku.lower():
        parts.append(model)
    if sku:
        parts.append(sku)
    return _limit(" ".join(parts) or name or model or sku or "Towar", 512)


def _sku_for_item(item: dict) -> str:
    return _limit(item.get("sku") or item.get("model") or "", 50)


def _strip_country_prefix(value: str) -> str:
    txt = _text(value)
    txt = re.sub(r"^(PL|POLSKA)\s*[-,]?\s+", "", txt, flags=re.IGNORECASE)
    return txt.strip()


def _place_of_issue(invoice: dict, company: dict) -> str:
    place = _first_nonempty(invoice.get("place"), invoice.get("issue_place"), company.get("city"))
    if place:
        return _limit(_strip_country_prefix(place), 256)

    address = _text(company.get("address"))
    if address:
        before_comma = address.split(",", 1)[0].strip()
        before_postcode = re.split(r"\b\d{2}-\d{3}\b", before_comma)[0].strip()
        candidate = before_postcode or before_comma
        candidate = re.sub(r"\s+\d+\w*(?:/\d+\w*)?$", "", candidate).strip()
        if candidate:
            return _limit(candidate.title(), 256)

    return "Kotuszów"


def _buyer_address(invoice: dict) -> tuple[str, str]:
    street = _text(invoice.get("buyer_street") or invoice.get("buyer_address"))
    post_city = _strip_country_prefix(
        " ".join(
            p for p in [_text(invoice.get("buyer_post_code")), _text(invoice.get("buyer_city"))] if p
        )
    )
    if street:
        return _limit(_strip_country_prefix(street), 512), _limit(post_city, 512)
    return _limit(post_city or "-", 512), ""


def _company_address(company: dict) -> tuple[str, str]:
    address = _text(company.get("address"))
    post_city = " ".join(
        p for p in [_text(company.get("post_code")), _text(company.get("city"))] if p
    )
    if address and post_city and post_city.lower() not in address.lower():
        return _limit(address, 512), _limit(post_city, 512)
    return _limit(address or post_city or "-", 512), ""


def _payment_code(payment_type: str) -> str:
    txt = _text(payment_type).lower()
    if "got" in txt:
        return "1"
    if "kart" in txt:
        return "2"
    if "przelew" in txt or "bank" in txt:
        return "6"
    if "mobil" in txt or "blik" in txt:
        return "7"
    return "6"


def _invoice_payment_type(invoice: dict) -> str:
    return _first_nonempty(
        invoice.get("payment_type"),
        invoice.get("payment_method"),
        invoice.get("payment_form"),
        invoice.get("payment"),
        "przelew",
    )


def _payment_due_date(invoice: dict) -> str:
    return _date(
        _first_nonempty(
            invoice.get("payment_to"),
            invoice.get("due_date"),
            invoice.get("payment_due"),
            invoice.get("payment_due_date"),
            invoice.get("issue_date"),
        )
    )


def validate_ksef_invoice(invoice: dict, company: dict, items: list[dict]) -> list[str]:
    """Podstawowa kontrola danych wymaganych do FA(3)."""
    problems: list[str] = []

    if not _text(invoice.get("invoice_no")):
        problems.append("Brak numeru faktury.")
    if not _date(invoice.get("issue_date")):
        problems.append("Brak daty wystawienia.")
    if not _date(invoice.get("sell_date")):
        problems.append("Brak daty sprzedaży.")

    if not _text(company.get("company_name")):
        problems.append("Brak nazwy sprzedawcy w danych firmy.")
    if len(_nip(company.get("nip"))) != 10:
        problems.append("NIP sprzedawcy musi mieć 10 cyfr.")
    if not _text(company.get("address")) and not _text(company.get("city")):
        problems.append("Brak adresu sprzedawcy w danych firmy.")

    if not _text(invoice.get("buyer_name")):
        problems.append("Brak nazwy nabywcy.")
    buyer_nip = _nip(invoice.get("buyer_tax_no"))
    if buyer_nip and len(buyer_nip) != 10:
        problems.append("NIP nabywcy musi mieć 10 cyfr albo pole powinno być puste.")
    if not _text(invoice.get("buyer_street")) and not _text(invoice.get("buyer_city")):
        problems.append("Brak adresu nabywcy.")

    if not items:
        problems.append("Brak pozycji faktury.")

    for idx, item in enumerate(items, start=1):
        qty = _dec(item.get("qty"))
        if not _name_for_item(item):
            problems.append(f"Pozycja {idx}: brak nazwy/modelu.")
        if qty <= 0:
            problems.append(f"Pozycja {idx}: ilość musi być większa od 0.")
        if _dec(item.get("net_price")) < 0:
            problems.append(f"Pozycja {idx}: cena netto nie może być ujemna.")

    return problems


def build_ksef_draft_xml(invoice: dict, company: dict, items: list[dict]) -> str:
    """
    Buduje XML FA(3) dla zwykłej faktury VAT 23%.
    Nazwa funkcji została zachowana dla zgodności z app.py.
    """
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
    seller_addr1, seller_addr2 = _company_address(company)
    seller_addr = SubElement(seller, _tag("Adres"))
    _add(seller_addr, "KodKraju", "PL")
    _add(seller_addr, "AdresL1", seller_addr1)
    if seller_addr2:
        _add(seller_addr, "AdresL2", seller_addr2)

    buyer = SubElement(root, _tag("Podmiot2"))
    buyer_id = SubElement(buyer, _tag("DaneIdentyfikacyjne"))
    buyer_nip = _nip(invoice.get("buyer_tax_no"))
    if buyer_nip:
        _add(buyer_id, "NIP", buyer_nip)
    else:
        _add(buyer_id, "BrakID", "1")
    _add(buyer_id, "Nazwa", _limit(invoice.get("buyer_name"), 512))
    buyer_addr1, buyer_addr2 = _buyer_address(invoice)
    buyer_addr = SubElement(buyer, _tag("Adres"))
    _add(buyer_addr, "KodKraju", _text(invoice.get("buyer_country")) or "PL")
    _add(buyer_addr, "AdresL1", buyer_addr1)
    if buyer_addr2:
        _add(buyer_addr, "AdresL2", buyer_addr2)
    _add(buyer, "JST", "2")
    _add(buyer, "GV", "2")

    fa = SubElement(root, _tag("Fa"))
    _add(fa, "KodWaluty", "PLN")
    _add(fa, "P_1", _date(invoice.get("issue_date")))
    _add(fa, "P_1M", _place_of_issue(invoice, company))
    _add(fa, "P_2", _limit(invoice.get("invoice_no"), 256))
    if _date(invoice.get("sell_date")):
        _add(fa, "P_6", _date(invoice.get("sell_date")))

    rows: list[tuple[dict, Decimal, Decimal, Decimal]] = []
    total_net = Decimal("0.00")
    for item in items:
        qty = _dec(item.get("qty"))
        unit_net = _dec(item.get("net_price")).quantize(PRICE_Q, rounding=ROUND_HALF_UP)
        line_net = _dec(item.get("line_value_net"), str(unit_net * qty)).quantize(MONEY_Q, rounding=ROUND_HALF_UP)
        if line_net == 0 and qty and unit_net:
            line_net = (unit_net * qty).quantize(MONEY_Q, rounding=ROUND_HALF_UP)
        line_vat = (line_net * Decimal("0.23")).quantize(MONEY_Q, rounding=ROUND_HALF_UP)
        rows.append((item, qty, unit_net, line_net))
        total_net += line_net

    total_vat = (total_net * Decimal("0.23")).quantize(MONEY_Q, rounding=ROUND_HALF_UP)
    total_gross = (total_net + total_vat).quantize(MONEY_Q, rounding=ROUND_HALF_UP)

    _add(fa, "P_13_1", _money(total_net))
    _add(fa, "P_14_1", _money(total_vat))
    _add(fa, "P_15", _money(total_gross))

    annotations = SubElement(fa, _tag("Adnotacje"))
    _add(annotations, "P_16", "2")
    _add(annotations, "P_17", "2")
    _add(annotations, "P_18", "2")
    _add(annotations, "P_18A", "2")
    exempt = SubElement(annotations, _tag("Zwolnienie"))
    _add(exempt, "P_19N", "1")
    transport = SubElement(annotations, _tag("NoweSrodkiTransportu"))
    _add(transport, "P_22N", "1")
    _add(annotations, "P_23", "2")
    margin = SubElement(annotations, _tag("PMarzy"))
    _add(margin, "P_PMarzyN", "1")

    _add(fa, "RodzajFaktury", "VAT")

    for idx, (item, qty, unit_net, line_net) in enumerate(rows, start=1):
        row = SubElement(fa, _tag("FaWiersz"))
        _add(row, "NrWierszaFa", str(idx))
        _add(row, "P_7", _name_for_item(item))
        _add(row, "P_8A", "szt.")
        _add(row, "P_8B", _qty(qty))
        _add(row, "P_9A", _price(unit_net))
        _add(row, "P_11", _money(line_net))
        _add(row, "P_12", "23")

    payment = SubElement(fa, _tag("Platnosc"))
    due = _payment_due_date(invoice)
    if due:
        due_node = SubElement(payment, _tag("TerminPlatnosci"))
        _add(due_node, "Termin", due)
    _add(payment, "FormaPlatnosci", _payment_code(_invoice_payment_type(invoice)))

    rough = tostring(root, encoding="utf-8")
    return minidom.parseString(rough).toprettyxml(indent="  ", encoding="utf-8").decode("utf-8")


def validate_fa3_xml(xml_text: str, schema_path: str) -> list[str]:
    """Waliduje XML względem lokalnego schematu FA(3), jeśli aplikacja ma pliki XSD."""
    try:
        from lxml import etree
    except Exception:
        return ["Brak biblioteki lxml do lokalnej walidacji XSD."]

    if not os.path.exists(schema_path):
        return [f"Brak pliku schematu XSD: {schema_path}"]

    class LocalResolver(etree.Resolver):
        def resolve(self, url, pubid, context):
            if url.endswith("StrukturyDanych_v10-0E.xsd"):
                local = os.path.join(os.path.dirname(schema_path), "StrukturyDanych_v10-0E.xsd")
                if os.path.exists(local):
                    return self.resolve_filename(local, context)
            if url.endswith("ElementarneTypyDanych_v10-0E.xsd"):
                local = os.path.join(os.path.dirname(schema_path), "ElementarneTypyDanych_v10-0E.xsd")
                if os.path.exists(local):
                    return self.resolve_filename(local, context)
            if url.endswith("KodyKrajow_v10-0E.xsd"):
                local = os.path.join(os.path.dirname(schema_path), "KodyKrajow_v10-0E.xsd")
                if os.path.exists(local):
                    return self.resolve_filename(local, context)
            return None

    parser = etree.XMLParser()
    parser.resolvers.add(LocalResolver())
    try:
        schema_doc = etree.parse(schema_path, parser)
        schema = etree.XMLSchema(schema_doc)
        doc = etree.fromstring(xml_text.encode("utf-8"))
        schema.assertValid(doc)
        return []
    except Exception as exc:
        msg = str(exc)
        if hasattr(exc, "error_log") and exc.error_log:
            msg = "; ".join(str(e) for e in list(exc.error_log)[:5])
        return [msg]


def xml_filename(invoice_no: str) -> str:
    # Numer faktury zostaje wyłącznie w XML w polu P_2.
    # Nazwa pliku jest losowa, żeby przy testach/importach KSeF nie sugerował się nazwą
    # ani nie wpadał na konflikt z wcześniej wczytanym plikiem o tej samej nazwie.
    return f"ksef_{uuid.uuid4().hex[:12]}.xml"
