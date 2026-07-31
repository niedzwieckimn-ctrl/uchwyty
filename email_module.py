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
import json
import os
import urllib.error
import urllib.request
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable


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
    subject = f"Przyjęliśmy zamówienie {order_no}"
    html_body = f"""
    <div style="font-family:Arial,sans-serif;color:#111;line-height:1.5;max-width:760px">
      <h2 style="margin-bottom:8px">Dziękujemy za zamówienie</h2>
      <p>Zamówienie <b>{_esc(order_no)}</b> zostało przyjęte. Poniżej przesyłamy krótkie podsumowanie.</p>
      <p>
        <b>Klient:</b> {_esc(customer_name)}<br>
        <b>Email:</b> {_esc(customer_email)}<br>
        <b>Notatka:</b> {_esc(note)}
      </p>
      {_items_table(items)}
      <p style="margin-top:18px">Jeśli coś się nie zgadza, odpowiedz na tę wiadomość — sprawdzimy to od razu.</p>
      <p style="color:#555;margin-top:18px">Pozdrawiamy,<br>Niedźwieccy</p>
    </div>
    """
    text_body = (
        f"Przyjęliśmy zamówienie {order_no}\n"
        f"Klient: {customer_name}\n"
        f"Email: {customer_email}\n"
        f"Notatka: {note}\n\n"
        "Jeśli coś się nie zgadza, odpowiedz na tę wiadomość — sprawdzimy to od razu.\n"
        "Pozdrawiamy, Niedźwieccy"
    )
    return send_email(recipients, subject, html_body, text_body)


def send_invoice_available(invoice: dict, pdf_url: str = "", admin_email: str = "") -> dict:
    invoice_no = invoice.get("invoice_no") or "faktura"
    buyer_name = invoice.get("buyer_name") or invoice.get("customer_name") or "Klient"
    buyer_email = invoice.get("buyer_email") or invoice.get("customer_email") or ""
    recipients = _uniq_emails([buyer_email, admin_email or email_config_summary().get("admin_email")])
    link_html = (
        f"<p><a href='{_esc(pdf_url)}' style='display:inline-block;background:#111;color:#fff;"
        "padding:10px 14px;border-radius:10px;text-decoration:none'>Pobierz PDF</a></p>"
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
      <p>Prosimy o uregulowanie płatności. Jeśli przelew został już wykonany, możesz zignorować tę wiadomość.</p>
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
