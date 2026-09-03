# -*- coding: utf-8 -*-
"""
Moduł maili dla Niedźwieccy Orders.

Sekrety trzymamy wyłącznie po stronie Rendera:
- RESEND_API_KEY
- EMAIL_FROM, np. "Niedźwieccy <biuro@niedzwieccy.com>"
- ADMIN_EMAIL, np. "biuro@niedzwieccy.com"
- EMAIL_ENABLED=1/0
"""

from __future__ import annotations

import html
import base64
import json
import os
import urllib.error
import urllib.request
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable


RESEND_API_URL = "https://api.resend.com/emails"


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default or "").strip()


def _money(value, currency: str = "PLN") -> str:
    try:
        amount = Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return f"{amount:.2f} {str(currency or 'PLN').upper()}"
    except Exception:
        return f"{value or 0} {str(currency or 'PLN').upper()}"


def _esc(value) -> str:
    return html.escape(str(value or ""), quote=True)


def _email_shell(title: str, content: str, footer: str = "Zespół Niedźwieccy", closing: str = "Pozdrawiamy") -> str:
    """Wspólny, czytelny układ wszystkich wiadomości transakcyjnych."""
    return (
        "<div style='margin:0;padding:28px 14px;background:#f3f6fb;font-family:Arial,sans-serif;color:#10203d'>"
        "<div style='max-width:620px;margin:0 auto;background:#fff;border:1px solid #e2e8f2;border-radius:18px;overflow:hidden'>"
        "<div style='padding:30px 34px 28px'>"
        f"<h1 style='margin:0 0 24px;font-size:26px;line-height:1.25'>{_esc(title)}</h1>"
        f"{content}"
        f"<p style='margin:26px 0 0;line-height:1.6;color:#52617c'>{_esc(closing)},<br><b>{_esc(footer)}</b></p>"
        "</div></div></div>"
    )


def _email_info_box(content: str, *, danger: bool = False) -> str:
    background = "#fff5f5" if danger else "#f7f9fd"
    border = "#fecaca" if danger else "#e3e9f3"
    return f"<div style='margin:22px 0;padding:20px;background:{background};border:1px solid {border};border-radius:14px;line-height:1.8'>{content}</div>"


def _email_button(label: str, url: str, *, danger: bool = False) -> str:
    if not url:
        return ""
    color = "#dc2626" if danger else "#4f70eb"
    return f"<p style='margin:24px 0'><a href='{_esc(url)}' style='display:inline-block;padding:13px 22px;background:{color};color:#fff;text-decoration:none;font-weight:bold;border-radius:10px'>{_esc(label)}</a></p>"


