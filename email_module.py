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
    html_body = f"""
    <div style="font-family:Arial,sans-serif;color:#111;line-height:1.5;max-width:760px">
      <h2 style="margin-bottom:8px">{_esc(copy['title'])}</h2>
      <p>{copy['intro'].format(order_no=_esc(order_no))}</p>
      <p>
        <b>{_esc(copy['customer'])}:</b> {_esc(customer_name)}<br>
        <b>{_esc(copy['email'])}:</b> {_esc(customer_email)}<br>
        <b>{_esc(copy['note'])}:</b> {_esc(note)}
      </p>
      {items_table}
      <p style="margin-top:18px">{_esc(copy['reply'])}</p>
      <p style="color:#555;margin-top:18px">{_esc(copy['regards'])},<br>Niedźwieccy</p>
    </div>
    """
    text_body = (
        f"{copy['title']}: {order_no}\n"
        f"{copy['customer']}: {customer_name}\n"
        f"{copy['email']}: {customer_email}\n"
        f"{copy['note']}: {note}\n"
        f"{copy['total']}: {_money(total, currency)}\n\n"
        f"{copy['reply']}\n{copy['regards']}, Niedźwieccy"
    )
    return send_email(recipients, subject, html_body, text_body)


def send_invoice_available(invoice: dict, pdf_url: str = "", admin_email: str = "") -> dict:
    invoice_no = invoice.get("invoice_no") or "faktura"
    buyer_name = invoice.get("buyer_name") or invoice.get("customer_name") or "Klient"
    buyer_email = invoice.get("buyer_email") or invoice.get("customer_email") or ""
    recipients = _uniq_emails([buyer_email, admin_email or email_config_summary().get("admin_email")])
    link_html = (
        f"<p><a href='{_esc(pdf_url)}' style='display:inline-block;background:#111;color:#fff;"
        "padding:10px 14px;border-radius:10px;text-decoration:none'>Otwórz fakturę w panelu</a></p>"
        if pdf_url
        else ""
    )
    subject = f"Nowa faktura do pobrania: {invoice_no}"
    html_body = f"""
    <div style="font-family:Arial,sans-serif;color:#111;line-height:1.5;max-width:760px">
      <h2 style="margin-bottom:8px">Nowa faktura jest dostępna</h2>
      <p>Udostępniliśmy fakturę <b>{_esc(invoice_no)}</b>. Możesz ją pobrać w panelu klienta albo przyciskiem poniżej.</p>
      <p>
        <b>Klient:</b> {_esc(buyer_name)}<br>
        <b>Kwota brutto:</b> {_esc(_money(invoice.get('total_gross')))}<br>
        <b>Termin płatności:</b> {_esc(invoice.get('payment_to') or '-')}
      </p>
      {link_html}
      <p style="color:#555">Po pobraniu faktury komunikat w panelu klienta przestanie się pojawiać.</p>
      <p style="color:#555;margin-top:18px">Pozdrawiamy,<br>Niedźwieccy</p>
    </div>
    """
    text_body = (
        f"Nowa faktura jest dostępna: {invoice_no}\n"
        f"Kwota brutto: {_money(invoice.get('total_gross'))}\n"
        f"Termin płatności: {invoice.get('payment_to') or '-'}\n\n"
        "Po pobraniu faktury komunikat w panelu klienta przestanie się pojawiać.\n"
        "Pozdrawiamy, Niedźwieccy"
    )
    return send_email(recipients, subject, html_body, text_body)


def send_payment_reminder(invoice: dict, pdf_url: str = "", admin_email: str = "") -> dict:
    invoice_no = invoice.get("invoice_no") or "faktura"
    buyer_name = invoice.get("buyer_name") or invoice.get("customer_name") or "Klient"
    buyer_email = invoice.get("buyer_email") or invoice.get("customer_email") or ""
    recipients = _uniq_emails([buyer_email, admin_email or email_config_summary().get("admin_email")])
    link_html = (
        f"<p><a href='{_esc(pdf_url)}' style='display:inline-block;background:#dc2626;color:#fff;"
        "padding:10px 14px;border-radius:10px;text-decoration:none'>Pobierz fakturę PDF</a></p>"
        if pdf_url
        else ""
    )
    subject = f"Przypomnienie o płatności: {invoice_no}"
    html_body = f"""
    <div style="font-family:Arial,sans-serif;color:#111;line-height:1.5;max-width:760px">
      <h2 style="margin-bottom:8px">Przypomnienie o płatności</h2>
      <p>Przypominamy o fakturze <b>{_esc(invoice_no)}</b>, która jest jeszcze oznaczona jako nieopłacona.</p>
      <p>
        <b>Klient:</b> {_esc(buyer_name)}<br>
        <b>Kwota brutto:</b> {_esc(_money(invoice.get('total_gross')))}<br>
        <b>Termin płatności:</b> {_esc(invoice.get('payment_to') or '-')}
      </p>
      {link_html}
      <p>Prosimy o uregulowanie płatności. Jeśli przelew został już wykonany, możesz zignorować tę wiadomość.<br>Wadomość została wygenerowana automatycznie, prosimy na nią nie odpowiadać.</p>
      <p style="color:#555;margin-top:18px">Pozdrawiamy,<br>Niedźwieccy</p>
    </div>
    """
    text_body = (
        f"Przypomnienie o płatności za {invoice_no}\n"
        f"Kwota brutto: {_money(invoice.get('total_gross'))}\n"
        f"Termin płatności: {invoice.get('payment_to') or '-'}\n\n"
        "Prosimy o uregulowanie płatności. Jeśli przelew został już wykonany, możesz zignorować tę wiadomość.\n"
        "Pozdrawiamy, Niedźwieccy"
    )
    return send_email(recipients, subject, html_body, text_body)
