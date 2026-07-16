# -*- coding: utf-8 -*-
"""
Moduł KSeF — etap 1.

Ten plik nie wysyła faktur do KSeF. Na tym etapie buduje roboczy XML
z danych faktury i robi podstawową kontrolę brakujących danych.
Wysyłkę do API KSeF warto podpiąć dopiero po testach XML i konfiguracji
tokenów/certyfikatów.
"""

from __future__ import annotations

import html
import re
from decimal import Decimal, ROUND_HALF_UP
from xml.etree.ElementTree import Element, SubElement, tostring
from xml.dom import minidom


MONEY_Q = Decimal("0.01")


def _text(value) -> str:
    return str(value or "").strip()


def _nip(value) -> str:
    return re.sub(r"\D+", "", _text(value))


def _money(value) -> str:
    try:
        return str(Decimal(str(value or "0")).quantize(MONEY_Q, rounding=ROUND_HALF_UP))
    except Exception:
        return "0.00"


def _add(parent, tag: str, value=None):
    node = SubElement(parent, tag)
    node.text = _text(value)
    return node


def validate_ksef_invoice(invoice: dict, company: dict, items: list[dict]) -> list[str]:
    """Zwraca listę braków, które trzeba poprawić przed realnym KSeF."""
    problems = []

    if not _text(invoice.get("invoice_no")):
        problems.append("Brak numeru faktury.")
    if not _text(invoice.get("issue_date")):
        problems.append("Brak daty wystawienia.")
    if not _text(invoice.get("sell_date")):
        problems.append("Brak daty sprzedaży.")

    if not _text(company.get("company_name")):
        problems.append("Brak nazwy sprzedawcy w danych firmy.")
    if not _nip(company.get("nip")):
        problems.append("Brak NIP sprzedawcy w danych firmy.")
    if not _text(company.get("address")):
        problems.append("Brak adresu sprzedawcy w danych firmy.")

    if not _text(invoice.get("buyer_name")):
        problems.append("Brak nazwy nabywcy.")
    if not _nip(invoice.get("buyer_tax_no")):
        problems.append("Brak NIP nabywcy.")
    if not _text(invoice.get("buyer_street")) and not _text(invoice.get("buyer_city")):
        problems.append("Brak adresu nabywcy.")

    if not items:
        problems.append("Brak pozycji faktury.")

    for idx, item in enumerate(items, start=1):
        name = _text(item.get("name") or item.get("model") or item.get("sku"))
        qty = Decimal(str(item.get("qty") or "0"))
        if not name:
            problems.append(f"Pozycja {idx}: brak nazwy/modelu.")
        if qty <= 0:
            problems.append(f"Pozycja {idx}: ilość musi być większa od 0.")

    return problems


def build_ksef_draft_xml(invoice: dict, company: dict, items: list[dict]) -> str:
    """
    Buduje roboczy XML na potrzeby kontroli danych.
    To nie jest jeszcze finalny plik podpisany/wysłany do bramki KSeF.
    """
    root = Element("FakturaRoboczaKSeF")
    root.set("wersjaModulu", "draft-1")
    root.set("uwaga", "XML roboczy; przed wysyłką wymaga walidacji ze schemą KSeF FA")

    naglowek = SubElement(root, "Naglowek")
    _add(naglowek, "KodFormularza", "FA")
    _add(naglowek, "WariantFormularza", "roboczy")
    _add(naglowek, "DataWytworzeniaFa", _text(invoice.get("issue_date")))

    podmiot1 = SubElement(root, "Sprzedawca")
    _add(podmiot1, "Nazwa", company.get("company_name"))
    _add(podmiot1, "NIP", _nip(company.get("nip")))
    _add(podmiot1, "Adres", company.get("address"))
    _add(podmiot1, "Email", company.get("email"))
    _add(podmiot1, "Telefon", company.get("phone"))
    _add(podmiot1, "RachunekBankowy", company.get("bank_account"))

    podmiot2 = SubElement(root, "Nabywca")
    _add(podmiot2, "Nazwa", invoice.get("buyer_name"))
    _add(podmiot2, "NIP", _nip(invoice.get("buyer_tax_no")))
    _add(podmiot2, "Ulica", invoice.get("buyer_street"))
    _add(podmiot2, "KodPocztowy", invoice.get("buyer_post_code"))
    _add(podmiot2, "Miejscowosc", invoice.get("buyer_city"))
    _add(podmiot2, "Kraj", invoice.get("buyer_country") or "PL")
    _add(podmiot2, "Email", invoice.get("buyer_email"))

    fa = SubElement(root, "Faktura")
    _add(fa, "NumerFaktury", invoice.get("invoice_no"))
    _add(fa, "DataWystawienia", invoice.get("issue_date"))
    _add(fa, "DataSprzedazy", invoice.get("sell_date"))
    _add(fa, "FormaPlatnosci", invoice.get("payment_type"))
    _add(fa, "TerminPlatnosci", invoice.get("payment_to"))
    _add(fa, "Waluta", "PLN")

    pozycje = SubElement(fa, "Pozycje")
    total_net = Decimal("0")
    total_vat = Decimal("0")
    total_gross = Decimal("0")

    for idx, item in enumerate(items, start=1):
        qty = Decimal(str(item.get("qty") or "0"))
        unit_net = Decimal(str(item.get("net_price") or "0")).quantize(MONEY_Q, rounding=ROUND_HALF_UP)
        line_net = Decimal(str(item.get("line_value_net") or (unit_net * qty))).quantize(MONEY_Q, rounding=ROUND_HALF_UP)
        line_vat = Decimal(str(item.get("line_value_vat") or (line_net * Decimal("0.23")))).quantize(MONEY_Q, rounding=ROUND_HALF_UP)
        line_gross = Decimal(str(item.get("line_value_gross") or (line_net + line_vat))).quantize(MONEY_Q, rounding=ROUND_HALF_UP)

        total_net += line_net
        total_vat += line_vat
        total_gross += line_gross

        p = SubElement(pozycje, "Pozycja")
        _add(p, "Lp", idx)
        _add(p, "SKU", item.get("sku"))
        _add(p, "Nazwa", item.get("name") or item.get("model") or item.get("sku"))
        _add(p, "Ilosc", qty)
        _add(p, "Jednostka", "szt.")
        _add(p, "CenaNetto", _money(unit_net))
        _add(p, "StawkaVAT", "23")
        _add(p, "WartoscNetto", _money(line_net))
        _add(p, "KwotaVAT", _money(line_vat))
        _add(p, "WartoscBrutto", _money(line_gross))

    suma = SubElement(fa, "Podsumowanie")
    _add(suma, "SumaNetto", _money(invoice.get("total_net") or total_net))
    _add(suma, "SumaVAT", _money(total_vat))
    _add(suma, "SumaBrutto", _money(invoice.get("total_gross") or total_gross))

    rough = tostring(root, encoding="utf-8")
    return minidom.parseString(rough).toprettyxml(indent="  ", encoding="utf-8").decode("utf-8")


def xml_filename(invoice_no: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", _text(invoice_no) or "faktura")
    return f"{safe}_ksef_roboczy.xml"