def _uniq_emails(values: Iterable[str] | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        email = str(value or "").strip().lower()
        if not email or "@" not in email or email in seen:
            continue
        seen.add(email)
        out.append(email)
    return out


def email_config_summary() -> dict:
    api_key = _env("RESEND_API_KEY")
    sender = _env("EMAIL_FROM", "Niedzwieccy Orders <onboarding@resend.dev>")
    admin = _env("ADMIN_EMAIL", _env("ADMIN_MAIL", "biuro@niedzwieccy.com"))
    enabled = _env("EMAIL_ENABLED", "1").lower() not in {"0", "false", "no", "off"}
    missing = []
    if enabled and not api_key:
        missing.append("RESEND_API_KEY")
    if enabled and not sender:
        missing.append("EMAIL_FROM")
    return {
        "enabled": enabled,
        "configured": enabled and not missing,
        "missing": missing,
        "from": sender,
        "admin_email": admin,
        "api_key_set": bool(api_key),
    }


def _decode_resend_error(exc: urllib.error.HTTPError) -> tuple[str, dict]:
    raw = exc.read().decode("utf-8", errors="replace")
    if not raw:
        return f"HTTP {exc.code}", {}
    try:
        body = json.loads(raw)
        message = body.get("message") or body.get("error") or raw
        return str(message), body
    except Exception:
        return raw[:1200], {"raw": raw[:1200]}


def send_email(to, subject: str, html_body: str, text_body: str = "", attachments=None) -> dict:
    cfg = email_config_summary()
    recipients = _uniq_emails(to if isinstance(to, (list, tuple, set)) else [to])
    if not recipients:
        return {"ok": False, "skipped": True, "error": "Brak odbiorców"}
    if not cfg["enabled"]:
        return {"ok": False, "skipped": True, "error": "EMAIL_ENABLED=0"}
    if not cfg["configured"]:
        return {"ok": False, "skipped": True, "error": "Brak konfiguracji: " + ", ".join(cfg["missing"])}

    payload = {
        "from": cfg["from"],
        "to": recipients,
        "subject": subject,
        "html": html_body,
    }
    if text_body:
        payload["text"] = text_body
    encoded_attachments = []
    for attachment in attachments or []:
        if not isinstance(attachment, dict):
            continue
        filename = str(attachment.get("filename") or "").strip()
        content = attachment.get("content")
        if not filename or content in (None, b"", ""):
            continue
        if isinstance(content, (bytes, bytearray)):
            content = base64.b64encode(bytes(content)).decode("ascii")
        elif not isinstance(content, str):
            continue
        encoded_attachments.append({"filename": filename, "content": content})
    if encoded_attachments:
        payload["attachments"] = encoded_attachments

    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        RESEND_API_URL,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {_env('RESEND_API_KEY')}",
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            # Resend stoi za Cloudflare. Domyślny Python-urllib bywa odrzucany jako bot.
            "User-Agent": "NiedzwieccyOrders/1.0 (+https://niedzwieccy.com)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                body = json.loads(raw) if raw else {}
            except Exception:
                body = {"raw": raw}
            return {"ok": 200 <= int(resp.status) < 300, "status": int(resp.status), "body": body, "to": recipients}
    except urllib.error.HTTPError as exc:
        message, body = _decode_resend_error(exc)
        return {"ok": False, "status": exc.code, "error": message, "body": body, "to": recipients}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "to": recipients}


ORDER_COPY = {
    "pl": {
        "subject": "Przyjęliśmy zamówienie {order_no}", "title": "Dziękujemy za zamówienie",
        "intro": "Zamówienie <b>{order_no}</b> zostało przyjęte. Poniżej przesyłamy podsumowanie.",
        "customer": "Klient", "email": "Email", "note": "Notatka", "name": "Nazwa", "qty": "Ilość",
        "unit": "Cena/szt.", "value": "Wartość", "total": "Razem",
        "empty": "Brak pozycji", "reply": "Jeśli coś się nie zgadza, odpowiedz na tę wiadomość — sprawdzimy to od razu.",
        "regards": "Pozdrawiamy",
    },
    "de": {
        "subject": "Wir haben Ihre Bestellung {order_no} erhalten", "title": "Vielen Dank für Ihre Bestellung",
        "intro": "Ihre Bestellung <b>{order_no}</b> ist bei uns eingegangen. Unten finden Sie die Zusammenfassung.",
        "customer": "Kunde", "email": "E-Mail", "note": "Anmerkung", "name": "Bezeichnung", "qty": "Menge",
        "unit": "Preis/Stk.", "value": "Wert", "total": "Gesamt",
        "empty": "Keine Positionen", "reply": "Falls etwas nicht stimmt, antworten Sie bitte auf diese Nachricht — wir prüfen es sofort.",
        "regards": "Viele Grüße",
    },
    "en": {
        "subject": "We received your order {order_no}", "title": "Thank you for your order",
        "intro": "Your order <b>{order_no}</b> has been received. A summary is shown below.",
        "customer": "Customer", "email": "Email", "note": "Note", "name": "Name", "qty": "Quantity",
        "unit": "Unit price", "value": "Value", "total": "Total",
        "empty": "No items", "reply": "If anything is incorrect, reply to this message and we will check it right away.",
        "regards": "Kind regards",
    },
    "es": {
        "subject": "Hemos recibido su pedido {order_no}", "title": "Gracias por su pedido",
        "intro": "Hemos recibido el pedido <b>{order_no}</b>. A continuación encontrará el resumen.",
        "customer": "Cliente", "email": "Email", "note": "Nota", "name": "Nombre", "qty": "Cantidad",
        "unit": "Precio/ud.", "value": "Importe", "total": "Total",
        "empty": "Sin artículos", "reply": "Si algo no es correcto, responda a este mensaje y lo revisaremos de inmediato.",
        "regards": "Saludos",
    },
    "it": {
        "subject": "Abbiamo ricevuto l'ordine {order_no}", "title": "Grazie per il suo ordine",
        "intro": "L'ordine <b>{order_no}</b> è stato ricevuto. Di seguito trova il riepilogo.",
        "customer": "Cliente", "email": "Email", "note": "Nota", "name": "Nome", "qty": "Quantità",
        "unit": "Prezzo/pz.", "value": "Valore", "total": "Totale",
        "empty": "Nessun articolo", "reply": "Se qualcosa non è corretto, risponda a questo messaggio e lo verificheremo subito.",
        "regards": "Cordiali saluti",
    },
}


