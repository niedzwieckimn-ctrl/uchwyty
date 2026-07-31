# -*- coding: utf-8 -*-
"""
Moduł maili dla Niedźwieccy Orders.

Sekrety trzymamy wyłącznie po stronie Rendera:
- RESEND_API_KEY
- EMAIL_FROM, np. "Niedźwieccy <faktury@twojadomena.pl>"
- ADMIN_EMAIL, np. "biuro@niedzwieccy.com"
- EMAIL_ENABLED=1/0
"""

import html
import json
import os
import urllib.error
import urllib.request
from decimal import Decimal, ROUND_HALF_UP


RESEND_API_URL = "https://api.resend.com/emails"


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default or "").strip()


def _money(value) -> str:
    try:
        amount = Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return f"{amount:.2f} PLN"
    except Exception:
        return f"{value or 0} PLN"


def _esc(value) -> str:
    return html.escape(str(value or ""), quote=True)


def _uniq_emails(values) -> list[str]:
    out = []
    seen = set()
    for value in values or []:
        email = str(value or "").strip().lower()
        if not email or "@" not in email or email in seen:
            continue
        seen.add(email)
        out.append(email)
    return out


def email_config_summary() -> dict:
    api_key = _env("RESEND_API_KEY")
    sender = _env("EMAIL_FROM", "Niedźwieccy Orders <onboarding@resend.dev>")
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
    }


def send_email(to, subject: str, html_body: str, text_body: str = "") -> dict:
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

    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        RESEND_API_URL,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {_env('RESEND_API_KEY')}",
            "Content-Type": "application/json",
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
        raw = exc.read().decode("utf-8", errors="replace")
        return {"ok": False, "status": exc.code, "error": raw[:800], "to": recipients}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "to": recipients}


