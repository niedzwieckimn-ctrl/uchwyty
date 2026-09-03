"""Tax-type selection shared by PDF and KSeF routing."""

VALID_INVOICE_TYPES = frozenset({"domestic", "wdt", "export"})
ALIASES = {
    "domestic": "domestic", "domestic_23": "domestic",
    "wdt": "wdt", "wdt_0": "wdt",
    "export": "export", "export_0": "export",
}


def normalize_invoice_type(value):
    return ALIASES.get(str(value or "").strip().lower(), "")


def resolve_invoice_type(invoice, items=None):
    """Return an explicit type, with a non-mutating legacy fallback.

    The old application supported only domestic 23% and foreign WDT 0%.
    Therefore old 0%/foreign-currency records safely resolve to WDT. Export is
    deliberately never inferred: new export invoices must store it explicitly.
    """
    invoice = invoice or {}
    for key in ("invoice_type", "document_type"):
        resolved = normalize_invoice_type(invoice.get(key))
        if resolved:
            return resolved
    rows = items or []
    zero_rate = any(str(row.get("vat_rate", "")).strip() in {"0", "0.0", "0.00"} for row in rows)
    currency = str(invoice.get("currency") or (rows[0].get("currency") if rows else "PLN") or "PLN").upper()
    return "wdt" if zero_rate or currency != "PLN" else "domestic"


def require_invoice_type(value):
    resolved = normalize_invoice_type(value)
    if not resolved:
        raise ValueError("Nieobsługiwany typ faktury")
    return resolved