def _items_table(items, copy: dict, currency: str) -> tuple[str, Decimal]:
    rows = []
    total = Decimal("0.00")
    for item in items or []:
        sku = item.get("sku") or item.get("product_sku") or item.get("model") or ""
        name = item.get("name") or item.get("product_name") or item.get("model_name") or ""
        qty = item.get("qty") or item.get("quantity") or item.get("invoice_qty") or 0
        unit_price = Decimal(str(item.get("net_price") or item.get("unit_net_price") or 0))
        line_value = (unit_price * Decimal(str(qty or 0))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        total += line_value
        rows.append(
            "<tr>"
            f"<td style='padding:8px;border-bottom:1px solid #eee'>{_esc(sku)}</td>"
            f"<td style='padding:8px;border-bottom:1px solid #eee'>{_esc(name)}</td>"
            f"<td style='padding:8px;border-bottom:1px solid #eee;text-align:right'>{_esc(qty)}</td>"
            f"<td style='padding:8px;border-bottom:1px solid #eee;text-align:right'>{_esc(_money(unit_price, currency))}</td>"
            f"<td style='padding:8px;border-bottom:1px solid #eee;text-align:right'>{_esc(_money(line_value, currency))}</td>"
            "</tr>"
        )
    if not rows:
        rows.append(f"<tr><td colspan='5' style='padding:8px;border-bottom:1px solid #eee'>{_esc(copy['empty'])}</td></tr>")
    table = (
        "<table style='border-collapse:collapse;width:100%;max-width:760px'>"
        "<thead><tr>"
        "<th style='text-align:left;padding:8px;border-bottom:2px solid #111'>SKU</th>"
        f"<th style='text-align:left;padding:8px;border-bottom:2px solid #111'>{_esc(copy['name'])}</th>"
        f"<th style='text-align:right;padding:8px;border-bottom:2px solid #111'>{_esc(copy['qty'])}</th>"
        f"<th style='text-align:right;padding:8px;border-bottom:2px solid #111'>{_esc(copy['unit'])}</th>"
        f"<th style='text-align:right;padding:8px;border-bottom:2px solid #111'>{_esc(copy['value'])}</th>"
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody>"
        f"<tfoot><tr><th colspan='4' style='padding:10px 8px;text-align:right'>{_esc(copy['total'])}</th>"
        f"<th style='padding:10px 8px;text-align:right'>{_esc(_money(total, currency))}</th></tr></tfoot></table>"
    )
    return table, total


def send_order_confirmation(order: dict, items: list[dict], admin_email: str = "") -> dict:
    order_no = order.get("order_no") or f"ZAM-{order.get('id') or ''}"
    customer_name = order.get("customer_name") or order.get("name") or order.get("customer_email") or "Klient"
    customer_email = order.get("customer_email") or order.get("email") or ""
    note = order.get("note") or "-"
    language = str(order.get("language") or "pl").lower()
    copy = ORDER_COPY.get(language, ORDER_COPY["pl"])
    currency = str(order.get("currency") or "PLN").upper()
    recipients = _uniq_emails([customer_email, admin_email or email_config_summary().get("admin_email")])
    subject = copy["subject"].format(order_no=order_no)
    items_table, total = _items_table(items, copy, currency)
    details = _email_info_box(
        f"<div><span style='color:#62708c'>{_esc(copy['customer'])}:</span> <b>{_esc(customer_name)}</b></div>"
        f"<div><span style='color:#62708c'>{_esc(copy['email'])}:</span> <b>{_esc(customer_email)}</b></div>"
        f"<div><span style='color:#62708c'>{_esc(copy['note'])}:</span> {_esc(note)}</div>"
    )
    html_body = _email_shell(
        copy["title"],
        f"<p style='margin:0 0 20px;line-height:1.6'>{copy['intro'].format(order_no=_esc(order_no))}</p>"
        f"{details}<div style='overflow-x:auto'>{items_table}</div>"
        f"<p style='margin:22px 0 0;line-height:1.6'>{_esc(copy['reply'])}</p>",
        closing=copy["regards"],
    )
    text_body = (
        f"{copy['title']}: {order_no}\n"
        f"{copy['customer']}: {customer_name}\n"
        f"{copy['email']}: {customer_email}\n"
        f"{copy['note']}: {note}\n"
        f"{copy['total']}: {_money(total, currency)}\n\n"
        f"{copy['reply']}\n{copy['regards']}, Niedźwieccy"
    )
    return send_email(recipients, subject, html_body, text_body)


INVOICE_COPY = {
    "pl": {
        "subject": "Nowa faktura: {invoice_no}", "title": "Nowa faktura jest dostępna",
        "intro": "W załączeniu przesyłamy fakturę <b>{invoice_no}</b>.",
        "invoice_no": "Numer faktury", "customer": "Klient", "gross": "Kwota brutto",
        "due": "Termin płatności", "attachment": "Faktura PDF jest dołączona do tej wiadomości.",
        "regards": "Pozdrawiamy",
    },
    "de": {
        "subject": "Neue Rechnung: {invoice_no}", "title": "Ihre neue Rechnung",
        "intro": "Im Anhang finden Sie die Rechnung <b>{invoice_no}</b>.",
        "invoice_no": "Rechnungsnummer", "customer": "Kunde", "gross": "Bruttobetrag",
        "due": "Zahlungsfrist", "attachment": "Die Rechnung ist dieser E-Mail als PDF beigefügt.",
        "regards": "Freundliche Grüße",
    },
    "en": {
        "subject": "New invoice: {invoice_no}", "title": "Your new invoice",
        "intro": "Please find invoice <b>{invoice_no}</b> attached.",
        "invoice_no": "Invoice number", "customer": "Customer", "gross": "Gross amount",
        "due": "Payment due", "attachment": "The invoice is attached to this email as a PDF.",
        "regards": "Kind regards",
    },
    "es": {
        "subject": "Nueva factura: {invoice_no}", "title": "Su nueva factura",
        "intro": "Adjuntamos la factura <b>{invoice_no}</b>.",
        "invoice_no": "Número de factura", "customer": "Cliente", "gross": "Importe bruto",
        "due": "Fecha de vencimiento", "attachment": "La factura está adjunta a este correo en formato PDF.",
        "regards": "Saludos cordiales",
    },
    "it": {
        "subject": "Nuova fattura: {invoice_no}", "title": "La sua nuova fattura",
        "intro": "In allegato trova la fattura <b>{invoice_no}</b>.",
        "invoice_no": "Numero fattura", "customer": "Cliente", "gross": "Importo lordo",
        "due": "Scadenza pagamento", "attachment": "La fattura è allegata a questa e-mail in formato PDF.",
        "regards": "Cordiali saluti",
    },
}


def send_invoice_available(invoice: dict, pdf_url: str = "", admin_email: str = "", pdf_attachment=None) -> dict:
    invoice_no = invoice.get("invoice_no") or "faktura"
    buyer_name = invoice.get("buyer_name") or invoice.get("customer_name") or "Klient"
    buyer_email = invoice.get("buyer_email") or invoice.get("customer_email") or ""
    language = str(invoice.get("language") or "pl").lower()
    copy = INVOICE_COPY.get(language, INVOICE_COPY["pl"])
    currency = str(invoice.get("currency") or "PLN").upper()
    recipients = _uniq_emails([buyer_email, admin_email or email_config_summary().get("admin_email")])
    subject = copy["subject"].format(invoice_no=invoice_no)
    details = _email_info_box(
        f"<div><span style='color:#62708c'>{_esc(copy['invoice_no'])}:</span> <b>{_esc(invoice_no)}</b></div>"
        f"<div><span style='color:#62708c'>{_esc(copy['customer'])}:</span> <b>{_esc(buyer_name)}</b></div>"
        f"<div><span style='color:#62708c'>{_esc(copy['gross'])}:</span> <b>{_esc(_money(invoice.get('total_gross'), currency))}</b></div>"
        f"<div><span style='color:#62708c'>{_esc(copy['due'])}:</span> <b>{_esc(invoice.get('payment_to') or '-')}</b></div>"
    )
    html_body = _email_shell(
        copy["title"],
        f"<p style='margin:0;line-height:1.6'>{copy['intro'].format(invoice_no=_esc(invoice_no))}</p>"
        f"{details}<p style='margin:0;line-height:1.6;color:#52617c'>{_esc(copy['attachment'])}</p>",
        closing=copy["regards"],
    )
    text_body = (
        f"{copy['title']}: {invoice_no}\n"
        f"{copy['gross']}: {_money(invoice.get('total_gross'), currency)}\n"
        f"{copy['due']}: {invoice.get('payment_to') or '-'}\n\n"
        f"{copy['attachment']}\n{copy['regards']}, Niedźwieccy"
    )
    attachments = []
    if isinstance(pdf_attachment, dict) and pdf_attachment.get("content"):
        attachments.append(pdf_attachment)
    return send_email(recipients, subject, html_body, text_body, attachments=attachments)

def send_payment_reminder(invoice: dict, pdf_url: str = "", admin_email: str = "") -> dict:
    invoice_no = invoice.get("invoice_no") or "faktura"
    buyer_name = invoice.get("buyer_name") or invoice.get("customer_name") or "Klient"
    buyer_email = invoice.get("buyer_email") or invoice.get("customer_email") or ""
    recipients = _uniq_emails([buyer_email, admin_email or email_config_summary().get("admin_email")])
    link_html = _email_button("Pobierz fakturę PDF", pdf_url, danger=True)
    subject = f"Przypomnienie o płatności: {invoice_no}"
    details = _email_info_box(
        f"<div><span style='color:#991b1b'>Numer faktury:</span> <b>{_esc(invoice_no)}</b></div>"
        f"<div><span style='color:#991b1b'>Klient:</span> <b>{_esc(buyer_name)}</b></div>"
        f"<div><span style='color:#991b1b'>Kwota brutto:</span> <b>{_esc(_money(invoice.get('total_gross')))}</b></div>"
        f"<div><span style='color:#991b1b'>Termin płatności:</span> <b>{_esc(invoice.get('payment_to') or '-')}</b></div>",
        danger=True,
    )
    html_body = _email_shell(
        "Przypomnienie o płatności",
        f"<p style='margin:0;line-height:1.6'>Przypominamy o fakturze <b>{_esc(invoice_no)}</b>, która jest oznaczona jako nieopłacona.</p>"
        f"{details}{link_html}<p style='margin:0;line-height:1.6'>Prosimy o uregulowanie płatności. Jeśli przelew został już wykonany, możesz zignorować tę wiadomość.</p>"
        "<p style='margin:12px 0 0;color:#71809f;font-size:13px'>Wiadomość została wygenerowana automatycznie.</p>",
    )
    text_body = (
        f"Przypomnienie o płatności za {invoice_no}\n"
        f"Kwota brutto: {_money(invoice.get('total_gross'))}\n"
        f"Termin płatności: {invoice.get('payment_to') or '-'}\n\n"
        "Prosimy o uregulowanie płatności. Jeśli przelew został już wykonany, możesz zignorować tę wiadomość.\n"
        "Pozdrawiamy, Niedźwieccy"
    )
    return send_email(recipients, subject, html_body, text_body)