def _items_table(items) -> str:
    rows = []
    for item in items or []:
        sku = item.get("sku") or item.get("product_sku") or item.get("model") or ""
        name = item.get("name") or item.get("product_name") or item.get("model_name") or ""
        qty = item.get("qty") or item.get("quantity") or item.get("invoice_qty") or 0
        rows.append(
            "<tr>"
            f"<td style='padding:8px;border-bottom:1px solid #eee'>{_esc(sku)}</td>"
            f"<td style='padding:8px;border-bottom:1px solid #eee'>{_esc(name)}</td>"
            f"<td style='padding:8px;border-bottom:1px solid #eee;text-align:right'>{_esc(qty)}</td>"
            "</tr>"
        )
    if not rows:
        rows.append("<tr><td colspan='3' style='padding:8px;border-bottom:1px solid #eee'>Brak pozycji</td></tr>")
    return (
        "<table style='border-collapse:collapse;width:100%;max-width:760px'>"
        "<thead><tr>"
        "<th style='text-align:left;padding:8px;border-bottom:2px solid #111'>SKU</th>"
        "<th style='text-align:left;padding:8px;border-bottom:2px solid #111'>Nazwa</th>"
        "<th style='text-align:right;padding:8px;border-bottom:2px solid #111'>Ilość</th>"
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def send_order_confirmation(order: dict, items: list[dict], admin_email: str = "") -> dict:
    order_no = order.get("order_no") or f"ZAM-{order.get('id') or ''}"
    customer_name = order.get("customer_name") or order.get("name") or order.get("customer_email") or "Klient"
    customer_email = order.get("customer_email") or order.get("email") or ""
    note = order.get("note") or "-"
    recipients = _uniq_emails([customer_email, admin_email or email_config_summary().get("admin_email")])
    subject = f"Potwierdzenie zamówienia {order_no}"
    html_body = f"""
    <div style="font-family:Arial,sans-serif;color:#111;line-height:1.45">
      <h2>Potwierdzenie złożenia zamówienia</h2>
      <p>Zamówienie <b>{_esc(order_no)}</b> zostało zapisane w systemie.</p>
      <p>
        <b>Klient:</b> {_esc(customer_name)}<br>
        <b>Email:</b> {_esc(customer_email)}<br>
        <b>Notatka:</b> {_esc(note)}
      </p>
      {_items_table(items)}
      <p style="color:#555;margin-top:18px">To jest automatyczna wiadomość z panelu zamówień.</p>
    </div>
    """
    text_body = f"Potwierdzenie zamówienia {order_no}\nKlient: {customer_name}\nEmail: {customer_email}\nNotatka: {note}"
    return send_email(recipients, subject, html_body, text_body)


def send_invoice_available(invoice: dict, pdf_url: str = "", admin_email: str = "") -> dict:
    invoice_no = invoice.get("invoice_no") or "faktura"
    buyer_name = invoice.get("buyer_name") or invoice.get("customer_name") or "Klient"
    buyer_email = invoice.get("buyer_email") or invoice.get("customer_email") or ""
    recipients = _uniq_emails([buyer_email, admin_email or email_config_summary().get("admin_email")])
    link_html = f"<p><a href='{_esc(pdf_url)}' style='display:inline-block;background:#111;color:#fff;padding:10px 14px;border-radius:10px;text-decoration:none'>Pobierz PDF</a></p>" if pdf_url else ""
    subject = f"Nowa faktura {invoice_no}"
    html_body = f"""
    <div style="font-family:Arial,sans-serif;color:#111;line-height:1.45">
      <h2>Masz nową fakturę</h2>
      <p>Faktura <b>{_esc(invoice_no)}</b> została udostępniona w panelu klienta.</p>
      <p>
        <b>Klient:</b> {_esc(buyer_name)}<br>
        <b>Kwota brutto:</b> {_esc(_money(invoice.get('total_gross')))}<br>
        <b>Termin płatności:</b> {_esc(invoice.get('payment_to') or '-')}
      </p>
      {link_html}
      <p style="color:#555">To jest automatyczna wiadomość z panelu zamówień.</p>
    </div>
    """
    text_body = f"Masz nową fakturę {invoice_no}. Kwota brutto: {_money(invoice.get('total_gross'))}. Termin płatności: {invoice.get('payment_to') or '-'}"
    return send_email(recipients, subject, html_body, text_body)


def send_payment_reminder(invoice: dict, pdf_url: str = "", admin_email: str = "") -> dict:
    invoice_no = invoice.get("invoice_no") or "faktura"
    buyer_name = invoice.get("buyer_name") or invoice.get("customer_name") or "Klient"
    buyer_email = invoice.get("buyer_email") or invoice.get("customer_email") or ""
    recipients = _uniq_emails([buyer_email, admin_email or email_config_summary().get("admin_email")])
    link_html = f"<p><a href='{_esc(pdf_url)}' style='display:inline-block;background:#dc2626;color:#fff;padding:10px 14px;border-radius:10px;text-decoration:none'>Pobierz fakturę PDF</a></p>" if pdf_url else ""
    subject = f"Przypomnienie o płatności: {invoice_no}"
    html_body = f"""
    <div style="font-family:Arial,sans-serif;color:#111;line-height:1.45">
      <h2>Przypomnienie o płatności</h2>
      <p>Faktura <b>{_esc(invoice_no)}</b> jest oznaczona jako nieopłacona.</p>
      <p>
        <b>Klient:</b> {_esc(buyer_name)}<br>
        <b>Kwota brutto:</b> {_esc(_money(invoice.get('total_gross')))}<br>
        <b>Termin płatności:</b> {_esc(invoice.get('payment_to') or '-')}
      </p>
      {link_html}
      <p>Prosimy o uregulowanie zaległej płatności.</p>
    </div>
    """
    text_body = f"Przypomnienie o płatności za {invoice_no}. Kwota brutto: {_money(invoice.get('total_gross'))}. Termin: {invoice.get('payment_to') or '-'}"
    return send_email(recipients, subject, html_body, text_body)
