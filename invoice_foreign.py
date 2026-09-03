"""Foreign (WDT/export) PDF entry point, isolated from domestic routing."""

from invoice_types import require_invoice_type


def generate(order, items, invoice, *, renderer):
    invoice_type = require_invoice_type(invoice.get("invoice_type"))
    if invoice_type not in {"wdt", "export"}:
        raise ValueError("Nieobsługiwany typ faktury zagranicznej")
    payload = dict(invoice)
    payload["invoice_type"] = invoice_type
    payload["vat_rate"] = 0
    return renderer(order, items, payload)

