# -*- coding: utf-8 -*-
import os
import io
import html
import csv
import base64
import re
import json
import hashlib
import glob
import sqlite3
import socket
import time
import threading
import uuid
import secrets
import hmac
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

from flask import (
    Flask, request, redirect, url_for, jsonify, session, g,
    send_file, abort
)
from flask import render_template_string
from jinja2 import DictLoader
from werkzeug.security import check_password_hash

import qrcode
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from ksef_module import build_ksef_draft_xml, validate_fa3_xml, validate_ksef_invoice, xml_filename
from cash_flow_module import register_cash_flow, cash_flow_overdue_invoices
from inventory_analytics import build_replenishment_analysis, recommended_replenishments
from proforma_module import generate_proforma_pdf
try:
    from ksef_api import ksef_config_summary, send_invoice_to_ksef
except Exception:
    send_invoice_to_ksef = None

    def ksef_config_summary():
        return {"configured": False, "missing": ["ksef_api.py"], "env": "", "base_url": ""}

_EMAIL_IMPORT_ERROR = ""
try:
    from email_module import (
        email_config_summary,
        send_email,
        send_order_confirmation,
        send_invoice_available,
        send_payment_reminder,
    )
except Exception as exc:
    _EMAIL_IMPORT_ERROR = str(exc)
    send_email = None
    send_order_confirmation = None
    send_invoice_available = None
    send_payment_reminder = None

    def email_config_summary():
        return {
            "configured": False,
            "missing": ["email_module.py"],
            "enabled": False,
            "import_error": _EMAIL_IMPORT_ERROR,
        }


# =========================
# KONFIG
# =========================

# TWOJE IP (z ipconfig -> IPv4)
BASE_URL = "http://192.168.68.103:5000"

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(APP_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "app.db")

os.makedirs(DATA_DIR, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 20 MB
app.config["JSON_AS_ASCII"] = False
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
)


_MOJIBAKE_REPLACEMENTS = {
    "Ä…": "ą", "Ä‡": "ć", "Ä™": "ę", "Ĺ‚": "ł", "Ĺ„": "ń",
    "Ăł": "ó", "Ĺ›": "ś", "Ĺş": "ź", "ĹĽ": "ż",
    "Ä„": "Ą", "Ä†": "Ć", "Ä": "Ę", "Ĺ": "Ł", "Ĺƒ": "Ń",
    "Ă“": "Ó", "Ĺš": "Ś", "Ĺą": "Ź", "Ĺ»": "Ż",
    "Ã³": "ó", "Å‚": "ł", "Å„": "ń", "Å›": "ś", "Åº": "ź",
    "Å¼": "ż", "Å": "Ł", "Åƒ": "Ń", "Åš": "Ś", "Å¹": "Ź",
    "Å»": "Ż", "Ä": "ą", "Ä": "ć", "Ä": "ę",
    "â€˘": "•", "â€¢": "•", "â€“": "–", "â€”": "—",
    "â€ž": "„", "â€ť": "”", "â€ś": "“", "â€™": "’",
    "â†": "←", "â†’": "→",
}


def fix_polish_mojibake(text: str) -> str:
    if not text or not any(marker in text for marker in ("Ä", "Ĺ", "Ă", "Å", "Ã", "â")):
        return text
    for bad, good in _MOJIBAKE_REPLACEMENTS.items():
        text = text.replace(bad, good)
    return text


@app.after_request
def force_utf8_html(response):
    if response.direct_passthrough:
        return response
    if response.mimetype in {"text/html", "text/plain", "application/json"}:
        response.headers["Content-Type"] = f"{response.mimetype}; charset=utf-8"
    if response.mimetype == "text/html":
        body = response.get_data(as_text=True)
        fixed = fix_polish_mojibake(body)
        if fixed != body:
            response.set_data(fixed)
    return response


def _detect_lan_base_url(port: int) -> str:
    try:
        sck = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sck.connect(("8.8.8.8", 80))
        ip = sck.getsockname()[0]
        sck.close()
        if ip and not ip.startswith("127."):
            return f"http://{ip}:{port}"
    except Exception:
        pass
    return ""


def build_public_url(path: str) -> str:
    # Dla QR preferuj adres LAN; jeĹ›li aplikacja jest otwarta lokalnie,
    # sprĂłbuj wykryÄ‡ LAN IP automatycznie (bardziej niezawodne niĹĽ staĹ‚y BASE_URL).
    base_cfg = (BASE_URL or "").rstrip("/")
    try:
        host = (request.host or "").split(":")[0].lower()
        req_base = (request.host_url or "").rstrip("/")
        req_port = to_int((request.host or "").split(":")[1] if ":" in (request.host or "") else 5000, 5000)
    except RuntimeError:
        host = ""
        req_base = ""
        req_port = 5000

    if host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}:
        base = _detect_lan_base_url(req_port) or base_cfg or req_base
    else:
        base = req_base or base_cfg or _detect_lan_base_url(req_port)

    return f"{base}{path}"


# =========================
# DB
# =========================

def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    c = conn()
    cur = c.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS products(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sku TEXT UNIQUE NOT NULL,
        model TEXT,
        ean TEXT,
        name TEXT,
        archived INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS stock(
        product_id INTEGER PRIMARY KEY,
        qty INTEGER NOT NULL DEFAULT 0,
        FOREIGN KEY(product_id) REFERENCES products(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS customers(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        address TEXT,
        phone TEXT,
        email TEXT,
        nip TEXT,
        language TEXT NOT NULL DEFAULT 'pl',
        price_list TEXT NOT NULL DEFAULT 'pln',
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS orders(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_no TEXT UNIQUE NOT NULL,
        customer_id INTEGER,
        customer_name TEXT NOT NULL,
        customer_address TEXT,
        customer_phone TEXT,
        customer_email TEXT,
        status TEXT NOT NULL DEFAULT 'new', -- new/packed/shipped/cancelled
        note TEXT,
        created_at TEXT NOT NULL,
        warehouse_issued INTEGER NOT NULL DEFAULT 0,
        currency TEXT NOT NULL DEFAULT 'PLN',
        price_list TEXT NOT NULL DEFAULT 'pln',
        tracking_no TEXT,
        carrier TEXT,
        packed_at TEXT,
        shipped_at TEXT,
        FOREIGN KEY(customer_id) REFERENCES customers(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS order_items(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER NOT NULL,
        product_id INTEGER NOT NULL,
        sku TEXT NOT NULL,
        qty INTEGER NOT NULL,
        unit_net_price REAL,
        unit_gross_price REAL,
        unit_retail_price REAL,
        currency TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY(order_id) REFERENCES orders(id),
        FOREIGN KEY(product_id) REFERENCES products(id)
    )
    """)

    # Paczki z Chin (prosty moduĹ‚ na start)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS china_packages(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        package_no TEXT UNIQUE NOT NULL,
        status TEXT NOT NULL DEFAULT 'planned', -- planned/ordered/shipped/arrived
        tracking TEXT,
        note TEXT,
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS china_items(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        package_id INTEGER NOT NULL,
        product_id INTEGER NOT NULL,
        sku TEXT NOT NULL,
        qty INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(package_id) REFERENCES china_packages(id),
        FOREIGN KEY(product_id) REFERENCES products(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS pricing(
        model TEXT PRIMARY KEY,
        net_price REAL NOT NULL DEFAULT 0,
        gross_price REAL NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS company_profile(
        id INTEGER PRIMARY KEY CHECK(id=1),
        company_name TEXT,
        address TEXT,
        nip TEXT,
        phone TEXT,
        email TEXT,
        bank_account TEXT,
        bank_swift TEXT,
        updated_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS invoices(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER NOT NULL,
        invoice_no TEXT NOT NULL,
        issue_date TEXT NOT NULL,
        sell_date TEXT NOT NULL,
        payment_type TEXT NOT NULL,
        payment_to TEXT,
        buyer_name TEXT,
        buyer_tax_no TEXT,
        buyer_street TEXT,
        buyer_post_code TEXT,
        buyer_city TEXT,
        buyer_country TEXT,
        buyer_email TEXT,
        buyer_phone TEXT,
        total_net REAL NOT NULL DEFAULT 0,
        total_gross REAL NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        UNIQUE(invoice_no),
        FOREIGN KEY(order_id) REFERENCES orders(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS invoice_meta(
        invoice_id INTEGER PRIMARY KEY,
        pdf_path TEXT,
        invoice_items_json TEXT,
        sent_to_client INTEGER NOT NULL DEFAULT 0,
        seen_by_client INTEGER NOT NULL DEFAULT 0,
        payment_reminder INTEGER NOT NULL DEFAULT 0,
        paid INTEGER NOT NULL DEFAULT 0,
        paid_at TEXT,
        seen_at TEXT,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(invoice_id) REFERENCES invoices(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS ksef_documents(
        invoice_id INTEGER PRIMARY KEY,
        status TEXT NOT NULL DEFAULT 'draft',
        ksef_number TEXT,
        xml_path TEXT,
        last_error TEXT,
        validated_at TEXT,
        sent_at TEXT,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(invoice_id) REFERENCES invoices(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS cash_flow_settings(
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS cash_flow_expenses(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        expense_date TEXT NOT NULL,
        category TEXT NOT NULL,
        description TEXT,
        document_no TEXT NOT NULL,
        amount REAL NOT NULL CHECK(amount > 0),
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS invoice_allocations(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        invoice_id INTEGER NOT NULL,
        order_id INTEGER NOT NULL,
        order_item_id INTEGER NOT NULL,
        product_id INTEGER,
        sku TEXT,
        qty INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(invoice_id) REFERENCES invoices(id),
        FOREIGN KEY(order_id) REFERENCES orders(id),
        FOREIGN KEY(order_item_id) REFERENCES order_items(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS client_search_logs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_email TEXT,
        customer_name TEXT,
        query TEXT NOT NULL,
        product_sku TEXT,
        product_model TEXT,
        product_name TEXT,
        results_count INTEGER NOT NULL DEFAULT 0,
        source TEXT,
        created_at TEXT NOT NULL
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS email_events(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_key TEXT UNIQUE,
        event_type TEXT NOT NULL,
        ref_id TEXT,
        recipient TEXT,
        ok INTEGER NOT NULL DEFAULT 0,
        result_json TEXT,
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("PRAGMA table_info(client_search_logs)")
    search_cols = {r["name"] for r in cur.fetchall()}
    if "product_sku" not in search_cols:
        cur.execute("ALTER TABLE client_search_logs ADD COLUMN product_sku TEXT")
    if "product_model" not in search_cols:
        cur.execute("ALTER TABLE client_search_logs ADD COLUMN product_model TEXT")
    if "product_name" not in search_cols:
        cur.execute("ALTER TABLE client_search_logs ADD COLUMN product_name TEXT")

    # migracja: pierwsze wersje magazynu miały w products tylko SKU, EAN i nazwę.
    # Potwierdzenie e-mail korzysta również z modelu przy awaryjnym odczycie ceny.
    cur.execute("PRAGMA table_info(products)")
    product_cols = {r[1] for r in cur.fetchall()}
    if "model" not in product_cols:
        cur.execute("ALTER TABLE products ADD COLUMN model TEXT")
    if "archived" not in product_cols:
        cur.execute("ALTER TABLE products ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")

    cur.execute("PRAGMA table_info(company_profile)")
    company_cols = {r[1] for r in cur.fetchall()}
    if "bank_swift" not in company_cols:
        cur.execute("ALTER TABLE company_profile ADD COLUMN bank_swift TEXT")

    cur.execute("PRAGMA table_info(china_packages)")
    china_package_cols = {r[1] for r in cur.fetchall()}
    if "cost_amount" not in china_package_cols:
        cur.execute("ALTER TABLE china_packages ADD COLUMN cost_amount REAL NOT NULL DEFAULT 0")
    if "cost_document_no" not in china_package_cols:
        cur.execute("ALTER TABLE china_packages ADD COLUMN cost_document_no TEXT")

    cur.execute("PRAGMA table_info(invoice_meta)")
    invoice_meta_cols = {r[1] for r in cur.fetchall()}
    if "seen_by_client" not in invoice_meta_cols:
        cur.execute("ALTER TABLE invoice_meta ADD COLUMN seen_by_client INTEGER NOT NULL DEFAULT 0")
    if "seen_at" not in invoice_meta_cols:
        cur.execute("ALTER TABLE invoice_meta ADD COLUMN seen_at TEXT")
    if "payment_reminder" not in invoice_meta_cols:
        cur.execute("ALTER TABLE invoice_meta ADD COLUMN payment_reminder INTEGER NOT NULL DEFAULT 0")
    if "paid" not in invoice_meta_cols:
        cur.execute("ALTER TABLE invoice_meta ADD COLUMN paid INTEGER NOT NULL DEFAULT 0")
    if "paid_at" not in invoice_meta_cols:
        cur.execute("ALTER TABLE invoice_meta ADD COLUMN paid_at TEXT")

    # migracja: starsze bazy mogÄ… nie mieÄ‡ kolumny NIP u klientĂłw
    cur.execute("PRAGMA table_info(customers)")
    customer_cols = {r[1] for r in cur.fetchall()}
    if "nip" not in customer_cols:
        cur.execute("ALTER TABLE customers ADD COLUMN nip TEXT")
    if "language" not in customer_cols:
        cur.execute("ALTER TABLE customers ADD COLUMN language TEXT NOT NULL DEFAULT 'pl'")
    if "price_list" not in customer_cols:
        cur.execute("ALTER TABLE customers ADD COLUMN price_list TEXT NOT NULL DEFAULT 'pln'")
    cur.execute("""
      UPDATE customers
      SET language='pl'
      WHERE language IS NULL OR LOWER(TRIM(language)) NOT IN ('pl','de','en','es','it')
    """)
    cur.execute("""
      UPDATE customers
      SET price_list='pln'
      WHERE price_list IS NULL OR LOWER(TRIM(price_list)) NOT IN ('pln','eu_eur')
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS pricing_eur(
        sku TEXT PRIMARY KEY,
        ean TEXT,
        price_eur REAL NOT NULL DEFAULT 0,
        uvp_eur REAL NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """)

    # migracja: QR zamĂłwieĹ„
    cur.execute("PRAGMA table_info(orders)")
    order_cols = {r[1] for r in cur.fetchall()}
    if "qr_data_url" not in order_cols:
        cur.execute("ALTER TABLE orders ADD COLUMN qr_data_url TEXT")
    if "warehouse_issued" not in order_cols:
        cur.execute("ALTER TABLE orders ADD COLUMN warehouse_issued INTEGER NOT NULL DEFAULT 0")
    if "idempotency_key" not in order_cols:
        cur.execute("ALTER TABLE orders ADD COLUMN idempotency_key TEXT")
    if "currency" not in order_cols:
        cur.execute("ALTER TABLE orders ADD COLUMN currency TEXT NOT NULL DEFAULT 'PLN'")
    if "price_list" not in order_cols:
        cur.execute("ALTER TABLE orders ADD COLUMN price_list TEXT NOT NULL DEFAULT 'pln'")
    if "tracking_no" not in order_cols:
        cur.execute("ALTER TABLE orders ADD COLUMN tracking_no TEXT")
    if "carrier" not in order_cols:
        cur.execute("ALTER TABLE orders ADD COLUMN carrier TEXT")
    if "packed_at" not in order_cols:
        cur.execute("ALTER TABLE orders ADD COLUMN packed_at TEXT")
    if "shipped_at" not in order_cols:
        cur.execute("ALTER TABLE orders ADD COLUMN shipped_at TEXT")

    cur.execute("PRAGMA table_info(order_items)")
    order_item_cols = {r[1] for r in cur.fetchall()}
    if "unit_net_price" not in order_item_cols:
        cur.execute("ALTER TABLE order_items ADD COLUMN unit_net_price REAL")
    if "unit_gross_price" not in order_item_cols:
        cur.execute("ALTER TABLE order_items ADD COLUMN unit_gross_price REAL")
    if "unit_retail_price" not in order_item_cols:
        cur.execute("ALTER TABLE order_items ADD COLUMN unit_retail_price REAL")
    if "currency" not in order_item_cols:
        cur.execute("ALTER TABLE order_items ADD COLUMN currency TEXT")

    # UĹ‚atwia agregowanie "w dostawie" po statusach paczek
    cur.execute("CREATE INDEX IF NOT EXISTS idx_order_items_order_id ON order_items(order_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_order_items_product_id ON order_items(product_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_orders_status_issued_created ON orders(status, warehouse_issued, created_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_china_packages_status ON china_packages(status)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_china_items_package_id ON china_items(package_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_china_items_product_id ON china_items(product_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_client_search_logs_created ON client_search_logs(created_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_client_search_logs_email_query ON client_search_logs(customer_email, query)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_client_search_logs_model ON client_search_logs(product_model)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_pricing_model_norm ON pricing(TRIM(LOWER(model)))")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_pricing_eur_sku_norm ON pricing_eur(TRIM(LOWER(sku)))")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_email_events_key ON email_events(event_key)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_email_events_type_created ON email_events(event_type, created_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_cash_flow_expenses_date ON cash_flow_expenses(expense_date)")
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_idempotency_key ON orders(idempotency_key) WHERE idempotency_key IS NOT NULL")

    c.commit()
    c.close()

init_db()


# =========================
# UTILS
# =========================

APP_TZ = ZoneInfo("Europe/Warsaw") if ZoneInfo else None

def app_now():
    return datetime.now(APP_TZ) if APP_TZ else datetime.now()

def now_iso():
    return app_now().strftime("%Y-%m-%d %H:%M:%S")


def overdue_invoice_rows(db_conn, *, current_time=None):
    """Widok zaleglosci oparty na wspolnej logice modulu Cash flow."""
    result = cash_flow_overdue_invoices(
        db_conn, current_time=current_time or app_now()
    )
    for invoice in result:
        invoice["order_display"] = order_display_no(
            invoice.get("source_order_id"), invoice.get("source_order_created_at"),
            invoice.get("source_order_no"), invoice.get("source_order_note")
        ) if invoice.get("source_order_id") else "-"
    return result

SHORT_ORDER_NO_RE = re.compile(r"^ZAM-(\d{6})(\d+)$", re.I)


def order_date_code(created_at: str | None = "") -> str:
    created = norm(created_at)
    if len(created) >= 10 and created[4:5] == "-" and created[7:8] == "-":
        return created[2:4] + created[5:7] + created[8:10]
    return app_now().strftime("%y%m%d")


def is_short_order_no(value: str | None) -> bool:
    return bool(SHORT_ORDER_NO_RE.match(norm(value)))


def make_order_no(order_id: int | None = None, created_at: str | None = "") -> str:
    # Format: ZAM-2607141 = ZAM- + YYMMDD + kolejny numer w danym dniu.
    date_code = order_date_code(created_at)
    day = norm(created_at)[:10] if norm(created_at) else app_now().strftime("%Y-%m-%d")
    seq = 1
    try:
        c = conn()
        cur = c.cursor()
        if order_id:
            cur.execute("SELECT order_no FROM orders WHERE substr(created_at,1,10)=? AND id<>?", (day, int(order_id)))
        else:
            cur.execute("SELECT order_no FROM orders WHERE substr(created_at,1,10)=?", (day,))
        for r in cur.fetchall():
            raw = norm(r["order_no"])
            m = SHORT_ORDER_NO_RE.match(raw)
            if m and m.group(1) == date_code:
                seq = max(seq, int(m.group(2)) + 1)
            elif raw and raw.upper() != "TEMP":
                seq += 1
        c.close()
    except Exception:
        oid = int(order_id or 1)
        seq = max(1, oid)
    return f"ZAM-{date_code}{seq}"


def canonical_order_no(order_id: int | None, created_at: str | None = "", raw_order_no: str | None = "") -> str:
    raw = norm(raw_order_no)
    if raw and raw.upper() != "TEMP":
        if raw.startswith("ORD-"):
            return "ZAM-" + raw[4:]
        return raw

    return make_order_no(order_id, created_at)


def order_display_no(order_id: int | None, created_at: str | None = "", raw_order_no: str | None = "", note: str | None = "") -> str:
    base = canonical_order_no(order_id, created_at, raw_order_no)
    note_text = norm(note)
    return f"{base} {note_text}" if note_text else base


def normalize_temp_order_numbers():
    c = conn()
    cur = c.cursor()
    cur.execute("SELECT id, order_no, created_at FROM orders ORDER BY created_at, id")
    rows = cur.fetchall()
    changed = []
    used_seq_by_date = {}
    for r in rows:
        raw = norm(r["order_no"])
        m = SHORT_ORDER_NO_RE.match(raw)
        if m:
            used_seq_by_date[m.group(1)] = max(used_seq_by_date.get(m.group(1), 0), int(m.group(2)))

    for r in rows:
        raw = norm(r["order_no"])
        if is_short_order_no(raw):
            continue
        date_code = order_date_code(r["created_at"])
        used_seq_by_date[date_code] = used_seq_by_date.get(date_code, 0) + 1
        new_no = f"ZAM-{date_code}{used_seq_by_date[date_code]}"
        if new_no != (r["order_no"] or ""):
            cur.execute("UPDATE orders SET order_no=? WHERE id=?", (new_no, r["id"]))
            changed.append((int(r["id"]), new_no))
    c.commit()
    c.close()

    if supabase_enabled():
        for oid, ono in changed:
            try:
                supabase_update_rows("orders", {"order_no": ono}, {"id": oid})
            except Exception:
                pass
    return len(changed)

def _email_key(value: str) -> str:
    return norm(value).strip().lower()

def _order_name_is_fallback(order_name: str, email_value: str) -> bool:
    email_key = _email_key(email_value)
    if not email_key:
        return False
    local_part = email_key.split("@")[0]
    current = norm(order_name).strip().lower()
    return current in {"", email_key, local_part}

def link_orders_to_customers_by_email(sync_remote: bool = True):
    c = conn()
    cur = c.cursor()

    cur.execute("""
      SELECT id, name, address, phone, email
      FROM customers
      WHERE TRIM(COALESCE(email, '')) <> ''
      ORDER BY id
    """)
    customer_rows = [dict(r) for r in cur.fetchall()]
    customers_by_email = {_email_key(r["email"]): r for r in customer_rows if _email_key(r.get("email"))}

    cur.execute("""
      SELECT id, customer_id, customer_name, customer_address, customer_phone, customer_email
      FROM orders
      WHERE TRIM(COALESCE(customer_email, '')) <> ''
      ORDER BY id
    """)
    order_rows = [dict(r) for r in cur.fetchall()]

    changed = []
    for order_row in order_rows:
        email_key = _email_key(order_row.get("customer_email"))
        customer = customers_by_email.get(email_key)
        if not customer:
            continue

        updates = {}
        if int(order_row.get("customer_id") or 0) != int(customer["id"]):
            updates["customer_id"] = int(customer["id"])

        if _order_name_is_fallback(order_row.get("customer_name"), order_row.get("customer_email")) and norm(customer.get("name")):
            updates["customer_name"] = norm(customer.get("name"))

        if not norm(order_row.get("customer_address")) and norm(customer.get("address")):
            updates["customer_address"] = norm(customer.get("address"))

        if not norm(order_row.get("customer_phone")) and norm(customer.get("phone")):
            updates["customer_phone"] = norm(customer.get("phone"))

        if not norm(order_row.get("customer_email")) and norm(customer.get("email")):
            updates["customer_email"] = norm(customer.get("email"))

        if not updates:
            continue

        sets = ", ".join([f"{k}=?" for k in updates.keys()])
        values = list(updates.values()) + [int(order_row["id"])]
        cur.execute(f"UPDATE orders SET {sets} WHERE id=?", values)
        changed.append((int(order_row["id"]), updates))

    c.commit()
    c.close()

    if sync_remote and supabase_enabled():
        for order_id, updates in changed:
            try:
                supabase_update_rows("orders", updates, {"id": order_id})
            except Exception:
                pass

    return len(changed)


def make_qr_data_url(value: str) -> str:
    raw_value = norm(value)
    if not raw_value:
        return ""
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=1
    )
    qr.add_data(raw_value)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def next_invoice_no(issue_date: str) -> str:
    dt = datetime.strptime(issue_date, "%Y-%m-%d")
    mm = dt.strftime("%m")
    yyyy = dt.strftime("%Y")
    c = conn()
    cur = c.cursor()
    cur.execute("SELECT COUNT(*) AS n FROM invoices WHERE substr(issue_date,1,7)=?", (f"{yyyy}-{mm}",))
    n = int(cur.fetchone()["n"] or 0) + 1
    c.close()
    return f"FVAT {n}/{mm}/{yyyy}"


def invoice_no_exists(invoice_no: str, exclude_invoice_id: int = 0) -> int:
    invoice_no = norm(invoice_no)
    if not invoice_no:
        return 0
    c = conn()
    cur = c.cursor()
    cur.execute("""
      SELECT id
      FROM invoices
      WHERE lower(trim(invoice_no)) = lower(trim(?))
        AND id <> ?
      LIMIT 1
    """, (invoice_no, int(exclude_invoice_id or 0)))
    row = cur.fetchone()
    c.close()
    return int(row["id"]) if row else 0


def split_address(addr: str):
    raw = (addr or "").strip()
    if not raw:
        return "", "", ""

    # wspieraj adres w wielu liniach oraz jednoliniowy (np. "ul. X 1, 00-001 Warszawa")
    parts = [x.strip() for x in raw.splitlines() if x.strip()]
    if len(parts) == 1 and "," in raw:
        comma_parts = [x.strip() for x in raw.split(",") if x.strip()]
        if len(comma_parts) >= 2:
            parts = [comma_parts[0], " ".join(comma_parts[1:])]

    street = parts[0] if parts else ""
    post_code = ""
    city = ""
    if len(parts) > 1:
        line2 = parts[1].strip()
        m = re.match(r"^(\d{2}-\d{3})\s*(.*)$", line2)
        if m:
            post_code = m.group(1).strip()
            city = m.group(2).strip()
        else:
            pc = line2.split(" ", 1)
            post_code = pc[0].strip() if pc else ""
            city = pc[1].strip() if len(pc) > 1 else ""
    return street, post_code, city


def payment_type_pl(x: str) -> str:
    v = norm(x).lower()
    mapping = {
        "cash": "gotĂłwka",
        "gotowka": "gotĂłwka",
        "transfer": "przelew",
        "card": "karta",
        "karta": "karta",
    }
    return mapping.get(v, v or "-")


VAT_23 = Decimal("0.23")
MONEY_Q = Decimal("0.01")
CURRENT_ORDER_STATUSES = {
    "new", "pending", "unconfirmed", "confirmed", "packed", "packed_partial",
    "in_delivery", "shipped", "partially_shipped",
}


def money_dec(value) -> Decimal:
    try:
        return Decimal(str(value or "0")).quantize(MONEY_Q, rounding=ROUND_HALF_UP)
    except Exception:
        return Decimal("0.00")


def money_float(value) -> float:
    return float(money_dec(value))


def vat23_from_net(net_value) -> Decimal:
    return (money_dec(net_value) * VAT_23).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def gross_from_net_23(net_value) -> Decimal:
    net = money_dec(net_value)
    return (net + vat23_from_net(net)).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def find_logo_path() -> str:
    search_dirs = [
        APP_DIR,
        os.path.join(APP_DIR, "static"),
        DATA_DIR,
    ]
    for folder in search_dirs:
        for fn in ("logo.png", "logo.jpg", "logo.jpeg", "logo.webp"):
            pth = os.path.join(folder, fn)
            if os.path.exists(pth):
                return pth
    return ""


def to_int(x, default=0):
    try:
        return int(str(x).strip())
    except:
        return default

def to_float(x, default=0.0):
    try:
        return float(str(x).strip().replace(" ", "").replace(",", "."))
    except:
        return default

def norm(s):
    if s is None:
        return ""
    return str(s).strip()

def order_status_label(status: str) -> str:
    v = norm(status).lower()
    mapping = {
        "new": "Niepotwierdzone",
        "pending": "Niepotwierdzone",
        "unconfirmed": "Niepotwierdzone",
        "confirmed": "Potwierdzone",
        "packed": "W trakcie pakowania / czeka na kuriera",
        "packed_partial": "Pakowanie częściowej wysyłki",
        "in_delivery": "W dostawie",
        "shipped": "Wysłane",
        "partially_shipped": "Wysłane częściowo",
        "issued": "W realizacji",
        "completed": "Zrealizowane",
    }
    return mapping.get(v, status or "-")

def order_status_css(status: str) -> str:
    v = norm(status).lower()
    mapping = {
        "new": "st-unconfirmed",
        "pending": "st-unconfirmed",
        "unconfirmed": "st-unconfirmed",
        "confirmed": "st-confirmed",
        "packed": "st-delivery",
        "packed_partial": "st-delivery",
        "in_delivery": "st-delivery",
        "shipped": "st-confirmed",
        "partially_shipped": "st-delivery",
        "issued": "st-delivery",
        "completed": "st-issued",
    }
    return mapping.get(v, "")

def carrier_tracking_url(carrier: str, tracking_no: str) -> str:
    carrier_key = norm(carrier).lower()
    number = re.sub(r"\s+", "", norm(tracking_no))
    encoded = urllib.parse.quote(number)
    bases = {
        "inpost": "https://inpost.pl/sledzenie-przesylek?number=",
        "dpd": "https://tracktrace.dpd.com.pl/parcelDetails?p1=",
        "fedex": "https://www.fedex.com/fedextrack/?trknbr=",
        "dhl": "https://www.dhl.com/pl-pl/home/tracking.html?tracking-id=",
        "ups": "https://www.ups.com/track?tracknum=",
    }
    return bases.get(carrier_key, "https://t.17track.net/en#nums=") + encoded

def guess_col(headers, candidates):
    h = [x.strip().lower() for x in headers]
    for cand in candidates:
        cand = cand.lower()
        if cand in h:
            return h.index(cand)
    # luĹşne dopasowanie: np. "model" w "Model uchwytu"
    for i, col in enumerate(h):
        for cand in candidates:
            if cand.lower() in col:
                return i
    return None

def ensure_stock_row(product_id):
    c = conn()
    cur = c.cursor()
    cur.execute("INSERT OR IGNORE INTO stock(product_id, qty) VALUES (?, 0)", (product_id,))
    c.commit()
    c.close()


# =========================
# SUPABASE (cloud sync)
# =========================

SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "https://qfzawzkynmqkbjlbtkjd.supabase.co").strip().rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "").strip()
CLIENT_ALLOWED_ORIGINS = {
    value.strip().rstrip("/")
    for value in os.environ.get("CLIENT_ALLOWED_ORIGINS", "").split(",")
    if value.strip()
}
CLIENT_PANEL_URL = (
    os.environ.get("CLIENT_PANEL_URL")
    or "https://panel-klienta-niedzwieccy.netlify.app"
).strip().rstrip("/")
ADMIN_ACTION_TOKEN = os.environ.get("ADMIN_ACTION_TOKEN", "").strip()
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin").strip()
ADMIN_PASSWORD_HASH = os.environ.get("ADMIN_PASSWORD_HASH", "").strip()
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "").strip()
if not SUPABASE_SERVICE_ROLE_KEY:
    app.logger.error("Brak SUPABASE_SERVICE_ROLE_KEY; funkcje synchronizacji i zamówień klienta będą niedostępne.")
if not CLIENT_ALLOWED_ORIGINS:
    app.logger.warning("CLIENT_ALLOWED_ORIGINS jest puste; przeglądarkowe żądania tworzenia zamówień będą odrzucane.")
SUPABASE_STORAGE_BUCKET = (os.environ.get("SUPABASE_STORAGE_BUCKET") or "invoice-pdfs").strip()
SUPABASE_AUTO_SYNC_ON_WRITE = (os.environ.get("SUPABASE_AUTO_SYNC_ON_WRITE") or "1").strip().lower() in ("1", "true", "yes", "on")
SUPABASE_MIN_SYNC_INTERVAL_SEC = float((os.environ.get("SUPABASE_MIN_SYNC_INTERVAL_SEC") or "2").strip())
SUPABASE_MIN_PULL_INTERVAL_SEC = float((os.environ.get("SUPABASE_MIN_PULL_INTERVAL_SEC") or "2").strip())

SUPABASE_SYNC_TABLES = [
    ("products", "id"),
    ("stock", "product_id"),
    ("customers", "id"),
    ("orders", "id"),
    ("order_items", "id"),
    ("china_packages", "id"),
    ("china_items", "id"),
    ("pricing", "model"),
    ("pricing_eur", "sku"),
    ("company_profile", "id"),
    ("invoices", "id"),
    ("invoice_meta", "invoice_id"),
    ("invoice_allocations", "id"),
    ("ksef_documents", "invoice_id"),
    ("cash_flow_settings", "key"),
    ("cash_flow_expenses", "id"),
]

# KolejnoĹ›Ä‡ PULL jest waĹĽna: najpierw rodzice, potem dzieci.
SUPABASE_PULL_TABLES = [
    ("company_profile", "id"),
    ("pricing", "model"),
    ("pricing_eur", "sku"),
    ("customers", "id"),
    ("products", "id"),
    ("orders", "id"),
    ("china_packages", "id"),
    ("stock", "product_id"),
    ("order_items", "id"),
    ("china_items", "id"),
    ("invoices", "id"),
    ("invoice_meta", "invoice_id"),
    ("invoice_allocations", "id"),
    ("ksef_documents", "invoice_id"),
    ("cash_flow_settings", "key"),
    ("cash_flow_expenses", "id"),
]

_supabase_sync_lock = threading.Lock()
_supabase_sync_state = {
    "running": False,
    "last_started_ts": 0.0,
    "last_result": None,
}

def supabase_enabled() -> bool:
    return bool(SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY)

def _chunks(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]

def supabase_upsert_rows(table: str, rows: list, on_conflict: str):
    if not rows:
        return
    if not supabase_enabled():
        raise RuntimeError("Brak konfiguracji SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY")

    qs = urllib.parse.urlencode({"on_conflict": on_conflict})
    url = f"{SUPABASE_URL}/rest/v1/{table}?{qs}"
    payload = json.dumps(rows, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("apikey", SUPABASE_SERVICE_ROLE_KEY)
    req.add_header("Authorization", f"Bearer {SUPABASE_SERVICE_ROLE_KEY}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Prefer", "resolution=merge-duplicates,return=minimal")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            if resp.status >= 300:
                raise RuntimeError(f"Supabase HTTP {resp.status}")
    except urllib.error.HTTPError as exc:
        raw_error = exc.read().decode("utf-8", errors="replace")[:1200]
        try:
            error_payload = json.loads(raw_error)
            detail = norm(
                error_payload.get("message")
                or error_payload.get("details")
                or error_payload.get("hint")
                or raw_error
            )
        except Exception:
            detail = norm(raw_error)
        raise RuntimeError(f"Supabase HTTP {exc.code}: {detail or 'brak szczegółów'}") from exc

def sqlite_table_rows(table: str):
    c = conn()
    cur = c.cursor()
    cur.execute(f"SELECT * FROM {table}")
    rows = [dict(r) for r in cur.fetchall()]
    c.close()

    return rows

def sync_all_to_supabase():
    if not supabase_enabled():
        return {"ok": False, "error": "Brak konfiguracji SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY"}

    out = {"ok": True, "tables": {}, "synced_at": now_iso()}
    for table, conflict_col in SUPABASE_SYNC_TABLES:
        try:
            rows = sqlite_table_rows(table)
            for pack in _chunks(rows, 500):
                supabase_upsert_rows(table, pack, conflict_col)
            out["tables"][table] = {"rows": len(rows), "status": "ok"}
        except Exception as e:
            out["ok"] = False
            out["tables"][table] = {"status": "error", "error": str(e)}
    return out

def trigger_background_supabase_sync(reason: str = "write"):
    if not SUPABASE_AUTO_SYNC_ON_WRITE:
        return False, "disabled"
    if not supabase_enabled():
        return False, "not_configured"

    now_ts = time.time()
    with _supabase_sync_lock:
        if _supabase_sync_state["running"]:
            return False, "already_running"
        if (now_ts - float(_supabase_sync_state["last_started_ts"])) < SUPABASE_MIN_SYNC_INTERVAL_SEC:
            return False, "throttled"
        _supabase_sync_state["running"] = True
        _supabase_sync_state["last_started_ts"] = now_ts

    def _job():
        try:
            result = sync_all_to_supabase()
            result["reason"] = reason
        except Exception as e:
            result = {"ok": False, "error": str(e), "reason": reason, "synced_at": now_iso()}
        finally:
            with _supabase_sync_lock:
                _supabase_sync_state["running"] = False
                _supabase_sync_state["last_result"] = result

    th = threading.Thread(target=_job, daemon=True)
    th.start()
    return True, "started"



def supabase_request(path: str, method: str = "GET", params: dict | None = None, payload=None, prefer: str | None = None, timeout: int = 60):
    if not supabase_enabled():
        raise RuntimeError("Brak konfiguracji SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY")

    url = f"{SUPABASE_URL}{path}"
    if params:
        qs = urllib.parse.urlencode(params, doseq=True)
        url = f"{url}?{qs}"

    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("apikey", SUPABASE_SERVICE_ROLE_KEY)
    req.add_header("Authorization", f"Bearer {SUPABASE_SERVICE_ROLE_KEY}")
    req.add_header("Content-Type", "application/json")
    if prefer:
        req.add_header("Prefer", prefer)

    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if not raw:
            return None
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if "application/json" in ctype or raw[:1] in (b"[", b"{"):
            return json.loads(raw.decode("utf-8"))
        return raw.decode("utf-8", errors="replace")


def supabase_storage_ref(object_path: str, bucket: str | None = None) -> str:
    bucket = bucket or SUPABASE_STORAGE_BUCKET
    return f"supabase://{bucket}/{object_path.lstrip('/')}"


def parse_supabase_storage_ref(value: str) -> tuple[str, str] | None:
    raw = norm(value)
    if not raw.startswith("supabase://"):
        return None
    rest = raw[len("supabase://"):]
    if "/" not in rest:
        return None
    bucket, object_path = rest.split("/", 1)
    return bucket, object_path


def supabase_storage_object_url(bucket: str, object_path: str) -> str:
    quoted_path = urllib.parse.quote(object_path.lstrip("/"), safe="/")
    return f"{SUPABASE_URL}/storage/v1/object/{urllib.parse.quote(bucket, safe='')}/{quoted_path}"


def ensure_supabase_storage_bucket(bucket: str | None = None):
    bucket = bucket or SUPABASE_STORAGE_BUCKET
    if not supabase_enabled() or not bucket:
        return
    payload = json.dumps({"id": bucket, "name": bucket, "public": False}).encode("utf-8")
    req = urllib.request.Request(f"{SUPABASE_URL}/storage/v1/bucket", data=payload, method="POST")
    req.add_header("apikey", SUPABASE_SERVICE_ROLE_KEY)
    req.add_header("Authorization", f"Bearer {SUPABASE_SERVICE_ROLE_KEY}")
    req.add_header("Content-Type", "application/json")
    try:
        urllib.request.urlopen(req, timeout=30).read()
    except urllib.error.HTTPError as e:
        if e.code not in (400, 409):
            raise


def supabase_storage_upload_file(local_path: str, object_path: str, bucket: str | None = None, content_type: str = "application/pdf") -> str:
    if not supabase_enabled():
        raise RuntimeError("Brak konfiguracji Supabase")
    bucket = bucket or SUPABASE_STORAGE_BUCKET
    ensure_supabase_storage_bucket(bucket)
    with open(local_path, "rb") as f:
        data = f.read()
    req = urllib.request.Request(supabase_storage_object_url(bucket, object_path), data=data, method="POST")
    req.add_header("apikey", SUPABASE_SERVICE_ROLE_KEY)
    req.add_header("Authorization", f"Bearer {SUPABASE_SERVICE_ROLE_KEY}")
    req.add_header("Content-Type", content_type)
    req.add_header("x-upsert", "true")
    with urllib.request.urlopen(req, timeout=90) as resp:
        resp.read()
    return supabase_storage_ref(object_path, bucket)


def supabase_storage_download_bytes(storage_ref: str) -> tuple[bytes, str]:
    parsed = parse_supabase_storage_ref(storage_ref)
    if not parsed:
        raise RuntimeError("Nieprawidłowa ścieżka Supabase Storage")
    bucket, object_path = parsed
    req = urllib.request.Request(supabase_storage_object_url(bucket, object_path), method="GET")
    req.add_header("apikey", SUPABASE_SERVICE_ROLE_KEY)
    req.add_header("Authorization", f"Bearer {SUPABASE_SERVICE_ROLE_KEY}")
    with urllib.request.urlopen(req, timeout=90) as resp:
        return resp.read(), os.path.basename(object_path)


def supabase_insert_row(table: str, row: dict):
    res = supabase_request(
        f"/rest/v1/{table}",
        method="POST",
        payload=[row],
        prefer="return=representation",
    )
    if isinstance(res, list):
        return res[0] if res else None
    return res


def supabase_update_rows(table: str, values: dict, filters: dict):
    params = {k: f"eq.{v}" for k, v in filters.items()}
    return supabase_request(
        f"/rest/v1/{table}",
        method="PATCH",
        params=params,
        payload=values,
        prefer="return=minimal",
    )


def supabase_delete_rows(table: str, filters: dict):
    params = {k: f"eq.{v}" for k, v in filters.items()}
    return supabase_request(
        f"/rest/v1/{table}",
        method="DELETE",
        params=params,
        prefer="return=minimal",
    )


def supabase_select_rows(table: str, order_by: str = "id", page_size: int = 1000, extra_params: dict | None = None):
    rows = []
    offset = 0
    while True:
        params = {"select": "*", "limit": page_size, "offset": offset}
        if order_by:
            params["order"] = f"{order_by}.asc"
        if extra_params:
            params.update(extra_params)
        chunk = supabase_request(f"/rest/v1/{table}", method="GET", params=params) or []
        if not isinstance(chunk, list):
            raise RuntimeError(f"NieprawidĹ‚owa odpowiedĹş Supabase dla tabeli {table}")
        rows.extend(chunk)
        if len(chunk) < page_size:
            break
        offset += page_size
    return rows


def local_client_search_rows(limit: int = 5000):
    c = conn()
    cur = c.cursor()
    cur.execute("""
      SELECT customer_email, customer_name, query, product_sku, product_model, product_name, results_count, source, created_at
      FROM client_search_logs
      ORDER BY created_at DESC, id DESC
      LIMIT ?
    """, (limit,))
    rows = [dict(r) for r in cur.fetchall()]
    c.close()
    return rows


def supabase_client_search_rows(limit: int = 5000):
    if not supabase_enabled():
        return []
    rows = supabase_request(
        "/rest/v1/client_search_logs",
        method="GET",
        params={
            "select": "customer_email,customer_name,query,product_sku,product_model,product_name,results_count,source,created_at",
            "order": "created_at.desc",
            "limit": str(limit),
        },
        timeout=30,
    ) or []
    return rows if isinstance(rows, list) else []


def load_client_search_rows(limit: int = 5000):
    local_rows = local_client_search_rows(limit=limit)
    cloud_rows = []
    cloud_ok = False
    if supabase_enabled():
        try:
            cloud_rows = supabase_client_search_rows(limit=limit)
            cloud_ok = True
        except Exception:
            cloud_rows = []

    merged = []
    seen = set()
    for row in list(cloud_rows) + list(local_rows):
        cleaned = {
            "customer_email": norm((row or {}).get("customer_email")).lower(),
            "customer_name": norm((row or {}).get("customer_name")),
            "query": norm((row or {}).get("query")),
            "product_sku": norm((row or {}).get("product_sku")),
            "product_model": norm((row or {}).get("product_model")),
            "product_name": norm((row or {}).get("product_name")),
            "results_count": to_int((row or {}).get("results_count"), 0),
            "source": norm((row or {}).get("source")) or "stock",
            "created_at": norm((row or {}).get("created_at")),
        }
        if not cleaned["query"]:
            continue
        key = (
            cleaned["customer_email"],
            cleaned["customer_name"],
            cleaned["query"].lower(),
            cleaned["product_sku"].lower(),
            cleaned["product_model"].lower(),
            cleaned["product_name"].lower(),
            cleaned["results_count"],
            cleaned["source"],
            cleaned["created_at"],
        )
        if key in seen:
            continue
        seen.add(key)
        merged.append(cleaned)

    merged.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    source_label = "Supabase + kopia lokalna" if cloud_ok else "Kopia lokalna"
    return merged[:limit], source_label


def save_client_search_log_local(row: dict):
    c = conn()
    cur = c.cursor()
    cur.execute("""
      INSERT INTO client_search_logs(customer_email, customer_name, query, product_sku, product_model, product_name, results_count, source, created_at)
      VALUES(?,?,?,?,?,?,?,?,?)
    """, (
        row.get("customer_email", ""),
        row.get("customer_name", ""),
        row.get("query", ""),
        row.get("product_sku", ""),
        row.get("product_model", ""),
        row.get("product_name", ""),
        to_int(row.get("results_count"), 0),
        row.get("source", "stock"),
        row.get("created_at") or now_iso(),
    ))
    c.commit()
    c.close()


def save_client_search_log_supabase(row: dict) -> bool:
    if not supabase_enabled():
        return False
    payload = {
        "customer_email": row.get("customer_email", ""),
        "customer_name": row.get("customer_name", ""),
        "query": row.get("query", ""),
        "product_sku": row.get("product_sku", ""),
        "product_model": row.get("product_model", ""),
        "product_name": row.get("product_name", ""),
        "results_count": to_int(row.get("results_count"), 0),
        "source": row.get("source", "stock"),
        "created_at": row.get("created_at") or now_iso(),
    }
    supabase_insert_row("client_search_logs", payload)
    return True


def sqlite_table_columns(table: str):
    c = conn()
    cur = c.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    cols = [r[1] for r in cur.fetchall()]
    c.close()
    return cols


def sqlite_upsert_rows(table: str, rows: list, conflict_col: str):
    if not rows:
        return 0

    table_cols = sqlite_table_columns(table)
    usable_cols = [c for c in table_cols if any(c in row for row in rows)]
    if not usable_cols:
        return 0

    placeholders = ",".join(["?"] * len(usable_cols))
    update_cols = [c for c in usable_cols if c != conflict_col]
    if update_cols:
        update_sql = ", ".join([f"{c}=excluded.{c}" for c in update_cols])
        sql = f"INSERT INTO {table}({','.join(usable_cols)}) VALUES({placeholders}) ON CONFLICT({conflict_col}) DO UPDATE SET {update_sql}"
    else:
        sql = f"INSERT INTO {table}({','.join(usable_cols)}) VALUES({placeholders}) ON CONFLICT({conflict_col}) DO NOTHING"

    c = conn()
    cur = c.cursor()
    cnt = 0
    for row in rows:
        values = [row.get(col) for col in usable_cols]
        cur.execute(sql, values)
        cnt += 1
    c.commit()
    c.close()
    return cnt


def sqlite_delete_missing_rows(table: str, conflict_col: str, remote_keys: list):
    c = conn()
    cur = c.cursor()
    if not remote_keys:
        cur.execute(f"DELETE FROM {table}")
        deleted = cur.rowcount if cur.rowcount is not None else 0
        c.commit()
        c.close()
        return deleted

    cur.execute(f"SELECT {conflict_col} FROM {table}")
    local_keys = [r[0] for r in cur.fetchall()]
    remote_set = {str(x) for x in remote_keys}
    to_delete = [x for x in local_keys if str(x) not in remote_set]
    deleted = 0
    if to_delete:
        for i in range(0, len(to_delete), 800):
            pack = to_delete[i:i+800]
            ph = ",".join(["?"] * len(pack))
            cur.execute(f"DELETE FROM {table} WHERE {conflict_col} IN ({ph})", tuple(pack))
            deleted += cur.rowcount if cur.rowcount is not None else 0
    c.commit()
    c.close()
    return deleted


def pull_shared_tables_from_supabase(force: bool = False):
    if not supabase_enabled():
        return {"ok": False, "error": "not_configured"}

    now_ts = time.time()
    with _supabase_sync_lock:
        last_started = float(_supabase_sync_state.get("last_pull_started_ts") or 0.0)
        if (not force) and (now_ts - last_started) < SUPABASE_MIN_PULL_INTERVAL_SEC:
            return {"ok": True, "status": "throttled"}
        _supabase_sync_state["last_pull_started_ts"] = now_ts

    result = {"ok": True, "tables": {}, "pulled_at": now_iso()}
    fetched = {}

    for table, conflict_col in SUPABASE_PULL_TABLES:
        try:
            fetched[(table, conflict_col)] = supabase_select_rows(table, order_by=conflict_col)
        except Exception as e:
            result["ok"] = False
            result["tables"][table] = {"status": "error", "stage": "fetch", "error": str(e)}

    for table, conflict_col in SUPABASE_PULL_TABLES:
        if (table, conflict_col) not in fetched:
            continue
        try:
            remote_rows = fetched[(table, conflict_col)]
            sqlite_upsert_rows(table, remote_rows, conflict_col)
            result["tables"].setdefault(table, {})["rows"] = len(remote_rows)
            result["tables"][table]["upsert"] = "ok"
        except Exception as e:
            result["ok"] = False
            result["tables"].setdefault(table, {})
            result["tables"][table].update({"status": "error", "stage": "upsert", "error": str(e)})

    for table, conflict_col in reversed(SUPABASE_PULL_TABLES):
        if (table, conflict_col) not in fetched:
            continue
        if table == "ksef_documents":
            result["tables"].setdefault(table, {})
            result["tables"][table]["deleted_local"] = 0
            if result["tables"][table].get("upsert") == "ok":
                result["tables"][table]["status"] = "ok"
            continue
        try:
            remote_rows = fetched[(table, conflict_col)]
            remote_keys = [row.get(conflict_col) for row in remote_rows if row.get(conflict_col) is not None]
            deleted = sqlite_delete_missing_rows(table, conflict_col, remote_keys)
            result["tables"].setdefault(table, {})
            result["tables"][table]["deleted_local"] = deleted
            if result["tables"][table].get("upsert") == "ok":
                result["tables"][table]["status"] = "ok"
        except Exception as e:
            result["ok"] = False
            result["tables"].setdefault(table, {})
            result["tables"][table].update({"status": "error", "stage": "cleanup", "error": str(e)})

    try:
        normalize_temp_order_numbers()
        link_orders_to_customers_by_email(sync_remote=True)
    except Exception:
        pass
    return result


def maybe_pull_shared_from_supabase(force: bool = False):
    try:
        # `force=True` jest używane również przez chronione akcje POST
        # (np. ponowna wysyłka potwierdzenia po restarcie Rendera).
        if force or request.method == "GET":
            pull_shared_tables_from_supabase(force=force)
            # Starsze wersje zapisywały każdą rozpoczętą wysyłkę jako
            # ``shipped``. Po pobraniu danych napraw status na podstawie
            # faktycznie zrealizowanych pozycji, aby panel klienta nie
            # sugerował wysłania całego zamówienia.
            reconcile_legacy_shipped_order_statuses()
            # Status płatności mógł zostać zapisany przed dodaniem automatycznego
            # zamykania zamówień. Przeliczenie jest idempotentne i uzupełnia
            # wyłącznie zaległe statusy na podstawie istniejących faktur.
            reconcile_paid_order_statuses()
            # Historycznych faktur zbiorczych nie da się zawsze jednoznacznie
            # rozdzielić na zamówienia. Zgodnie z regułą biznesową wszystkie
            # nieanulowane zamówienia starsze niż 14 dni uznajemy za zakończone.
            reconcile_legacy_orders_by_age()
    except Exception:
        pass


def sync_local_rows_to_supabase(table: str, conflict_col: str, ids: list):
    ids = [x for x in ids if x is not None]
    if not ids or not supabase_enabled():
        return 0

    c = conn()
    cur = c.cursor()
    ph = ",".join(["?"] * len(ids))
    cur.execute(f"SELECT * FROM {table} WHERE {conflict_col} IN ({ph})", tuple(ids))
    rows = [dict(r) for r in cur.fetchall()]
    c.close()
    if rows:
        supabase_upsert_rows(table, rows, conflict_col)
    return len(rows)


def sync_local_table_to_supabase(table: str, conflict_col: str, chunk_size: int = 250):
    """Synchronizuj całą tabelę partiami, bez przekraczania limitu żądania."""
    if not supabase_enabled():
        return 0
    rows = sqlite_table_rows(table)
    for pack in _chunks(rows, max(1, int(chunk_size or 250))):
        supabase_upsert_rows(table, pack, conflict_col)
    return len(rows)


def sync_order_to_supabase(order_id: int):
    sync_local_rows_to_supabase("orders", "id", [order_id])
    c = conn()
    cur = c.cursor()
    cur.execute("SELECT id FROM order_items WHERE order_id=?", (order_id,))
    item_ids = [int(r["id"]) for r in cur.fetchall()]
    c.close()
    if item_ids:
        sync_local_rows_to_supabase("order_items", "id", item_ids)


def remote_first_create_customer(
    name: str,
    address: str,
    phone: str,
    email: str,
    nip: str,
    language: str = "pl",
    price_list: str = "pln",
):
    language = normalize_client_language(language)
    price_list = normalize_client_price_list(price_list)
    created = supabase_insert_row("customers", {
        "name": name,
        "address": address,
        "phone": phone,
        "email": email,
        "nip": nip,
        "language": language,
        "price_list": price_list,
        "created_at": now_iso(),
    })
    if not created or "id" not in created:
        raise RuntimeError("Supabase nie zwrĂłciĹ‚ ID dla klienta")

    customer_id = int(created["id"])
    c = conn()
    cur = c.cursor()
    cur.execute(
        "INSERT INTO customers(id, name, address, phone, email, nip, language, price_list, created_at) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name, address=excluded.address, phone=excluded.phone, email=excluded.email, nip=excluded.nip, language=excluded.language, price_list=excluded.price_list, created_at=excluded.created_at",
        (customer_id, name, address, phone, email, nip, language, price_list, created.get("created_at") or now_iso())
    )
    c.commit()
    c.close()
    return customer_id


def remote_first_create_order(
    customer_id,
    customer_name,
    customer_address,
    customer_phone,
    customer_email,
    note,
    items,
    idempotency_key=None,
    price_list="pln",
    currency="PLN",
):
    created_at = now_iso()
    price_list = normalize_client_price_list(price_list)
    currency = normalize_order_currency(currency or price_list_currency(price_list))
    order_payload = {
        "order_no": "TEMP",
        "customer_id": customer_id if customer_id else None,
        "customer_name": customer_name,
        "customer_address": customer_address,
        "customer_phone": customer_phone,
        "customer_email": customer_email,
        "status": "new",
        "note": note,
        "created_at": created_at,
        "qr_data_url": "",
        "price_list": price_list,
        "currency": currency,
    }
    if idempotency_key:
        order_payload["idempotency_key"] = idempotency_key
    created_order = supabase_insert_row("orders", order_payload)
    if not created_order or "id" not in created_order:
        raise RuntimeError("Supabase nie zwrĂłciĹ‚ ID dla zamĂłwienia")

    order_id = int(created_order["id"])
    order_no = make_order_no(order_id, created_at)
    qr_data_url = ""
    supabase_update_rows("orders", {"order_no": order_no, "qr_data_url": qr_data_url}, {"id": order_id})

    c = conn()
    try:
        cur = c.cursor()
        cur.execute(
            "INSERT INTO orders(id, order_no, customer_id, customer_name, customer_address, customer_phone, customer_email, status, note, created_at, qr_data_url, idempotency_key, price_list, currency) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET order_no=excluded.order_no, customer_id=excluded.customer_id, customer_name=excluded.customer_name, customer_address=excluded.customer_address, customer_phone=excluded.customer_phone, customer_email=excluded.customer_email, status=excluded.status, note=excluded.note, created_at=excluded.created_at, qr_data_url=excluded.qr_data_url, idempotency_key=excluded.idempotency_key, price_list=excluded.price_list, currency=excluded.currency",
            (order_id, order_no, customer_id if customer_id else None, customer_name, customer_address, customer_phone, customer_email, "new", note, created_at, qr_data_url, idempotency_key, price_list, currency)
        )

        for raw_item in items:
            if isinstance(raw_item, dict):
                pid = to_int(raw_item.get("product_id"), 0)
                qty = to_int(raw_item.get("qty"), 0)
                unit_net_price = money_float(raw_item.get("unit_net_price"))
                unit_gross_price = money_float(raw_item.get("unit_gross_price"))
                unit_retail_price = money_float(raw_item.get("unit_retail_price"))
                item_currency = normalize_order_currency(raw_item.get("currency") or currency)
            else:
                pid, qty = raw_item[:2]
                unit_net_price = unit_gross_price = unit_retail_price = None
                item_currency = currency
            cur.execute("SELECT sku FROM products WHERE id=?", (pid,))
            p = cur.fetchone()
            if not p:
                raise ValueError(f"Nie istnieje produkt ID {pid}")
            item_payload = {
                "order_id": order_id,
                "product_id": pid,
                "sku": p["sku"],
                "qty": qty,
                "created_at": now_iso(),
                "currency": item_currency,
            }
            if unit_net_price is not None:
                item_payload.update({
                    "unit_net_price": unit_net_price,
                    "unit_gross_price": unit_gross_price,
                    "unit_retail_price": unit_retail_price,
                })
            created_item = supabase_insert_row("order_items", item_payload)
            if not created_item or "id" not in created_item:
                raise RuntimeError("Supabase nie zwrócił ID dla pozycji zamówienia")
            cur.execute(
                "INSERT INTO order_items(id, order_id, product_id, sku, qty, unit_net_price, unit_gross_price, unit_retail_price, currency, created_at) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET order_id=excluded.order_id, product_id=excluded.product_id, sku=excluded.sku, qty=excluded.qty, unit_net_price=excluded.unit_net_price, unit_gross_price=excluded.unit_gross_price, unit_retail_price=excluded.unit_retail_price, currency=excluded.currency, created_at=excluded.created_at",
                (int(created_item["id"]), order_id, pid, p["sku"], qty, unit_net_price, unit_gross_price, unit_retail_price, item_currency, created_item.get("created_at") or now_iso())
            )
        c.commit()
    except Exception:
        c.rollback()
        try:
            supabase_delete_rows("order_items", {"order_id": order_id})
            supabase_delete_rows("orders", {"id": order_id})
        except Exception as rollback_exc:
            app.logger.error("Niepełny rollback zamówienia order_id=%s: %s", order_id, rollback_exc)
        raise
    finally:
        c.close()
    try:
        normalize_temp_order_numbers()
    except Exception:
        pass
    return order_id


def get_stock(product_id):
    c = conn()
    cur = c.cursor()
    cur.execute("SELECT qty FROM stock WHERE product_id=?", (product_id,))
    r = cur.fetchone()
    c.close()
    return int(r["qty"]) if r else 0

def change_stock(product_id, delta):
    c = conn()
    cur = c.cursor()
    cur.execute("INSERT OR IGNORE INTO stock(product_id, qty) VALUES (?, 0)", (product_id,))
    cur.execute("UPDATE stock SET qty = qty + ? WHERE product_id=?", (delta, product_id))
    c.commit()
    c.close()

def safe_filename(s):
    s = re.sub(r"[^a-zA-Z0-9_\-\.]+", "_", s)
    return s[:80] if s else "file"


def invoice_dir_for_customer(customer_name: str) -> str:
    root = os.path.join(DATA_DIR, "faktury")
    os.makedirs(root, exist_ok=True)
    customer_dir = os.path.join(root, safe_filename(customer_name or "klient"))
    os.makedirs(customer_dir, exist_ok=True)
    return customer_dir


def get_pdf_font_names():
    regular = "Helvetica"
    bold = "Helvetica-Bold"

    # Szukaj czcionek Unicode takĹĽe po wildcardach i lokalnym katalogu app/fonts.
    regular_candidates = [
        # Lokalne fonty aplikacji (najwyĹĽszy priorytet)
        ("AppFont-Regular", os.path.join(APP_DIR, "fonts", "regular.ttf")),
        ("AppFontRoot-Regular", os.path.join(APP_DIR, "regular.ttf")),

        # Linux
        ("DejaVuSans", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        ("DejaVuSansCondensed", "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed.ttf"),
        ("LiberationSans", "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
        ("NotoSans", "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf"),

        # Windows
        ("Arial", r"C:\Windows\Fonts\arial.ttf"),
        ("Calibri", r"C:\Windows\Fonts\calibri.ttf"),
        ("Tahoma", r"C:\Windows\Fonts\tahoma.ttf"),

        # macOS
        ("ArialMT", "/System/Library/Fonts/Supplemental/Arial.ttf"),
        ("HelveticaNeue", "/System/Library/Fonts/Helvetica.ttc"),
    ]
    bold_candidates = [
        ("AppFont-Bold", os.path.join(APP_DIR, "fonts", "bold.ttf")),
        ("AppFontRoot-Bold", os.path.join(APP_DIR, "bold.ttf")),

        # Linux
        ("DejaVuSans-Bold", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        ("DejaVuSansCondensed-Bold", "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf"),
        ("LiberationSans-Bold", "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"),
        ("NotoSans-Bold", "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf"),

        # Windows
        ("Arial-Bold", r"C:\Windows\Fonts\arialbd.ttf"),
        ("Calibri-Bold", r"C:\Windows\Fonts\calibrib.ttf"),
        ("Tahoma-Bold", r"C:\Windows\Fonts\tahomabd.ttf"),

        # macOS
        ("Arial-BoldMT", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
    ]

    # Dodatkowe wildcardy gdy Ĺ›cieĹĽki systemowe rĂłĹĽniÄ… siÄ™ miÄ™dzy maszynami.
    for path in glob.glob('/usr/share/fonts/**/*DejaVuSans*.ttf', recursive=True)[:6]:
        regular_candidates.append((f"AutoReg-{safe_filename(os.path.basename(path))}", path))
    for path in glob.glob('/usr/share/fonts/**/*NotoSans*.ttf', recursive=True)[:6]:
        regular_candidates.append((f"AutoReg-{safe_filename(os.path.basename(path))}", path))
    for path in glob.glob('/usr/share/fonts/**/*LiberationSans*.ttf', recursive=True)[:6]:
        regular_candidates.append((f"AutoReg-{safe_filename(os.path.basename(path))}", path))

    for path in glob.glob('/usr/share/fonts/**/*Bold*.ttf', recursive=True)[:10]:
        bold_candidates.append((f"AutoBold-{safe_filename(os.path.basename(path))}", path))

    def register_first(candidates):
        for name, path in candidates:
            if not path or not os.path.exists(path):
                continue
            try:
                if name not in pdfmetrics.getRegisteredFontNames():
                    pdfmetrics.registerFont(TTFont(name, path))
                return name
            except Exception:
                continue
        return None

    reg = register_first(regular_candidates)
    bld = register_first(bold_candidates)

    if reg:
        regular = reg
    if bld:
        bold = bld
    elif reg:
        bold = reg

    return regular, bold


CLIENT_LANGUAGES = {"pl", "de", "en", "es", "it"}
CLIENT_PRICE_LISTS = {"pln", "eu_eur"}

ORDER_PDF_TRANSLATIONS = {
    "pl": {
        "title": "ZAMÓWIENIE", "order_number": "Numer zamówienia", "date": "Data",
        "customer": "Klient", "delivery_address": "Adres dostawy", "status": "Status",
        "email": "E-mail", "phone": "Telefon", "notes": "Uwagi klienta",
        "sku": "SKU", "product": "Model / nazwa", "quantity": "Ilość",
        "unit_net": "Cena jedn. netto", "line_net": "Wartość netto",
        "unit_gross": "Cena jedn. brutto", "line_gross": "Wartość brutto",
        "net_total": "Razem netto", "vat": "VAT", "gross_total": "Razem brutto",
        "currency": "PLN", "page": "Strona",
        "status_unconfirmed": "Niepotwierdzone", "status_confirmed": "Potwierdzone",
        "status_in_delivery": "W dostawie", "status_completed": "Zrealizowane",
        "status_cancelled": "Anulowane", "status_other": "Inny",
    },
    "de": {
        "title": "BESTELLUNG", "order_number": "Bestellnummer", "date": "Datum",
        "customer": "Kunde", "delivery_address": "Lieferadresse", "status": "Status",
        "email": "E-Mail", "phone": "Telefon", "notes": "Kundenhinweise",
        "sku": "SKU", "product": "Modell / Bezeichnung", "quantity": "Menge",
        "unit_net": "Nettostückpreis", "line_net": "Nettowert",
        "unit_gross": "Bruttostückpreis", "line_gross": "Bruttowert",
        "net_total": "Nettobetrag", "vat": "MwSt.", "gross_total": "Gesamtbetrag",
        "currency": "PLN", "page": "Seite",
        "status_unconfirmed": "Unbestätigt", "status_confirmed": "Bestätigt",
        "status_in_delivery": "In Lieferung", "status_completed": "Abgeschlossen",
        "status_cancelled": "Storniert", "status_other": "Sonstiger",
    },
    "en": {
        "title": "ORDER", "order_number": "Order number", "date": "Date",
        "customer": "Customer", "delivery_address": "Delivery address", "status": "Status",
        "email": "Email", "phone": "Phone", "notes": "Customer notes",
        "sku": "SKU", "product": "Model / product", "quantity": "Quantity",
        "unit_net": "Net unit price", "line_net": "Net value",
        "unit_gross": "Gross unit price", "line_gross": "Gross value",
        "net_total": "Net total", "vat": "VAT", "gross_total": "Gross total",
        "currency": "PLN", "page": "Page",
        "status_unconfirmed": "Unconfirmed", "status_confirmed": "Confirmed",
        "status_in_delivery": "In delivery", "status_completed": "Completed",
        "status_cancelled": "Cancelled", "status_other": "Other",
    },
    "es": {
        "title": "PEDIDO", "order_number": "Número de pedido", "date": "Fecha",
        "customer": "Cliente", "delivery_address": "Dirección de entrega", "status": "Estado",
        "email": "Correo electrónico", "phone": "Teléfono", "notes": "Notas del cliente",
        "sku": "SKU", "product": "Modelo / producto", "quantity": "Cantidad",
        "unit_net": "Precio unitario neto", "line_net": "Importe neto",
        "unit_gross": "Precio unitario bruto", "line_gross": "Importe bruto",
        "net_total": "Total neto", "vat": "IVA", "gross_total": "Total bruto",
        "currency": "PLN", "page": "Página",
        "status_unconfirmed": "Sin confirmar", "status_confirmed": "Confirmado",
        "status_in_delivery": "En entrega", "status_completed": "Completado",
        "status_cancelled": "Cancelado", "status_other": "Otro",
    },
    "it": {
        "title": "ORDINE", "order_number": "Numero ordine", "date": "Data",
        "customer": "Cliente", "delivery_address": "Indirizzo di consegna", "status": "Stato",
        "email": "E-mail", "phone": "Telefono", "notes": "Note del cliente",
        "sku": "SKU", "product": "Modello / prodotto", "quantity": "Quantità",
        "unit_net": "Prezzo unitario netto", "line_net": "Valore netto",
        "unit_gross": "Prezzo unitario lordo", "line_gross": "Valore lordo",
        "net_total": "Totale netto", "vat": "IVA", "gross_total": "Totale lordo",
        "currency": "PLN", "page": "Pagina",
        "status_unconfirmed": "Non confermato", "status_confirmed": "Confermato",
        "status_in_delivery": "In consegna", "status_completed": "Completato",
        "status_cancelled": "Annullato", "status_other": "Altro",
    },
}

PACKING_LIST_TRANSLATIONS = {
    "pl": {
        "title": "LISTA PAKOWANIA", "invoice": "Faktura", "continued": "dalszy ciąg",
        "order": "ZAMÓWIENIE", "date": "DATA", "customer": "KLIENT", "checked": "✓",
        "line": "LP.", "product": "MODEL / NAZWA", "source": "ZAMÓWIENIE / NOTATKA",
        "quantity": "ILOŚĆ", "positions": "Pozycje", "total_qty": "Razem sztuk",
        "packages": "Liczba pudełek", "packed_by": "Spakował(a)", "signature": "Podpis",
    },
    "de": {
        "title": "PACKLISTE", "invoice": "Rechnung", "continued": "Fortsetzung",
        "order": "BESTELLUNG", "date": "DATUM", "customer": "KUNDE", "checked": "✓",
        "line": "POS.", "product": "MODELL / BEZEICHNUNG", "source": "BESTELLUNG / NOTIZ",
        "quantity": "MENGE", "positions": "Positionen", "total_qty": "Gesamtmenge",
        "packages": "Anzahl Pakete", "packed_by": "Gepackt von", "signature": "Unterschrift",
    },
    "en": {
        "title": "PACKING LIST", "invoice": "Invoice", "continued": "continued",
        "order": "ORDER", "date": "DATE", "customer": "CUSTOMER", "checked": "✓",
        "line": "NO.", "product": "MODEL / PRODUCT", "source": "ORDER / NOTE",
        "quantity": "QTY", "positions": "Items", "total_qty": "Total quantity",
        "packages": "Packages", "packed_by": "Packed by", "signature": "Signature",
    },
    "es": {
        "title": "LISTA DE EMBALAJE", "invoice": "Factura", "continued": "continuación",
        "order": "PEDIDO", "date": "FECHA", "customer": "CLIENTE", "checked": "✓",
        "line": "N.º", "product": "MODELO / PRODUCTO", "source": "PEDIDO / NOTA",
        "quantity": "CANT.", "positions": "Artículos", "total_qty": "Cantidad total",
        "packages": "Número de paquetes", "packed_by": "Preparado por", "signature": "Firma",
    },
    "it": {
        "title": "LISTA DI IMBALLAGGIO", "invoice": "Fattura", "continued": "continua",
        "order": "ORDINE", "date": "DATA", "customer": "CLIENTE", "checked": "✓",
        "line": "N.", "product": "MODELLO / PRODOTTO", "source": "ORDINE / NOTA",
        "quantity": "Q.TÀ", "positions": "Articoli", "total_qty": "Quantità totale",
        "packages": "Numero colli", "packed_by": "Preparato da", "signature": "Firma",
    },
}


def normalize_client_language(value) -> str:
    language = norm(value).lower()
    return language if language in CLIENT_LANGUAGES else "pl"


def normalize_client_price_list(value) -> str:
    price_list = norm(value).lower()
    return price_list if price_list in CLIENT_PRICE_LISTS else "pln"


def price_list_for_language(language) -> str:
    """Polish accounts use PLN; every supported foreign language uses EUR."""
    return "pln" if normalize_client_language(language) == "pl" else "eu_eur"


def price_list_currency(value) -> str:
    return "EUR" if normalize_client_price_list(value) == "eu_eur" else "PLN"


def normalize_order_currency(value) -> str:
    return "EUR" if norm(value).upper() == "EUR" else "PLN"


def order_pdf_text(language: str, key: str) -> str:
    language = normalize_client_language(language)
    return ORDER_PDF_TRANSLATIONS.get(language, {}).get(key) or ORDER_PDF_TRANSLATIONS["pl"].get(key, "")


def packing_list_text(language: str, key: str) -> str:
    language = normalize_client_language(language)
    return PACKING_LIST_TRANSLATIONS.get(language, {}).get(key) or PACKING_LIST_TRANSLATIONS["pl"].get(key, "")


def localized_pdf_money(value, language: str) -> str:
    amount = money_dec(value)
    raw = f"{amount:,.2f}"
    if normalize_client_language(language) == "en":
        return raw
    return raw.replace(",", "\u0000").replace(".", ",").replace("\u0000", " ")


def localized_pdf_date(value, language: str) -> str:
    raw = norm(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except Exception:
        try:
            parsed = datetime.strptime(raw[:10], "%Y-%m-%d")
        except Exception:
            return norm(value)
    if normalize_client_language(language) in {"es", "en"}:
        return parsed.strftime("%d/%m/%Y")
    return parsed.strftime("%d.%m.%Y")


def order_pdf_status(status, language: str) -> str:
    key = norm(status).lower()
    if key in {"new", "pending", "unconfirmed"}:
        label_key = "status_unconfirmed"
    elif key in {"confirmed", "packed", "packed_partial", "issued"}:
        label_key = "status_confirmed"
    elif key in {"in_delivery", "shipped", "partially_shipped"}:
        label_key = "status_in_delivery"
    elif key == "completed":
        label_key = "status_completed"
    elif key == "cancelled":
        label_key = "status_cancelled"
    else:
        label_key = "status_other"
    return order_pdf_text(language, label_key)


def generate_client_order_pdf(
    order_row: dict,
    items: list[dict],
    language: str,
    retail_prices: bool = False,
) -> tuple[io.BytesIO, str]:
    """Create one localized order PDF without persisting a second document copy."""
    language = normalize_client_language(language)
    currency = normalize_order_currency(order_row.get("currency"))
    regular_font, bold_font = get_pdf_font_names()
    page_width, page_height = 210 * mm, 297 * mm
    left, right = 15 * mm, 195 * mm
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(page_width, page_height))
    # Wersja przyjazna drukarkom laserowym: wysoki kontrast, neutralne szarosci
    # i grubsze linie zamiast delikatnych ekranowych odcieni niebieskiego.
    ink = (0.0, 0.0, 0.0)
    muted_ink = (0.12, 0.12, 0.12)
    pale_gray = (0.91, 0.91, 0.91)
    rule_gray = (0.38, 0.38, 0.38)

    c = conn()
    try:
        company_row = c.execute("SELECT * FROM company_profile WHERE id=1").fetchone()
        company = dict(company_row) if company_row else {}
    finally:
        c.close()

    def txt(value) -> str:
        return fix_polish_mojibake(norm(value))

    def fit(value, font_name, size, max_width) -> str:
        value = txt(value)
        if pdfmetrics.stringWidth(value, font_name, size) <= max_width:
            return value
        suffix = "..."
        while value and pdfmetrics.stringWidth(value + suffix, font_name, size) > max_width:
            value = value[:-1]
        return value + suffix if value else ""

    def wrap(value, font_name, size, max_width, max_lines=3):
        words = txt(value).split()
        lines, current = [], ""
        for word in words:
            candidate = word if not current else f"{current} {word}"
            if pdfmetrics.stringWidth(candidate, font_name, size) <= max_width:
                current = candidate
            else:
                if current:
                    lines.append(current)
                current = fit(word, font_name, size, max_width)
                if len(lines) >= max_lines:
                    break
        if current and len(lines) < max_lines:
            lines.append(current)
        return lines or ["-"]

    def draw_complete(value, x, y, font_name, size, max_width, min_size=6.2):
        """Draw the complete value, reducing the font instead of adding ellipsis."""
        value = txt(value)
        fitted_size = size
        while fitted_size > min_size and pdfmetrics.stringWidth(value, font_name, fitted_size) > max_width:
            fitted_size -= 0.2
        pdf.setFont(font_name, max(fitted_size, min_size))
        pdf.drawString(x, y, value)

    page_no = 0
    order_number = canonical_order_no(
        order_row.get("id"), order_row.get("created_at"), order_row.get("order_no")
    )

    def footer():
        pdf.setStrokeColorRGB(*rule_gray)
        pdf.setLineWidth(0.7)
        pdf.line(left, 12 * mm, right, 12 * mm)
        pdf.setFont(bold_font, 7.5)
        pdf.setFillColorRGB(*muted_ink)
        pdf.drawString(left, 7.5 * mm, txt(company.get("company_name") or "Niedźwieccy"))
        pdf.drawRightString(right, 7.5 * mm, f"{order_pdf_text(language, 'page')} {page_no}")

    def new_page(first=False):
        nonlocal page_no
        if not first:
            footer()
            pdf.showPage()
        page_no += 1
        # Czysty, biały nagłówek. Logo i typografia tworzą hierarchię bez
        # ciężkiego, ciemnego pasa zajmującego całą szerokość dokumentu.
        logo_path = find_logo_path()
        if logo_path:
            try:
                pdf.drawImage(ImageReader(logo_path), left, page_height - 28 * mm, 32 * mm, 20 * mm,
                              preserveAspectRatio=True, anchor="w", mask="auto")
            except Exception:
                pass
        pdf.setFillColorRGB(*ink)
        pdf.setFont(bold_font, 18)
        pdf.drawRightString(right, page_height - 15 * mm, order_pdf_text(language, "title"))
        pdf.setFillColorRGB(*muted_ink)
        pdf.setFont(bold_font, 9)
        pdf.drawRightString(right, page_height - 21 * mm, fit(order_number, regular_font, 9, 80 * mm))
        pdf.setStrokeColorRGB(*rule_gray)
        pdf.setLineWidth(0.8)
        pdf.line(left, page_height - 31 * mm, right, page_height - 31 * mm)
        return page_height - 41 * mm

    def draw_table_header(y):
        pdf.setFillColorRGB(*pale_gray)
        pdf.setStrokeColorRGB(*rule_gray)
        pdf.setLineWidth(0.8)
        pdf.roundRect(left, y - 7 * mm, right - left, 8 * mm, 2 * mm, fill=1, stroke=1)
        pdf.setFillColorRGB(*ink)
        pdf.setFont(bold_font, 8.0)
        pdf.drawString(left + 2 * mm, y - 4 * mm, order_pdf_text(language, "sku"))
        pdf.drawString(left + 43 * mm, y - 4 * mm, order_pdf_text(language, "product"))
        pdf.drawRightString(left + 122 * mm, y - 4 * mm, order_pdf_text(language, "quantity"))
        unit_price_key = "unit_gross" if retail_prices else "unit_net"
        line_value_key = "line_gross" if retail_prices else "line_net"
        pdf.drawRightString(left + 152 * mm, y - 4 * mm, order_pdf_text(language, unit_price_key))
        pdf.drawRightString(right - 2 * mm, y - 4 * mm, order_pdf_text(language, line_value_key))
        return y - 10 * mm

    y = new_page(first=True)
    pdf.setFillColorRGB(*ink)
    pdf.setFont(bold_font, 9)
    pdf.drawString(left, y, order_pdf_text(language, "order_number"))
    pdf.drawString(left + 72 * mm, y, order_pdf_text(language, "date"))
    pdf.drawString(left + 117 * mm, y, order_pdf_text(language, "status"))
    pdf.setFont(bold_font, 9.5)
    pdf.drawString(left, y - 5 * mm, fit(order_number, bold_font, 9.5, 65 * mm))
    pdf.drawString(left + 72 * mm, y - 5 * mm, localized_pdf_date(order_row.get("created_at"), language))
    pdf.drawString(left + 117 * mm, y - 5 * mm, order_pdf_status(order_row.get("status"), language))
    y -= 15 * mm

    customer_name = order_row.get("customer_name") or "-"
    customer_address = order_row.get("customer_address") or ""
    pdf.setFont(bold_font, 9)
    pdf.drawString(left, y, order_pdf_text(language, "customer"))
    pdf.drawString(left + 95 * mm, y, order_pdf_text(language, "delivery_address"))
    pdf.setFont(bold_font, 8.8)
    customer_lines = wrap(customer_name, bold_font, 8.8, 82 * mm, 2)
    contact = " - ".join(x for x in [txt(order_row.get("customer_email")), txt(order_row.get("customer_phone"))] if x)
    if contact:
        customer_lines.extend(wrap(contact, regular_font, 7.5, 82 * mm, 2))
    address_lines = wrap(customer_address or "-", regular_font, 8.5, 82 * mm, 3)
    max_lines = max(len(customer_lines), len(address_lines))
    for idx in range(max_lines):
        if idx < len(customer_lines):
            pdf.drawString(left, y - (idx + 1) * 4.5 * mm, customer_lines[idx])
        if idx < len(address_lines):
            pdf.drawString(left + 95 * mm, y - (idx + 1) * 4.5 * mm, address_lines[idx])
    y -= (max_lines + 2) * 4.5 * mm

    y = draw_table_header(y)
    total_net = Decimal("0.00")
    total_gross = Decimal("0.00")
    for item in items:
        qty = to_int(item.get("qty"), 0)
        unit_net = money_dec(item.get("net_price"))
        unit_gross = money_dec(item.get("gross_price"))
        if retail_prices:
            # Cennik detaliczny jest zawsze wyliczany od ceny netto B2B.
            # Brutto liczymy od wartości źródłowej, a kwoty zaokrąglamy dopiero
            # na wyjściu — dokładnie tak samo jak ceny detaliczne w panelu.
            retail_net = unit_net * Decimal("1.45")
            unit_net = retail_net.quantize(MONEY_Q, rounding=ROUND_HALF_UP)
            unit_gross = (retail_net * Decimal("1.23")).quantize(MONEY_Q, rounding=ROUND_HALF_UP)
        line_net = (unit_net * qty).quantize(MONEY_Q, rounding=ROUND_HALF_UP)
        line_gross = (unit_gross * qty).quantize(MONEY_Q, rounding=ROUND_HALF_UP)
        total_net += line_net
        total_gross += line_gross
        if y < 31 * mm:
            y = new_page()
            y = draw_table_header(y)
        pdf.setFillColorRGB(*ink)
        product_label = " / ".join(x for x in [txt(item.get("model")), txt(item.get("name"))] if x) or "-"
        draw_complete(item.get("sku"), left + 2 * mm, y, bold_font, 8.2, 38 * mm)
        pdf.setFont(bold_font, 8.2)
        pdf.drawString(left + 43 * mm, y, fit(product_label, bold_font, 8.2, 45 * mm))
        pdf.drawRightString(left + 122 * mm, y, str(qty))
        displayed_unit_price = unit_gross if retail_prices else unit_net
        displayed_line_value = line_gross if retail_prices else line_net
        pdf.drawRightString(left + 152 * mm, y, f"{localized_pdf_money(displayed_unit_price, language)} {currency}")
        pdf.drawRightString(right - 2 * mm, y, f"{localized_pdf_money(displayed_line_value, language)} {currency}")
        pdf.setStrokeColorRGB(*rule_gray)
        pdf.setLineWidth(0.7)
        pdf.line(left, y - 2.5 * mm, right, y - 2.5 * mm)
        y -= 7 * mm

    total_net = total_net.quantize(MONEY_Q, rounding=ROUND_HALF_UP)
    total_gross = total_gross.quantize(MONEY_Q, rounding=ROUND_HALF_UP)
    vat_value = (total_gross - total_net).quantize(MONEY_Q, rounding=ROUND_HALF_UP)
    if y < 48 * mm:
        y = new_page()
    y -= 3 * mm
    pdf.setFillColorRGB(*ink)
    totals_to_draw = (
        (("gross_total", total_gross, True),)
        if retail_prices
        else (("net_total", total_net, False), ("vat", vat_value, False), ("gross_total", total_gross, True))
    )
    for key, value, is_bold in totals_to_draw:
        pdf.setFont(bold_font if is_bold else regular_font, 10 if is_bold else 9)
        pdf.drawRightString(right, y, f"{order_pdf_text(language, key)}: {localized_pdf_money(value, language)} {currency}")
        y -= 5.5 * mm

    note = norm(order_row.get("note"))
    if note:
        if y < 34 * mm:
            y = new_page()
        pdf.setFont(bold_font, 9)
        pdf.drawString(left, y, order_pdf_text(language, "notes"))
        pdf.setFont(regular_font, 8.5)
        for line in wrap(note, regular_font, 8.5, right - left, 5):
            y -= 4.5 * mm
            pdf.drawString(left, y, line)

    footer()
    pdf.save()
    buffer.seek(0)
    pdf_variant = "_RETAIL" if retail_prices else ""
    filename = f"ORDER_{safe_filename(order_number)}{pdf_variant}_{language}.pdf"
    return buffer, filename


def generate_sales_invoice(order_row, items):
    customer_dir = invoice_dir_for_customer(order_row["customer_name"])
    fname = f"FV_{safe_filename(canonical_order_no(order_row['id'] if 'id' in order_row.keys() else None, order_row['created_at'] if 'created_at' in order_row.keys() else '', order_row['order_no']))}.pdf"
    fpath = os.path.join(customer_dir, fname)

    c = conn()
    cur = c.cursor()
    cur.execute("SELECT * FROM company_profile WHERE id=1")
    company = cur.fetchone()
    cur.execute("SELECT model, net_price, gross_price FROM pricing")
    pricing_rows = cur.fetchall()
    cur.execute("SELECT sku, model, name FROM products")
    product_rows = cur.fetchall()
    c.close()

    pricing_map = {norm(r["model"]): r for r in pricing_rows}
    product_map = {norm(r["sku"]): r for r in product_rows}

    def pdf_txt(value) -> str:
        return fix_polish_mojibake(norm(value))

    def fit_pdf_text(value, font_name, font_size, max_width, suffix="...") -> str:
        text = pdf_txt(value)
        if pdfmetrics.stringWidth(text, font_name, font_size) <= max_width:
            return text
        while text and pdfmetrics.stringWidth(text + suffix, font_name, font_size) > max_width:
            text = text[:-1]
        return (text + suffix) if text else ""

    w = 210 * mm
    h = 297 * mm
    cpdf = canvas.Canvas(fpath, pagesize=(w, h))

    pdf_font, pdf_font_bold = get_pdf_font_names()

    y = h - 18 * mm
    cpdf.setFont(pdf_font_bold, 14)
    cpdf.drawString(15 * mm, y, f"Faktura sprzedaĹĽowa: {canonical_order_no(order_row['id'] if 'id' in order_row.keys() else None, order_row['created_at'] if 'created_at' in order_row.keys() else '', order_row['order_no'])}")

    y -= 8 * mm
    cpdf.setFont(pdf_font, 10)
    cpdf.drawString(15 * mm, y, f"Data: {order_row['created_at']}")

    y -= 9 * mm
    cpdf.setFont(pdf_font_bold, 10)
    cpdf.drawString(15 * mm, y, "Sprzedawca:")
    y -= 6 * mm
    cpdf.setFont(pdf_font, 8.5)
    if company:
        cpdf.drawString(15 * mm, y, f"{company['company_name'] or '-'}")
        y -= 5 * mm
        for ln in (company["address"] or "-").splitlines():
            cpdf.drawString(15 * mm, y, ln)
            y -= 5 * mm
        cpdf.drawString(15 * mm, y, f"NIP: {company['nip'] or '-'}")
        y -= 5 * mm
        cpdf.drawString(15 * mm, y, f"Tel: {company['phone'] or '-'}  Email: {company['email'] or '-'}")
        y -= 5 * mm
        cpdf.drawString(15 * mm, y, f"Konto: {company['bank_account'] or '-'}")
    else:
        cpdf.drawString(15 * mm, y, "Brak danych firmy (uzupeĹ‚nij w zakĹ‚adce: Dane mojej firmy)")

    y -= 8 * mm
    cpdf.setFont(pdf_font_bold, 10)
    cpdf.drawString(15 * mm, y, "Nabywca:")
    y -= 6 * mm
    cpdf.setFont(pdf_font, 9)
    cpdf.drawString(15 * mm, y, f"{order_row['customer_name'] or '-'}")
    y -= 5 * mm
    for ln in (order_row["customer_address"] or "-").splitlines():
        cpdf.drawString(15 * mm, y, ln)
        y -= 5 * mm
    cpdf.drawString(15 * mm, y, f"Tel: {order_row['customer_phone'] or '-'}  Email: {order_row['customer_email'] or '-'}")

    y -= 10 * mm
    cpdf.setFont(pdf_font_bold, 9)
    cpdf.drawString(15 * mm, y, "SKU")
    cpdf.drawString(45 * mm, y, "Model")
    cpdf.drawString(95 * mm, y, "IloĹ›Ä‡")
    cpdf.drawString(112 * mm, y, "Netto/szt")
    cpdf.drawString(140 * mm, y, "Brutto/szt")
    cpdf.drawString(170 * mm, y, "WartoĹ›Ä‡ brutto")
    y -= 5 * mm

    total_net = 0.0
    total_gross = 0.0
    cpdf.setFont(pdf_font, 9)

    for it in items:
        model = norm(it["model"])
        pr = pricing_map.get(model)
        net = float(pr["net_price"]) if pr else 0.0
        gross = float(pr["gross_price"]) if pr else 0.0
        qty = int(it["qty"])
        line_net = net * qty
        line_gross = gross * qty
        total_net += line_net
        total_gross += line_gross

        cpdf.drawString(15 * mm, y, it["sku"])
        cpdf.drawString(45 * mm, y, (model or "-")[:24])
        cpdf.drawRightString(108 * mm, y, str(qty))
        cpdf.drawRightString(136 * mm, y, f"{net:.2f}")
        cpdf.drawRightString(164 * mm, y, f"{gross:.2f}")
        cpdf.drawRightString(195 * mm, y, f"{line_gross:.2f}")
        y -= 5 * mm

        if y < 28 * mm:
            cpdf.showPage()
            y = h - 20 * mm
            cpdf.setFont(pdf_font, 9)

    y -= 6 * mm
    cpdf.setFont(pdf_font_bold, 10)
    cpdf.drawRightString(195 * mm, y, f"Suma netto: {total_net:.2f} PLN")
    y -= 5 * mm
    cpdf.drawRightString(195 * mm, y, f"Suma brutto: {total_gross:.2f} PLN")

    y -= 8 * mm
    cpdf.setFont(pdf_font, 9)
    cpdf.drawString(15 * mm, y, "Ceny pobrane z zakĹ‚adki Cennik (model, netto, brutto).")

    cpdf.save()
    return fpath


def generate_order_invoice_pdf(order_row, items, meta):
    customer_dir = invoice_dir_for_customer(meta.get("buyer_name") or (order_row["customer_name"] if order_row and "customer_name" in order_row.keys() else "") or "Klient")
    fname = f"{safe_filename(meta['invoice_no'])}.pdf"
    fpath = os.path.join(customer_dir, fname)

    c = conn()
    cur = c.cursor()
    cur.execute("SELECT * FROM company_profile WHERE id=1")
    company = cur.fetchone()
    cur.execute("SELECT model, net_price, gross_price FROM pricing")
    pricing_rows = cur.fetchall()
    cur.execute("SELECT sku, model, name FROM products")
    product_rows = cur.fetchall()
    c.close()

    pricing_map = {norm(r["model"]): r for r in pricing_rows}
    product_map = {norm(r["sku"]): r for r in product_rows}

    def pdf_txt(value) -> str:
        return fix_polish_mojibake(norm(value))

    def fit_pdf_text(value, font_name, font_size, max_width, suffix="...") -> str:
        text = pdf_txt(value)
        if pdfmetrics.stringWidth(text, font_name, font_size) <= max_width:
            return text
        while text and pdfmetrics.stringWidth(text + suffix, font_name, font_size) > max_width:
            text = text[:-1]
        return (text + suffix) if text else ""

    def wrap_pdf_text(value, font_name, font_size, max_width, max_lines=None):
        text = pdf_txt(value)
        if not text:
            return []
        out = []
        for raw_line in str(text).replace("\r", "\n").split("\n"):
            words = raw_line.split()
            if not words:
                out.append("")
                continue
            line = ""
            for word in words:
                candidate = word if not line else f"{line} {word}"
                if pdfmetrics.stringWidth(candidate, font_name, font_size) <= max_width:
                    line = candidate
                    continue
                if line:
                    out.append(line)
                    line = word
                else:
                    out.append(fit_pdf_text(word, font_name, font_size, max_width))
                    line = ""
                if max_lines and len(out) >= max_lines:
                    out[-1] = fit_pdf_text(out[-1], font_name, font_size, max_width)
                    return out
            if line:
                out.append(line)
            if max_lines and len(out) >= max_lines:
                out = out[:max_lines]
                out[-1] = fit_pdf_text(out[-1], font_name, font_size, max_width)
                return out
        return out

    w = 210 * mm
    h = 297 * mm
    cpdf = canvas.Canvas(fpath, pagesize=(w, h))
    pdf_font, pdf_font_bold = get_pdf_font_names()

    header_y = h - 20 * mm
    cpdf.setFont(pdf_font_bold, 14)
    cpdf.drawString(15 * mm, header_y, f"Faktura VAT: {meta['invoice_no']}")

    y = h - 34 * mm
    logo = find_logo_path()
    if logo:
        try:
            logo_img = ImageReader(logo)
            img_w, img_h = logo_img.getSize()
            max_w = 60 * mm
            max_h = 24 * mm
            scale = min(max_w / float(img_w), max_h / float(img_h)) if img_w and img_h else 1.0
            draw_w = float(img_w) * scale
            draw_h = float(img_h) * scale
            draw_x = 195 * mm - draw_w
            draw_y = h - 10 * mm - draw_h
            cpdf.drawImage(logo_img, draw_x, draw_y, width=draw_w, height=draw_h, preserveAspectRatio=True, mask="auto")
        except Exception:
            pass

    y -= 7 * mm
    cpdf.setFont(pdf_font, 10)
    cpdf.drawString(15 * mm, y, f"Miejsce: {pdf_txt(meta.get('place') or '-')}")
    cpdf.drawString(85 * mm, y, f"Data wystawienia: {pdf_txt(meta['issue_date'])}")
    cpdf.drawString(150 * mm, y, f"Data sprzedaży: {pdf_txt(meta['sell_date'])}")

    y -= 7 * mm
    cpdf.drawString(15 * mm, y, f"Forma płatności: {pdf_txt(payment_type_pl(meta.get('payment_type')))}")
    cpdf.drawString(85 * mm, y, f"Termin płatności: {pdf_txt(meta.get('payment_to') or '-')}")

    y -= 10 * mm
    cpdf.setFont(pdf_font_bold, 10)
    cpdf.drawString(15 * mm, y, "Sprzedawca")
    cpdf.drawString(110 * mm, y, "Nabywca")

    y -= 6 * mm
    cpdf.setFont(pdf_font, 9)
    seller_name = pdf_txt((company["company_name"] if company else "") or "-")
    seller_nip = pdf_txt((company["nip"] if company else "") or "-")
    seller_addr = pdf_txt((company["address"] if company else "") or "-")
    seller_phone = pdf_txt((company["phone"] if company else "") or "")
    seller_email = pdf_txt((company["email"] if company else "") or "")
    seller_bank = pdf_txt((company["bank_account"] if company else "") or "")

    buyer_name = pdf_txt(meta.get("buyer_name") or (order_row["customer_name"] if order_row and "customer_name" in order_row.keys() else "") or "-")
    buyer_tax_no = pdf_txt(meta.get("buyer_tax_no") or "-")
    buyer_street = pdf_txt(meta.get("buyer_street") or "-")
    buyer_post = pdf_txt(meta.get("buyer_post_code") or "")
    buyer_city = pdf_txt(meta.get("buyer_city") or "")
    buyer_country = pdf_txt(meta.get("buyer_country") or "PL")
    buyer_email = pdf_txt(meta.get("buyer_email") or "")
    buyer_phone = pdf_txt(meta.get("buyer_phone") or "")

    seller_lines = [seller_name, f"NIP: {seller_nip}", seller_addr]
    if seller_phone:
        seller_lines.append(f"tel: {seller_phone}")
    if seller_email:
        seller_lines.append(f"email: {seller_email}")
    if seller_bank:
        seller_lines.append(f"konto: {seller_bank}")

    buyer_lines = [buyer_name, f"NIP: {buyer_tax_no}", buyer_street, f"{buyer_post} {buyer_city}".strip(), buyer_country]
    if buyer_phone:
        buyer_lines.append(f"tel: {buyer_phone}")
    if buyer_email:
        buyer_lines.append(f"email: {buyer_email}")

    seller_x = 15 * mm
    buyer_x = 108 * mm
    seller_width = 84 * mm
    buyer_width = 87 * mm
    line_gap = 4.8 * mm
    seller_wrapped = []
    buyer_wrapped = []
    for line in seller_lines:
        seller_wrapped.extend(wrap_pdf_text(line, pdf_font, 8.7, seller_width, max_lines=2))
    for line in buyer_lines:
        buyer_wrapped.extend(wrap_pdf_text(line, pdf_font, 8.7, buyer_width, max_lines=2))

    max_len = max(len(seller_wrapped), len(buyer_wrapped))
    cpdf.setFont(pdf_font, 8.7)
    for i in range(max_len):
        if i < len(seller_wrapped):
            cpdf.drawString(seller_x, y, seller_wrapped[i])
        if i < len(buyer_wrapped):
            cpdf.drawString(buyer_x, y, buyer_wrapped[i])
        y -= line_gap

    y -= 4 * mm
    table_left = 12 * mm
    table_right = 198 * mm
    row_h = 12 * mm
    # L.p. | Nazwa/SKU | Ilo?? | Netto/szt | Brutto/szt | Wart. netto | VAT
    col_x = [12 * mm, 20 * mm, 100 * mm, 113 * mm, 136 * mm, 159 * mm, 182 * mm, 198 * mm]

    def cell_center(x1, x2):
        return (x1 + x2) / 2.0

    def cell_baseline(y_top, h_cell, font_name, font_size):
        asc = pdfmetrics.getAscent(font_name, font_size)
        desc = pdfmetrics.getDescent(font_name, font_size)
        text_h = asc - desc
        y_bottom = y_top - h_cell + 1
        return y_bottom + (h_cell - text_h) / 2.0 - desc

    cpdf.setFillColorRGB(0.96, 0.96, 0.96)
    cpdf.rect(table_left, y - row_h + 1, table_right - table_left, row_h, stroke=0, fill=1)
    cpdf.setFillColorRGB(0, 0, 0)
    header_font = 7.6
    cpdf.setFont(pdf_font_bold, header_font)
    header_y = cell_baseline(y, row_h, pdf_font_bold, header_font)
    cpdf.drawCentredString(cell_center(col_x[0], col_x[1]), header_y, "L.p.")
    cpdf.drawCentredString(cell_center(col_x[1], col_x[2]), header_y, "Nazwa/SKU")
    cpdf.drawCentredString(cell_center(col_x[2], col_x[3]), header_y, "Ilość")
    cpdf.drawCentredString(cell_center(col_x[3], col_x[4]), header_y, "Netto/szt")
    cpdf.drawCentredString(cell_center(col_x[4], col_x[5]), header_y, "Brutto/szt")
    cpdf.drawCentredString(cell_center(col_x[5], col_x[6]), header_y, "Wartość netto")
    cpdf.drawCentredString(cell_center(col_x[6], col_x[7]), header_y, "VAT")
    cpdf.line(table_left, y + 1, table_right, y + 1)
    cpdf.line(table_left, y - row_h + 1, table_right, y - row_h + 1)
    for cx in col_x:
        cpdf.line(cx, y + 1, cx, y - row_h + 1)
    y -= row_h

    total_net = 0.0
    total_net_dec = Decimal("0.00")
    discount_pct = max(0.0, to_float(meta.get("discount_percent"), 0.0))
    body_font = 8.2
    cpdf.setFont(pdf_font, body_font)

    lp = 1
    for it in items:
        sku = pdf_txt(it.get("sku"))
        product_row = product_map.get(norm(sku))
        model = pdf_txt(it.get("model") or (product_row["model"] if product_row else ""))
        name = pdf_txt(it.get("name") or (product_row["name"] if product_row else ""))
        common_name = name or model
        pr = pricing_map.get(model) or pricing_map.get(sku)
        # Faktura musi zachowac cene zapisana w chwili jej utworzenia.
        # Aktualny cennik jest tylko awaryjnym zrodlem dla bardzo starych
        # pozycji, w ktorych ceny jeszcze nie byly utrwalane w JSON-ie.
        saved_net_price = it.get("net_price")
        if saved_net_price is None or norm(saved_net_price) == "":
            saved_net_price = pr["net_price"] if pr else 0
        net_dec = money_dec(saved_net_price)
        qty = int(it["qty"])
        line_net_dec = (net_dec * Decimal(qty)).quantize(MONEY_Q, rounding=ROUND_HALF_UP)
        if discount_pct > 0:
            line_net_dec = (line_net_dec * (Decimal("100.0") - Decimal(str(discount_pct))) / Decimal("100.0")).quantize(MONEY_Q, rounding=ROUND_HALF_UP)
        unit_gross_dec = gross_from_net_23(net_dec)

        net = money_float(net_dec)
        gross = money_float(unit_gross_dec)
        line_net = money_float(line_net_dec)

        total_net += line_net
        total_net_dec += line_net_dec

        text_y = cell_baseline(y, row_h, pdf_font, body_font)
        cpdf.drawCentredString(cell_center(col_x[0], col_x[1]), text_y, str(lp))
        name_left = col_x[1] + 1.5 * mm
        name_width = (col_x[2] - col_x[1]) - 3 * mm
        cpdf.setFont(pdf_font_bold, body_font)
        cpdf.drawString(name_left, y - 4.4 * mm, fit_pdf_text(sku or "-", pdf_font_bold, body_font, name_width))
        cpdf.setFont(pdf_font, body_font)
        if common_name:
            label = common_name if common_name.lower() == model.lower() else f"{common_name} / {model}".strip(" /")
            cpdf.drawString(name_left, y - 8.7 * mm, fit_pdf_text(label, pdf_font, body_font, name_width))
        cpdf.drawCentredString(cell_center(col_x[2], col_x[3]), text_y, str(qty))
        cpdf.drawRightString(col_x[4] - 1.5 * mm, text_y, f"{net:.2f}")
        cpdf.drawRightString(col_x[5] - 1.5 * mm, text_y, f"{gross:.2f}")
        cpdf.drawRightString(col_x[6] - 1.5 * mm, text_y, f"{line_net:.2f}")
        cpdf.drawCentredString(cell_center(col_x[6], col_x[7]), text_y, "23%")
        cpdf.line(table_left, y - row_h + 1, table_right, y - row_h + 1)
        for cx in col_x:
            cpdf.line(cx, y + 1, cx, y - row_h + 1)
        y -= row_h
        lp += 1
        if y < 26 * mm:
            cpdf.showPage()
            y = h - 20 * mm
            cpdf.setFont(pdf_font, body_font)

    total_net_dec = total_net_dec.quantize(MONEY_Q, rounding=ROUND_HALF_UP)
    total_tax_dec = vat23_from_net(total_net_dec)
    total_gross_dec = (total_net_dec + total_tax_dec).quantize(MONEY_Q, rounding=ROUND_HALF_UP)
    total_net = money_float(total_net_dec)
    total_tax = money_float(total_tax_dec)
    total_gross = money_float(total_gross_dec)
    y -= 6 * mm
    cpdf.setFont(pdf_font_bold, 10)
    if discount_pct > 0:
        cpdf.drawRightString(198 * mm, y, f"Rabat: {discount_pct:.2f}%")
        y -= 5 * mm
    cpdf.drawRightString(198 * mm, y, f"Suma netto: {total_net:.2f} PLN")
    y -= 5 * mm
    cpdf.drawRightString(198 * mm, y, f"VAT 23%: {total_tax:.2f} PLN")
    y -= 5 * mm
    cpdf.drawRightString(198 * mm, y, f"Suma brutto: {total_gross:.2f} PLN")

    ksef_number = norm(meta.get("ksef_number") or "")
    if ksef_number:
        y -= 10 * mm
        if y < 22 * mm:
            cpdf.showPage()
            y = h - 20 * mm
        cpdf.setFont(pdf_font_bold, 9)
        cpdf.drawString(15 * mm, y, "KSeF")
        y -= 5 * mm
        cpdf.setFont(pdf_font, 8.5)
        cpdf.drawString(15 * mm, y, "Faktura została wystawiona i jest dostępna w Krajowym Systemie e-Faktur.")
        y -= 5 * mm
        cpdf.setFont(pdf_font_bold, 8.5)
        cpdf.drawString(15 * mm, y, f"Numer KSeF: {pdf_txt(ksef_number)}")

    cpdf.save()
    return fpath, round(total_net,2), round(total_gross,2)


def packing_list_pdf_path_for_invoice(invoice_pdf_path: str, invoice_no: str) -> str:
    if parse_supabase_storage_ref(invoice_pdf_path):
        invoice_pdf_path = ""
    base_dir = os.path.dirname(invoice_pdf_path) if invoice_pdf_path else os.path.join(DATA_DIR, "faktury")
    return os.path.join(base_dir, f"{safe_filename(invoice_no)}_lista_pakowania.pdf")


def invoice_storage_object_path(invoice_id: int, invoice_no: str, suffix: str = ".pdf") -> str:
    return f"invoices/{int(invoice_id)}/{safe_filename(invoice_no)}{suffix}"


def invoice_packing_storage_object_path(invoice_id: int, invoice_no: str) -> str:
    return invoice_storage_object_path(invoice_id, invoice_no, "_lista_pakowania.pdf")


def upload_invoice_pdfs_to_supabase(invoice_id: int, invoice_no: str, invoice_pdf_path: str, packing_pdf_path: str = "") -> str:
    if not supabase_enabled():
        return invoice_pdf_relpath(invoice_pdf_path)
    invoice_ref = supabase_storage_upload_file(
        invoice_pdf_path,
        invoice_storage_object_path(invoice_id, invoice_no),
        content_type="application/pdf",
    )
    if packing_pdf_path and os.path.exists(packing_pdf_path):
        try:
            supabase_storage_upload_file(
                packing_pdf_path,
                invoice_packing_storage_object_path(invoice_id, invoice_no),
                content_type="application/pdf",
            )
        except Exception:
            pass
    return invoice_ref


def generate_invoice_packing_list_pdf(order_row, items, meta, invoice_pdf_path: str = "") -> str:
    customer_dir = invoice_dir_for_customer(meta.get("buyer_name") or (order_row["customer_name"] if order_row and "customer_name" in order_row.keys() else "") or "Klient")
    fpath = packing_list_pdf_path_for_invoice(invoice_pdf_path or os.path.join(customer_dir, f"{safe_filename(meta['invoice_no'])}.pdf"), meta["invoice_no"])
    w, h = 210 * mm, 297 * mm
    cpdf = canvas.Canvas(fpath, pagesize=(w, h))
    pdf_font, pdf_font_bold = get_pdf_font_names()
    # Lista pakowania jest dokumentem roboczym. Kolory są celowo dużo
    # ciemniejsze niż w ekranowych PDF-ach, aby pozostały czytelne także na
    # kolorowych drukarkach laserowych i przy druku ekonomicznym.
    navy, muted, blue = (0.0, 0.0, 0.0), (0.12, 0.12, 0.12), (0.0, 0.0, 0.0)
    pale_blue, line_color = (0.91, 0.91, 0.91), (0.38, 0.38, 0.38)

    def order_value(key, default=""):
        if not order_row:
            return default
        try:
            return order_row[key] if key in order_row.keys() else default
        except (AttributeError, KeyError, TypeError):
            return order_row.get(key, default) if isinstance(order_row, dict) else default

    def fit_text(text, max_width, font_name, font_size):
        text = norm(text or "") or "-"
        if cpdf.stringWidth(text, font_name, font_size) <= max_width:
            return text
        while text and cpdf.stringWidth(text + "...", font_name, font_size) > max_width:
            text = text[:-1]
        return (text + "...") if text else "..."

    def fit_font_size(text, max_width, font_name, preferred_size, minimum_size=6.0):
        """Zmniejsza font bez obcinania tekstu (uzywane dla numeru zamowienia)."""
        text = norm(text or "") or "-"
        size = float(preferred_size)
        while size > minimum_size and cpdf.stringWidth(text, font_name, size) > max_width:
            size -= 0.25
        return max(size, minimum_size)

    def strip_note_from_order_no(order_no, note):
        order_no, note = norm(order_no or ""), norm(note or "")
        if note and order_no.lower().endswith((" " + note).lower()):
            return order_no[:-(len(note) + 1)].strip()
        return order_no

    packing_order_numbers = []
    for item in items:
        source_no = strip_note_from_order_no(
            item.get("source_order_no"),
            item.get("source_order_note"),
        )
        if source_no and source_no not in packing_order_numbers:
            packing_order_numbers.append(source_no)

    language = normalize_client_language(meta.get("language") or order_value("language"))
    if not (meta.get("language") or order_value("language")):
        customer_email = norm(order_value("customer_email") or meta.get("buyer_email"))
        if customer_email:
            try:
                language = normalize_client_language(_client_profile_for_email(customer_email).get("language"))
            except Exception as exc:
                app.logger.warning("Nie udało się ustalić języka listy pakowania dla %s: %s", customer_email, type(exc).__name__)
    tr = lambda key: packing_list_text(language, key)

    def draw_header(continuation=False):
        logo_path = find_logo_path()
        if logo_path:
            try:
                cpdf.drawImage(ImageReader(logo_path), 15 * mm, h - 28 * mm, 32 * mm, 20 * mm,
                               preserveAspectRatio=True, anchor="w", mask="auto")
            except Exception:
                pass
        cpdf.setFillColorRGB(*navy)
        cpdf.setFont(pdf_font_bold, 18)
        cpdf.drawRightString(195 * mm, h - 15 * mm, tr("title"))
        cpdf.setFillColorRGB(*muted)
        cpdf.setFont(pdf_font_bold, 9)
        document_label_key = norm(meta.get("document_label_key")) or "invoice"
        subtitle_value = norm(meta.get("invoice_no") or "-")
        if document_label_key == "order" and packing_order_numbers:
            subtitle_value = ", ".join(packing_order_numbers)
        subtitle = f"{tr(document_label_key)}: {subtitle_value}"
        subtitle_size = fit_font_size(subtitle, 105 * mm, pdf_font_bold, 9, minimum_size=5.5)
        cpdf.setFont(pdf_font_bold, subtitle_size)
        if continuation:
            subtitle += f"  |  {tr('continued')}"
        cpdf.drawRightString(195 * mm, h - 23 * mm, subtitle)
        cpdf.setStrokeColorRGB(*line_color)
        cpdf.setLineWidth(0.8)
        cpdf.line(15 * mm, h - 31 * mm, 195 * mm, h - 31 * mm)
        return h - 41 * mm

    def draw_table_header(current_y):
        cpdf.setFillColorRGB(*pale_blue)
        cpdf.setStrokeColorRGB(*line_color)
        cpdf.setLineWidth(0.8)
        cpdf.roundRect(15 * mm, current_y - 7 * mm, 180 * mm, 10 * mm, 2 * mm, fill=1, stroke=1)
        cpdf.setFillColorRGB(*blue)
        cpdf.setFont(pdf_font_bold, 9)
        for label, x_mm in ((tr("checked"), 19), (tr("line"), 29), ("SKU", 42), (tr("product"), 91), (tr("source"), 132)):
            cpdf.drawString(x_mm * mm, current_y - 3.5 * mm, label)
        cpdf.drawRightString(190 * mm, current_y - 3.5 * mm, tr("quantity"))
        return current_y - 13 * mm

    y = draw_header()
    customer_name = norm(meta.get("buyer_name") or order_value("customer_name") or "-")
    main_order_no = ", ".join(packing_order_numbers) or norm(order_value("order_no") or order_value("number") or "-")
    # Data w naglowku pochodzi z zamowienia. Osobna data wydruku jest
    # umieszczana na dole przy polu osoby pakujacej.
    order_created_at = norm(order_value("created_at") or "")
    order_date = order_created_at[:10] if len(order_created_at) >= 10 else app_now().strftime("%Y-%m-%d")
    print_date = app_now().strftime("%Y-%m-%d")
    cpdf.setFillColorRGB(*muted)
    cpdf.setFont(pdf_font_bold, 9)
    cpdf.drawString(15 * mm, y, tr("order"))
    cpdf.drawString(72 * mm, y, tr("date"))
    cpdf.drawString(122 * mm, y, tr("customer"))
    y -= 6 * mm
    cpdf.setFillColorRGB(*navy)
    cpdf.setFont(pdf_font_bold, 10.5)
    main_order_font_size = fit_font_size(main_order_no, 50 * mm, pdf_font_bold, 10.5, minimum_size=5.5)
    cpdf.setFont(pdf_font_bold, main_order_font_size)
    cpdf.drawString(15 * mm, y, main_order_no)
    cpdf.setFont(pdf_font_bold, 10.5)
    cpdf.drawString(72 * mm, y, order_date)
    cpdf.drawString(122 * mm, y, fit_text(customer_name, 73 * mm, pdf_font, 10))
    y = draw_table_header(y - 11 * mm)

    total_qty = 0
    item_count = 0
    for it in items:
        qty = int(it.get("qty") or 0)
        if qty <= 0:
            continue
        item_count += 1
        total_qty += qty
        source_order_no = norm(it.get("source_order_no") or "")
        note = norm(it.get("source_order_note") or "")
        source_order_no = strip_note_from_order_no(source_order_no, note)
        source_text = " · ".join(x for x in (source_order_no, note) if x) or "-"
        sku = norm(it.get("sku") or "")
        model_name = norm(it.get("model") or it.get("name") or "")
        if y < 36 * mm:
            cpdf.showPage()
            y = draw_table_header(draw_header(continuation=True))
        cpdf.setStrokeColorRGB(0.05, 0.05, 0.05)
        cpdf.setLineWidth(1.2)
        cpdf.roundRect(18 * mm, y - 2.5 * mm, 5 * mm, 5 * mm, 1 * mm, fill=0, stroke=1)
        cpdf.setFillColorRGB(*navy)
        cpdf.setFont(pdf_font_bold, 9.5)
        cpdf.drawString(29 * mm, y, str(item_count))
        cpdf.setFont(pdf_font_bold, 10)
        cpdf.drawString(42 * mm, y, fit_text(sku, 44 * mm, pdf_font_bold, 10))
        cpdf.setFont(pdf_font_bold, 9.5)
        cpdf.drawString(91 * mm, y, fit_text(model_name, 36 * mm, pdf_font_bold, 9.5))
        cpdf.setFillColorRGB(*muted)
        # Numer zamowienia jest identyfikatorem roboczym i nigdy nie moze byc
        # zakonczony wielokropkiem. W razie dluzszego numeru zmniejszamy font,
        # zachowujac cala wartosc wraz z dopiskiem/notatka.
        source_font_size = fit_font_size(source_text, 43 * mm, pdf_font_bold, 8.5)
        cpdf.setFont(pdf_font_bold, source_font_size)
        cpdf.drawString(132 * mm, y, source_text)
        cpdf.setFillColorRGB(*navy)
        cpdf.setFont(pdf_font_bold, 12)
        cpdf.drawRightString(190 * mm, y, str(qty))
        cpdf.setStrokeColorRGB(*line_color)
        cpdf.setLineWidth(0.7)
        cpdf.line(15 * mm, y - 5.5 * mm, 195 * mm, y - 5.5 * mm)
        y -= 11 * mm

    if y < 50 * mm:
        cpdf.showPage()
        y = draw_header(continuation=True)
    y -= 2 * mm
    cpdf.setFillColorRGB(*pale_blue)
    cpdf.setStrokeColorRGB(*line_color)
    cpdf.setLineWidth(0.8)
    cpdf.roundRect(15 * mm, y - 13 * mm, 180 * mm, 18 * mm, 3 * mm, fill=1, stroke=1)
    cpdf.setFillColorRGB(*navy)
    cpdf.setFont(pdf_font_bold, 10)
    cpdf.drawString(21 * mm, y - 5 * mm, f"{tr('positions')}: {item_count}")
    cpdf.drawString(70 * mm, y - 5 * mm, f"{tr('total_qty')}: {total_qty}")
    cpdf.drawString(128 * mm, y - 5 * mm, f"{tr('packages')}:  ______")
    y -= 28 * mm
    cpdf.setFillColorRGB(*muted)
    cpdf.setFont(pdf_font_bold, 9)
    cpdf.drawString(15 * mm, y, f"{tr('packed_by')}:")
    cpdf.drawString(91 * mm, y, f"{tr('date').title()}:")
    cpdf.drawString(104 * mm, y, print_date)
    cpdf.drawString(145 * mm, y, f"{tr('signature')}:")
    cpdf.setStrokeColorRGB(*line_color)
    cpdf.setLineWidth(0.8)
    cpdf.line(37 * mm, y - 1 * mm, 82 * mm, y - 1 * mm)
    cpdf.line(103 * mm, y - 1 * mm, 134 * mm, y - 1 * mm)
    cpdf.line(159 * mm, y - 1 * mm, 195 * mm, y - 1 * mm)
    cpdf.save()
    return fpath


def invoice_pdf_relpath(abs_path: str) -> str:
    try:
        return os.path.relpath(abs_path, DATA_DIR)
    except Exception:
        return abs_path

def invoice_pdf_abspath(rel_path: str) -> str:
    return os.path.join(DATA_DIR, rel_path)

def find_invoice_pdf_fallback(invoice_no: str) -> str:
    root = os.path.join(DATA_DIR, "faktury")
    target = f"{safe_filename(invoice_no or '')}.pdf"
    if not target or target == ".pdf" or not os.path.isdir(root):
        return ""
    for dirpath, _, filenames in os.walk(root):
        for fn in filenames:
            if fn == target:
                return os.path.join(dirpath, fn)
    return ""

def invoice_pdf_exists(pdf_path: str, invoice_no: str = "") -> tuple[bool, str]:
    abs_path = ""
    raw_pdf = norm(pdf_path)
    if parse_supabase_storage_ref(raw_pdf):
        try:
            supabase_storage_download_bytes(raw_pdf)
            return True, raw_pdf
        except Exception:
            return False, raw_pdf
    if raw_pdf:
        abs_path = raw_pdf if os.path.isabs(raw_pdf) else invoice_pdf_abspath(raw_pdf)
    if abs_path and os.path.exists(abs_path):
        return True, abs_path
    fallback = find_invoice_pdf_fallback(invoice_no)
    if fallback and os.path.exists(fallback):
        return True, fallback
    return False, ""


def load_invoice_meta(invoice_id: int):
    c = conn()
    cur = c.cursor()
    cur.execute("SELECT * FROM invoice_meta WHERE invoice_id=?", (invoice_id,))
    row = cur.fetchone()
    c.close()
    return dict(row) if row else None

def upsert_invoice_meta(
    invoice_id: int,
    pdf_path: str = "",
    invoice_items_json: str = "",
    sent_to_client: int | None = None,
    seen_by_client: int | None = None,
    seen_at: str | None = None,
    payment_reminder: int | None = None,
    paid: int | None = None,
    paid_at: str | None = None
):
    current = load_invoice_meta(invoice_id) or {}
    if sent_to_client is None:
        sent_to_client = int(current.get("sent_to_client") or 0)
    if seen_by_client is None:
        seen_by_client = int(current.get("seen_by_client") or 0)
    if seen_at is None:
        seen_at = current.get("seen_at")
    if payment_reminder is None:
        payment_reminder = int(current.get("payment_reminder") or 0)
    if paid is None:
        paid = int(current.get("paid") or 0)
    if paid_at is None:
        paid_at = current.get("paid_at")

    c = conn()
    cur = c.cursor()
    cur.execute("""
      INSERT INTO invoice_meta(invoice_id, pdf_path, invoice_items_json, sent_to_client, seen_by_client, payment_reminder, paid, paid_at, seen_at, updated_at)
      VALUES(?,?,?,?,?,?,?,?,?,?)
      ON CONFLICT(invoice_id) DO UPDATE SET
        pdf_path=excluded.pdf_path,
        invoice_items_json=excluded.invoice_items_json,
        sent_to_client=excluded.sent_to_client,
        seen_by_client=excluded.seen_by_client,
        payment_reminder=excluded.payment_reminder,
        paid=excluded.paid,
        paid_at=excluded.paid_at,
        seen_at=excluded.seen_at,
        updated_at=excluded.updated_at
    """, (invoice_id, pdf_path, invoice_items_json, int(sent_to_client), int(seen_by_client), int(payment_reminder), int(paid), paid_at, seen_at, now_iso()))
    c.commit()
    c.close()


def sync_invoice_meta_to_supabase(invoice_id: int):
    if not supabase_enabled():
        return
    meta = load_invoice_meta(invoice_id)
    if not meta:
        return
    try:
        sync_local_rows_to_supabase("invoice_meta", "invoice_id", [invoice_id])
        return
    except Exception:
        pass

    # Fallback dla Supabase bez najnowszych kolumn payment_reminder/paid/paid_at.
    legacy = {
        "invoice_id": meta.get("invoice_id"),
        "pdf_path": meta.get("pdf_path") or "",
        "invoice_items_json": meta.get("invoice_items_json") or "",
        "sent_to_client": int(meta.get("sent_to_client") or 0),
        "seen_by_client": int(meta.get("seen_by_client") or 0),
        "seen_at": meta.get("seen_at"),
        "updated_at": meta.get("updated_at") or now_iso(),
    }
    supabase_upsert_rows("invoice_meta", [legacy], "invoice_id")

def prepare_invoice_items(order_items: list[dict], form):
    prepared = []
    for it in order_items:
        remaining_qty = int(it.get("remaining_qty") if it.get("remaining_qty") is not None else it.get("qty") or 0)
        qty = to_int(form.get(f"invoice_qty_{it['id']}"), 0)
        if qty <= 0:
            continue
        qty = min(qty, remaining_qty)
        if qty <= 0:
            continue
        row = dict(it)
        row["order_item_id"] = int(it.get("id") or 0)
        row["source_order_id"] = int(it.get("order_id") or it.get("source_order_id") or 0)
        row["source_order_no"] = it.get("source_order_no") or ""
        row["source_order_note"] = it.get("source_order_note") or ""
        row["ordered_qty"] = int(it.get("qty") or 0)
        row["invoiced_qty_before"] = int(it.get("invoiced_qty") or 0)
        row["qty"] = qty
        line_net = money_dec(row.get("net_price")) * Decimal(qty)
        line_net = line_net.quantize(MONEY_Q, rounding=ROUND_HALF_UP)
        line_vat = vat23_from_net(line_net)
        line_gross = (line_net + line_vat).quantize(MONEY_Q, rounding=ROUND_HALF_UP)
        row["gross_price"] = money_float(gross_from_net_23(row.get("net_price")))
        row["vat_rate"] = 23
        row["line_value_net"] = money_float(line_net)
        row["line_value_vat"] = money_float(line_vat)
        row["line_value_gross"] = money_float(line_gross)
        prepared.append(row)
    return prepared


def invoiced_qty_by_order_item_ids(order_item_ids: list[int]):
    ids = [int(x) for x in order_item_ids if x is not None]
    out = {x: 0 for x in ids}
    if not ids:
        return out

    c = conn()
    cur = c.cursor()
    ph = ",".join(["?"] * len(ids))
    cur.execute(f"""
      SELECT order_item_id, COALESCE(SUM(qty),0) AS qty
      FROM invoice_allocations
      WHERE order_item_id IN ({ph})
      GROUP BY order_item_id
    """, tuple(ids))
    for r in cur.fetchall():
        out[int(r["order_item_id"])] = int(r["qty"] or 0)
    c.close()
    return out


def replace_invoice_allocations(invoice_id: int, invoice_items: list[dict]):
    c = conn()
    cur = c.cursor()
    cur.execute("DELETE FROM invoice_allocations WHERE invoice_id=?", (invoice_id,))
    allocation_ids = []
    for it in invoice_items:
        order_item_id = int(it.get("order_item_id") or it.get("id") or 0)
        order_id = int(it.get("source_order_id") or it.get("order_id") or 0)
        qty = int(it.get("qty") or 0)
        if order_item_id <= 0 or order_id <= 0 or qty <= 0:
            continue
        cur.execute("""
          INSERT INTO invoice_allocations(invoice_id, order_id, order_item_id, product_id, sku, qty, created_at)
          VALUES(?,?,?,?,?,?,?)
        """, (
            invoice_id,
            order_id,
            order_item_id,
            int(it.get("product_id") or 0) or None,
            it.get("sku") or "",
            qty,
            now_iso()
        ))
        allocation_ids.append(int(cur.lastrowid))
    c.commit()
    c.close()
    return allocation_ids


def order_fully_invoiced(cur, order_id: int) -> bool:
    cur.execute("SELECT id, qty FROM order_items WHERE order_id=?", (order_id,))
    rows = [dict(r) for r in cur.fetchall()]
    if not rows:
        return False
    item_ids = [int(r["id"]) for r in rows]
    ph = ",".join(["?"] * len(item_ids))
    cur.execute(f"""
      SELECT order_item_id, COALESCE(SUM(qty),0) AS qty
      FROM invoice_allocations
      WHERE order_item_id IN ({ph})
      GROUP BY order_item_id
    """, tuple(item_ids))
    done = {int(r["order_item_id"]): int(r["qty"] or 0) for r in cur.fetchall()}
    return all(int(row["qty"] or 0) > 0 and int(done.get(int(row["id"]), 0)) >= int(row["qty"] or 0) for row in rows)


def finalize_fully_invoiced_orders(order_ids: list[int]):
    touched = sorted({int(x) for x in order_ids if x})
    if not touched:
        return [], []

    c = conn()
    cur = c.cursor()
    changed_order_ids = []
    changed_product_ids = []

    for order_id in touched:
        if not order_fully_invoiced(cur, order_id):
            continue
        cur.execute("SELECT id, status, warehouse_issued FROM orders WHERE id=?", (order_id,))
        order_row = cur.fetchone()
        if not order_row:
            continue

        warehouse_issued = int(order_row["warehouse_issued"] or 0)
        if warehouse_issued == 0:
            cur.execute("SELECT product_id, qty FROM order_items WHERE order_id=?", (order_id,))
            for it in cur.fetchall():
                pid = int(it["product_id"])
                qty = int(it["qty"] or 0)
                cur.execute("INSERT OR IGNORE INTO stock(product_id, qty) VALUES (?, 0)", (pid,))
                cur.execute("UPDATE stock SET qty = qty - ? WHERE product_id=?", (qty, pid))
                changed_product_ids.append(pid)
            warehouse_issued = 1

        current_status = norm(order_row["status"]).lower()
        # Wystawienie faktury oznacza realizację, a nie zakończenie zamówienia.
        # Status wysyłki zachowujemy; „Zrealizowane” ustawia dopiero opłacenie
        # wszystkich faktur przypisanych do zamówienia.
        preserved = {"shipped", "partially_shipped", "completed", "issued", "cancelled"}
        next_status = current_status if current_status in preserved else "packed"
        if current_status != next_status or int(order_row["warehouse_issued"] or 0) != warehouse_issued:
            if next_status == "packed":
                cur.execute("""
                  UPDATE orders
                  SET status=?, warehouse_issued=?, packed_at=COALESCE(packed_at, ?)
                  WHERE id=?
                """, (next_status, warehouse_issued, now_iso(), order_id))
            else:
                cur.execute("UPDATE orders SET status=?, warehouse_issued=? WHERE id=?", (next_status, warehouse_issued, order_id))
            changed_order_ids.append(order_id)

    c.commit()
    c.close()

    if supabase_enabled():
        if changed_order_ids:
            try:
                sync_local_rows_to_supabase("orders", "id", changed_order_ids)
            except Exception:
                pass
        if changed_product_ids:
            try:
                sync_local_rows_to_supabase("stock", "product_id", list(set(changed_product_ids)))
            except Exception:
                pass

    return changed_order_ids, list(set(changed_product_ids))


def reconcile_legacy_shipped_order_statuses():
    """Uzupełnia precyzyjny status starszych wysyłek bez zmiany dokumentów.

    Dawny kod zapisywał ``shipped`` również wtedy, gdy faktura/wysyłka
    obejmowała tylko część pozycji. Źródłem prawdy o zrealizowanych ilościach
    są istniejące ``invoice_allocations``. Rekord bez żadnej alokacji zostaje
    nietknięty, bo nie da się bezpiecznie ustalić, czy był wysłany częściowo.
    """
    c = conn()
    cur = c.cursor()
    cur.execute("SELECT id, status FROM orders WHERE LOWER(COALESCE(status,'')) IN ('shipped','partially_shipped')")
    shipped_rows = [dict(row) for row in cur.fetchall()]
    changed_to_partial = []

    for order_row in shipped_rows:
        order_id = int(order_row["id"])
        if norm(order_row.get("status")).lower() != "shipped":
            continue

        cur.execute("""
          SELECT
            COALESCE(SUM(oi.qty), 0) AS ordered_qty,
            COALESCE(SUM(MIN(oi.qty, COALESCE(done.invoiced_qty, 0))), 0) AS completed_qty
          FROM order_items oi
          LEFT JOIN (
            SELECT order_item_id, SUM(qty) AS invoiced_qty
            FROM invoice_allocations
            GROUP BY order_item_id
          ) done ON done.order_item_id=oi.id
          WHERE oi.order_id=?
        """, (order_id,))
        progress = cur.fetchone()
        ordered_qty = int(progress["ordered_qty"] or 0) if progress else 0
        completed_qty = int(progress["completed_qty"] or 0) if progress else 0

        if ordered_qty > 0 and 0 < completed_qty < ordered_qty:
            cur.execute("UPDATE orders SET status='partially_shipped' WHERE id=?", (order_id,))
            changed_to_partial.append(order_id)

    c.commit()
    c.close()

    if changed_to_partial and supabase_enabled():
        for order_id in changed_to_partial:
            try:
                supabase_update_rows("orders", {"status": "partially_shipped"}, {"id": order_id})
            except Exception:
                app.logger.exception("Nie udało się zsynchronizować częściowej wysyłki zamówienia %s", order_id)

    shipped_order_ids = [int(row["id"]) for row in shipped_rows]
    if shipped_order_ids:
        finalize_fully_invoiced_orders(shipped_order_ids)

    return changed_to_partial


def finalize_legacy_shipped_orders_with_full_invoice():
    """Zachowuje zgodność ze starszym wywołaniem naprawy statusów."""
    return reconcile_legacy_shipped_order_statuses()


def reconcile_orders_after_invoice_change(order_ids: list[int]):
    touched = sorted({int(x) for x in order_ids if x})
    if not touched:
        return [], []

    c = conn()
    cur = c.cursor()
    changed_order_ids = []
    changed_product_ids = []

    for order_id in touched:
        cur.execute("SELECT id, status, warehouse_issued FROM orders WHERE id=?", (order_id,))
        order_row = cur.fetchone()
        if not order_row:
            continue

        fully = order_fully_invoiced(cur, order_id)
        warehouse_issued = int(order_row["warehouse_issued"] or 0)
        current_status = norm(order_row["status"]).lower()

        if fully and warehouse_issued == 0:
            cur.execute("SELECT product_id, qty FROM order_items WHERE order_id=?", (order_id,))
            for it in cur.fetchall():
                pid = int(it["product_id"])
                qty = int(it["qty"] or 0)
                cur.execute("INSERT OR IGNORE INTO stock(product_id, qty) VALUES (?, 0)", (pid,))
                cur.execute("UPDATE stock SET qty = qty - ? WHERE product_id=?", (qty, pid))
                changed_product_ids.append(pid)
            preserved = {"shipped", "partially_shipped", "completed", "issued", "cancelled"}
            next_status = current_status if current_status in preserved else "packed"
            if next_status == "packed":
                cur.execute("""
                  UPDATE orders
                  SET status=?, warehouse_issued=1, packed_at=COALESCE(packed_at, ?)
                  WHERE id=?
                """, (next_status, now_iso(), order_id))
            else:
                cur.execute("UPDATE orders SET status=?, warehouse_issued=1 WHERE id=?", (next_status, order_id))
            changed_order_ids.append(order_id)

        elif not fully and warehouse_issued == 1:
            cur.execute("SELECT product_id, qty FROM order_items WHERE order_id=?", (order_id,))
            for it in cur.fetchall():
                pid = int(it["product_id"])
                qty = int(it["qty"] or 0)
                cur.execute("INSERT OR IGNORE INTO stock(product_id, qty) VALUES (?, 0)", (pid,))
                cur.execute("UPDATE stock SET qty = qty + ? WHERE product_id=?", (qty, pid))
                changed_product_ids.append(pid)
            next_status = "confirmed" if current_status in {"issued", "packed", "packed_partial"} else (current_status or "confirmed")
            cur.execute("UPDATE orders SET status=?, warehouse_issued=0 WHERE id=?", (next_status, order_id))
            changed_order_ids.append(order_id)

    c.commit()
    c.close()

    if supabase_enabled():
        if changed_order_ids:
            try:
                sync_local_rows_to_supabase("orders", "id", changed_order_ids)
            except Exception:
                pass
        if changed_product_ids:
            try:
                sync_local_rows_to_supabase("stock", "product_id", list(set(changed_product_ids)))
            except Exception:
                pass

    return changed_order_ids, list(set(changed_product_ids))


def invoice_edit_items(invoice_id: int, invoice_row: dict):
    c = conn()
    cur = c.cursor()
    cur.execute("SELECT order_id, order_item_id, qty FROM invoice_allocations WHERE invoice_id=?", (invoice_id,))
    current_alloc_rows = [dict(r) for r in cur.fetchall()]
    current_qty_by_item = {int(r["order_item_id"]): int(r["qty"] or 0) for r in current_alloc_rows}
    allocated_order_ids = {int(r["order_id"]) for r in current_alloc_rows if int(r.get("order_id") or 0)}

    email = _email_key(invoice_row.get("buyer_email"))
    if not email and invoice_row.get("order_id"):
        cur.execute("SELECT customer_email FROM orders WHERE id=?", (invoice_row.get("order_id"),))
        rr = cur.fetchone()
        email = _email_key(rr["customer_email"]) if rr else ""

    order_ids = set(allocated_order_ids)
    if invoice_row.get("order_id"):
        order_ids.add(int(invoice_row.get("order_id")))
    if email:
        status_ph = ",".join(["?"] * len(CURRENT_ORDER_STATUSES))
        cur.execute(f"""
          SELECT id
          FROM orders
          WHERE LOWER(COALESCE(customer_email,'')) = ?
            AND (
              LOWER(COALESCE(status,'')) IN ({status_ph})
              OR id IN (SELECT order_id FROM invoice_allocations WHERE invoice_id=?)
            )
          ORDER BY created_at DESC, id DESC
        """, (email, *sorted(CURRENT_ORDER_STATUSES), invoice_id))
        order_ids.update(int(r["id"]) for r in cur.fetchall())

    if not order_ids:
        c.close()
        return []

    ids = sorted(order_ids)
    ph = ",".join(["?"] * len(ids))
    cur.execute(f"""
      SELECT oi.*, p.model, p.name, COALESCE(s.qty,0) AS stock_qty,
             oo.order_no AS source_order_no,
             oo.created_at AS source_order_created_at,
             oo.note AS source_order_note,
             COALESCE(pr.net_price, 0) AS net_price,
             COALESCE(pr.gross_price, 0) AS gross_price
      FROM order_items oi
      JOIN orders oo ON oo.id=oi.order_id
      JOIN products p ON p.id=oi.product_id
      LEFT JOIN stock s ON s.product_id=oi.product_id
      LEFT JOIN pricing pr ON (TRIM(LOWER(pr.model)) = TRIM(LOWER(p.model)) OR TRIM(LOWER(pr.model)) = TRIM(LOWER(p.sku)))
      WHERE oi.order_id IN ({ph})
      ORDER BY oo.created_at DESC, oo.id DESC, oi.id
    """, ids)
    items = [dict(r) for r in cur.fetchall()]
    c.close()

    invoiced_by_item = invoiced_qty_by_order_item_ids([int(it["id"]) for it in items])
    out = []
    for it in items:
        item_id = int(it["id"])
        current_qty = int(current_qty_by_item.get(item_id, 0))
        ordered_qty = int(it.get("qty") or 0)
        invoiced_total = int(invoiced_by_item.get(item_id, 0))
        invoiced_other = max(0, invoiced_total - current_qty)
        max_qty = max(0, ordered_qty - invoiced_other)
        row = dict(it)
        row["order_item_id"] = item_id
        row["source_order_id"] = int(it.get("order_id") or 0)
        row["source_order_no"] = order_display_no(
            row["source_order_id"],
            it.get("source_order_created_at"),
            it.get("source_order_no"),
            it.get("source_order_note") or ""
        )
        row["source_order_note"] = it.get("source_order_note") or ""
        row["ordered_qty"] = ordered_qty
        row["invoiced_other_qty"] = invoiced_other
        row["current_invoice_qty"] = current_qty
        row["remaining_qty"] = max_qty
        if max_qty > 0 or current_qty > 0:
            out.append(row)
    return out


def prepare_invoice_edit_items(edit_items: list[dict], form):
    prepared = []
    for it in edit_items:
        max_qty = int(it.get("remaining_qty") or 0)
        qty = to_int(form.get(f"invoice_qty_{it['id']}"), 0)
        qty = max(0, min(qty, max_qty))
        if qty <= 0:
            continue
        row = dict(it)
        row["order_item_id"] = int(it.get("id") or it.get("order_item_id") or 0)
        row["source_order_id"] = int(it.get("order_id") or it.get("source_order_id") or 0)
        row["ordered_qty"] = int(it.get("ordered_qty") or it.get("qty") or 0)
        row["invoiced_qty_before"] = int(it.get("invoiced_other_qty") or 0)
        row["qty"] = qty
        line_net = money_dec(row.get("net_price")) * Decimal(qty)
        line_net = line_net.quantize(MONEY_Q, rounding=ROUND_HALF_UP)
        line_vat = vat23_from_net(line_net)
        line_gross = (line_net + line_vat).quantize(MONEY_Q, rounding=ROUND_HALF_UP)
        row["gross_price"] = money_float(gross_from_net_23(row.get("net_price")))
        row["vat_rate"] = 23
        row["line_value_net"] = money_float(line_net)
        row["line_value_vat"] = money_float(line_vat)
        row["line_value_gross"] = money_float(line_gross)
        prepared.append(row)
    return prepared


# =========================
# TEMPLATES (BASE as "file")
# =========================

CLIENT_API_PATHS = {
    "/api/client_stock_catalog", "/api/client_search_log", "/api/client/orders",
    "/api/order_lookup", "/api/client_invoices", "/api/client_order_email",
    "/api/client/profile",
}
_rate_lock = threading.Lock()
_rate_hits = {}


def _rate_limit(bucket: str, limit: int, window_seconds: int):
    now = time.time()
    key = (bucket, request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip())
    with _rate_lock:
        hits = [ts for ts in _rate_hits.get(key, []) if now - ts < window_seconds]
        if len(hits) >= limit:
            return False
        hits.append(now)
        _rate_hits[key] = hits
    return True


def _admin_password_ok(candidate: str) -> bool:
    if ADMIN_PASSWORD_HASH:
        try:
            return check_password_hash(ADMIN_PASSWORD_HASH, candidate)
        except Exception:
            return False
    return bool(ADMIN_PASSWORD) and hmac.compare_digest(ADMIN_PASSWORD, candidate)


@app.before_request
def security_gate():
    path = request.path
    if path == "/login":
        if request.method == "POST" and not _rate_limit("admin_login", 8, 15 * 60):
            return "Zbyt wiele prób logowania. Spróbuj później.", 429
        return None

    is_client_api = (
        path in CLIENT_API_PATHS
        or path.startswith("/api/invoices/")
        or path.startswith("/api/client/orders/")
    )
    if is_client_api:
        if request.method == "OPTIONS":
            return None
        if path == "/api/client/orders" and not _rate_limit("client_orders", 12, 10 * 60):
            return jsonify(ok=False, error="Zbyt wiele prób złożenia zamówienia"), 429
        if not _rate_limit("client_api", 180, 60):
            return jsonify(ok=False, error="Zbyt wiele żądań"), 429
        user = _authenticated_client_user()
        if not user:
            # Stare e-maile wskazuja endpoint PDF bez naglowka Authorization.
            # Przekierowujemy do panelu; dokument nadal wymaga tokenu po loginie.
            old_invoice_link = re.fullmatch(r"/api/invoices/(\d+)/download", path)
            if request.method == "GET" and old_invoice_link:
                invoice_id = int(old_invoice_link.group(1))
                panel_url = (
                    f"{CLIENT_PANEL_URL}/?"
                    + urllib.parse.urlencode({"section": "invoices", "invoice": invoice_id})
                )
                return redirect(panel_url, code=302)
            return jsonify(ok=False, error="Brak autoryzacji"), 401
        g.client_user = user
        return None

    if path == "/logout":
        return None
    if not session.get("admin_authenticated"):
        if path.startswith("/api/"):
            return jsonify(ok=False, error="Brak autoryzacji administratora"), 401
        return redirect(url_for("login", next=request.full_path if request.query_string else path))

    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        if request.is_json:
            origin = norm(request.headers.get("Origin")).rstrip("/")
            expected = request.host_url.rstrip("/")
            if origin and origin != expected:
                return jsonify(ok=False, error="Nieprawidłowe źródło żądania"), 403
        else:
            supplied = norm(request.form.get("csrf_token") or request.headers.get("X-CSRF-Token"))
            if not supplied or not hmac.compare_digest(supplied, session.get("csrf_token", "")):
                return "Nieprawidłowy token bezpieczeństwa formularza. Odśwież stronę.", 403


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        if not app.secret_key or not (ADMIN_PASSWORD_HASH or ADMIN_PASSWORD):
            error = "Brak konfiguracji logowania administratora na serwerze."
        elif hmac.compare_digest(norm(request.form.get("username")), ADMIN_USERNAME) and _admin_password_ok(request.form.get("password") or ""):
            session.clear()
            session["admin_authenticated"] = True
            session["csrf_token"] = secrets.token_urlsafe(32)
            session.permanent = True
            target = norm(request.args.get("next"))
            return redirect(target if target.startswith("/") and not target.startswith("//") else url_for("home"))
        else:
            error = "Nieprawidłowy login lub hasło."
    return render_template_string(r'''<!doctype html><html lang="pl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Logowanie — Niedźwieccy</title><style>body{margin:0;font-family:Inter,Segoe UI,sans-serif;background:#f5f6fa;color:#17233c;display:grid;place-items:center;min-height:100vh}.box{width:min(420px,calc(100% - 28px));background:#fff;padding:30px;border-radius:24px;box-shadow:0 18px 55px rgba(20,35,65,.13)}h1{margin:0 0 5px}.muted{color:#718096;font-size:13px;margin-bottom:22px}label{display:block;font-size:12px;font-weight:700;margin:12px 0 6px}input{width:100%;box-sizing:border-box;padding:12px;border:1px solid #dfe3ec;border-radius:13px;font:inherit}button{width:100%;margin-top:18px;padding:12px;border:0;border-radius:13px;background:#5577ee;color:#fff;font-weight:700}.error{background:#fff1f2;color:#b9384c;padding:10px;border-radius:12px;font-size:12px}</style></head><body><form class="box" method="post"><h1>Panel magazynu</h1><div class="muted">Zaloguj się jako administrator.</div>{% if error %}<div class="error">{{ error }}</div>{% endif %}<label>Login</label><input name="username" autocomplete="username" required><label>Hasło</label><input name="password" type="password" autocomplete="current-password" required><button type="submit">Zaloguj</button></form></body></html>''', error=error)


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.after_request
def security_headers_and_csrf(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(self), microphone=(), geolocation=()")
    response.headers.setdefault("Content-Security-Policy", "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline' https://unpkg.com https://cdn.jsdelivr.net; connect-src 'self' https://*.supabase.co https://api.resend.com")
    if session.get("admin_authenticated") and response.content_type and response.content_type.startswith("text/html"):
        body = response.get_data(as_text=True)
        token = session.get("csrf_token", "")
        if token:
            hidden = f'<input type="hidden" name="csrf_token" value="{token}">'
            body = re.sub(r'(<form\b[^>]*\bmethod=["\']post["\'][^>]*>)', r'\1' + hidden, body, flags=re.I)
            response.set_data(body)
    return response

BASE = r"""
<!doctype html>
<html lang="pl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ title or "NiedĹşwieccy Orders" }}</title>
  <style>
    :root{--navy:#12213d;--navy2:#0b1730;--blue:#5577ee;--blue2:#3f63dc;--mint:#31b98b;--amber:#f5a524;--red:#e05263;--ink:#17233c;--muted:#718096;--bg:#f5f6fa;--line:#e7eaf2;--card:#fff;--radius:22px;--shadow:0 12px 35px rgba(31,45,78,.07)}
    *{box-sizing:border-box}html{background:var(--bg)}body{font-family:Inter,ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:0;background:radial-gradient(circle at 85% -10%,#eaf0ff 0,transparent 28%),var(--bg);color:var(--ink);line-height:1.45}
    .top{position:fixed;inset:10px auto 10px 10px;width:238px;background:linear-gradient(165deg,var(--navy),var(--navy2));color:#fff;padding:22px 14px;border-radius:26px;display:flex;flex-direction:column;z-index:1100;box-shadow:0 24px 50px rgba(10,24,54,.22);overflow-y:auto}
    .brand{font-size:19px;font-weight:800;letter-spacing:-.3px;padding:4px 10px 20px;display:flex;align-items:center;gap:10px}.brand:before{content:"◇";display:grid;place-items:center;width:36px;height:36px;border:1px solid rgba(255,255,255,.32);border-radius:12px;background:rgba(255,255,255,.08);font-size:20px}
    .nav{display:flex!important;flex-direction:column;align-items:stretch!important;gap:5px!important;flex-wrap:nowrap!important;width:100%}.nav a,.nav-drop-btn{display:flex;align-items:center;color:#dce5f7;text-decoration:none;padding:11px 12px;border:0;border-radius:13px;background:transparent;font:inherit;font-size:14px;font-weight:600;cursor:pointer;transition:.18s ease}.nav a:hover,.nav-drop-btn:hover,.nav a.active{background:rgba(93,128,246,.24);color:#fff;transform:translateX(2px)}
    .nav a:before{width:25px;font-size:16px;opacity:.9}.nav a:nth-child(1):before{content:"⌂"}.nav a:nth-child(2):before{content:"▣"}.nav a:nth-child(3):before{content:"＋"}.nav a:nth-child(4):before{content:"▤"}.nav a:nth-child(5):before{content:"K"}.nav a:nth-child(6):before{content:"⌕"}.nav a:nth-child(7):before{content:"▦"}.nav a:nth-child(8):before{content:"◇"}.nav a:nth-child(9):before{content:"▧"}
    .nav-dropdown{position:relative;display:block}.nav-drop-btn{width:100%;text-align:left}.nav-drop-btn:before{content:"⚙";width:25px}.nav-dropdown-menu{display:none;margin:4px 0 2px 12px;border-left:1px solid rgba(255,255,255,.16);padding:2px 0 2px 8px}.nav-dropdown:hover .nav-dropdown-menu,.nav-dropdown:focus-within .nav-dropdown-menu{display:grid;gap:2px}.nav-dropdown-menu a{font-size:13px;padding:8px 10px}.nav-dropdown-menu a:before{display:none}
    .top>.right{margin:auto 8px 0!important;padding-top:16px;border-top:1px solid rgba(255,255,255,.13);color:#91a1bd!important;font-size:10px;overflow-wrap:anywhere}
    .mobile-toggle{display:none}.wrap{max-width:1500px;margin:0 0 0 258px;padding:28px 28px 18px;min-height:100vh}
    .card{background:rgba(255,255,255,.94);border:1px solid rgba(226,230,239,.9);border-radius:var(--radius);padding:20px;box-shadow:var(--shadow);margin-bottom:16px;overflow-x:auto}.card:hover{border-color:#dce2f1}
    .row{display:grid;grid-template-columns:1fr 1fr;gap:16px}h1{font-size:26px;letter-spacing:-.7px;margin:0 0 16px}h2{font-size:17px;letter-spacing:-.2px;margin:0 0 13px}.muted{color:var(--muted);font-size:12px}
    .btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;padding:10px 14px;border:1px solid #dce1eb;border-radius:13px;background:#fff;color:var(--ink);font-weight:650;text-decoration:none;cursor:pointer;box-shadow:0 3px 10px rgba(30,44,75,.04);transition:.18s ease}.btn:hover{transform:translateY(-1px);border-color:#bfc9df;box-shadow:0 7px 16px rgba(30,44,75,.09)}.btn.primary{background:linear-gradient(135deg,var(--blue),var(--blue2));color:#fff;border-color:transparent}.btn.danger{background:#fff0f2;color:#b92d43;border-color:#ffd6dc}.btn.ok{background:#e9faf4;color:#14835f;border-color:#c6f0e2}
    input,select,textarea{width:100%;padding:11px 13px;border:1px solid #dfe3ec;border-radius:13px;background:#fbfcfe;color:var(--ink);font:inherit;font-size:14px;outline:none;transition:.18s}input:focus,select:focus,textarea:focus{border-color:#7892f3;background:#fff;box-shadow:0 0 0 4px rgba(85,119,238,.11)}textarea{min-height:90px}
    table{width:100%;border-collapse:separate;border-spacing:0;min-width:660px}th,td{border-bottom:1px solid #edf0f5;padding:12px 11px;text-align:left;vertical-align:middle}th{background:#f8f9fc;color:#64718a;font-size:11px;text-transform:uppercase;letter-spacing:.45px;font-weight:750}thead th:first-child{border-radius:12px 0 0 12px}thead th:last-child{border-radius:0 12px 12px 0}tbody tr{transition:.15s}tbody tr:hover{background:#fafbff}
    .badge{display:inline-block;padding:5px 10px;border-radius:999px;border:1px solid #dfe4ef;background:#f8faff;color:#526079;font-size:11px;font-weight:700}.st-confirmed,.badge-paid{background:#e8f9f3!important;color:#16835f!important;border-color:#c9efe2!important}.st-unconfirmed{background:#fff1f2!important;color:#be3b50!important;border-color:#ffd7dc!important}.st-delivery{background:#edf3ff!important;color:#4166d3!important;border-color:#d9e4ff!important}.st-issued{background:#f0f2f6!important;color:#667085!important}
    .flex{display:flex;gap:10px;flex-wrap:wrap;align-items:center}.right{margin-left:auto}.small{font-size:12px}.grid3{display:grid;grid-template-columns:2fr 1fr 1fr;gap:16px}.line{height:1px;background:#edf0f5;margin:16px 0}.hint{background:#fff9e9;border:1px solid #f8e6ae;padding:12px 14px;border-radius:14px;color:#7e641b;font-size:13px}.kpi{display:flex;gap:10px;flex-wrap:wrap}.kpi .pill{background:#f4f7ff;border:1px solid #e1e8fb;padding:8px 11px;border-radius:999px;color:#516582;font-size:12px}.items-row{display:grid;grid-template-columns:2fr 120px 120px 120px;gap:10px;align-items:center}
    @media(max-width:980px){.top{transform:translateX(-110%);transition:.25s}.top.open{transform:translateX(0)}.mobile-toggle{display:grid;place-items:center;position:fixed;right:14px;bottom:14px;z-index:1200;width:52px;height:52px;border:0;border-radius:17px;background:var(--navy);color:#fff;font-size:22px;box-shadow:0 12px 28px rgba(12,28,58,.28)}.wrap{margin-left:0;padding:18px 14px 80px}.row,.grid3{grid-template-columns:1fr}.items-row{grid-template-columns:1fr 1fr}}
    @media(max-width:560px){.card{padding:15px;border-radius:18px}.items-row{grid-template-columns:1fr}.flex>.right{margin-left:0}h1{font-size:22px}}
  </style>
</head>
<body>
  <button class="mobile-toggle" type="button" onclick="document.querySelector('.top').classList.toggle('open')">☰</button>
  <div class="top">
    <div class="brand">Niedźwieccy</div>
    <div class="nav flex">
      <a class="{% if request.endpoint == 'home' %}active{% endif %}" href="{{ url_for('home') }}">Pulpit</a>
      <a class="{% if request.endpoint in ['orders','order_view'] %}active{% endif %}" href="{{ url_for('orders') }}">Zamówienia</a>
      <a class="{% if request.endpoint == 'order_new' %}active{% endif %}" href="{{ url_for('order_new') }}">Nowe zamówienie</a>
      <a href="{{ url_for('invoices') }}">Faktury</a>
      <a href="{{ url_for('ksef_dashboard') }}">KSeF</a>
      <a href="{{ url_for('client_searches') }}">Wyszukiwania</a>
      <a href="{{ url_for('stock') }}">Stan magazynu</a>
      <a href="{{ url_for('china') }}">Dostawy (P/O)</a>
      <a href="{{ url_for('order_scan') }}">Skan QR</a>
      <div class="nav-dropdown">
        <button class="nav-drop-btn" type="button">Ustawienia ▾</button>
        <div class="nav-dropdown-menu">
          <a href="{{ url_for('products') }}">Produkty</a>
          <a href="{{ url_for('customers') }}">Klienci</a>
          <a href="{{ url_for('pricing') }}">Cennik</a>
          <a href="{{ url_for('company') }}">Dane mojej firmy</a>
          <a href="{{ url_for('cash_flow') }}">Cash flow</a>
          <a href="{{ url_for('email_test') }}">Test maili</a>
        </div>
      </div>
      <a href="{{ url_for('logout') }}">Wyloguj</a>
    </div>
    <div class="right muted">Magazyn główny<br>{{ base_url }}</div>
  </div>

  <div class="wrap">
    {% block content %}{% endblock %}
    <div class="muted small" style="margin:14px 2px;">Dane na dysku: <b>{{ db_path }}</b></div>
  </div>

<script>
async function refreshStock(productId, targetId){
  if(!productId){ document.getElementById(targetId).innerText = "-"; return; }
  const r = await fetch("/api/product/"+productId);
  const j = await r.json();
  document.getElementById(targetId).innerText = (j.stock ?? "-");
}

function addItemRow(){
  const tpl = document.getElementById("itemRowTpl");
  const container = document.getElementById("itemsContainer");
  const node = tpl.content.cloneNode(true);
  container.appendChild(node);
}

function removeRow(btn){
  const row = btn.closest(".items-row");
  if(row) row.remove();
}
</script>

</body>
</html>
"""

# loader: BASE dostÄ™pny jako "base.html"
app.jinja_loader = DictLoader({"base.html": BASE})
app.jinja_env.globals["canonical_order_no"] = canonical_order_no
app.jinja_env.globals["order_display_no"] = order_display_no
app.jinja_env.globals["order_status_label"] = order_status_label if "order_status_label" in globals() else None
app.jinja_env.globals["order_status_css"] = order_status_css if "order_status_css" in globals() else None
app.jinja_env.globals["carrier_tracking_url"] = carrier_tracking_url


# =========================
# PAGES
# =========================

@app.get("/")
def home():
    maybe_pull_shared_from_supabase()
    # Zachowujemy kolejność: Wysłane -> faktura -> Zrealizowane.
    # Przy okazji naprawiamy rekordy utworzone przez starszą wersję kodu.
    finalize_legacy_shipped_orders_with_full_invoice()
    c = conn()
    cur = c.cursor()
    cur.execute("SELECT COUNT(*) AS n FROM products WHERE COALESCE(archived,0)=0")
    n_products = cur.fetchone()["n"]
    cur.execute("SELECT COUNT(*) AS n FROM orders WHERE status IN ('new','packed','packed_partial','confirmed','in_delivery','shipped','partially_shipped')")
    n_orders_current = cur.fetchone()["n"]
    cur.execute("SELECT COUNT(*) AS n FROM china_packages WHERE status IN ('planned','ordered','shipped')")
    n_china_active = cur.fetchone()["n"]
    cur.execute("SELECT COALESCE(SUM(qty),0) AS n FROM stock")
    n_stock_qty = cur.fetchone()["n"]
    cur.execute("""
      SELECT COALESCE(SUM(ci.qty),0) AS n
      FROM china_items ci
      JOIN china_packages cp ON cp.id=ci.package_id
      WHERE cp.status IN ('planned', 'ordered', 'shipped')
    """)
    n_in_delivery_qty = cur.fetchone()["n"]

    cur.execute("""
      SELECT COALESCE(SUM(
        (COALESCE(s.qty,0) + COALESCE(d.in_delivery_qty,0)) * COALESCE(pr.net_price,0)
      ), 0) AS v
      FROM products p
      LEFT JOIN stock s ON s.product_id=p.id
      LEFT JOIN (
        SELECT ci.product_id, SUM(ci.qty) AS in_delivery_qty
        FROM china_items ci
        JOIN china_packages cp ON cp.id=ci.package_id
        WHERE cp.status IN ('planned', 'ordered', 'shipped')
        GROUP BY ci.product_id
      ) d ON d.product_id=p.id
      LEFT JOIN pricing pr ON (
        TRIM(LOWER(pr.model)) = TRIM(LOWER(p.model))
        OR TRIM(LOWER(pr.model)) = TRIM(LOWER(p.sku))
      )
      WHERE COALESCE(p.archived,0)=0
    """)
    inventory_value_net = float(cur.fetchone()["v"] or 0)
    cur.execute("SELECT COUNT(*) AS n FROM orders WHERE date(created_at)=date('now','localtime')")
    n_orders_today = int(cur.fetchone()["n"] or 0)
    # "Wydane dzisiaj" ma opisywac dzien faktycznego wydania przez
    # fakturowanie, a nie dzien utworzenia zamowienia. Jedna faktura moze
    # zawierac wiele pozycji (a nawet kilka zamowien), dlatego liczymy
    # unikalne zamowienia na podstawie zapisanych alokacji faktury.
    # Druga czesc UNION jest zgodnosciowym fallbackiem dla starszych faktur,
    # ktore powstaly przed wprowadzeniem invoice_allocations.
    cur.execute("""
      SELECT COUNT(DISTINCT issued.order_id) AS n
      FROM (
        SELECT ia.order_id, ia.created_at AS issued_at
        FROM invoice_allocations ia

        UNION ALL

        SELECT i.order_id, i.created_at AS issued_at
        FROM invoices i
        WHERE i.order_id IS NOT NULL
          AND NOT EXISTS (
            SELECT 1 FROM invoice_allocations ia2 WHERE ia2.invoice_id=i.id
          )
      ) issued
      JOIN orders o ON o.id=issued.order_id
      WHERE COALESCE(o.warehouse_issued,0)=1
        AND date(issued.issued_at)=date('now','localtime')
    """)
    n_issued_today = int(cur.fetchone()["n"] or 0)
    # Zamowienia, ktore mozna wydac z obecnego stanu. Stan jest rezerwowany
    # od najstarszego zamowienia, aby ta sama sztuka nie byla liczona dwa razy.
    status_ph = ",".join(["?"] * len(CURRENT_ORDER_STATUSES))
    cur.execute(f"""
      SELECT o.id, o.order_no, o.created_at, o.note, oi.product_id,
             SUM(oi.qty) AS required_qty
      FROM orders o
      JOIN order_items oi ON oi.order_id=o.id
      WHERE LOWER(COALESCE(o.status,'')) IN ({status_ph})
        AND COALESCE(o.warehouse_issued,0)=0
      GROUP BY o.id, oi.product_id
      ORDER BY o.created_at, o.id, oi.product_id
    """, tuple(sorted(CURRENT_ORDER_STATUSES)))
    issue_rows = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT product_id, MAX(0, COALESCE(qty,0)) AS qty FROM stock")
    issue_stock_pool = {int(r["product_id"]): int(r["qty"] or 0) for r in cur.fetchall()}
    issue_orders = {}
    for row in issue_rows:
        order_id = int(row["id"])
        issue_orders.setdefault(order_id, {"order": row, "needs": []})["needs"].append(
            (int(row["product_id"]), int(row["required_qty"] or 0))
        )
    issuable_orders = []
    for candidate in issue_orders.values():
        if candidate["needs"] and all(issue_stock_pool.get(pid, 0) >= qty for pid, qty in candidate["needs"]):
            issuable_orders.append(candidate["order"])
            for pid, qty in candidate["needs"]:
                issue_stock_pool[pid] = issue_stock_pool.get(pid, 0) - qty
    n_issuable_today = len(issuable_orders)
    issuable_order_labels = [
        order_display_no(r["id"], r.get("created_at"), r.get("order_no"), r.get("note") or "")
        for r in issuable_orders[:3]
    ]
    reorder_horizon_days = 60
    try:
        cur.execute("SELECT value FROM cash_flow_settings WHERE key='reorder_horizon_days'")
        horizon_row = cur.fetchone()
        reorder_horizon_days = int(float(horizon_row["value"])) if horizon_row else 60
    except Exception:
        reorder_horizon_days = 60
    if reorder_horizon_days not in (45, 60, 90):
        reorder_horizon_days = 60
    cur.execute("""
      SELECT o.id,o.order_no,o.customer_name,o.created_at,o.status,o.currency,
             COALESCE(SUM(oi.qty * COALESCE(oi.unit_net_price,pr.net_price,0)),0) AS total_net
      FROM orders o
      LEFT JOIN order_items oi ON oi.order_id=o.id
      LEFT JOIN products p ON p.id=oi.product_id
      LEFT JOIN pricing pr ON (TRIM(LOWER(pr.model))=TRIM(LOWER(p.model)) OR TRIM(LOWER(pr.model))=TRIM(LOWER(p.sku)))
      GROUP BY o.id ORDER BY o.id DESC LIMIT 8
    """)
    recent_orders = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT status,COUNT(*) AS n FROM orders GROUP BY status")
    status_counts = {norm(r["status"]).lower(): int(r["n"] or 0) for r in cur.fetchall()}
    status_new = sum(status_counts.get(x,0) for x in ("new","pending","unconfirmed"))
    status_work = sum(status_counts.get(x,0) for x in ("confirmed","packed","packed_partial","in_delivery","shipped","partially_shipped","issued"))
    status_done = status_counts.get("completed",0)
    status_cancelled = status_counts.get("cancelled",0)
    status_total = status_new + status_work + status_done + status_cancelled
    status_divisor = max(1, status_total)
    overdue_invoices = overdue_invoice_rows(c)
    overdue_count = len(overdue_invoices)
    overdue_total = sum(float(inv.get("total_gross") or 0) for inv in overdue_invoices)
    c.close()

    replenishment_analysis = build_replenishment_analysis(
        conn, today=app_now().date(), horizon_days=reorder_horizon_days
    )
    all_replenishment_rows = recommended_replenishments(
        replenishment_analysis, limit=max(10, len(replenishment_analysis))
    )
    replenishment_rows = all_replenishment_rows[:5]
    replenishment_count = len(all_replenishment_rows)

    tpl = r"""
    {% extends "base.html" %}
    {% block content %}
      <style>
        .dashboard-head{display:flex;align-items:center;gap:14px;margin-bottom:18px}.dashboard-head h1{margin:0}.search-shell{margin-left:28px;flex:1;max-width:580px;position:relative}.search-shell input{padding-left:42px;background:#fff}.search-shell:before{content:"⌕";position:absolute;left:15px;top:9px;color:#8793aa;font-size:19px;z-index:2}.metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:16px;margin-bottom:16px}.metric{display:grid;grid-template-columns:55px 1fr;gap:14px;align-items:center;background:#fff;border:1px solid #e7eaf2;border-radius:22px;padding:18px;box-shadow:var(--shadow)}.metric .icon{display:grid;place-items:center;width:55px;height:55px;border-radius:17px;background:var(--soft,#edf3ff);color:var(--tone,#5577ee);font-size:23px}.metric span{color:#718096;font-size:12px;font-weight:650}.metric b{display:block;margin-top:2px;font-size:25px;letter-spacing:-.6px}.metric small{display:block;margin-top:4px;color:#2da176;font-size:10px}.dash-grid{display:grid;grid-template-columns:minmax(0,2.15fr) minmax(300px,.9fr);gap:16px;align-items:start}.panel-title{display:flex;align-items:center;gap:9px;margin-bottom:13px}.panel-title h2{margin:0}.panel-title .btn{margin-left:auto;padding:7px 11px;font-size:11px}.orders-card{padding-bottom:8px}.orders-card table{min-width:780px}.orders-card td{font-size:12px}.customer-name{font-weight:700}.order-no{color:#4166d3;font-weight:750;text-decoration:none}.side-stack{display:grid;gap:16px}.stock-list{display:grid;gap:2px}.stock-item{display:grid;grid-template-columns:42px 1fr auto;align-items:center;gap:10px;padding:10px 2px;border-bottom:1px solid #edf0f5}.stock-icon{display:grid;place-items:center;width:39px;height:39px;border-radius:12px;background:#f1f4f9;color:#68758d}.stock-name{font-size:12px;font-weight:700}.stock-sku{font-size:9px;color:#8b96a9}.stock-qty{font-size:12px;font-weight:800}.stock-qty:after{content:"";display:inline-block;width:7px;height:7px;margin-left:8px;border-radius:50%;background:#ee5262}.donut-wrap{display:grid;grid-template-columns:145px 1fr;align-items:center;gap:14px}.donut{width:140px;height:140px;border-radius:50%;display:grid;place-items:center;background:conic-gradient(#5577ee 0 calc(var(--p1)*1%),#65a7ec calc(var(--p1)*1%) calc((var(--p1) + var(--p2))*1%),#31b98b calc((var(--p1) + var(--p2))*1%) calc((var(--p1) + var(--p2) + var(--p3))*1%),#e05263 calc((var(--p1) + var(--p2) + var(--p3))*1%) 100%)}.donut:before{content:"";width:86px;height:86px;background:#fff;border-radius:50%;position:absolute}.donut-label{position:relative;text-align:center;font-size:11px;color:#77849b}.donut-label b{display:block;color:#17233c;font-size:25px}.legend{display:grid;gap:9px}.legend-row{display:grid;grid-template-columns:9px 1fr auto;gap:7px;align-items:center;font-size:10px}.legend-dot{width:8px;height:8px;border-radius:50%}.quick-card{grid-column:1}.quick-grid{display:grid;grid-template-columns:repeat(6,1fr);gap:10px}.quick-grid .btn{min-height:80px;flex-direction:column;background:#f8faff;border-color:#e8ecf6;font-size:11px}.quick-grid .btn b{font-size:20px}.quick-grid .btn:nth-child(1){background:#edf3ff;color:#4166d3}.quick-grid .btn:nth-child(2){background:#eaf9f4;color:#16835f}.quick-grid .btn:nth-child(3){background:#eef3ff;color:#4b6bd3}.quick-grid .btn:nth-child(4){background:#fff5e5;color:#c57a10}.quick-grid .btn:nth-child(5){background:#f3edff;color:#7650ce}@media(max-width:1200px){.metrics{grid-template-columns:1fr 1fr}.dash-grid{grid-template-columns:1fr}.quick-card{grid-column:auto}}@media(max-width:760px){.dashboard-head{flex-wrap:wrap}.search-shell{order:3;margin-left:0;flex-basis:100%}.metrics{grid-template-columns:1fr}.quick-grid{grid-template-columns:1fr 1fr}.donut-wrap{grid-template-columns:1fr}.donut{margin:auto}}
      </style>
      <style>@media(min-width:1201px){.metrics{grid-template-columns:repeat(6,minmax(0,1fr));gap:12px}.metric{padding:14px;grid-template-columns:46px 1fr;gap:10px}.metric .icon{width:46px;height:46px}.metric small{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}</style>

      <div class="dashboard-head">
        <div><h1>Pulpit</h1><div class="muted">Przewagę buduje się codziennie — jedną dobrą decyzją naraz.</div></div>
        <form class="search-shell" action="{{ url_for('orders') }}"><input name="q" placeholder="Szukaj zamówień, produktów, klientów..."></form>
        <a class="btn primary" href="{{ url_for('order_new') }}">＋ Nowe zamówienie</a>
      </div>

      <div class="metrics">
        <div class="metric"><div class="icon">▣</div><div><span>Nowe zamówienia</span><b>{{ n_orders_today }}</b><small>{{ n_orders_current }} aktualnie w toku</small></div></div>
        <div class="metric" style="--soft:#eaf9f4;--tone:#1aa176"><div class="icon">◇</div><div><span>Wydane dzisiaj</span><b>{{ n_issued_today }}</b><small>{{ n_stock_qty }} szt. na stanie</small></div></div>
        <a class="metric" href="{{ url_for('orders', tab='new', ready_today=1) }}" style="--soft:#eaf9f4;--tone:#16835f;text-decoration:none;color:inherit"><div class="icon">✓</div><div><span>Możesz wydać dziś</span><b>{{ n_issuable_today }}</b><small title="{{ issuable_order_labels|join(', ') }}">{{ issuable_order_labels|join(', ') if issuable_order_labels else 'Brak kompletnych zamówień' }}</small></div></a>
        <a class="metric" href="{{ url_for('overdue_payments') }}" style="--soft:#fff0f1;--tone:#d9485b;text-decoration:none;color:inherit"><div class="icon">!</div><div><span>Zaległości</span><b>{{ overdue_count }}</b><small>{% if overdue_count %}Sprawdź płatności · {{ "{:,.0f}".format(overdue_total).replace(',', ' ') }} zł{% else %}Brak zaległych faktur{% endif %}</small></div></a>
        <div class="metric" style="--soft:#fff6e6;--tone:#db8a13"><div class="icon">△</div><div><span>Trzeba uzupełnić</span><b>{{ replenishment_count }}</b><small>Według rankingu zakupowego</small></div></div>
        <div class="metric" style="--soft:#edf3ff;--tone:#5577ee"><div class="icon">▤</div><div><span>Wartość magazynu</span><b>{{ "{:,.0f}".format(inventory_value_net).replace(',', ' ') }} zł</b><small>Netto z towarem w drodze</small></div></div>
      </div>

      <div class="dash-grid">
        <div class="card orders-card">
          <div class="panel-title"><span>▣</span><h2>Ostatnie zamówienia</h2><a class="btn" href="{{ url_for('orders') }}">Zobacz wszystkie</a></div>
          <table><thead><tr><th>Nr zamówienia</th><th>Klient</th><th>Data</th><th>Wartość</th><th>Status</th><th></th></tr></thead><tbody>
          {% for o in recent_orders %}<tr><td><a class="order-no" href="{{ url_for('order_view',order_id=o.id) }}">{{ canonical_order_no(o.id,o.created_at,o.order_no) }}</a></td><td class="customer-name">{{ o.customer_name or '-' }}</td><td>{{ o.created_at[:16] }}</td><td>{{ "%.2f"|format(o.total_net) }} {{ o.currency or 'PLN' }}</td><td><span class="badge {{ order_status_css(o.status) }}">{{ order_status_label(o.status) }}</span></td><td>{% if (o.status or '')|lower in ['new','pending','unconfirmed'] %}<form method="post" action="{{ url_for('order_status_update', order_id=o.id) }}"><input type="hidden" name="status" value="confirmed"><input type="hidden" name="return_to" value="dashboard"><button class="btn primary" type="submit">Potwierdź</button></form>{% endif %}</td></tr>{% endfor %}
          {% if not recent_orders %}<tr><td colspan="6" class="muted">Brak zamówień do wyświetlenia.</td></tr>{% endif %}
          </tbody></table>
        </div>
        <div class="side-stack">
          <div class="card"><div class="panel-title"><span style="color:#e69a20">△</span><h2>Trzeba uzupełnić:</h2><a class="btn" href="{{ url_for('cash_flow') }}">Wszystkie</a></div><div class="stock-list">
            {% for p in replenishment_rows %}<div class="stock-item"><div class="stock-icon">◇</div><div><div class="stock-name">{{ p.model or p.name or p.sku }}</div><div class="stock-sku">SKU: {{ p.sku }} · wynik {{ p.reorder_score }} · dostępne {{ p.available_qty }}</div></div><div class="stock-qty">+{{ p.suggested_qty }} szt.</div></div>{% endfor %}
            {% if not replenishment_rows %}<div class="muted">Brak produktów wymagających uzupełnienia.</div>{% endif %}
          </div></div>
          <div class="card"><div class="panel-title"><h2>Status zamówień</h2></div><div class="donut-wrap"><div class="donut" style="--p1:{{ status_new*100/status_divisor }};--p2:{{ status_work*100/status_divisor }};--p3:{{ status_done*100/status_divisor }}"><div class="donut-label"><b>{{ status_total }}</b>łącznie</div></div><div class="legend">
            <div class="legend-row"><i class="legend-dot" style="background:#5577ee"></i><span>Nowe</span><b>{{ status_new }}</b></div><div class="legend-row"><i class="legend-dot" style="background:#65a7ec"></i><span>W realizacji</span><b>{{ status_work }}</b></div><div class="legend-row"><i class="legend-dot" style="background:#31b98b"></i><span>Zrealizowane</span><b>{{ status_done }}</b></div><div class="legend-row"><i class="legend-dot" style="background:#e05263"></i><span>Anulowane</span><b>{{ status_cancelled }}</b></div>
          </div></div></div>
        </div>
        <div class="card quick-card"><div class="panel-title"><span>ϟ</span><h2>Szybkie akcje</h2></div><div class="quick-grid">
          <a class="btn" href="{{ url_for('order_new') }}"><b>＋</b><span>Nowe zamówienie</span></a><a class="btn" href="{{ url_for('products') }}"><b>◇</b><span>Dodaj produkt</span></a><a class="btn" href="{{ url_for('china') }}"><b>⇢</b><span>Przyjęcie dostawy</span></a><a class="btn" href="{{ url_for('stock') }}"><b>⇧</b><span>Wydanie z magazynu</span></a><a class="btn" href="{{ url_for('order_scan') }}"><b>▧</b><span>Skanuj QR</span></a><a class="btn" href="{{ url_for('invoices') }}"><b>▤</b><span>Faktury</span></a>
        </div>
      </div>
      </div>
    {% endblock %}
    """
    return render_template_string(tpl, title="Start", base_url=BASE_URL, db_path=DB_PATH,
                                  n_products=n_products, n_orders_current=n_orders_current, n_china_active=n_china_active,
                                  n_stock_qty=n_stock_qty, n_in_delivery_qty=n_in_delivery_qty,
                                  inventory_value_net=inventory_value_net, n_orders_today=n_orders_today,
                                  n_issued_today=n_issued_today, n_issuable_today=n_issuable_today,
                                  overdue_count=overdue_count, overdue_total=overdue_total,
                                  issuable_order_labels=issuable_order_labels, replenishment_rows=replenishment_rows,
                                  replenishment_count=replenishment_count,
                                  recent_orders=recent_orders, status_new=status_new, status_work=status_work,
                                  status_done=status_done, status_cancelled=status_cancelled, status_total=status_total,
                                  status_divisor=status_divisor)


@app.get("/searches")
def client_searches():
    q = norm(request.args.get("q"))
    rows, source_label = load_client_search_rows(limit=5000)
    if q:
        needle = q.lower()
        rows = [
            r for r in rows
            if needle in (r.get("query") or "").lower()
            or needle in (r.get("customer_email") or "").lower()
            or needle in (r.get("customer_name") or "").lower()
        ]

    global_stats = {}
    model_stats = {}
    client_stats = {}
    phrase_events_seen = set()
    model_events_seen = set()
    for r in rows:
        query = norm(r.get("query"))
        if not query:
            continue
        email = norm(r.get("customer_email")).lower()
        name = norm(r.get("customer_name"))
        client_key = email or name or "anon"
        product_sku = norm(r.get("product_sku"))
        product_model = norm(r.get("product_model"))
        product_name = norm(r.get("product_name"))
        results_count = to_int(r.get("results_count"), 0)
        created_at = norm(r.get("created_at"))

        product_key = product_sku or product_model
        model_event_key = (email, name, query.lower(), product_key.lower(), created_at)
        if product_key and 0 < results_count <= 20 and model_event_key not in model_events_seen:
            model_events_seen.add(model_event_key)
            m = model_stats.setdefault(product_key, {
                "product_model": product_key,
                "product_sku": product_sku,
                "product_name": product_name or product_model,
                "searches_count": 0,
                "clients": set(),
                "phrases": set(),
                "last_at": "",
            })
            m["searches_count"] += 1
            m["clients"].add(client_key)
            if query:
                m["phrases"].add(query)
            if product_sku and not m.get("product_sku"):
                m["product_sku"] = product_sku
            if (product_name or product_model) and not m.get("product_name"):
                m["product_name"] = product_name or product_model
            if created_at > m["last_at"]:
                m["last_at"] = created_at

        phrase_event_key = (email, name, query.lower(), created_at)
        if phrase_event_key in phrase_events_seen:
            continue
        phrase_events_seen.add(phrase_event_key)

        g = global_stats.setdefault(query, {
            "query": query,
            "searches_count": 0,
            "clients": set(),
            "no_result_count": 0,
            "max_results": 0,
            "last_at": "",
        })
        g["searches_count"] += 1
        g["clients"].add(client_key)
        if results_count == 0:
            g["no_result_count"] += 1
        g["max_results"] = max(g["max_results"], results_count)
        if created_at > g["last_at"]:
            g["last_at"] = created_at

        client_label = name or email or "Nieznany klient"
        skey = (client_label, email, query)
        s = client_stats.setdefault(skey, {
            "client_label": client_label,
            "customer_email": email,
            "query": query,
            "searches_count": 0,
            "no_result_count": 0,
            "max_results": 0,
            "last_at": "",
        })
        s["searches_count"] += 1
        if results_count == 0:
            s["no_result_count"] += 1
        s["max_results"] = max(s["max_results"], results_count)
        if created_at > s["last_at"]:
            s["last_at"] = created_at

    model_rows = []
    for r in model_stats.values():
        item = dict(r)
        item["clients_count"] = len(item.pop("clients"))
        phrases = sorted(item.pop("phrases"))
        item["phrases_preview"] = ", ".join(phrases[:5])
        model_rows.append(item)
    model_rows.sort(key=lambda r: (r["searches_count"], r["last_at"]), reverse=True)
    model_rows = model_rows[:10]

    global_rows = []
    for r in global_stats.values():
        item = dict(r)
        item["clients_count"] = len(item.pop("clients"))
        global_rows.append(item)
    global_rows.sort(key=lambda r: (r["searches_count"], r["last_at"]), reverse=True)
    global_rows = global_rows[:10]

    summary_rows = list(client_stats.values())
    summary_rows.sort(key=lambda r: r["last_at"], reverse=True)
    summary_rows = summary_rows[:50]

    latest_rows = rows[:50]
    total_count = len(rows)

    tpl = r"""
    {% extends "base.html" %}
    {% block content %}
      <div class="card">
        <div class="flex">
          <h1 style="margin:0;">Top wyszukiwania</h1>
          <span class="badge">Łącznie: {{ total_count }}</span>
          <span class="badge">{{ source_label }}</span>
        </div>
        <form method="get" class="grid3" style="margin-top:10px;">
          <input name="q" value="{{ q }}" placeholder="Szukaj: klient / email / fraza">
          <button class="btn primary" type="submit">Szukaj</button>
          <a class="btn" href="{{ url_for('client_searches') }}">Wyczyść</a>
        </form>
      </div>

      <div class="card">
        <h2>TOP 10 modeli / SKU</h2>
        <div class="muted" style="margin-bottom:8px;">
          Najważniejsze produkty, które klienci realnie zobaczyli po wyszukaniu w panelu — także po nazwie zwyczajowej, rozstawie albo części SKU.
        </div>
        <table>
          <thead>
            <tr><th>Model / SKU</th><th>Nazwa</th><th>Ile razy</th><th>Klientów</th><th>Ostatnio</th></tr>
          </thead>
          <tbody>
            {% for r in model_rows %}
              <tr>
                <td><b>{{ r.product_model }}</b>{% if r.product_sku and r.product_sku != r.product_model %}<div class="muted">{{ r.product_sku }}</div>{% endif %}</td>
                <td>{{ r.product_name or '-' }}</td>
                <td><span class="badge">{{ r.searches_count }}</span></td>
                <td>{{ r.clients_count }}</td>
                <td class="muted">{{ r.last_at }}</td>
              </tr>
            {% endfor %}
            {% if not model_rows %}
              <tr><td colspan="5" class="muted">Brak zapisanych wyszukiwań.</td></tr>
            {% endif %}
          </tbody>
        </table>
      </div>

      <details class="card">
        <summary style="cursor:pointer;font-weight:700;font-size:16px;">Pokaż szczegóły: frazy, klienci i ostatnie wpisy</summary>

      <div style="margin-top:14px;">
        <h2>Frazy klientów</h2>
        <div class="muted" style="margin-bottom:8px;">
          Tu zostają wpisane teksty klienta. Pomaga sprawdzić, jak klienci szukają produktów i gdzie pojawiają się literówki albo brakujące nazwy.
        </div>
        <table>
          <thead>
            <tr><th>Fraza</th><th>Wyszukań</th><th>Klientów</th><th>Bez wyników</th><th>Najwięcej wyników</th><th>Ostatnio</th></tr>
          </thead>
          <tbody>
            {% for r in global_rows %}
              <tr>
                <td><b>{{ r.query }}</b></td>
                <td><span class="badge">{{ r.searches_count }}</span></td>
                <td>{{ r.clients_count }}</td>
                <td>{% if r.no_result_count %}<span class="badge">{{ r.no_result_count }}</span>{% else %}-{% endif %}</td>
                <td>{{ r.max_results }}</td>
                <td class="muted">{{ r.last_at }}</td>
              </tr>
            {% endfor %}
            {% if not global_rows %}
              <tr><td colspan="6" class="muted">Brak zapisanych fraz.</td></tr>
            {% endif %}
          </tbody>
        </table>
      </div>

      <div style="margin-top:18px;">
        <h2>Wyszukiwania według klienta</h2>
        <div class="muted" style="margin-bottom:8px;">Tu zobaczysz, kto konkretnie szukał danej frazy.</div>
        <table>
          <thead>
            <tr><th>Klient</th><th>Email</th><th>Fraza</th><th>Ile razy</th><th>Bez wyników</th><th>Ostatnio</th></tr>
          </thead>
          <tbody>
            {% for r in summary_rows %}
              <tr>
                <td><b>{{ r.client_label }}</b></td>
                <td>{{ r.customer_email or '-' }}</td>
                <td>{{ r.query }}</td>
                <td><span class="badge">{{ r.searches_count }}</span></td>
                <td>{% if r.no_result_count %}<span class="badge">{{ r.no_result_count }}</span>{% else %}-{% endif %}</td>
                <td class="muted">{{ r.last_at }}</td>
              </tr>
            {% endfor %}
            {% if not summary_rows %}
              <tr><td colspan="6" class="muted">Brak zapisanych wyszukiwań.</td></tr>
            {% endif %}
          </tbody>
        </table>
      </div>

      <div style="margin-top:18px;">
        <h2>Ostatnie wpisy</h2>
        <table>
          <thead>
            <tr><th>Czas</th><th>Klient</th><th>Email</th><th>Fraza</th><th>Model / SKU</th><th>Wyniki</th></tr>
          </thead>
          <tbody>
            {% for r in latest_rows %}
              <tr>
                <td class="muted">{{ r.created_at }}</td>
                <td>{{ r.customer_name or '-' }}</td>
                <td>{{ r.customer_email or '-' }}</td>
                <td><b>{{ r.query }}</b></td>
                <td>{{ r.product_model or r.product_sku or '-' }}</td>
                <td>{{ r.results_count }}</td>
              </tr>
            {% endfor %}
            {% if not latest_rows %}
              <tr><td colspan="6" class="muted">Brak wpisów.</td></tr>
            {% endif %}
          </tbody>
        </table>
      </div>
      </details>
    {% endblock %}
    """
    return render_template_string(tpl, title="Top wyszukiwania", base_url=BASE_URL, db_path=DB_PATH,
                                  model_rows=model_rows, global_rows=global_rows, summary_rows=summary_rows, latest_rows=latest_rows,
                                  total_count=total_count, q=q, source_label=source_label)


def client_searches_v2():
    q = norm(request.args.get("q"))
    rows, source_label = load_client_search_rows(limit=5000)

    customer_name_by_email = {}
    order_name_by_email = {}
    product_name_by_sku = {}
    known_product_names = {}
    try:
        c = conn()
        cur = c.cursor()
        cur.execute("SELECT email, name FROM customers WHERE TRIM(COALESCE(email,''))<>''")
        for rr in cur.fetchall():
            email_key = _email_key(rr["email"])
            company_name = norm(rr["name"])
            if email_key and company_name and not _order_name_is_fallback(company_name, email_key):
                customer_name_by_email[email_key] = company_name

        # Render moze miec niepelna lokalna kopie klientow. Supabase jest tutaj
        # zrodlem prawdy, wiec uzupelniamy mape nazw jednym zbiorczym odczytem.
        if supabase_enabled():
            try:
                cloud_customers = supabase_request(
                    "/rest/v1/customers",
                    params={"select": "email,name", "limit": 5000},
                    timeout=20,
                ) or []
                for rr in cloud_customers:
                    email_key = _email_key(rr.get("email"))
                    company_name = norm(rr.get("name"))
                    if email_key and company_name and not _order_name_is_fallback(company_name, email_key):
                        customer_name_by_email[email_key] = company_name
            except Exception as exc:
                app.logger.warning("Nie udalo sie pobrac nazw klientow dla historii wyszukiwan: %s", type(exc).__name__)

        cur.execute("""
          SELECT customer_email, customer_name
          FROM orders
          WHERE TRIM(COALESCE(customer_email,''))<>''
          ORDER BY id DESC
        """)
        for rr in cur.fetchall():
            email_key = _email_key(rr["customer_email"])
            company_name = norm(rr["customer_name"])
            if email_key and company_name and email_key not in order_name_by_email and not _order_name_is_fallback(company_name, email_key):
                order_name_by_email[email_key] = company_name

        cur.execute("SELECT sku, name FROM products WHERE COALESCE(archived,0)=0 AND TRIM(COALESCE(name,''))<>''")
        for rr in cur.fetchall():
            sku = norm(rr["sku"])
            product_name = norm(rr["name"])
            if sku and product_name:
                product_name_by_sku[sku.lower()] = product_name
                known_product_names.setdefault(product_name.lower(), product_name)
        c.close()
    except Exception:
        try:
            c.close()
        except Exception:
            pass

    def display_customer_name(row):
        email = _email_key(row.get("customer_email"))
        raw_name = norm(row.get("customer_name"))
        for candidate in (customer_name_by_email.get(email), order_name_by_email.get(email), raw_name):
            candidate = norm(candidate)
            if candidate and not _order_name_is_fallback(candidate, email) and "@" not in candidate:
                return candidate
        return "-"

    def canonical_product_name(row):
        product_name = norm(row.get("product_name"))
        if product_name and product_name != "-":
            return product_name
        product_sku = norm(row.get("product_sku")).lower()
        product_model = norm(row.get("product_model")).lower()
        query_key = norm(row.get("query")).lower()
        if product_sku and product_sku in product_name_by_sku:
            return product_name_by_sku[product_sku]
        if product_model and product_model in product_name_by_sku:
            return product_name_by_sku[product_model]
        if query_key and query_key in known_product_names:
            return known_product_names[query_key]
        return ""

    for row in rows:
        row["_client_label"] = display_customer_name(row)
        row["_product_label"] = canonical_product_name(row)

    if q:
        needle = q.lower()
        rows = [
            r for r in rows
            if needle in (r.get("query") or "").lower()
            or needle in (r.get("customer_name") or "").lower()
            or needle in (r.get("_client_label") or "").lower()
            or needle in (r.get("_product_label") or "").lower()
            or needle in (r.get("product_sku") or "").lower()
            or needle in (r.get("product_model") or "").lower()
        ]

    phrase_stats = {}
    name_stats = {}
    client_stats = {}
    phrase_events_seen = set()
    name_events_seen = set()

    for r in rows:
        query = norm(r.get("query"))
        if not query:
            continue
        email = norm(r.get("customer_email")).lower()
        client_label = norm(r.get("_client_label"))
        client_key = email or client_label or "anon"
        product_name = norm(r.get("_product_label"))
        results_count = to_int(r.get("results_count"), 0)
        created_at = norm(r.get("created_at"))

        name_key = product_name.lower()
        name_event_key = (client_key, query.lower(), name_key, created_at)
        if name_key and results_count > 0 and name_event_key not in name_events_seen:
            name_events_seen.add(name_event_key)
            item = name_stats.setdefault(name_key, {
                "product_name": product_name,
                "searches_count": 0,
                "clients": set(),
                "last_at": "",
            })
            item["searches_count"] += 1
            item["clients"].add(client_key)
            if created_at > item["last_at"]:
                item["last_at"] = created_at

        phrase_event_key = (client_key, query.lower(), created_at)
        if phrase_event_key in phrase_events_seen:
            continue
        phrase_events_seen.add(phrase_event_key)

        phrase = phrase_stats.setdefault(query, {
            "query": query,
            "searches_count": 0,
            "clients": set(),
            "no_result_count": 0,
            "max_results": 0,
            "last_at": "",
        })
        phrase["searches_count"] += 1
        phrase["clients"].add(client_key)
        if results_count == 0:
            phrase["no_result_count"] += 1
        phrase["max_results"] = max(phrase["max_results"], results_count)
        if created_at > phrase["last_at"]:
            phrase["last_at"] = created_at

        summary_name = client_label if client_label and client_label != "-" else "Nieznany klient"
        skey = (summary_name, query)
        summary = client_stats.setdefault(skey, {
            "client_label": summary_name,
            "query": query,
            "searches_count": 0,
            "no_result_count": 0,
            "max_results": 0,
            "last_at": "",
        })
        summary["searches_count"] += 1
        if results_count == 0:
            summary["no_result_count"] += 1
        summary["max_results"] = max(summary["max_results"], results_count)
        if created_at > summary["last_at"]:
            summary["last_at"] = created_at

    name_rows = []
    for r in name_stats.values():
        item = dict(r)
        item["clients_count"] = len(item.pop("clients"))
        name_rows.append(item)
    name_rows.sort(key=lambda r: (r["searches_count"], r["last_at"]), reverse=True)
    name_rows = name_rows[:10]

    phrase_rows = []
    for r in phrase_stats.values():
        item = dict(r)
        item["clients_count"] = len(item.pop("clients"))
        phrase_rows.append(item)
    phrase_rows.sort(key=lambda r: (r["searches_count"], r["last_at"]), reverse=True)
    phrase_rows = phrase_rows[:10]

    summary_rows = list(client_stats.values())
    summary_rows.sort(key=lambda r: r["last_at"], reverse=True)
    summary_rows = summary_rows[:50]

    latest_rows = rows[:50]
    total_count = len(rows)

    tpl = r"""
    {% extends "base.html" %}
    {% block content %}
      <div class="card">
        <div class="flex">
          <h1 style="margin:0;">Top wyszukiwania</h1>
          <span class="badge">Łącznie: {{ total_count }}</span>
          <span class="badge">{{ source_label }}</span>
        </div>
        <form method="get" class="grid3" style="margin-top:10px;">
          <input name="q" value="{{ q }}" placeholder="Szukaj: klient / fraza / nazwa">
          <button class="btn primary" type="submit">Szukaj</button>
          <a class="btn" href="{{ url_for('client_searches') }}">Wyczyść</a>
        </form>
      </div>

      <div class="card">
        <h2>TOP 10 nazw zwyczajowych</h2>
        <div class="muted" style="margin-bottom:8px;">
          Ranking jest zsumowany po nazwie zwyczajowej, np. Winsor, Carl, Cerne — bez rozbijania na każdy rozmiar SKU.
        </div>
        <table>
          <thead>
            <tr><th>Nazwa zwyczajowa</th><th>Ile razy</th><th>Klientów</th><th>Ostatnio</th></tr>
          </thead>
          <tbody>
            {% for r in name_rows %}
              <tr>
                <td><b>{{ r.product_name or '-' }}</b></td>
                <td><span class="badge">{{ r.searches_count }}</span></td>
                <td>{{ r.clients_count }}</td>
                <td class="muted">{{ r.last_at }}</td>
              </tr>
            {% endfor %}
            {% if not name_rows %}
              <tr><td colspan="4" class="muted">Brak zapisanych wyszukiwań.</td></tr>
            {% endif %}
          </tbody>
        </table>
      </div>

      <details class="card">
        <summary style="cursor:pointer;font-weight:700;font-size:16px;">Pokaż szczegóły: frazy, klienci i ostatnie wpisy</summary>

        <div style="margin-top:14px;">
          <h2>Frazy klientów</h2>
          <div class="muted" style="margin-bottom:8px;">
            Tu zostają wpisane teksty klienta. Pomaga sprawdzić, jak klienci szukają produktów i gdzie pojawiają się literówki albo brakujące nazwy.
          </div>
          <table>
            <thead>
              <tr><th>Fraza</th><th>Wyszukań</th><th>Klientów</th><th>Bez wyników</th><th>Najwięcej wyników</th><th>Ostatnio</th></tr>
            </thead>
            <tbody>
              {% for r in phrase_rows %}
                <tr>
                  <td><b>{{ r.query }}</b></td>
                  <td><span class="badge">{{ r.searches_count }}</span></td>
                  <td>{{ r.clients_count }}</td>
                  <td>{% if r.no_result_count %}<span class="badge">{{ r.no_result_count }}</span>{% else %}-{% endif %}</td>
                  <td>{{ r.max_results }}</td>
                  <td class="muted">{{ r.last_at }}</td>
                </tr>
              {% endfor %}
              {% if not phrase_rows %}
                <tr><td colspan="6" class="muted">Brak zapisanych fraz.</td></tr>
              {% endif %}
            </tbody>
          </table>
        </div>

        <div style="margin-top:18px;">
          <h2>Wyszukiwania według klienta</h2>
          <div class="muted" style="margin-bottom:8px;">Tu zobaczysz, która firma szukała danej frazy.</div>
          <table>
            <thead>
              <tr><th>Klient</th><th>Fraza</th><th>Ile razy</th><th>Bez wyników</th><th>Ostatnio</th></tr>
            </thead>
            <tbody>
              {% for r in summary_rows %}
                <tr>
                  <td><b>{{ r.client_label }}</b></td>
                  <td>{{ r.query }}</td>
                  <td><span class="badge">{{ r.searches_count }}</span></td>
                  <td>{% if r.no_result_count %}<span class="badge">{{ r.no_result_count }}</span>{% else %}-{% endif %}</td>
                  <td class="muted">{{ r.last_at }}</td>
                </tr>
              {% endfor %}
              {% if not summary_rows %}
                <tr><td colspan="5" class="muted">Brak zapisanych wyszukiwań.</td></tr>
              {% endif %}
            </tbody>
          </table>
        </div>

        <div style="margin-top:18px;">
          <h2>Ostatnie wpisy</h2>
          <table>
            <thead>
              <tr><th>Czas</th><th>Klient</th><th>Fraza</th><th>Nazwa</th><th>Model / SKU</th><th>Wyniki</th></tr>
            </thead>
            <tbody>
              {% for r in latest_rows %}
                <tr>
                  <td class="muted">{{ r.created_at }}</td>
                  <td>{{ r._client_label or '-' }}</td>
                  <td><b>{{ r.query }}</b></td>
                  <td>{{ r._product_label or '-' }}</td>
                  <td>{{ r.product_model or r.product_sku or '-' }}</td>
                  <td>{{ r.results_count }}</td>
                </tr>
              {% endfor %}
              {% if not latest_rows %}
                <tr><td colspan="6" class="muted">Brak wpisów.</td></tr>
              {% endif %}
            </tbody>
          </table>
        </div>
      </details>
    {% endblock %}
    """
    return render_template_string(tpl, title="Top wyszukiwania", base_url=BASE_URL, db_path=DB_PATH,
                                  name_rows=name_rows, phrase_rows=phrase_rows, summary_rows=summary_rows, latest_rows=latest_rows,
                                  total_count=total_count, q=q, source_label=source_label)


app.view_functions["client_searches"] = client_searches_v2


register_cash_flow(app, {
    "conn": conn,
    "now_iso": now_iso,
    "app_now": app_now,
    "to_float": to_float,
    "maybe_pull_shared_from_supabase": maybe_pull_shared_from_supabase,
    "supabase_enabled": supabase_enabled,
    "supabase_upsert_rows": supabase_upsert_rows,
    "supabase_delete_rows": supabase_delete_rows,
    "BASE_URL": BASE_URL,
    "DB_PATH": DB_PATH,
})


@app.after_request
def auto_sync_after_write(response):
    try:
        is_client_api = (
            request.path in CLIENT_API_PATHS
            or request.path.startswith("/api/invoices/")
            or request.path.startswith("/api/client/orders/")
        )
        if is_client_api:
            origin = norm(request.headers.get("Origin")).rstrip("/")
            if origin and origin in CLIENT_ALLOWED_ORIGINS:
                response.headers["Access-Control-Allow-Origin"] = origin
                response.headers["Vary"] = "Origin"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, Idempotency-Key"
        elif request.path.startswith("/api/"):
            response.headers["Access-Control-Allow-Origin"] = "*"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"

        no_auto_sync_paths = {
            "/api/client_search_log", "/api/client_order_email", "/api/client/profile"
        }
        if response.status_code < 400 and request.method in ("POST", "PUT", "PATCH", "DELETE") and request.path not in no_auto_sync_paths:
            trigger_background_supabase_sync(reason=f"{request.method} {request.path}")
    except Exception:
        pass
    return response


# -------------------------
# COMPANY
# -------------------------

def normalize_company_bank_fields(bank_account, bank_swift=""):
    """Separate a legacy `IBAN BIC` value and store both fields canonically."""
    account_raw = norm(bank_account).upper()
    swift_raw = norm(bank_swift).upper()

    # Starsze instalacje przechowywały czasem IBAN i SWIFT w jednym polu.
    match = re.fullmatch(
        r"\s*(PL)?(\d{26})(?:\s+([A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?))?\s*",
        account_raw,
    )
    if match:
        account_raw = match.group(2)
        if not swift_raw and match.group(3):
            swift_raw = match.group(3)

    # Numer konta zapisujemy bez PL, spacji i myślników. Walidację sumy
    # kontrolnej nadal wykonuje moduł KSeF przed utworzeniem XML.
    compact_account = re.sub(r"[\s-]+", "", account_raw)
    if compact_account.startswith("PL"):
        compact_account = compact_account[2:]
    if re.fullmatch(r"\d{26}", compact_account):
        account_raw = compact_account

    return account_raw, re.sub(r"\s+", "", swift_raw)

@app.get("/company")
def company():
    maybe_pull_shared_from_supabase()
    c = conn()
    cur = c.cursor()
    cur.execute("SELECT * FROM company_profile WHERE id=1")
    row = cur.fetchone()
    if row:
        clean_account, clean_swift = normalize_company_bank_fields(
            row["bank_account"], row["bank_swift"]
        )
        if clean_account != norm(row["bank_account"]) or clean_swift != norm(row["bank_swift"]):
            cur.execute(
                "UPDATE company_profile SET bank_account=?, bank_swift=?, updated_at=? WHERE id=1",
                (clean_account, clean_swift, now_iso()),
            )
            c.commit()
            cur.execute("SELECT * FROM company_profile WHERE id=1")
            row = cur.fetchone()
    c.close()

    tpl = r"""
    {% extends "base.html" %}
    {% block content %}
      <div class="card">
        <h1>Dane mojej firmy</h1>
        <div class="muted">Te dane trafiÄ… na fakturÄ™ sprzedaĹĽowÄ….</div>
      </div>

      <div class="card">
        <form method="post" action="{{ url_for('company_save') }}" class="row">
          <div><label class="muted small">Nazwa firmy</label><input name="company_name" value="{{ row['company_name'] if row else '' }}"></div>
          <div><label class="muted small">NIP</label><input name="nip" value="{{ row['nip'] if row else '' }}"></div>
          <div><label class="muted small">Telefon</label><input name="phone" value="{{ row['phone'] if row else '' }}"></div>
          <div><label class="muted small">Email</label><input name="email" value="{{ row['email'] if row else '' }}"></div>
          <div><label class="muted small">Konto bankowe</label><input name="bank_account" value="{{ row['bank_account'] if row else '' }}"></div>
          <div><label class="muted small">SWIFT / BIC</label><input name="bank_swift" value="{{ row['bank_swift'] if row else '' }}" placeholder="np. ALBPPLPWXXX"></div>
          <div><label class="muted small">Adres</label><textarea name="address">{{ row['address'] if row else '' }}</textarea></div>
          <div class="flex" style="align-items:flex-end;"><button class="btn primary" type="submit">Zapisz dane firmy</button></div>
        </form>
      </div>
    {% endblock %}
    """
    return render_template_string(tpl, title="Dane mojej firmy", base_url=BASE_URL, db_path=DB_PATH, row=row)

@app.post("/company/save")
def company_save():
    company_name = norm(request.form.get("company_name"))
    address = norm(request.form.get("address"))
    nip = norm(request.form.get("nip"))
    phone = norm(request.form.get("phone"))
    email = norm(request.form.get("email"))
    bank_account, bank_swift = normalize_company_bank_fields(
        request.form.get("bank_account"), request.form.get("bank_swift")
    )

    c = conn()
    cur = c.cursor()
    cur.execute("""
      INSERT INTO company_profile(id, company_name, address, nip, phone, email, bank_account, bank_swift, updated_at)
      VALUES(1,?,?,?,?,?,?,?,?)
      ON CONFLICT(id) DO UPDATE SET
        company_name=excluded.company_name,
        address=excluded.address,
        nip=excluded.nip,
        phone=excluded.phone,
        email=excluded.email,
        bank_account=excluded.bank_account,
        bank_swift=excluded.bank_swift,
        updated_at=excluded.updated_at
    """, (company_name, address, nip, phone, email, bank_account, bank_swift, now_iso()))
    c.commit()
    c.close()
    return redirect(url_for("company"))


# -------------------------
# PRICING
# -------------------------

@app.get("/pricing")
def pricing():
    maybe_pull_shared_from_supabase()
    q = norm(request.args.get("q"))
    eur_imported = max(0, int(to_float(request.args.get("eur_imported"), 0)))
    eur_import_error = norm(request.args.get("eur_import_error"))
    eur_local_saved = max(0, int(to_float(request.args.get("eur_local_saved"), 0)))
    c = conn()
    cur = c.cursor()
    if q:
        like = f"%{q}%"
        cur.execute("SELECT * FROM pricing WHERE model LIKE ? ORDER BY model LIMIT 2000", (like,))
    else:
        cur.execute("SELECT * FROM pricing ORDER BY model LIMIT 2000")
    rows = cur.fetchall()
    if q:
        like = f"%{q}%"
        cur.execute(
            "SELECT * FROM pricing_eur WHERE sku LIKE ? OR ean LIKE ? ORDER BY sku LIMIT 2000",
            (like, like),
        )
    else:
        cur.execute("SELECT * FROM pricing_eur ORDER BY sku LIMIT 2000")
    eur_rows = cur.fetchall()
    c.close()

    tpl = r"""
    {% extends "base.html" %}
    {% block content %}
      {% if eur_imported %}
        <div class="card" style="border-color:#9ad9c4;background:#f0fff9;">
          <b>Cennik UE zapisany w Supabase: {{ eur_imported }} pozycji.</b>
        </div>
      {% endif %}
      {% if eur_import_error %}
        <div class="card" style="border-color:#f3b8b8;background:#fff4f4;">
          <b>Cennik zapisano lokalnie ({{ eur_local_saved }} pozycji), ale Supabase odrzucił synchronizację.</b>
          <div class="muted" style="margin-top:8px;word-break:break-word;">{{ eur_import_error }}</div>
        </div>
      {% endif %}
      <div class="card">
        <h1>Cennik</h1>
        <div class="muted">Import pliku cen (kolumny: model, netto, brutto). ObsĹ‚uga CSV i XLSX (jeĹ›li dostÄ™pny openpyxl).</div>
      </div>

      <div class="card">
        <h2>Import cennika</h2>
        <form method="post" action="{{ url_for('pricing_import') }}" enctype="multipart/form-data" class="row">
          <div>
            <input type="file" name="file" accept=".csv,.xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,text/csv" required>
          </div>
          <div class="flex" style="align-items:flex-end;">
            <button class="btn primary" type="submit">Importuj cennik</button>
          </div>
        </form>
      </div>

      <div class="card">
        <h2>Cennik UE — EUR</h2>
        <div class="muted" style="margin-bottom:12px;">
          Import arkusza Preisliste.xlsx. Wymagane kolumny: Articel (SKU), PREIS EUR i UVP; GTIN/EAN jest opcjonalny.
          Import nie zmienia polskiego cennika ani stanów magazynowych.
        </div>
        <form method="post" action="{{ url_for('pricing_eur_import') }}" enctype="multipart/form-data" class="row">
          <div>
            <input type="file" name="file" accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" required>
          </div>
          <div class="flex" style="align-items:flex-end;">
            <button class="btn primary" type="submit">Importuj cennik UE</button>
          </div>
        </form>
        <div class="muted small" style="margin-top:10px;">Aktualnie zapisanych pozycji: {{ eur_rows|length }}</div>
      </div>

      <div class="card">
        <form method="get" class="grid3" style="margin-bottom:10px;">
          <input name="q" value="{{ q }}" placeholder="Szukaj modelu">
          <button class="btn primary" type="submit">Szukaj</button>
          <a class="btn" href="{{ url_for('pricing') }}">WyczyĹ›Ä‡</a>
        </form>
        <h2>Pozycje cennika</h2>
        <table>
          <thead><tr><th>Model</th><th>Netto</th><th>Brutto</th></tr></thead>
          <tbody>
            {% for r in rows %}
              <tr>
                <td><b>{{ r['model'] }}</b></td>
                <td>{{ "%.2f"|format(r['net_price']) }}</td>
                <td>{{ "%.2f"|format(r['gross_price']) }}</td>
              </tr>
            {% endfor %}
            {% if not rows %}
              <tr><td colspan="3" class="muted">Brak pozycji cennika.</td></tr>
            {% endif %}
          </tbody>
        </table>
      </div>

      <div class="card">
        <h2>Pozycje cennika UE</h2>
        <table>
          <thead><tr><th>SKU</th><th>EAN</th><th>Cena EUR</th><th>UVP EUR</th></tr></thead>
          <tbody>
            {% for r in eur_rows %}
              <tr>
                <td><b>{{ r['sku'] }}</b></td>
                <td>{{ r['ean'] or '-' }}</td>
                <td>{{ "%.2f"|format(r['price_eur']) }} EUR</td>
                <td>{{ "%.2f"|format(r['uvp_eur']) }} EUR</td>
              </tr>
            {% endfor %}
            {% if not eur_rows %}
              <tr><td colspan="4" class="muted">Cennik UE nie został jeszcze zaimportowany.</td></tr>
            {% endif %}
          </tbody>
        </table>
      </div>
    {% endblock %}
    """
    return render_template_string(
        tpl,
        title="Cennik",
        base_url=BASE_URL,
        db_path=DB_PATH,
        rows=rows,
        eur_rows=eur_rows,
        q=q,
        eur_imported=eur_imported,
        eur_import_error=eur_import_error,
        eur_local_saved=eur_local_saved,
    )

@app.post("/pricing/import")
def pricing_import():
    f = request.files.get("file")
    if not f:
        return "Brak pliku", 400

    filename = norm(f.filename).lower()
    parsed_rows = []

    if filename.endswith(".xlsx"):
        try:
            from openpyxl import load_workbook
        except Exception:
            return "Brak biblioteki openpyxl do odczytu XLSX. UĹĽyj CSV albo doinstaluj openpyxl.", 400

        wb = load_workbook(f, data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            return "Pusty plik", 400
        headers = [norm(x) for x in rows[0]]
        data = rows[1:]
        i_model = guess_col(headers, ["model"])
        i_sku = guess_col(headers, ["sku", "symbol", "index", "indeks", "kod", "code"])
        i_name = guess_col(headers, ["nazwa", "name", "produkt", "product"])
        i_ean = guess_col(headers, ["ean", "gtin"])
        i_net = guess_col(headers, ["netto", "net", "cena netto"])
        i_gross = guess_col(headers, ["brutto", "gross", "cena brutto"])
        if i_model is None or i_net is None or i_gross is None:
            return "Plik musi mieÄ‡ kolumny: model, netto, brutto", 400
        for r in data:
            if not r:
                continue
            model = norm(r[i_model]) if len(r) > i_model else ""
            if not model:
                continue
            sku = norm(r[i_sku]) if i_sku is not None and len(r) > i_sku else model
            name = norm(r[i_name]) if i_name is not None and len(r) > i_name else ""
            ean = norm(r[i_ean]) if i_ean is not None and len(r) > i_ean else ""
            net = to_float(r[i_net] if len(r) > i_net else "", 0.0)
            gross = to_float(r[i_gross] if len(r) > i_gross else "", 0.0)
            parsed_rows.append((sku, model, name, ean, net, gross))

    else:
        raw = f.read()
        try:
            text = raw.decode("utf-8-sig")
        except Exception:
            text = raw.decode("latin2", errors="replace")
        sample = text[:5000]
        delim = ";" if sample.count(";") >= sample.count(",") else ","
        rdr = csv.reader(io.StringIO(text), delimiter=delim)
        rows = list(rdr)
        if not rows:
            return "Pusty plik", 400
        headers = rows[0]
        data = rows[1:]
        i_model = guess_col(headers, ["model"])
        i_sku = guess_col(headers, ["sku", "symbol", "index", "indeks", "kod", "code"])
        i_name = guess_col(headers, ["nazwa", "name", "produkt", "product"])
        i_ean = guess_col(headers, ["ean", "gtin"])
        i_net = guess_col(headers, ["netto", "net", "cena netto"])
        i_gross = guess_col(headers, ["brutto", "gross", "cena brutto"])
        if i_model is None or i_net is None or i_gross is None:
            return "Plik musi mieÄ‡ kolumny: model, netto, brutto", 400
        for r in data:
            if not r:
                continue
            model = norm(r[i_model]) if len(r) > i_model else ""
            if not model:
                continue
            sku = norm(r[i_sku]) if i_sku is not None and len(r) > i_sku else model
            name = norm(r[i_name]) if i_name is not None and len(r) > i_name else ""
            ean = norm(r[i_ean]) if i_ean is not None and len(r) > i_ean else ""
            net = to_float(r[i_net] if len(r) > i_net else "", 0.0)
            gross = to_float(r[i_gross] if len(r) > i_gross else "", 0.0)
            parsed_rows.append((sku, model, name, ean, net, gross))

    c = conn()
    cur = c.cursor()
    changed_product_ids = []
    for sku, model, name, ean, net, gross in parsed_rows:
        cur.execute("""
          INSERT INTO pricing(model, net_price, gross_price, created_at)
          VALUES(?,?,?,?)
          ON CONFLICT(model) DO UPDATE SET
            net_price=excluded.net_price,
            gross_price=excluded.gross_price,
            created_at=excluded.created_at
        """, (model, net, gross, now_iso()))
        if sku:
            cur.execute("SELECT id FROM products WHERE sku=? LIMIT 1", (sku,))
            existing = cur.fetchone()
            if existing:
                cur.execute("""
                  UPDATE products
                  SET model=COALESCE(NULLIF(?, ''), model),
                      ean=COALESCE(NULLIF(?, ''), ean),
                      name=COALESCE(NULLIF(?, ''), name),
                      archived=0
                  WHERE sku=?
                """, (model, ean, name, sku))
                pid = int(existing["id"])
            else:
                cur.execute(
                    "INSERT INTO products(sku, model, ean, name, created_at) VALUES (?,?,?,?,?)",
                    (sku, model, ean, name, now_iso())
                )
                pid = int(cur.lastrowid)
            changed_product_ids.append(pid)
            cur.execute("INSERT OR IGNORE INTO stock(product_id, qty) VALUES (?, 0)", (pid,))
    c.commit()
    c.close()
    if supabase_enabled():
        try:
            sync_local_table_to_supabase("pricing", "model")
        except Exception:
            pass
        try:
            sync_local_rows_to_supabase("products", "id", changed_product_ids)
        except Exception:
            pass
        try:
            sync_local_rows_to_supabase("stock", "product_id", changed_product_ids)
        except Exception:
            pass
    return redirect(url_for("pricing"))


def parse_eur_pricing_xlsx(file_obj) -> list[tuple[str, str, float, float]]:
    """Read the supplier EU price list without changing products or stock."""
    try:
        from openpyxl import load_workbook
    except Exception as exc:
        raise ValueError("Brak biblioteki openpyxl do odczytu XLSX") from exc

    try:
        wb = load_workbook(file_obj, data_only=True, read_only=True)
        ws = wb.active
        raw_rows = list(ws.iter_rows(values_only=True))
    except Exception as exc:
        raise ValueError("Nie udało się odczytać pliku XLSX") from exc
    finally:
        try:
            wb.close()
        except Exception:
            pass

    if not raw_rows:
        raise ValueError("Pusty plik")

    header_index = None
    indexes = None
    for row_index, raw_header in enumerate(raw_rows[:20]):
        headers = [norm(value) for value in (raw_header or ())]
        i_sku = guess_col(headers, ["articel", "artikel", "article", "sku"])
        i_ean = guess_col(headers, ["gtin", "ean"])
        i_price = guess_col(headers, ["preis eur", "price eur", "cena eur"])
        i_uvp = guess_col(headers, ["uvp"])
        if i_sku is not None and i_price is not None:
            header_index = row_index
            indexes = (i_sku, i_ean, i_price, i_uvp)
            break

    if header_index is None or indexes is None:
        raise ValueError("Nie znaleziono kolumn Articel/SKU i PREIS EUR")

    i_sku, i_ean, i_price, i_uvp = indexes
    parsed = []
    seen_skus = set()
    for excel_row_no, row in enumerate(raw_rows[header_index + 1 :], start=header_index + 2):
        row = tuple(row or ())
        sku = norm(row[i_sku]) if len(row) > i_sku else ""
        if not sku:
            continue
        sku_key = sku.casefold()
        if sku_key in seen_skus:
            raise ValueError(f"Powtórzony SKU w wierszu {excel_row_no}: {sku}")
        price_value = row[i_price] if len(row) > i_price else None
        price_eur = money_float(to_float(price_value, -1))
        if price_eur <= 0:
            raise ValueError(f"Nieprawidłowa cena EUR w wierszu {excel_row_no}: {sku}")
        uvp_value = row[i_uvp] if i_uvp is not None and len(row) > i_uvp else None
        uvp_eur = money_float(to_float(uvp_value, price_eur * 1.45))
        if uvp_eur <= 0:
            uvp_eur = money_float(price_eur * 1.45)
        ean = norm(row[i_ean]) if i_ean is not None and len(row) > i_ean else ""
        parsed.append((sku, ean, price_eur, uvp_eur))
        seen_skus.add(sku_key)

    if not parsed:
        raise ValueError("Plik nie zawiera żadnych pozycji cennika UE")
    return parsed


@app.post("/pricing/eur/import")
def pricing_eur_import():
    uploaded = request.files.get("file")
    if not uploaded or not norm(uploaded.filename).lower().endswith(".xlsx"):
        return "Wybierz plik XLSX z cennikiem UE", 400
    try:
        parsed_rows = parse_eur_pricing_xlsx(uploaded)
    except ValueError as exc:
        return str(exc), 400

    timestamp = now_iso()
    c = conn()
    try:
        cur = c.cursor()
        for sku, ean, price_eur, uvp_eur in parsed_rows:
            cur.execute(
                """
                INSERT INTO pricing_eur(sku, ean, price_eur, uvp_eur, created_at, updated_at)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(sku) DO UPDATE SET
                  ean=excluded.ean,
                  price_eur=excluded.price_eur,
                  uvp_eur=excluded.uvp_eur,
                  updated_at=excluded.updated_at
                """,
                (sku, ean, price_eur, uvp_eur, timestamp, timestamp),
            )
        c.commit()
    finally:
        c.close()

    if supabase_enabled():
        try:
            sync_local_table_to_supabase("pricing_eur", "sku")
        except Exception as exc:
            error_detail = norm(str(exc))[:600] or type(exc).__name__
            app.logger.exception("Nie udało się zsynchronizować cennika UE z Supabase")
            return redirect(url_for(
                "pricing",
                eur_local_saved=len(parsed_rows),
                eur_import_error=error_detail,
            ))
    return redirect(url_for("pricing", eur_imported=len(parsed_rows)))


# -------------------------
# CUSTOMERS
# -------------------------

@app.get("/customers")
def customers():
    maybe_pull_shared_from_supabase()
    q = norm(request.args.get("q"))
    c = conn()
    cur = c.cursor()
    if q:
        like = f"%{q}%"
        cur.execute("""
          SELECT * FROM customers
          WHERE name LIKE ? OR phone LIKE ? OR email LIKE ? OR address LIKE ? OR nip LIKE ?
          ORDER BY id DESC
          LIMIT 500
        """, (like, like, like, like, like))
    else:
        cur.execute("SELECT * FROM customers ORDER BY id DESC LIMIT 500")
    rows = cur.fetchall()
    c.close()

    tpl = r"""
    {% extends "base.html" %}
    {% block content %}
      <div class="card">
        <h1>Klienci stali</h1>
        <form method="get" class="grid3" style="margin-top:10px;">
          <input name="q" value="{{ q }}" placeholder="Szukaj: nazwa / telefon / email / adres / NIP">
          <button class="btn primary" type="submit">Szukaj</button>
          <a class="btn" href="{{ url_for('customers') }}">WyczyĹ›Ä‡</a>
        </form>
      </div>

      <div class="card">
        <h2>Dodaj klienta</h2>
        <form method="post" action="{{ url_for('customers_create') }}" class="row">
          <div>
            <label class="muted small">Nazwa</label>
            <input name="name" required>
          </div>
          <div>
            <label class="muted small">Telefon</label>
            <input name="phone">
          </div>
          <div>
            <label class="muted small">Email</label>
            <input name="email">
          </div>
          <div>
            <label class="muted small">NIP</label>
            <input name="nip" placeholder="np. 1234567890">
          </div>
          <div>
            <label class="muted small">Adres</label>
            <textarea name="address" placeholder="Ulica, kod, miasto"></textarea>
          </div>
          <div>
            <label class="muted small">Język panelu klienta</label>
            <select name="language">
              <option value="pl">PL — polski</option><option value="de">DE — niemiecki</option>
              <option value="en">EN — angielski</option><option value="es">ES — hiszpański</option>
              <option value="it">IT — włoski</option>
            </select>
          </div>
          <div>
            <label class="muted small">Cennik klienta</label>
            <select name="price_list">
              <option value="pln">Polska — PLN</option>
              <option value="eu_eur">UE — EUR</option>
            </select>
          </div>
          <div class="flex" style="align-items:flex-end;">
            <button class="btn primary" type="submit">Zapisz klienta</button>
          </div>
        </form>
      </div>

      <div class="card">
        <h2>Lista klientĂłw</h2>
        <table>
          <thead>
            <tr><th>Nazwa</th><th>Telefon</th><th>Email</th><th>NIP</th><th>Język</th><th>Cennik</th><th>Adres</th><th>Akcje</th></tr>
          </thead>
          <tbody>
            {% for r in rows %}
              <tr>
                <td><b>{{ r['name'] }}</b></td>
                <td>{{ r['phone'] or '-' }}</td>
                <td>{{ r['email'] or '-' }}</td>
                <td>{{ r['nip'] or '-' }}</td>
                <td><span class="badge">{{ (r['language'] or 'pl')|upper }}</span></td>
                <td><span class="badge">{{ 'UE — EUR' if r['price_list'] == 'eu_eur' else 'Polska — PLN' }}</span></td>
                <td style="white-space:pre-line;">{{ r['address'] or '-' }}</td>
                <td>
                  <div class="flex">
                    <a class="btn" href="{{ url_for('customers_edit', customer_id=r['id']) }}">Edytuj</a>
                    <form method="post" action="{{ url_for('customers_delete', customer_id=r['id']) }}" onsubmit="return confirm('UsunÄ…Ä‡ klienta?')">
                      <button class="btn danger" type="submit">UsuĹ„</button>
                    </form>
                  </div>
                </td>
              </tr>
            {% endfor %}
            {% if not rows %}
              <tr><td colspan="8" class="muted">Brak klientĂłw.</td></tr>
            {% endif %}
          </tbody>
        </table>
      </div>
    {% endblock %}
    """
    return render_template_string(tpl, title="Klienci", base_url=BASE_URL, db_path=DB_PATH, rows=rows, q=q)

@app.post("/customers/create")
def customers_create():
    name = norm(request.form.get("name"))
    address = norm(request.form.get("address"))
    phone = norm(request.form.get("phone"))
    email = norm(request.form.get("email"))
    nip = norm(request.form.get("nip"))
    language = normalize_client_language(request.form.get("language"))
    price_list = price_list_for_language(language)
    if not name:
        return "Brak nazwy klienta", 400

    if supabase_enabled():
        remote_first_create_customer(name, address, phone, email, nip, language, price_list)
    else:
        c = conn()
        cur = c.cursor()
        cur.execute(
            "INSERT INTO customers(name, address, phone, email, nip, language, price_list, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (name, address, phone, email, nip, language, price_list, now_iso())
        )
        c.commit()
        c.close()

    try:
        link_orders_to_customers_by_email(sync_remote=True)
    except Exception:
        pass
    return redirect(url_for("customers"))

@app.get("/customers/<int:customer_id>/edit")
def customers_edit(customer_id):
    c = conn()
    cur = c.cursor()
    cur.execute("SELECT * FROM customers WHERE id=?", (customer_id,))
    row = cur.fetchone()
    c.close()
    if not row:
        return "Nie znaleziono klienta", 404

    tpl = r"""
    {% extends "base.html" %}
    {% block content %}
      <div class="card">
        <h1>Edycja klienta</h1>
        <div class="muted">ZmieĹ„ dane zapisane dla staĹ‚ego klienta.</div>
      </div>

      <div class="card">
        <form method="post" action="{{ url_for('customers_update', customer_id=row['id']) }}" class="row">
          <div>
            <label class="muted small">Nazwa</label>
            <input name="name" value="{{ row['name'] }}" required>
          </div>
          <div>
            <label class="muted small">Telefon</label>
            <input name="phone" value="{{ row['phone'] or '' }}">
          </div>
          <div>
            <label class="muted small">Email</label>
            <input name="email" value="{{ row['email'] or '' }}">
          </div>
          <div>
            <label class="muted small">NIP</label>
            <input name="nip" value="{{ row['nip'] or '' }}" placeholder="np. 1234567890">
          </div>
          <div>
            <label class="muted small">Adres</label>
            <textarea name="address" placeholder="Ulica, kod, miasto">{{ row['address'] or '' }}</textarea>
          </div>
          <div>
            <label class="muted small">Język panelu klienta</label>
            <select name="language">
              {% for code, label in [('pl','PL — polski'),('de','DE — niemiecki'),('en','EN — angielski'),('es','ES — hiszpański'),('it','IT — włoski')] %}
                <option value="{{ code }}" {% if (row['language'] or 'pl') == code %}selected{% endif %}>{{ label }}</option>
              {% endfor %}
            </select>
          </div>
          <div>
            <label class="muted small">Cennik klienta</label>
            <select name="price_list">
              <option value="pln" {% if (row['price_list'] or 'pln') == 'pln' %}selected{% endif %}>Polska — PLN</option>
              <option value="eu_eur" {% if row['price_list'] == 'eu_eur' %}selected{% endif %}>UE — EUR</option>
            </select>
          </div>
          <div class="flex" style="align-items:flex-end;">
            <button class="btn primary" type="submit">Zapisz zmiany</button>
            <a class="btn" href="{{ url_for('customers') }}">PowrĂłt</a>
          </div>
        </form>
      </div>
    {% endblock %}
    """
    return render_template_string(tpl, title="Edycja klienta", base_url=BASE_URL, db_path=DB_PATH, row=row)

@app.post("/customers/<int:customer_id>/update")
def customers_update(customer_id):
    name = norm(request.form.get("name"))
    address = norm(request.form.get("address"))
    phone = norm(request.form.get("phone"))
    email = norm(request.form.get("email"))
    nip = norm(request.form.get("nip"))
    language = normalize_client_language(request.form.get("language"))
    price_list = price_list_for_language(language)
    if not name:
        return "Brak nazwy klienta", 400

    c = conn()
    cur = c.cursor()
    cur.execute("""
      UPDATE customers
      SET name=?, address=?, phone=?, email=?, nip=?, language=?, price_list=?
      WHERE id=?
    """, (name, address, phone, email, nip, language, price_list, customer_id))
    c.commit()
    c.close()

    if supabase_enabled():
        supabase_update_rows("customers", {
            "name": name,
            "address": address,
            "phone": phone,
            "email": email,
            "nip": nip,
            "language": language,
            "price_list": price_list,
        }, {"id": customer_id})

    try:
        link_orders_to_customers_by_email(sync_remote=True)
    except Exception:
        pass
    return redirect(url_for("customers"))

@app.post("/customers/<int:customer_id>/delete")
def customers_delete(customer_id):
    if supabase_enabled():
        supabase_delete_rows("customers", {"id": customer_id})

    c = conn()
    cur = c.cursor()
    cur.execute("DELETE FROM customers WHERE id=?", (customer_id,))
    c.commit()
    c.close()
    return redirect(url_for("customers"))


# -------------------------
# PRODUCTS
# -------------------------

@app.get("/products")
def products():
    maybe_pull_shared_from_supabase()
    q = norm(request.args.get("q"))
    c = conn()
    cur = c.cursor()
    if q:
        like = f"%{q}%"
        cur.execute("""
          SELECT p.*, COALESCE(s.qty,0) AS stock
          FROM products p
          LEFT JOIN stock s ON s.product_id=p.id
          WHERE COALESCE(p.archived,0)=0
            AND (p.sku LIKE ? OR p.model LIKE ? OR p.ean LIKE ? OR p.name LIKE ?)
          ORDER BY p.sku
          LIMIT 1000
        """, (like, like, like, like))
    else:
        cur.execute("""
          SELECT p.*, COALESCE(s.qty,0) AS stock
          FROM products p
          LEFT JOIN stock s ON s.product_id=p.id
          WHERE COALESCE(p.archived,0)=0
          ORDER BY p.sku
          LIMIT 1000
        """)
    rows = cur.fetchall()
    c.close()

    tpl = r"""
    {% extends "base.html" %}
    {% block content %}
      <div class="card">
        <div class="flex">
          <h1 style="margin:0;">Produkty</h1>
          <div class="right"></div>
        </div>
        {% if request.args.get('product_deleted') %}<div class="notice" style="margin-top:10px;color:#067a2d;">Usunięto produkt {{ request.args.get('product_deleted') }}.</div>{% endif %}
        {% if request.args.get('product_error') %}<div class="notice" style="margin-top:10px;color:#b00020;">{{ request.args.get('product_error') }}</div>{% endif %}
        <form method="get" class="grid3" style="margin-top:10px;">
          <input name="q" value="{{ q }}" placeholder="Szukaj: SKU / model / EAN / nazwa">
          <button class="btn primary" type="submit">Szukaj</button>
          <a class="btn" href="{{ url_for('products') }}">WyczyĹ›Ä‡</a>
        </form>
      </div>

      <div class="card">
        <h2>Import CSV (478 pozycji)</h2>
        <div class="muted">Wybierz plik CSV z Excela. Minimalnie: kolumna SKU (unikalna). PozostaĹ‚e: model, ean, name/nazwa.</div>
        <form method="post" action="{{ url_for('products_import') }}" enctype="multipart/form-data" class="row" style="margin-top:10px;">
          <div>
            <input type="file" name="file" accept=".csv,text/csv" required>
            <div class="muted small" style="margin-top:6px;">Kodowanie: najlepiej UTF-8. Separator zwykle â€ž;â€ť lub â€ž,â€ť â€“ program sam sprĂłbuje.</div>
          </div>
          <div class="flex" style="align-items:flex-end;">
            <button class="btn primary" type="submit">Importuj</button>
          </div>
        </form>
      </div>

      <div class="card">
        <h2>Lista (max 1000)</h2>
        <table>
          <thead>
            <tr>
              <th>SKU</th>
              <th>Model</th>
              <th>EAN</th>
              <th>Nazwa</th>
              <th>Stan</th>
              <th>Akcje</th>
            </tr>
          </thead>
          <tbody>
            {% for r in rows %}
            <tr>
              <td><b>{{ r["sku"] }}</b></td>
              <td>{{ r["model"] or "" }}</td>
              <td>{{ r["ean"] or "" }}</td>
              <td>{{ r["name"] or "" }}</td>
              <td><span class="badge">{{ r["stock"] }}</span></td>
              <td>
                <form method="post" action="{{ url_for('product_delete', product_id=r['id']) }}" onsubmit="return confirm('Usunąć wybrany produkt? Tej operacji nie można cofnąć.');">
                  <button class="btn danger" type="submit">Usuń</button>
                </form>
              </td>
            </tr>
            {% endfor %}
            {% if not rows %}
              <tr><td colspan="5" class="muted">Brak produktĂłw. ZrĂłb import CSV.</td></tr>
            {% endif %}
          </tbody>
        </table>
      </div>
    {% endblock %}
    """
    return render_template_string(tpl, title="Produkty", base_url=BASE_URL, db_path=DB_PATH, rows=rows, q=q)

@app.post("/products/<int:product_id>/delete")
def product_delete(product_id):
    c = conn()
    cur = c.cursor()
    cur.execute("""
      SELECT p.id, p.sku, COALESCE(s.qty,0) AS stock
      FROM products p
      LEFT JOIN stock s ON s.product_id=p.id
      WHERE p.id=?
    """, (product_id,))
    product = cur.fetchone()
    if not product:
        c.close()
        return redirect(url_for("products", product_error="Nie znaleziono produktu."))

    # Usuwanie z katalogu jest archiwizacją produktu. Historyczne dokumenty
    # nadal wskazują ten sam stabilny identyfikator, ale produkt nie jest już
    # dostępny w magazynie, panelu klienta ani formularzach nowych dokumentów.
    active_references = []
    cur.execute("""
      SELECT COUNT(*) AS n
      FROM order_items oi
      JOIN orders o ON o.id=oi.order_id
      WHERE oi.product_id=?
        AND LOWER(COALESCE(o.status,'')) IN
            ('new','pending','unconfirmed','confirmed','packed','in_delivery','shipped')
        AND COALESCE(o.warehouse_issued,0)=0
    """, (product_id,))
    if int(cur.fetchone()["n"] or 0) > 0:
        active_references.append("aktywnych zamówieniach")
    cur.execute("""
      SELECT COUNT(*) AS n
      FROM china_items ci
      JOIN china_packages cp ON cp.id=ci.package_id
      WHERE ci.product_id=?
        AND LOWER(COALESCE(cp.status,'')) IN ('planned','ordered','shipped')
    """, (product_id,))
    if int(cur.fetchone()["n"] or 0) > 0:
        active_references.append("aktywnych dostawach P/O")
    if int(product["stock"] or 0) != 0:
        active_references.append("niezerowym stanie magazynowym")

    sku = product["sku"]
    if active_references:
        c.close()
        return redirect(url_for(
            "products",
            q=sku,
            product_error="Nie można usunąć produktu, ponieważ jest używany w " + ", ".join(active_references) + ".",
        ))

    try:
        if supabase_enabled():
            # Kolejność ma znaczenie: najpierw ukrycie produktu, następnie dane
            # aktywnego katalogu. Stare order_items i invoice_allocations zostają.
            supabase_update_rows("products", {"archived": True}, {"id": product_id})
            supabase_delete_rows("stock", {"product_id": product_id})
            try:
                supabase_delete_rows("pricing_eur", {"sku": sku})
            except Exception:
                app.logger.warning("Nie udało się usunąć ceny EUR dla SKU %s", sku, exc_info=True)

        cur.execute("UPDATE products SET archived=1 WHERE id=?", (product_id,))
        cur.execute("DELETE FROM stock WHERE product_id=?", (product_id,))
        cur.execute("DELETE FROM pricing_eur WHERE sku=?", (sku,))
        c.commit()
    except Exception:
        c.rollback()
        c.close()
        app.logger.exception("Nie udało się zarchiwizować produktu %s", product_id)
        return redirect(url_for(
            "products",
            q=sku,
            product_error="Nie udało się usunąć produktu z aktywnego magazynu. Najpierw uruchom migrację Supabase.",
        ))
    c.close()
    return redirect(url_for("products", product_deleted=sku))

    references = []
    for table, label in (
        ("order_items", "zamówieniach"),
        ("china_items", "dostawach P/O"),
        ("invoice_allocations", "fakturach"),
    ):
        cur.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE product_id=?", (product_id,))
        if int(cur.fetchone()["n"] or 0) > 0:
            references.append(label)
    if int(product["stock"] or 0) != 0:
        references.append("stanie magazynowym")

    sku = product["sku"]
    if references:
        c.close()
        return redirect(url_for(
            "products",
            q=sku,
            product_error="Nie można usunąć produktu, ponieważ jest używany w " + ", ".join(references) + ".",
        ))

    try:
        if supabase_enabled():
            supabase_delete_rows("stock", {"product_id": product_id})
            try:
                supabase_delete_rows("products", {"id": product_id})
            except Exception:
                cur.execute("SELECT * FROM stock WHERE product_id=?", (product_id,))
                stock_row = cur.fetchone()
                if stock_row:
                    supabase_upsert_rows("stock", [dict(stock_row)], "product_id")
                raise
            try:
                supabase_delete_rows("pricing_eur", {"sku": sku})
            except Exception:
                # Brak osobnej ceny EUR nie może cofnąć poprawnego usunięcia
                # produktu. Osierocona cena nie jest widoczna bez produktu.
                app.logger.warning("Nie udało się usunąć ceny EUR dla SKU %s", sku, exc_info=True)

        cur.execute("DELETE FROM pricing_eur WHERE sku=?", (sku,))
        cur.execute("DELETE FROM stock WHERE product_id=?", (product_id,))
        cur.execute("DELETE FROM products WHERE id=?", (product_id,))
        c.commit()
    except Exception:
        c.rollback()
        c.close()
        app.logger.exception("Nie udało się bezpiecznie usunąć produktu %s", product_id)
        return redirect(url_for("products", q=sku, product_error="Nie udało się usunąć produktu. Sprawdź synchronizację magazynu."))
    c.close()
    return redirect(url_for("products", product_deleted=sku))

@app.post("/products/import")
def products_import():
    f = request.files.get("file")
    if not f:
        return "Brak pliku", 400

    raw = f.read()
    # SprĂłbuj UTF-8, jak nie pĂłjdzie to latin2
    try:
        text = raw.decode("utf-8-sig")
    except:
        text = raw.decode("latin2", errors="replace")

    # SprĂłbuj wykryÄ‡ delimiter
    sample = text[:5000]
    delim = ";" if sample.count(";") >= sample.count(",") else ","

    rdr = csv.reader(io.StringIO(text), delimiter=delim)
    rows = list(rdr)
    if not rows:
        return "Pusty CSV", 400

    headers = rows[0]
    data = rows[1:]

    i_sku = guess_col(headers, ["sku", "symbol", "index", "indeks", "kod", "code"])
    i_model = guess_col(headers, ["model", "model_uchwytu", "nazwa_modelu"])
    i_ean = guess_col(headers, ["ean", "gtin"])
    i_name = guess_col(headers, ["name", "nazwa", "produkt", "product"])

    if i_sku is None:
        return "CSV musi mieÄ‡ kolumnÄ™ SKU / Symbol / Indeks", 400

    c = conn()
    cur = c.cursor()
    added = 0
    updated = 0

    for row in data:
        if not row or len(row) <= i_sku:
            continue
        sku = norm(row[i_sku])
        if not sku:
            continue
        model = norm(row[i_model]) if i_model is not None and len(row) > i_model else ""
        ean = norm(row[i_ean]) if i_ean is not None and len(row) > i_ean else ""
        name = norm(row[i_name]) if i_name is not None and len(row) > i_name else ""

        cur.execute("SELECT id FROM products WHERE sku=?", (sku,))
        exists = cur.fetchone()
        if exists:
            cur.execute("UPDATE products SET model=?, ean=?, name=?, archived=0 WHERE sku=?", (model, ean, name, sku))
            updated += 1
            pid = exists["id"]
        else:
            cur.execute(
                "INSERT INTO products(sku, model, ean, name, created_at) VALUES (?,?,?,?,?)",
                (sku, model, ean, name, now_iso())
            )
            pid = cur.lastrowid
            added += 1

        cur.execute("INSERT OR IGNORE INTO stock(product_id, qty) VALUES (?, 0)", (pid,))

    c.commit()
    changed_ids = [int(r["id"]) for r in cur.execute(
        "SELECT id FROM products WHERE COALESCE(archived,0)=0"
    ).fetchall()]
    c.close()

    if supabase_enabled() and changed_ids:
        try:
            sync_local_rows_to_supabase("products", "id", changed_ids)
            sync_local_rows_to_supabase("stock", "product_id", changed_ids)
        except Exception:
            app.logger.warning("Nie udało się zsynchronizować importu produktów", exc_info=True)

    return redirect(url_for("products", q=""))


# -------------------------
# STOCK
# -------------------------

@app.get("/stock")
def stock():
    maybe_pull_shared_from_supabase()
    q = norm(request.args.get("q"))
    rows = build_replenishment_analysis(conn, today=app_now().date(), horizon_days=60)
    stock_total = sum(to_int(row.get("stock_qty"), 0) for row in rows)
    stock_with_china_total = sum(
        to_int(row.get("stock_qty"), 0) + to_int(row.get("incoming_qty"), 0)
        for row in rows
    )
    reserved_total = sum(
        to_int(row.get("reserved_qty"), 0) + to_int(row.get("reserved_incoming"), 0)
        for row in rows
    )
    if q:
        query = q.casefold()
        rows = [
            row for row in rows
            if any(query in norm(row.get(field)).casefold() for field in ("sku", "model", "ean", "name"))
        ]
    rows = sorted(rows, key=lambda row: norm(row.get("sku")).casefold())[:1000]

    tpl = r"""
    {% extends "base.html" %}
    {% block content %}
      <style>
        .stock-summary{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px;margin-bottom:16px}
        .stock-summary-card{background:#fff;border:1px solid #e7eaf2;border-radius:22px;padding:18px 20px;box-shadow:var(--shadow)}
        .stock-summary-card span{display:block;color:#718096;font-size:12px;font-weight:700}
        .stock-summary-card b{display:block;margin-top:5px;color:#17233c;font-size:28px;letter-spacing:-.6px}
        .stock-summary-card small{display:block;margin-top:4px;color:#2da176;font-size:11px}
        @media(max-width:760px){.stock-summary{grid-template-columns:1fr}}
      </style>

      <div class="stock-summary">
        <div class="stock-summary-card">
          <span>Na stanie łącznie</span>
          <b>{{ stock_total }} szt.</b>
          <small>Fizycznie w magazynie</small>
        </div>
        <div class="stock-summary-card">
          <span>Na stanie + CHINY</span>
          <b>{{ stock_with_china_total }} szt.</b>
          <small>Magazyn oraz towar w drodze</small>
        </div>
        <div class="stock-summary-card">
          <span>W zamówieniach</span>
          <b>{{ reserved_total }} szt.</b>
          <small>Rezerwacje aktywnych zamówień</small>
        </div>
      </div>

      <div class="card">
        <div class="flex">
          <h1 style="margin:0;">Magazyn</h1>
        </div>
        <form method="get" class="grid3" style="margin-top:10px;">
          <input name="q" value="{{ q }}" placeholder="Szukaj produktu: SKU / model / EAN / nazwa">
          <button class="btn primary" type="submit">Szukaj</button>
          <a class="btn" href="{{ url_for('stock') }}">WyczyĹ›Ä‡</a>
        </form>
      </div>

      <div class="card">
        <h2>Korekta stanu</h2>
        <div class="row">
          <div>
            <label class="muted small">Produkt (SKU)</label>
            <input list="skuList" id="skuInput" placeholder="np. CH010-BB-N28">
            <datalist id="skuList">
              {% for r in rows %}
                <option value="{{ r['sku'] }}">{{ r['sku'] }}</option>
              {% endfor %}
            </datalist>
          </div>
          <div>
            <label class="muted small">Zmiana (np. +10 albo -3)</label>
            <input id="deltaInput" placeholder="+10">
          </div>
        </div>
        <div class="flex" style="margin-top:10px;">
          <button class="btn ok" onclick="applyDelta(); return false;">Zapisz korektÄ™</button>
          <div class="muted" id="deltaMsg"></div>
        </div>
      </div>

      <div class="card">
        <h2>Stany (max 1000)</h2>
        <div class="muted" style="margin-bottom:8px;">
          Rezerwacje obejmują wszystkie aktywne, niewydane zamówienia. Jeżeli bieżący stan nie wystarcza, brakująca część rezerwuje towar w drodze.
        </div>
        <table>
          <thead>
            <tr><th>SKU</th><th>Model</th><th>EAN</th><th>Nazwa</th><th>Stan</th><th>Rezerwacje</th><th>Dostępne</th><th>W drodze</th><th>Zarezerwowane w drodze</th><th>Dostępne w drodze</th></tr>
          </thead>
          <tbody>
            {% for r in rows %}
              <tr>
                <td><b>{{ r['sku'] }}</b></td>
                <td>{{ r['model'] or "" }}</td>
                <td>{{ r['ean'] or "" }}</td>
                <td>{{ r['name'] or "" }}</td>
                <td><span class="badge">{{ r['stock_qty'] }}</span></td>
                <td><span class="badge">{{ r['reserved_qty'] }}</span></td>
                <td><span class="badge">{{ r['available_qty'] }}</span></td>
                <td><span class="badge">{{ r['incoming_qty'] }}</span></td>
                <td><span class="badge">{{ r['reserved_incoming'] }}</span></td>
                <td><span class="badge">{{ r['available_incoming'] }}</span></td>
              </tr>
            {% endfor %}
            {% if not rows %}
              <tr><td colspan="10" class="muted">Brak produktów.</td></tr>
            {% endif %}
          </tbody>
        </table>
      </div>

<script>
async function applyDelta(){
  const sku = document.getElementById("skuInput").value.trim();
  const delta = document.getElementById("deltaInput").value.trim();
  const msg = document.getElementById("deltaMsg");
  msg.innerText = "";
  if(!sku){ msg.innerText = "Podaj SKU"; return; }
  if(!delta){ msg.innerText = "Podaj zmianÄ™"; return; }

  const r = await fetch("/api/stock_delta", {
    method:"POST",
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({sku, delta})
  });
  const j = await r.json();
  if(!j.ok){ msg.innerText = "BĹ‚Ä…d: " + (j.error || ""); return; }
  msg.innerText = "OK. Nowy stan: " + j.new_qty;
  setTimeout(()=>location.reload(), 500);
}
</script>

    {% endblock %}
    """
    return render_template_string(
        tpl,
        title="Magazyn",
        base_url=BASE_URL,
        db_path=DB_PATH,
        rows=rows,
        q=q,
        stock_total=stock_total,
        stock_with_china_total=stock_with_china_total,
        reserved_total=reserved_total,
    )

@app.post("/api/stock_delta")
def api_stock_delta():
    data = request.get_json(force=True, silent=True) or {}
    sku = norm(data.get("sku"))
    delta_raw = norm(data.get("delta"))

    if not sku:
        return jsonify(ok=False, error="Brak SKU"), 400

    delta = to_int(delta_raw, None)
    if delta is None:
        # sprĂłbuj +10 / -3
        try:
            delta = int(delta_raw)
        except:
            return jsonify(ok=False, error="NieprawidĹ‚owa zmiana (np. +10 lub -3)"), 400

    c = conn()
    cur = c.cursor()
    cur.execute("SELECT id FROM products WHERE sku=? AND COALESCE(archived,0)=0", (sku,))
    p = cur.fetchone()
    if not p:
        c.close()
        return jsonify(ok=False, error="Nie ma takiego SKU"), 404
    pid = p["id"]
    cur.execute("INSERT OR IGNORE INTO stock(product_id, qty) VALUES (?, 0)", (pid,))
    cur.execute("UPDATE stock SET qty = qty + ? WHERE product_id=?", (delta, pid))
    cur.execute("SELECT qty FROM stock WHERE product_id=?", (pid,))
    new_qty = cur.fetchone()["qty"]
    c.commit()
    c.close()
    return jsonify(ok=True, new_qty=new_qty)

@app.get("/api/product/<int:product_id>")
def api_product(product_id):
    c = conn()
    cur = c.cursor()
    cur.execute("""
      SELECT p.*, COALESCE(s.qty,0) AS stock
      FROM products p
      LEFT JOIN stock s ON s.product_id=p.id
      WHERE p.id=? AND COALESCE(p.archived,0)=0
    """, (product_id,))
    r = cur.fetchone()
    c.close()
    if not r:
        return jsonify(ok=False), 404
    return jsonify(ok=True, id=r["id"], sku=r["sku"], model=r["model"], ean=r["ean"], name=r["name"], stock=r["stock"])


# -------------------------
# ORDERS
# -------------------------

@app.get("/orders")
def orders():
    maybe_pull_shared_from_supabase()
    try:
        link_orders_to_customers_by_email(sync_remote=True)
    except Exception:
        pass
    q = norm(request.args.get("q"))
    tab = norm(request.args.get("tab")) or "new"
    ready_today = norm(request.args.get("ready_today")) == "1"
    if tab not in {"new", "issued", "realized", "all"}:
        tab = "new"

    c = conn()
    cur = c.cursor()

    cur.execute("""
      SELECT
        SUM(CASE WHEN LOWER(COALESCE(status,''))='completed' THEN 1 ELSE 0 END) AS completed_count,
        SUM(CASE WHEN LOWER(COALESCE(status,'')) IN ('in_delivery','issued')
          OR (COALESCE(warehouse_issued,0)=1 AND LOWER(COALESCE(status,'')) NOT IN ('completed','cancelled'))
          THEN 1 ELSE 0 END) AS issued_count,
        SUM(CASE WHEN COALESCE(warehouse_issued,0)=0
          AND LOWER(COALESCE(status,'')) NOT IN ('in_delivery','issued','completed','cancelled')
          THEN 1 ELSE 0 END) AS to_issue_count
      FROM orders
    """)
    stats_row = cur.fetchone()
    order_stats = {
        "completed": int(stats_row["completed_count"] or 0),
        "issued": int(stats_row["issued_count"] or 0),
        "to_issue": int(stats_row["to_issue_count"] or 0),
    }

    where_parts = []
    params = []

    if tab == "new":
        where_parts.append("COALESCE(o.warehouse_issued,0)=0")
        where_parts.append("LOWER(COALESCE(o.status,'')) NOT IN ('in_delivery','issued','completed','cancelled')")
    elif tab == "issued":
        where_parts.append("(LOWER(COALESCE(o.status,'')) IN ('in_delivery','issued') OR (COALESCE(o.warehouse_issued,0)=1 AND LOWER(COALESCE(o.status,'')) NOT IN ('completed','cancelled')))")
    elif tab == "realized":
        where_parts.append("LOWER(COALESCE(o.status,''))='completed'")

    if q:
        where_parts.append("(order_no LIKE ? OR customer_name LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like])

    where_sql = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""
    sql = f"""
      SELECT o.*,
             COALESCE((
               SELECT SUM(oi.qty * COALESCE(oi.unit_net_price, pr.net_price, 0))
               FROM order_items oi
               LEFT JOIN products p ON p.id=oi.product_id
               LEFT JOIN pricing pr ON (TRIM(LOWER(pr.model)) = TRIM(LOWER(p.model)) OR TRIM(LOWER(pr.model)) = TRIM(LOWER(p.sku)))
               WHERE oi.order_id=o.id
             ), 0) AS order_value_net,
             CASE WHEN EXISTS (
               SELECT 1
               FROM order_items oi
               LEFT JOIN stock s ON s.product_id=oi.product_id
               WHERE oi.order_id=o.id
                 AND (
                   COALESCE(s.qty,0) + COALESCE((
                     SELECT SUM(ci.qty)
                     FROM china_items ci
                     JOIN china_packages cp ON cp.id=ci.package_id
                     WHERE ci.product_id=oi.product_id
                       AND cp.status IN ('planned', 'ordered', 'shipped')
                   ),0)
                 ) < oi.qty
             ) THEN 1 ELSE 0 END AS has_shortage
      FROM orders o
      {where_sql}
      ORDER BY o.id DESC
      LIMIT 300
    """
    cur.execute(sql, tuple(params))
    rows = [dict(r) for r in cur.fetchall()]

    visible_open_ids = sorted([r["id"] for r in rows if int(r.get("warehouse_issued") or 0) == 0 and norm(r["status"]).lower() in CURRENT_ORDER_STATUSES])
    if visible_open_ids:
        status_ph = ",".join(["?"] * len(CURRENT_ORDER_STATUSES))
        cur.execute(f"SELECT id FROM orders WHERE COALESCE(warehouse_issued,0)=0 AND LOWER(COALESCE(status,'')) IN ({status_ph}) AND id<=? ORDER BY id", (*sorted(CURRENT_ORDER_STATUSES), visible_open_ids[-1]))
        open_order_ids = [int(r["id"]) for r in cur.fetchall()]

        ph = ",".join(["?"] * len(open_order_ids))
        cur.execute(f"""
          SELECT oi.order_id, oi.product_id, SUM(oi.qty) AS qty
          FROM order_items oi
          WHERE oi.order_id IN ({ph})
          GROUP BY oi.order_id, oi.product_id
        """, tuple(open_order_ids))
        demand_rows = cur.fetchall()

        by_order = {}
        product_ids = set()
        for dr in demand_rows:
            oid = int(dr["order_id"])
            pid = int(dr["product_id"])
            qty = int(dr["qty"])
            by_order.setdefault(oid, []).append((pid, qty))
            product_ids.add(pid)

        pool_stock = {}
        pool_delivery = {}
        if product_ids:
            pph = ",".join(["?"] * len(product_ids))
            cur.execute(f"""
              SELECT p.id AS product_id,
                     COALESCE(s.qty,0) AS stock_qty,
                     COALESCE((
                       SELECT SUM(ci.qty)
                       FROM china_items ci
                       JOIN china_packages cp ON cp.id=ci.package_id
                       WHERE ci.product_id=p.id
                         AND cp.status IN ('planned', 'ordered', 'shipped')
                     ),0) AS in_delivery_qty
              FROM products p
              LEFT JOIN stock s ON s.product_id=p.id
              WHERE p.id IN ({pph})
            """, tuple(product_ids))
            for pr in cur.fetchall():
                pid = int(pr["product_id"])
                pool_stock[pid] = int(pr["stock_qty"])
                pool_delivery[pid] = int(pr["in_delivery_qty"])

        has_shortage = {oid: 0 for oid in open_order_ids}
        for oid in open_order_ids:
            for pid, need0 in by_order.get(oid, []):
                need = int(need0)
                stock_now = pool_stock.get(pid, 0)
                from_stock = min(stock_now, need)
                pool_stock[pid] = stock_now - from_stock
                need -= from_stock

                delivery_now = pool_delivery.get(pid, 0)
                from_delivery = min(delivery_now, need)
                pool_delivery[pid] = delivery_now - from_delivery
                need -= from_delivery

                if need > 0:
                    has_shortage[oid] = 1

        for r in rows:
            if r["status"] in ("new", "packed", "confirmed", "in_delivery"):
                r["has_shortage"] = has_shortage.get(r["id"], 0)
            else:
                r["has_shortage"] = 0

    if ready_today:
        # Dokladnie ten sam warunek co na kafelku pulpitu: cale zamowienie
        # musi miescic sie w fizycznym stanie. Towar w drodze nie wystarcza.
        status_ph = ",".join(["?"] * len(CURRENT_ORDER_STATUSES))
        cur.execute(f"""
          SELECT o.id, o.created_at, oi.product_id, SUM(oi.qty) AS required_qty
          FROM orders o
          JOIN order_items oi ON oi.order_id=o.id
          WHERE LOWER(COALESCE(o.status,'')) IN ({status_ph})
            AND COALESCE(o.warehouse_issued,0)=0
          GROUP BY o.id, oi.product_id
          ORDER BY o.created_at, o.id, oi.product_id
        """, tuple(sorted(CURRENT_ORDER_STATUSES)))
        ready_demand = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT product_id, MAX(0, COALESCE(qty,0)) AS qty FROM stock")
        ready_stock = {int(r["product_id"]): int(r["qty"] or 0) for r in cur.fetchall()}
        ready_orders = {}
        for demand in ready_demand:
            oid = int(demand["id"])
            ready_orders.setdefault(oid, []).append(
                (int(demand["product_id"]), int(demand["required_qty"] or 0))
            )
        ready_ids = set()
        for oid, needs in ready_orders.items():
            if needs and all(ready_stock.get(pid, 0) >= qty for pid, qty in needs):
                ready_ids.add(oid)
                for pid, qty in needs:
                    ready_stock[pid] = ready_stock.get(pid, 0) - qty
        rows = [r for r in rows if int(r["id"]) in ready_ids]

    c.close()

    tpl = r"""
    {% extends "base.html" %}
    {% block content %}
      <style>
        .order-stat-card{position:relative;display:flex;align-items:center;gap:16px;min-height:122px;text-decoration:none;color:inherit;cursor:pointer;overflow:hidden;transition:transform .15s ease,box-shadow .15s ease,border-color .15s ease;background:#fff;}
        .order-stat-card::after{content:'›';position:absolute;right:22px;top:50%;transform:translateY(-50%);font-size:30px;font-weight:400;opacity:.34;}
        .order-stat-card:hover{transform:translateY(-3px);box-shadow:0 16px 34px rgba(24,45,84,.14);}
        .order-stat-card:focus-visible{outline:3px solid rgba(79,111,235,.28);outline-offset:2px;}
        .order-stat-icon{display:grid;place-items:center;flex:0 0 50px;width:50px;height:50px;border-radius:15px;font-size:22px;font-weight:900;}
        .order-stat-copy{min-width:0;padding-right:34px;}.order-stat-label{font-weight:750;}.order-stat-value{font-size:34px;font-weight:850;line-height:1;margin-top:10px;}.order-stat-hint{font-size:12px;margin-top:7px;color:#71809f;}
        .order-stat-card.completed{border-color:#cdebdc;background:linear-gradient(135deg,#fff 45%,#effaf4)}.order-stat-card.completed .order-stat-icon{background:#dcf7e8;color:#08744d;}
        .order-stat-card.issued{border-color:#d8e2ff;background:linear-gradient(135deg,#fff 45%,#f0f4ff)}.order-stat-card.issued .order-stat-icon{background:#e1e9ff;color:#3156c7;}
        .order-stat-card.to-issue{border-color:#f4dfad;background:linear-gradient(135deg,#fff 45%,#fff8e8)}.order-stat-card.to-issue .order-stat-icon{background:#ffefc7;color:#9a6200;}
        .order-stat-card.active{border-width:2px;box-shadow:0 12px 28px rgba(24,45,84,.14);}.order-stat-card.active::after{opacity:.75;}
        .orders-toolbar{display:flex;align-items:center;gap:10px;justify-content:flex-end;margin-bottom:12px;}.orders-toolbar .all-orders{margin-right:auto;}
        @media(max-width:760px){.order-stat-card{min-height:104px}.orders-toolbar{align-items:stretch;flex-direction:column}.orders-toolbar .all-orders{margin-right:0}}
      </style>
      <div class="grid3">
        <a class="card order-stat-card completed {% if tab=='realized' %}active{% endif %}" href="{{ url_for('orders', tab='realized') }}" aria-label="Pokaż zrealizowane zamówienia" {% if tab=='realized' %}aria-current="page"{% endif %}>
          <div class="order-stat-icon">✓</div><div class="order-stat-copy"><div class="order-stat-label">Zrealizowane łącznie</div><div class="order-stat-value">{{ order_stats.completed }}</div><div class="order-stat-hint">Pokaż zakończone</div></div>
        </a>
        <a class="card order-stat-card issued {% if tab=='issued' %}active{% endif %}" href="{{ url_for('orders', tab='issued') }}" aria-label="Pokaż wydane zamówienia" {% if tab=='issued' %}aria-current="page"{% endif %}>
          <div class="order-stat-icon">⇢</div><div class="order-stat-copy"><div class="order-stat-label">Wydane</div><div class="order-stat-value">{{ order_stats.issued }}</div><div class="order-stat-hint">Pokaż wydane</div></div>
        </a>
        <a class="card order-stat-card to-issue {% if tab=='new' %}active{% endif %}" href="{{ url_for('orders', tab='new') }}" aria-label="Pokaż zamówienia do wydania" {% if tab=='new' %}aria-current="page"{% endif %}>
          <div class="order-stat-icon">○</div><div class="order-stat-copy"><div class="order-stat-label">Do wydania</div><div class="order-stat-value">{{ order_stats.to_issue }}</div><div class="order-stat-hint">Pokaż oczekujące</div></div>
        </a>
      </div>
      <div class="card">
        <div class="orders-toolbar">
          <a class="btn all-orders {% if tab=='all' %}primary{% endif %}" href="{{ url_for('orders', tab='all') }}">Wszystkie zamówienia</a>
          <a class="btn primary" href="{{ url_for('order_new') }}">+ Nowe zamówienie</a>
        </div>
        <form method="get" class="grid3">
          <input type="hidden" name="tab" value="{{ tab }}">
          <input name="q" value="{{ q }}" placeholder="Szukaj: numer zamĂłwienia lub klient">
          <button class="btn primary" type="submit">Szukaj</button>
          <a class="btn" href="{{ url_for('orders', tab=tab) }}">WyczyĹ›Ä‡</a>
        </form>
      </div>

      <style>
        .st-unconfirmed{background:#ef4444;color:#fff;border-color:#ef4444;}
        .st-confirmed{background:#16a34a;color:#fff;border-color:#16a34a;}
        .st-delivery{background:#2563eb;color:#fff;border-color:#2563eb;}
        .st-issued{background:#6b7280;color:#fff;border-color:#6b7280;}
      </style>

      <div class="card">
        <table>
          <thead>
            <tr><th>Nr</th><th>Klient</th><th>Status</th><th>WartoĹ›Ä‡ netto</th><th>Data</th><th>Akcje</th></tr>
          </thead>
          <tbody>
            {% for r in rows %}
              <tr {% if tab == 'new' and (r['has_shortage'] or r['status'] in ['new','pending','unconfirmed']) %}style="background:#ffe7e7;"{% endif %}>
                <td><b>{{ order_display_no(r['id'], r['created_at'], r['order_no'], r['note']) }}</b></td>
                <td>{{ r['customer_name'] }}</td>
                <td><span class="badge {{ order_status_css(r['status']) }}">{{ order_status_label(r['status']) }}</span></td>
                <td><span class="badge">{{ "%.2f"|format(r['order_value_net']) }} {{ r['currency'] or 'PLN' }}</span></td>
                <td class="muted">{{ r['created_at'] }}</td>
                <td class="flex">
                  <a class="btn" href="{{ url_for('order_view', order_id=r['id']) }}">SzczegĂłĹ‚y</a>
                  {% if r['status'] != 'issued' %}
                    <a class="btn" href="{{ url_for('order_label', order_id=r['id']) }}">Etykieta 30x50</a>
                    <form method="post" action="{{ url_for('order_delete', order_id=r['id']) }}" onsubmit="return confirm('UsunÄ…Ä‡ zamĂłwienie?')">
                      <button class="btn danger" type="submit">UsuĹ„</button>
                    </form>
                  {% else %}
                    <span class="muted">PodglÄ…d</span>
                  {% endif %}
                </td>
              </tr>
            {% endfor %}
            {% if not rows %}
              <tr><td colspan="5" class="muted">Brak zamĂłwieĹ„.</td></tr>
            {% endif %}
          </tbody>
        </table>
      </div>
    {% endblock %}
    """
    return render_template_string(tpl, title="ZamĂłwienia", base_url=BASE_URL, db_path=DB_PATH, rows=rows, q=q, tab=tab, order_stats=order_stats, order_status_label=order_status_label, order_status_css=order_status_css, canonical_order_no=canonical_order_no)

@app.get("/orders/new")
def order_new():
    maybe_pull_shared_from_supabase()
    c = conn()
    cur = c.cursor()
    cur.execute("SELECT id, sku, model, name FROM products WHERE COALESCE(archived,0)=0 ORDER BY sku LIMIT 5000")
    products_rows = cur.fetchall()
    cur.execute("SELECT id, name, address, phone, email, nip FROM customers ORDER BY name")
    customers_rows = cur.fetchall()
    c.close()

    tpl = r"""
    {% extends "base.html" %}
    {% block content %}
      <div class="card">
        <h1>Nowe zamĂłwienie</h1>
        <div class="muted">Produkty wybierasz z bazy. Przy wyborze pokazuje stan magazynowy.</div>
      </div>

      <div class="card">
        <form method="post" action="{{ url_for('order_create') }}">
          <div class="row">
            <div>
              <label class="muted small">Wybierz staĹ‚ego klienta (opcjonalnie)</label>
              <select id="customerSelect" name="customer_id" onchange="fillCustomer(this.value)">
                <option value="">-- rÄ™cznie / nowy klient --</option>
                {% for c in customers %}
                  <option value="{{ c['id'] }}">{{ c['name'] }}</option>
                {% endfor %}
              </select>
            </div>
            <div class="muted">Po wyborze pola klienta zostanÄ… automatycznie uzupeĹ‚nione.</div>
          </div>

          <div class="row">
            <div>
              <label class="muted small">ZamawiajÄ…cy (nazwa firmy / osoba)</label>
              <input name="customer_name" required>
            </div>
            <div>
              <label class="muted small">Telefon</label>
              <input name="customer_phone">
            </div>
          </div>

          <div class="row" style="margin-top:10px;">
            <div>
              <label class="muted small">Adres (na etykietÄ™)</label>
              <textarea name="customer_address" placeholder="Ulica, kod, miasto, kraj"></textarea>
            </div>
            <div>
              <label class="muted small">Email</label>
              <input name="customer_email">
              <div style="height:10px;"></div>
              <label class="muted small">Adres WysyĹ‚ki</label>
              <input name="note">
            </div>
          </div>

          <div class="line"></div>

          <div class="flex">
            <h2 style="margin:0;">Pozycje zamĂłwienia</h2>
            <button class="btn" onclick="addItemRow(); return false;">+ Dodaj pozycjÄ™</button>
          </div>

          <div id="itemsContainer" style="margin-top:10px;"></div>

          <template id="itemRowTpl">
            <div class="items-row card" style="margin:10px 0;">
              <div>
                <label class="muted small">Produkt (SKU)</label>
                <select name="product_id[]" onchange="refreshStock(this.value, this.dataset.stockTarget)" data-stock-target="">
                  <option value="">-- wybierz --</option>
                  {% for p in products %}
                    <option value="{{ p['id'] }}">{{ p['sku'] }}{% if p['model'] %} â€˘ {{ p['model'] }}{% endif %}{% if p['name'] %} â€˘ {{ p['name'] }}{% endif %}</option>
                  {% endfor %}
                </select>
              </div>
              <div>
                <label class="muted small">IloĹ›Ä‡</label>
                <input name="qty[]" value="1">
              </div>
              <div>
                <label class="muted small">Stan</label>
                <div class="badge" id="">-</div>
              </div>
              <div class="flex" style="align-items:flex-end;">
                <button class="btn danger" onclick="removeRow(this); return false;">UsuĹ„</button>
              </div>
            </div>
          </template>

          <div class="line"></div>
          <button class="btn primary" type="submit">Zapisz zamĂłwienie</button>
          <a class="btn" href="{{ url_for('orders') }}">Anuluj</a>
        </form>
      </div>

<script>
// po dodaniu wiersza trzeba podpiÄ…Ä‡ ID na badge (stan)
function addItemRow(){
  const tpl = document.getElementById("itemRowTpl");
  const container = document.getElementById("itemsContainer");
  const node = tpl.content.cloneNode(true);

  // znajdĹş select i badge w nowo wstawionym wierszu
  const wrap = node.querySelector(".items-row");
  const select = wrap.querySelector("select");
  const badge = wrap.querySelector(".badge");

  const id = "stock_" + Math.random().toString(36).slice(2);
  badge.id = id;
  select.dataset.stockTarget = id;

  container.appendChild(node);
}

addItemRow(); // startowo 1 pozycja

const customersData = {{ customers_json|safe }};
function fillCustomer(customerId){
  if(!customerId || !customersData[customerId]) return;
  const c = customersData[customerId];
  document.querySelector('input[name="customer_name"]').value = c.name || '';
  document.querySelector('textarea[name="customer_address"]').value = c.address || '';
  document.querySelector('input[name="customer_phone"]').value = c.phone || '';
  document.querySelector('input[name="customer_email"]').value = c.email || '';
}
</script>

    {% endblock %}
    """
    customers_json = {
        str(r["id"]): {
            "name": r["name"],
            "address": r["address"],
            "phone": r["phone"],
            "email": r["email"],
        }
        for r in customers_rows
    }
    return render_template_string(
        tpl,
        title="Nowe zamĂłwienie",
        base_url=BASE_URL,
        db_path=DB_PATH,
        products=products_rows,
        customers=customers_rows,
        customers_json=json.dumps(customers_json, ensure_ascii=False)
    )

@app.post("/orders/create")
def order_create():
    customer_id = to_int(request.form.get("customer_id"), 0)
    customer_name = norm(request.form.get("customer_name"))
    if not customer_name:
        return "Brak zamawiajÄ…cego", 400

    customer_address = norm(request.form.get("customer_address"))
    customer_phone = norm(request.form.get("customer_phone"))
    customer_email = norm(request.form.get("customer_email"))
    note = norm(request.form.get("note"))

    product_ids = request.form.getlist("product_id[]")
    qtys = request.form.getlist("qty[]")

    items = []
    for pid, q in zip(product_ids, qtys):
        pid = to_int(pid, 0)
        qty = to_int(q, 0)
        if pid > 0 and qty > 0:
            items.append((pid, qty))

    if not items:
        return "Dodaj minimum 1 pozycjÄ™", 400

    if supabase_enabled():
        oid = remote_first_create_order(customer_id if customer_id > 0 else None, customer_name, customer_address, customer_phone, customer_email, note, items)
    else:
        c = conn()
        cur = c.cursor()
        created_at = now_iso()
        cur.execute("""
          INSERT INTO orders(order_no, customer_id, customer_name, customer_address, customer_phone, customer_email, status, note, created_at, qr_data_url)
          VALUES(?,?,?,?,?,?,?,?,?,?)
        """, ("TEMP", customer_id if customer_id > 0 else None, customer_name, customer_address, customer_phone, customer_email, "new", note, created_at, ""))
        oid = cur.lastrowid

        order_no = make_order_no(oid, created_at)
        qr_data_url = ""
        cur.execute("UPDATE orders SET order_no=?, qr_data_url=? WHERE id=?", (order_no, qr_data_url, oid))

        for pid, qty in items:
            cur.execute("SELECT sku FROM products WHERE id=?", (pid,))
            p = cur.fetchone()
            if not p:
                continue
            sku = p["sku"]
            cur.execute("""
              INSERT INTO order_items(order_id, product_id, sku, qty, created_at)
              VALUES(?,?,?,?,?)
            """, (oid, pid, sku, qty, now_iso()))

        c.commit()
        c.close()

    try:
        normalize_temp_order_numbers()
    except Exception:
        pass
    return redirect(url_for("order_view", order_id=oid))

@app.get("/orders/<int:order_id>")
def order_view(order_id):
    maybe_pull_shared_from_supabase()
    try:
        link_orders_to_customers_by_email(sync_remote=True)
    except Exception:
        pass
    c = conn()
    cur = c.cursor()
    cur.execute("SELECT * FROM orders WHERE id=?", (order_id,))
    o = cur.fetchone()
    if not o:
        c.close()
        abort(404)

    cur.execute("""
      SELECT oi.*, p.model, p.ean, p.name,
             COALESCE(s.qty, 0) AS stock_qty,
             COALESCE(oi.unit_net_price, pr.net_price, 0) AS net_price,
             COALESCE(oi.unit_gross_price, pr.gross_price, 0) AS gross_price,
             COALESCE(oi.currency, ord.currency, 'PLN') AS currency,
             (oi.qty * COALESCE(oi.unit_net_price, pr.net_price, 0)) AS line_value_net,
             (oi.qty * COALESCE(oi.unit_gross_price, pr.gross_price, 0)) AS line_value_gross,
             COALESCE(s.qty,0) AS stock,
             COALESCE((
                SELECT SUM(ci.qty)
                FROM china_items ci
                JOIN china_packages cp ON cp.id=ci.package_id
                WHERE ci.product_id=oi.product_id
                  AND cp.status IN ('planned', 'ordered', 'shipped')
             ), 0) AS in_delivery
      FROM order_items oi
      JOIN orders ord ON ord.id=oi.order_id
      JOIN products p ON p.id=oi.product_id
      LEFT JOIN stock s ON s.product_id=p.id
      LEFT JOIN pricing pr ON (TRIM(LOWER(pr.model)) = TRIM(LOWER(p.model)) OR TRIM(LOWER(pr.model)) = TRIM(LOWER(p.sku)))
      WHERE oi.order_id=?
      ORDER BY oi.id
    """, (order_id,))
    items = [dict(r) for r in cur.fetchall()]

    for it in items:
        it["in_delivery_available"] = int(it.get("in_delivery", 0))
        it["delivery_used"] = 0
        it["line_shortage"] = 0

    if norm(o["status"]).lower() in CURRENT_ORDER_STATUSES:
        status_ph = ",".join(["?"] * len(CURRENT_ORDER_STATUSES))
        cur.execute(f"SELECT id FROM orders WHERE LOWER(COALESCE(status,'')) IN ({status_ph}) AND id<=? ORDER BY id", (*sorted(CURRENT_ORDER_STATUSES), order_id))
        scoped_order_ids = [int(r["id"]) for r in cur.fetchall()]
        if scoped_order_ids:
            sph = ",".join(["?"] * len(scoped_order_ids))
            cur.execute(f"""
              SELECT oi.id, oi.order_id, oi.product_id, oi.qty
              FROM order_items oi
              WHERE oi.order_id IN ({sph})
              ORDER BY oi.order_id, oi.id
            """, tuple(scoped_order_ids))
            seq_items = cur.fetchall()

            product_ids = {int(r["product_id"]) for r in seq_items}
            pool_stock = {}
            pool_delivery = {}
            if product_ids:
                pph = ",".join(["?"] * len(product_ids))
                cur.execute(f"""
                  SELECT p.id AS product_id,
                         COALESCE(s.qty,0) AS stock_qty,
                         COALESCE((
                           SELECT SUM(ci.qty)
                           FROM china_items ci
                           JOIN china_packages cp ON cp.id=ci.package_id
                           WHERE ci.product_id=p.id
                             AND cp.status IN ('planned', 'ordered', 'shipped')
                         ),0) AS in_delivery_qty
                  FROM products p
                  LEFT JOIN stock s ON s.product_id=p.id
                  WHERE p.id IN ({pph})
                """, tuple(product_ids))
                for pr in cur.fetchall():
                    pid = int(pr["product_id"])
                    pool_stock[pid] = int(pr["stock_qty"])
                    pool_delivery[pid] = int(pr["in_delivery_qty"])

            item_alloc = {}
            for sr in seq_items:
                pid = int(sr["product_id"])
                need = int(sr["qty"])

                stock_now = pool_stock.get(pid, 0)
                from_stock = min(stock_now, need)
                pool_stock[pid] = stock_now - from_stock
                need_after_stock = need - from_stock

                delivery_now = pool_delivery.get(pid, 0)
                from_delivery = min(delivery_now, need_after_stock)
                pool_delivery[pid] = delivery_now - from_delivery
                shortage = need_after_stock - from_delivery

                if int(sr["order_id"]) == order_id:
                    item_alloc[int(sr["id"])] = {
                        "in_delivery_available": from_delivery,
                        "delivery_used": from_delivery,
                        "line_shortage": shortage,
                    }

            for it in items:
                al = item_alloc.get(int(it["id"]))
                if al:
                    it.update(al)

    cur.execute("SELECT id, sku, model, name FROM products WHERE COALESCE(archived,0)=0 ORDER BY sku LIMIT 5000")
    products_rows = cur.fetchall()
    c.close()

    order_url = build_public_url(url_for("order_view", order_id=order_id))

    tpl = r"""
    {% extends "base.html" %}
    {% block content %}
      <div class="card">
        <div class="flex">
          <h1 style="margin:0;">{{ order_display_no(o['id'], o['created_at'], o['order_no'], o['note']) }}</h1>
          <span class="badge {{ order_status_css(o['status']) }}">{{ order_status_label(o['status']) }}</span>
          <div class="right flex">
            <a class="btn" href="{{ url_for('orders') }}">â† Lista</a>
            <a class="btn primary" href="{{ url_for('order_packing_list_download_admin', order_id=o['id']) }}" target="_blank">Pakuj</a>
            {% if (o['currency'] or 'PLN') == 'EUR' %}
              <a class="btn primary" href="{{ url_for('order_proforma', order_id=o['id']) }}" target="_blank">Proforma EUR</a>
              <span class="badge" title="Dokument końcowy wystawiasz ręcznie poza modułem KSeF.">Faktura końcowa — ręcznie</span>
            {% else %}
              <a class="btn primary" href="{{ url_for('order_invoice', order_id=o['id']) }}">Faktura</a>
            {% endif %}
            <form method="post" action="{{ url_for('order_confirmation_resend', order_id=o['id']) }}">
              <button class="btn" type="submit">Wyślij ponownie potwierdzenie</button>
            </form>
              {% if (o['status'] or '')|lower not in ['completed','cancelled'] %}
                <form method="post" action="{{ url_for('order_status_update', order_id=o['id']) }}" onsubmit="return confirm('Oznaczyć to zamówienie jako zrealizowane? Status będzie widoczny również w panelu klienta.')">
                  <input type="hidden" name="status" value="completed">
                  <button class="btn ok" type="submit">Zrealizowane</button>
                </form>
              {% endif %}
              <a class="btn primary" href="{{ url_for('order_label', order_id=o['id']) }}">Etykieta 30x50</a>
              {% if locked %}
                <span class="badge">Wydane z magazynu</span>
              {% endif %}
              <form method="post" action="{{ url_for('order_delete', order_id=o['id']) }}" onsubmit="return confirm('UsunÄ…Ä‡ zamĂłwienie?')">
                <button class="btn danger" type="submit">UsuĹ„ zamĂłwienie</button>
              </form>
          </div>
        </div>
        <div class="muted" style="margin-top:6px;">{{ o['created_at'] }}</div>
        <form method="post" action="{{ url_for('order_mark_shipped', order_id=o['id']) }}" class="flex" style="margin-top:14px;padding:14px;border:1px solid #dbe4f2;border-radius:16px;background:#f8fbff;">
          <div><b>Wysyłka do klienta</b><div class="muted">Wpisz numer przesyłki i oznacz zamówienie jako wysłane.</div></div>
          <select name="carrier" required style="min-width:150px;">
            <option value="">-- Kurier --</option>
            {% for carrier_key, carrier_name in [('inpost','InPost'),('dpd','DPD'),('fedex','FedEx'),('dhl','DHL'),('ups','UPS')] %}
              <option value="{{ carrier_key }}" {% if (o['carrier'] or '')|lower == carrier_key %}selected{% endif %}>{{ carrier_name }}</option>
            {% endfor %}
          </select>
          <input name="tracking_no" value="{{ o['tracking_no'] or '' }}" placeholder="Numer śledzenia" required style="min-width:260px;">
          <button class="btn primary" type="submit">Wysłane</button>
          {% if o['tracking_no'] %}<a class="btn" target="_blank" href="{{ carrier_tracking_url(o['carrier'], o['tracking_no']) }}">Śledź</a>{% endif %}
        </form>
        {% if request.args.get('shipment_sent') == '1' %}<div class="hint" style="margin-top:10px;">Status i numer przesyłki zapisane. Klient otrzymał e-mail.</div>{% endif %}
        {% if request.args.get('shipment_email_error') %}<div class="hint" style="margin-top:10px;">Status zapisany, ale e-mail nie został wysłany: {{ request.args.get('shipment_email_error') }}</div>{% endif %}
        {% if request.args.get('confirmation_sent') == '1' %}
          <div class="hint" style="margin-top:10px;">Potwierdzenie zamówienia zostało wysłane ponownie.</div>
        {% elif request.args.get('confirmation_error') %}
          <div class="hint" style="margin-top:10px; border-color:#fecaca; background:#fff1f2;">Nie udało się wysłać potwierdzenia: {{ request.args.get('confirmation_error') }}</div>
        {% endif %}
      </div>

      <div class="row">
        <div class="card">
          <h2>ZamawiajÄ…cy</h2>
          <div><b>{{ o['customer_name'] }}</b></div>
          <div class="muted" style="white-space:pre-line; margin-top:6px;">{{ o['customer_address'] or "-" }}</div>
          <div class="muted" style="margin-top:6px;">Tel: {{ o['customer_phone'] or "-" }}</div>
          <div class="muted">Email: {{ o['customer_email'] or "-" }}</div>
          <div class="line"></div>
          <div class="muted small">Kod zamĂłwienia do skanowania: <b>{{ canonical_order_no(o['id'], o['created_at'], o['order_no']) }}</b></div>
          <div class="muted small" style="margin-top:10px;">QR jest uĹĽywany do etykiety 30x50 i skanowania zamĂłwienia.</div>
        </div>

        <div class="card">
          <h2>Notatka</h2>
          <div>{{ o['note'] or "-" }}</div>
          <div class="line"></div>
          <div class="hint">
            <b>Wydaj z magazynu</b> odejmie iloĹ›ci z magazynu, ale nie zmieni automatycznie statusu klienta na â€žZrealizowaneâ€ť.<br>
            JeĹ›li brakuje stanu, pozycja moĹĽe byÄ‡ realizowana z <b>towaru w drodze z Chin</b> (kolumna â€žW dostawieâ€ť poniĹĽej).
          </div>
        </div>
      </div>

      {% if not locked %}
      <div class="card">
        <h2>Dodaj produkt do zamĂłwienia</h2>
        <form method="post" action="{{ url_for('order_item_add', order_id=o['id']) }}" class="items-row">
          <div>
            <select name="product_id" required>
              <option value="">-- wybierz produkt --</option>
              {% for p in products %}
                <option value="{{ p['id'] }}">{{ p['sku'] }}{% if p['model'] %} â€˘ {{ p['model'] }}{% endif %}{% if p['name'] %} â€˘ {{ p['name'] }}{% endif %}</option>
              {% endfor %}
            </select>
          </div>
          <div>
            <input name="qty" value="1" required>
          </div>
          <div class="flex" style="align-items:flex-end;">
            <button class="btn primary" type="submit">Dodaj</button>
          </div>
        </form>
      </div>
      {% endif %}

      <div class="card">
        <h2>Pozycje</h2>
        <table>
          <thead>
            <tr><th>SKU</th><th>Model / Nazwa</th><th>IloĹ›Ä‡</th><th>Cena netto</th><th>Cena brutto</th><th>WartoĹ›Ä‡ netto</th><th>WartoĹ›Ä‡ brutto</th><th>Stan teraz</th><th>W dostawie (dostÄ™pne)</th><th>Realizacja</th><th>Akcje</th></tr>
          </thead>
          <tbody>
            {% set ns = namespace(total_net=0, total_gross=0) %}
            {% for it in items %}
              {% set ns.total_net = ns.total_net + it['line_value_net'] %}
              {% set ns.total_gross = ns.total_gross + it['line_value_gross'] %}
              <tr>
                <td><b>{{ it['sku'] }}</b></td>
                <td>
                  {{ it['model'] or "" }}
                  {% if it['name'] %}<div class="muted small">{{ it['name'] }}</div>{% endif %}
                  {% if it['ean'] %}<div class="muted small">EAN: {{ it['ean'] }}</div>{% endif %}
                </td>
                <td>
                  {% if locked %}
                    <span class="badge">{{ it['qty'] }}</span>
                  {% else %}
                    <form method="post" action="{{ url_for('order_item_update', order_id=o['id'], item_id=it['id']) }}" class="flex">
                      <input name="qty" value="{{ it['qty'] }}" style="width:90px;">
                      <button class="btn" type="submit">ZmieĹ„</button>
                    </form>
                  {% endif %}
                </td>
                <td><span class="badge">{{ "%.2f"|format(it['net_price']) }} {{ it['currency'] or o['currency'] or 'PLN' }}</span></td>
                <td><span class="badge">{{ "%.2f"|format(it['gross_price']) }} {{ it['currency'] or o['currency'] or 'PLN' }}</span></td>
                <td><span class="badge">{{ "%.2f"|format(it['line_value_net']) }} {{ it['currency'] or o['currency'] or 'PLN' }}</span></td>
                <td><span class="badge">{{ "%.2f"|format(it['line_value_gross']) }} {{ it['currency'] or o['currency'] or 'PLN' }}</span></td>
                <td><span class="badge">{{ it['stock'] }}</span></td>
                <td><span class="badge">{{ it['in_delivery_available'] }}</span></td>
                <td>
                  {% if it['line_shortage'] <= 0 and it['delivery_used'] == 0 %}
                    <span class="badge">Z magazynu</span>
                  {% elif it['line_shortage'] <= 0 %}
                    <span class="badge">CzÄ™Ĺ›Ä‡ / caĹ‚oĹ›Ä‡ z Chin</span>
                  {% else %}
                    <span class="badge">Brak towaru</span>
                  {% endif %}
                </td>
                <td>
                  {% if not locked %}
                    <form method="post" action="{{ url_for('order_item_delete', order_id=o['id'], item_id=it['id']) }}" onsubmit="return confirm('UsunÄ…Ä‡ pozycjÄ™?')">
                      <button class="btn danger" type="submit">UsuĹ„</button>
                    </form>
                  {% else %}
                    <span class="muted">PodglÄ…d</span>
                  {% endif %}
                </td>
              </tr>
            {% endfor %}
            {% if items %}
              <tr>
                <td colspan="5" style="text-align:right;"><b>Suma netto:</b></td>
                <td><span class="badge"><b>{{ "%.2f"|format(ns.total_net) }} {{ o['currency'] or 'PLN' }}</b></span></td>
                <td colspan="5"></td>
              </tr>
              <tr>
                <td colspan="6" style="text-align:right;"><b>Suma brutto:</b></td>
                <td><span class="badge"><b>{{ "%.2f"|format(ns.total_gross) }} {{ o['currency'] or 'PLN' }}</b></span></td>
                <td colspan="4"></td>
              </tr>
            {% else %}
              <tr><td colspan="11" class="muted">Brak pozycji w zamĂłwieniu.</td></tr>
            {% endif %}
          </tbody>
        </table>
      </div>
    {% endblock %}
    """
    return render_template_string(tpl, title=canonical_order_no(o["id"], o["created_at"], o["order_no"]), base_url=BASE_URL, db_path=DB_PATH, o=o, items=items, order_url=order_url, products=products_rows, locked=(int(o["warehouse_issued"] or 0)==1), order_status_label=order_status_label, order_status_css=order_status_css, canonical_order_no=canonical_order_no)


@app.post("/orders/<int:order_id>/confirmation/resend")
def order_confirmation_resend(order_id):
    try:
        maybe_pull_shared_from_supabase(force=True)
    except Exception:
        pass
    result = _safe_saved_order_confirmation(order_id, force=True)
    if result.get("ok"):
        return redirect(url_for("order_view", order_id=order_id, confirmation_sent="1"))
    return redirect(url_for("order_view", order_id=order_id, confirmation_error=norm(result.get("error")) or "Nieznany błąd"))

@app.post("/orders/<int:order_id>/items/add")
def order_item_add(order_id):
    product_id = to_int(request.form.get("product_id"), 0)
    qty = to_int(request.form.get("qty"), 0)
    if product_id <= 0 or qty <= 0:
        return "NieprawidĹ‚owy produkt lub iloĹ›Ä‡", 400

    c = conn()
    cur = c.cursor()
    cur.execute("SELECT status, warehouse_issued FROM orders WHERE id=?", (order_id,))
    o = cur.fetchone()
    if not o:
        c.close()
        abort(404)
    if int(o["warehouse_issued"] or 0) == 1:
        c.close()
        return "ZamĂłwienie wydane z magazynu jest tylko do podglÄ…du", 400

    cur.execute("SELECT sku FROM products WHERE id=? AND COALESCE(archived,0)=0", (product_id,))
    p = cur.fetchone()
    if not p:
        c.close()
        return "Brak produktu", 404

    if supabase_enabled():
        created_item = supabase_insert_row("order_items", {
            "order_id": order_id,
            "product_id": product_id,
            "sku": p["sku"],
            "qty": qty,
            "created_at": now_iso(),
        })
        if not created_item or "id" not in created_item:
            c.close()
            return "Nie udaĹ‚o siÄ™ dodaÄ‡ pozycji do Supabase", 500
        cur.execute(
            "INSERT INTO order_items(id, order_id, product_id, sku, qty, created_at) VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET order_id=excluded.order_id, product_id=excluded.product_id, sku=excluded.sku, qty=excluded.qty, created_at=excluded.created_at",
            (int(created_item["id"]), order_id, product_id, p["sku"], qty, created_item.get("created_at") or now_iso())
        )
    else:
        cur.execute("""
          INSERT INTO order_items(order_id, product_id, sku, qty, created_at)
          VALUES(?,?,?,?,?)
        """, (order_id, product_id, p["sku"], qty, now_iso()))
    c.commit()
    c.close()
    return redirect(url_for("order_view", order_id=order_id))

@app.post("/orders/<int:order_id>/items/<int:item_id>/update")
def order_item_update(order_id, item_id):
    qty = to_int(request.form.get("qty"), 0)
    if qty <= 0:
        return "IloĹ›Ä‡ musi byÄ‡ > 0", 400
    c = conn()
    cur = c.cursor()
    cur.execute("SELECT status, warehouse_issued FROM orders WHERE id=?", (order_id,))
    o = cur.fetchone()
    if not o:
        c.close()
        abort(404)
    if int(o["warehouse_issued"] or 0) == 1:
        c.close()
        return "ZamĂłwienie wydane z magazynu jest tylko do podglÄ…du", 400
    invoiced_qty = int(invoiced_qty_by_order_item_ids([item_id]).get(int(item_id)) or 0)
    if qty < invoiced_qty:
        c.close()
        return f"Nie moĹĽesz ustawiÄ‡ iloĹ›ci poniĹĽej juĹĽ zafakturowanej ({invoiced_qty} szt.)", 400
    cur.execute("UPDATE order_items SET qty=? WHERE id=? AND order_id=?", (qty, item_id, order_id))
    c.commit()
    c.close()

    if supabase_enabled():
        supabase_update_rows("order_items", {"qty": qty}, {"id": item_id})

    return redirect(url_for("order_view", order_id=order_id))


@app.post("/orders/<int:order_id>/items/<int:item_id>/delete")
def order_item_delete(order_id, item_id):
    c = conn()
    cur = c.cursor()
    cur.execute("SELECT status, warehouse_issued FROM orders WHERE id=?", (order_id,))
    o = cur.fetchone()
    if not o:
        c.close()
        abort(404)
    if int(o["warehouse_issued"] or 0) == 1:
        c.close()
        return "ZamĂłwienie wydane z magazynu jest tylko do podglÄ…du", 400
    invoiced_qty = int(invoiced_qty_by_order_item_ids([item_id]).get(int(item_id)) or 0)
    if invoiced_qty > 0:
        c.close()
        return f"Nie moĹĽesz usunÄ…Ä‡ pozycji, bo jest juĹĽ zafakturowana ({invoiced_qty} szt.)", 400

    if supabase_enabled():
        supabase_delete_rows("order_items", {"id": item_id})

    cur.execute("DELETE FROM order_items WHERE id=? AND order_id=?", (item_id, order_id))
    c.commit()
    c.close()
    return redirect(url_for("order_view", order_id=order_id))


@app.post("/orders/<int:order_id>/delete")
def order_delete(order_id):
    c = conn()
    cur = c.cursor()
    cur.execute("SELECT id, warehouse_issued FROM orders WHERE id=?", (order_id,))
    o = cur.fetchone()
    if not o:
        c.close()
        abort(404)

    cur.execute("SELECT product_id, qty FROM order_items WHERE order_id=?", (order_id,))
    items = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT id FROM invoices WHERE order_id=?", (order_id,))
    invoice_ids = [int(r["id"]) for r in cur.fetchall()]

    changed_product_ids = []
    if int(o["warehouse_issued"] or 0) == 1:
        for it in items:
            pid = int(it["product_id"])
            qty = int(it["qty"])
            cur.execute("INSERT OR IGNORE INTO stock(product_id, qty) VALUES (?, 0)", (pid,))
            cur.execute("UPDATE stock SET qty = qty + ? WHERE product_id=?", (qty, pid))
            changed_product_ids.append(pid)

    if invoice_ids:
        cur.execute("DELETE FROM invoice_allocations WHERE invoice_id IN (" + ",".join(["?"] * len(invoice_ids)) + ")", tuple(invoice_ids))
        cur.execute("DELETE FROM invoice_meta WHERE invoice_id IN (" + ",".join(["?"] * len(invoice_ids)) + ")", tuple(invoice_ids))
    cur.execute("DELETE FROM invoice_allocations WHERE order_id=?", (order_id,))
    cur.execute("DELETE FROM invoices WHERE order_id=?", (order_id,))
    cur.execute("DELETE FROM order_items WHERE order_id=?", (order_id,))
    cur.execute("DELETE FROM orders WHERE id=?", (order_id,))
    c.commit()
    c.close()

    if supabase_enabled():
        try:
            supabase_delete_rows("invoice_allocations", {"order_id": order_id})
            for iid in invoice_ids:
                supabase_delete_rows("invoice_allocations", {"invoice_id": iid})
                supabase_delete_rows("invoice_meta", {"invoice_id": iid})
                supabase_delete_rows("invoices", {"id": iid})
            supabase_delete_rows("order_items", {"order_id": order_id})
            supabase_delete_rows("orders", {"id": order_id})
            if changed_product_ids:
                sync_local_rows_to_supabase("stock", "product_id", changed_product_ids)
        except Exception:
            pass

    return redirect(url_for("orders"))

@app.post("/orders/<int:order_id>/shipped")
def order_mark_shipped(order_id):
    tracking_no = re.sub(r"\s+", "", norm(request.form.get("tracking_no")))
    carrier = norm(request.form.get("carrier")).lower()
    if not tracking_no or len(tracking_no) > 120:
        return "Podaj poprawny numer przesyłki", 400

    if carrier not in {"inpost", "dpd", "fedex", "dhl", "ups"}:
        return "Wybierz poprawnego kuriera", 400

    maybe_pull_shared_from_supabase(force=True)
    c = conn()
    try:
        cur = c.cursor()
        cur.execute("SELECT * FROM orders WHERE id=?", (order_id,))
        row = cur.fetchone()
        if not row:
            abort(404)
        order = dict(row)
        package_orders = [order]
        packed_at = norm(order.get("packed_at"))
        recipient_key = _email_key(order.get("customer_email"))
        if packed_at and recipient_key:
            cur.execute(
                """SELECT * FROM orders
                   WHERE packed_at=?
                     AND LOWER(TRIM(COALESCE(customer_email,'')))=?
                     AND LOWER(COALESCE(status,'')) NOT IN ('cancelled','issued','completed')
                   ORDER BY id""",
                (packed_at, recipient_key),
            )
            grouped_rows = [dict(item) for item in cur.fetchall()]
            if grouped_rows:
                package_orders = grouped_rows
        package_order_ids = [to_int(item.get("id"), 0) for item in package_orders]
        try:
            packing_attachment = _order_packing_list_email_attachment(order)
        except Exception as exc:
            return redirect(url_for(
                "order_view",
                order_id=order_id,
                shipment_email_error=("Nie udało się przygotować listy pakowania: " + str(exc))[:240],
            ))
        placeholders = ",".join(["?"] * len(package_order_ids))
        shipped_at = now_iso()
        cur.execute(
            f"""UPDATE orders
                SET status=CASE
                      WHEN LOWER(COALESCE(status,'')) IN ('issued','completed') THEN status
                      WHEN LOWER(COALESCE(status,''))='packed_partial' THEN 'partially_shipped'
                      ELSE 'shipped'
                    END,
                    tracking_no=?, carrier=?, shipped_at=?
                WHERE id IN ({placeholders})""",
            (tracking_no, carrier, shipped_at, *package_order_ids),
        )
        c.commit()
        cur.execute(f"SELECT * FROM orders WHERE id IN ({placeholders}) ORDER BY id", tuple(package_order_ids))
        package_orders = [dict(item) for item in cur.fetchall()]
        order = next((item for item in package_orders if to_int(item.get("id"), 0) == order_id), package_orders[0])
    finally:
        c.close()

    if supabase_enabled():
        try:
            sync_local_rows_to_supabase("orders", "id", package_order_ids)
        except Exception as exc:
            app.logger.warning("Nie udało się zsynchronizować wysyłki zamówienia %s: %s", order_id, exc)

    try:
        result = _send_orders_shipped_email(package_orders, tracking_no, carrier, packing_attachment)
    except Exception as exc:
        result = {"ok": False, "error": str(exc)}
    tracking_hash = hashlib.sha256(tracking_no.encode("utf-8")).hexdigest()[:16]
    for package_order in package_orders:
        package_order_id = to_int(package_order.get("id"), 0)
        event_key = f"order_shipped:{package_order_id}:{carrier}:{tracking_hash}"
        _record_email_event(event_key, "order_shipped", package_order_id, package_order.get("customer_email"), result)
    if result.get("ok"):
        return redirect(url_for("order_view", order_id=order_id, shipment_sent="1"))
    return redirect(url_for(
        "order_view",
        order_id=order_id,
        shipment_email_error=norm(result.get("error"))[:240] or "nieznany błąd",
    ))


@app.post("/orders/<int:order_id>/status")
def order_status_update(order_id):
    new_status = norm(request.form.get("status")).lower()
    # Status "shipped" można nadać wyłącznie osobnym formularzem,
    # który wymaga numeru przesyłki i wysyła powiadomienie do klienta.
    allowed = {"new", "confirmed", "packed", "in_delivery", "issued", "completed"}
    if new_status not in allowed:
        return "NieprawidĹ‚owy status", 400

    c = conn()
    cur = c.cursor()
    cur.execute("SELECT id, order_no, qr_data_url, status, created_at, warehouse_issued FROM orders WHERE id=?", (order_id,))
    o = cur.fetchone()
    if not o:
        c.close()
        abort(404)

    qr_data_url = (o["qr_data_url"] or "").strip()
    if new_status == "confirmed":
        qr_data_url = make_qr_data_url(canonical_order_no(o["id"], o["created_at"], o["order_no"]))

    changed_product_ids = []
    warehouse_issued = int(o["warehouse_issued"] or 0)

    # Jedyny moment zdjÄ™cia stanu:
    # przy przejĹ›ciu na "in_delivery" i tylko jeĹ›li jeszcze nie byĹ‚o wydane.
    if new_status == "in_delivery" and warehouse_issued == 0:
        cur.execute("""
          SELECT oi.product_id, oi.qty
          FROM order_items oi
          WHERE oi.order_id=?
          ORDER BY oi.id
        """, (order_id,))
        items = cur.fetchall()

        for it in items:
            pid = int(it["product_id"])
            qty = int(it["qty"])
            cur.execute("INSERT OR IGNORE INTO stock(product_id, qty) VALUES (?, 0)", (pid,))
            cur.execute("UPDATE stock SET qty = qty - ? WHERE product_id=?", (qty, pid))
            changed_product_ids.append(pid)

        warehouse_issued = 1

    cur.execute(
        "UPDATE orders SET status=?, qr_data_url=?, warehouse_issued=? WHERE id=?",
        (new_status, qr_data_url, warehouse_issued, order_id)
    )
    c.commit()
    c.close()

    if supabase_enabled():
        try:
            supabase_update_rows(
                "orders",
                {"status": new_status, "qr_data_url": qr_data_url, "warehouse_issued": warehouse_issued},
                {"id": order_id}
            )
        except Exception:
            pass

        if changed_product_ids:
            try:
                sync_local_rows_to_supabase("stock", "product_id", changed_product_ids)
            except Exception:
                pass

    if norm(request.form.get("return_to")).lower() == "dashboard":
        return redirect(url_for("home"))
    return redirect(url_for("order_view", order_id=order_id))


@app.get("/orders/<int:order_id>/issue")
def order_issue(order_id):
    # Stara akcja wyĹ‚Ä…czona. Wydanie dzieje siÄ™ teraz przy zmianie statusu na "W dostawie".
    return redirect(url_for("order_view", order_id=order_id))


@app.route("/orders/<int:order_id>/invoice", methods=["GET", "POST"])
def order_invoice(order_id):
    maybe_pull_shared_from_supabase()
    sent_invoice_id = to_int(request.args.get("invoice_id"), 0) if norm(request.args.get("sent")) == "1" else 0
    if sent_invoice_id:
        meta = load_invoice_meta(sent_invoice_id) or {}
        upsert_invoice_meta(
            sent_invoice_id,
            meta.get("pdf_path", ""),
            meta.get("invoice_items_json", ""),
            sent_to_client=1,
            seen_by_client=int(meta.get("seen_by_client") or 0),
            seen_at=meta.get("seen_at"),
            payment_reminder=int(meta.get("payment_reminder") or 0),
            paid=int(meta.get("paid") or 0),
            paid_at=meta.get("paid_at")
        )
    c = conn()
    cur = c.cursor()
    cur.execute("SELECT * FROM orders WHERE id=?", (order_id,))
    o = cur.fetchone()
    if not o:
        c.close()
        abort(404)
    if normalize_order_currency(o["currency"]) == "EUR":
        c.close()
        return (
            "Faktura EUR nie jest wystawiana przez polski moduł KSeF. "
            "Zamówienie i jego potwierdzenie pozostają poprawnie zapisane w EUR.",
            409,
        )

    related_orders = [dict(o)] if norm(o["status"]).lower() in CURRENT_ORDER_STATUSES else []
    customer_email_key = _email_key(o["customer_email"])
    if customer_email_key:
        status_ph = ",".join(["?"] * len(CURRENT_ORDER_STATUSES))
        cur.execute(f"""
          SELECT *
          FROM orders
          WHERE LOWER(COALESCE(customer_email,'')) = ?
            AND LOWER(COALESCE(status,'')) IN ({status_ph})
          ORDER BY created_at DESC, id DESC
        """, (customer_email_key, *sorted(CURRENT_ORDER_STATUSES)))
        related_orders = [dict(r) for r in cur.fetchall()]

    related_order_ids = [int(r["id"]) for r in related_orders] or [-1]
    related_order_by_id = {int(r["id"]): r for r in related_orders}
    order_ph = ",".join(["?"] * len(related_order_ids))

    cur.execute(f"""
      SELECT oi.*, p.model, p.name, COALESCE(s.qty, 0) AS stock_qty,
             oo.order_no AS source_order_no,
             oo.created_at AS source_order_created_at,
             oo.note AS source_order_note,
             COALESCE(pr.net_price, 0) AS net_price,
             COALESCE(pr.gross_price, 0) AS gross_price,
             (oi.qty * COALESCE(pr.net_price, 0)) AS line_value_net,
             (oi.qty * COALESCE(pr.gross_price, 0)) AS line_value_gross
      FROM order_items oi
      JOIN orders oo ON oo.id=oi.order_id
      JOIN products p ON p.id=oi.product_id
      LEFT JOIN stock s ON s.product_id=oi.product_id
      LEFT JOIN pricing pr ON (TRIM(LOWER(pr.model)) = TRIM(LOWER(p.model)) OR TRIM(LOWER(pr.model)) = TRIM(LOWER(p.sku)))
      WHERE oi.order_id IN ({order_ph})
      ORDER BY oo.created_at DESC, oo.id DESC, oi.id
    """, related_order_ids)
    items = [dict(r) for r in cur.fetchall()]
    invoiced_by_item = invoiced_qty_by_order_item_ids([int(it["id"]) for it in items])
    for it in items:
        source_order = related_order_by_id.get(int(it.get("order_id") or 0), {})
        ordered_qty = int(it.get("qty") or 0)
        done_qty = int(invoiced_by_item.get(int(it["id"])) or 0)
        it["source_order_no"] = order_display_no(
            source_order.get("id") or it.get("order_id"),
            source_order.get("created_at") or it.get("source_order_created_at"),
            source_order.get("order_no") or it.get("source_order_no"),
            source_order.get("note") or it.get("source_order_note") or ""
        )
        it["source_order_note"] = source_order.get("note") or it.get("source_order_note") or ""
        it["ordered_qty"] = ordered_qty
        it["invoiced_qty"] = done_qty
        it["remaining_qty"] = max(0, ordered_qty - done_qty)

    # Automatyczna propozycja: nie wiecej niz pozostalo do zafakturowania i
    # nie wiecej niz fizycznie jest na magazynie. Wspolna pula zabezpiecza
    # pozycje tego samego produktu przed podwojnym wykorzystaniem stanu.
    invoice_stock_pool = {}
    for it in items:
        pid = int(it.get("product_id") or 0)
        invoice_stock_pool.setdefault(pid, max(0, int(it.get("stock_qty") or 0)))
        suggested_qty = min(int(it.get("remaining_qty") or 0), invoice_stock_pool.get(pid, 0))
        it["suggested_invoice_qty"] = max(0, suggested_qty)
        invoice_stock_pool[pid] = max(0, invoice_stock_pool.get(pid, 0) - suggested_qty)

    cur.execute("SELECT * FROM company_profile WHERE id=1")
    company = cur.fetchone()

    customer_row = None
    if o["customer_id"]:
        cur.execute("SELECT * FROM customers WHERE id=?", (o["customer_id"],))
        customer_row = cur.fetchone()
    if not customer_row:
        cur.execute("SELECT * FROM customers WHERE name=? ORDER BY id DESC LIMIT 1", (o["customer_name"],))
        customer_row = cur.fetchone()

    cur.execute(f"""
      SELECT
        i.*,
        m.invoice_id AS meta_invoice_id,
        COALESCE(m.pdf_path,'') AS pdf_path,
        COALESCE(m.sent_to_client,0) AS sent_to_client,
        COALESCE(m.seen_by_client,0) AS seen_by_client,
        COALESCE(m.payment_reminder,0) AS payment_reminder,
        COALESCE(m.paid,0) AS paid,
        COALESCE(m.paid_at,'') AS paid_at,
        COALESCE(m.seen_at,'') AS seen_at,
        COALESCE(m.invoice_items_json,'') AS invoice_items_json
      FROM invoices i
      LEFT JOIN invoice_meta m ON m.invoice_id = i.id
      WHERE i.order_id IN ({order_ph})
      ORDER BY i.id DESC
    """, related_order_ids)
    invoice_rows = [dict(r) for r in cur.fetchall()]
    c.close()

    default_issue = app_now().strftime("%Y-%m-%d")
    buyer_address_source = customer_row["address"] if customer_row and customer_row["address"] else (o["customer_address"] or "")
    st, pc, city = split_address(buyer_address_source)
    buyer_tax_no = customer_row["nip"] if customer_row and customer_row["nip"] else ""
    buyer_address_default = "\n".join([x for x in [st, f"{pc} {city}".strip()] if x]).strip()

    msg = ""
    if request.args.get("generated") == "1":
        msg = "Faktura zostaĹ‚a zapisana."
    if request.args.get("sent") == "1":
        msg = "Faktura zostaĹ‚a udostÄ™pniona klientowi."
    if request.args.get("deleted") == "1":
        msg = "Faktura zostaĹ‚a usuniÄ™ta."
    if request.args.get("deleted") == "1":
        msg = "Faktura zostaĹ‚a usuniÄ™ta."

    if request.method == "GET":
        data = {
            "invoice_no": next_invoice_no(default_issue),
            "place": "KotuszĂłw",
            "issue_date": default_issue,
            "sell_date": default_issue,
            "payment_type": "przelew",
            "payment_to": (app_now() + timedelta(days=7)).strftime("%Y-%m-%d"),
            "buyer_name": o["customer_name"] or "",
            "buyer_tax_no": buyer_tax_no,
            "buyer_address": buyer_address_default,
            "buyer_country": "PL",
            "buyer_email": o["customer_email"] or "",
            "buyer_phone": o["customer_phone"] or "",
            "discount_percent": "0",
        }
    else:
        data = {k: norm(request.form.get(k)) for k in [
            "invoice_no", "place", "issue_date", "sell_date", "payment_type", "payment_to",
            "buyer_name", "buyer_tax_no", "buyer_address", "buyer_country",
            "buyer_email", "buyer_phone", "discount_percent"
        ]}
        st, pc, city = split_address(data.get("buyer_address", ""))
        data["buyer_street"] = st
        data["buyer_post_code"] = pc
        data["buyer_city"] = city
        if not data["invoice_no"]:
            data["invoice_no"] = next_invoice_no(data["issue_date"] or default_issue)
        if not data["issue_date"]:
            data["issue_date"] = default_issue
        if not data["sell_date"]:
            data["sell_date"] = data["issue_date"]
        if not data["payment_to"]:
            try:
                issue_day = datetime.strptime(data["issue_date"], "%Y-%m-%d")
            except (TypeError, ValueError):
                issue_day = app_now()
            data["payment_to"] = (issue_day + timedelta(days=7)).strftime("%Y-%m-%d")

        invoice_items = prepare_invoice_items(items, request.form)
        if norm(request.form.get("submit_action")) == "packing":
            if not invoice_items:
                msg = "Lista pakowania musi zawierac co najmniej jedna pozycje."
            else:
                packed_order_ids = [
                    int(item.get("source_order_id") or item.get("order_id") or order_id)
                    for item in invoice_items
                ]
                packing_order_no = canonical_order_no(o["id"], o["created_at"], o["order_no"])
                packing_meta = {
                    "invoice_no": packing_order_no,
                    "document_label_key": "order",
                    "buyer_name": data.get("buyer_name") or o["customer_name"] or "Klient",
                    "buyer_email": data.get("buyer_email") or o["customer_email"] or "",
                }
                packing_path = generate_invoice_packing_list_pdf(o, invoice_items, packing_meta)
                mark_orders_packed(packed_order_ids, packing_path=packing_path, packing_items=invoice_items)
                return send_file(
                    packing_path,
                    mimetype="application/pdf",
                    as_attachment=True,
                    download_name=f"{safe_filename(packing_order_no)}_lista_pakowania.pdf",
                )
        existing_invoice_id = invoice_no_exists(data["invoice_no"])
        if existing_invoice_id:
            msg = f"Faktura o takim numerze już istnieje! Numer: {data['invoice_no']}. Wybierz inny numer faktury."
        elif not invoice_items:
            msg = "Faktura musi zawieraÄ‡ co najmniej jednÄ… pozycjÄ™."
        else:
            pdf_path, total_net, total_gross = generate_order_invoice_pdf(o, invoice_items, data)
            packing_pdf_path = generate_invoice_packing_list_pdf(o, invoice_items, data, pdf_path)
            c = conn()
            cur = c.cursor()
            cur.execute("""
              INSERT INTO invoices(order_id, invoice_no, issue_date, sell_date, payment_type, payment_to,
                                   buyer_name, buyer_tax_no, buyer_street, buyer_post_code, buyer_city, buyer_country,
                                   buyer_email, buyer_phone, total_net, total_gross, created_at)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                order_id, data["invoice_no"], data["issue_date"], data["sell_date"], data["payment_type"], data["payment_to"],
                data["buyer_name"], data["buyer_tax_no"], data["buyer_street"], data["buyer_post_code"], data["buyer_city"], data["buyer_country"],
                data["buyer_email"], data["buyer_phone"], total_net, total_gross, now_iso()
            ))
            invoice_id = cur.lastrowid
            if not invoice_id:
                cur.execute("SELECT id FROM invoices WHERE invoice_no=? LIMIT 1", (data["invoice_no"],))
                rr = cur.fetchone()
                invoice_id = int(rr["id"]) if rr else 0
            c.commit()
            c.close()
            stored_pdf_path = upload_invoice_pdfs_to_supabase(invoice_id, data["invoice_no"], pdf_path, packing_pdf_path)
            upsert_invoice_meta(invoice_id, stored_pdf_path, json.dumps(invoice_items, ensure_ascii=False), sent_to_client=None)
            allocation_ids = replace_invoice_allocations(invoice_id, invoice_items)
            touched_order_ids = [int(x.get("source_order_id") or x.get("order_id") or 0) for x in invoice_items]
            completed_order_ids, changed_product_ids = finalize_fully_invoiced_orders(touched_order_ids)
            if supabase_enabled():
                try:
                    sync_local_rows_to_supabase("invoices", "id", [invoice_id])
                except Exception:
                    pass
                try:
                    sync_invoice_meta_to_supabase(invoice_id)
                except Exception:
                    pass
                try:
                    sync_local_rows_to_supabase("invoice_allocations", "id", allocation_ids)
                except Exception:
                    pass
                if completed_order_ids:
                    try:
                        sync_local_rows_to_supabase("orders", "id", completed_order_ids)
                    except Exception:
                        pass
                if changed_product_ids:
                    try:
                        sync_local_rows_to_supabase("stock", "product_id", changed_product_ids)
                    except Exception:
                        pass
            _order_id, email_ok, email_error = _send_invoice_to_client(invoice_id)
            redirect_args = {
                "generated": "1",
                "invoice_id": invoice_id,
                "email_sent": "1" if email_ok else "0",
            }
            if email_error:
                redirect_args["email_error"] = email_error[:300]
            return redirect(url_for("invoices", **redirect_args))

    tpl = r"""
    {% extends "base.html" %}
    {% block content %}
      <div class="card">
        <div class="flex">
          <h1 style="margin:0;">Faktura z pozycji klienta: {{ o['customer_name'] or o['customer_email'] }}</h1>
          <a class="btn right" href="{{ url_for('order_view', order_id=o['id']) }}">â† SzczegĂłĹ‚y</a>
        </div>
        {% if msg %}
          <div class="hint" style="margin-top:10px;">{{ msg }}</div>
        {% endif %}
      </div>

      <div class="card">
        <form method="post" class="row">
          <div><label class="muted small">Numer faktury</label><input name="invoice_no" value="{{ d['invoice_no'] }}" required></div>
          <div><label class="muted small">Miejsce</label><input name="place" value="{{ d['place'] }}"></div>
          <div><label class="muted small">Data wystawienia</label><input id="invoice_issue_date" name="issue_date" type="date" value="{{ d['issue_date'] }}"></div>
          <div><label class="muted small">Data sprzedaĹĽy</label><input name="sell_date" type="date" value="{{ d['sell_date'] }}"></div>
          <div><label class="muted small">Forma pĹ‚atnoĹ›ci</label>
            <select name="payment_type">
              <option value="przelew" {% if d['payment_type'] in ['transfer','przelew'] %}selected{% endif %}>przelew</option>
              <option value="gotowka" {% if d['payment_type'] in ['cash','gotowka'] %}selected{% endif %}>gotĂłwka</option>
              <option value="karta" {% if d['payment_type'] in ['card','karta'] %}selected{% endif %}>karta</option>
            </select>
          </div>
          <div><label class="muted small">Termin pĹ‚atnoĹ›ci</label><input id="invoice_payment_to" name="payment_to" type="date" value="{{ d['payment_to'] }}"></div>
          <div><label class="muted small">Rabat %</label><input name="discount_percent" value="{{ d['discount_percent'] or "0" }}"></div>

          <div><label class="muted small">Nabywca</label><input name="buyer_name" value="{{ d['buyer_name'] }}" required></div>
          <div><label class="muted small">NIP nabywcy</label><input name="buyer_tax_no" value="{{ d['buyer_tax_no'] }}"></div>
          <div><label class="muted small">Adres nabywcy</label><textarea name="buyer_address" placeholder="Ulica&#10;Kod pocztowy Miasto">{{ d['buyer_address'] }}</textarea></div>
          <div><label class="muted small">Kraj</label><input name="buyer_country" value="{{ d['buyer_country'] }}"></div>
          <div><label class="muted small">Email</label><input name="buyer_email" value="{{ d['buyer_email'] }}"></div>
          <div><label class="muted small">Telefon</label><input name="buyer_phone" value="{{ d['buyer_phone'] }}"></div>

          <div style="grid-column:1/-1;">
            <h2>Pozycje faktury — wybierz ilości z zamówień klienta</h2>
            <div class="hint" style="margin-bottom:10px;">
              Wpisz ilość tylko przy pozycjach, które idą na fakturę. Zamówienia klienta zostają jako osobne listy/notatki.
            </div>
            <table>
              <thead><tr><th>Zamówienie</th><th>Notatka klienta</th><th>SKU</th><th>Model / Nazwa</th><th>Zamówiono</th><th>Zafakturowano</th><th>Pozostało</th><th>Na magazynie</th><th>Ilość na fakturze</th><th>Netto/szt</th><th>Brutto/szt</th></tr></thead>
              <tbody>
                {% for it in items %}
                <tr>
                  <td><b>{{ it['source_order_no'] }}</b></td>
                  <td>{{ it['source_order_note'] or '-' }}</td>
                  <td><b>{{ it['sku'] }}</b></td>
                  <td>{{ it['model'] or '' }}{% if it['name'] %}<div class="muted small">{{ it['name'] }}</div>{% endif %}</td>
                  <td>{{ it['ordered_qty'] }}</td>
                  <td>{{ it['invoiced_qty'] }}</td>
                  <td><b>{{ it['remaining_qty'] }}</b></td>
                  <td><b>{{ it['stock_qty'] }}</b> szt.</td>
                  <td>
                    <input type="number" min="0" name="invoice_qty_{{ it['id'] }}" value="{{ it['suggested_invoice_qty'] }}" max="{{ it['remaining_qty'] }}" style="width:110px;" {% if it['remaining_qty'] <= 0 %}disabled{% endif %}>
                  </td>
                  <td>{{ "%.2f"|format(it['net_price']) }}</td>
                  <td>{{ "%.2f"|format(it['gross_price']) }}</td>
                </tr>
                {% endfor %}
              </tbody>
            </table>
          </div>

          <div class="flex" style="align-items:flex-end;">
            <button class="btn" type="submit" name="submit_action" value="packing" formtarget="_blank">Pakuj</button>
            <button class="btn primary" type="submit" name="submit_action" value="invoice">Zapisz fakturÄ™ PDF</button>
          </div>
        </form>
      </div>

      <script>
      (() => {
        const issueDate = document.getElementById('invoice_issue_date');
        const paymentTo = document.getElementById('invoice_payment_to');
        if (!issueDate || !paymentTo) return;
        issueDate.addEventListener('change', () => {
          if (!issueDate.value) return;
          const parts = issueDate.value.split('-').map(Number);
          if (parts.length !== 3 || parts.some(Number.isNaN)) return;
          const due = new Date(Date.UTC(parts[0], parts[1] - 1, parts[2]));
          due.setUTCDate(due.getUTCDate() + 7);
          paymentTo.value = due.toISOString().slice(0, 10);
        });
      })();
      </script>

      <div class="card">
        <h2>Zapisane faktury</h2>
        <table>
          <thead><tr><th>Numer</th><th>Data</th><th>Netto</th><th>Brutto</th><th>Status klienta</th><th>Płatność</th><th>Akcje</th></tr></thead>
          <tbody>
            {% for inv in invoice_rows %}
              <tr>
                <td><b>{{ inv['invoice_no'] }}</b></td>
                <td>{{ inv['issue_date'] }}</td>
                <td>{{ "%.2f"|format(inv['total_net']) }}</td>
                <td>{{ "%.2f"|format(inv['total_gross']) }}</td>
                <td>{{ "Udostępniona" if inv['sent_to_client'] else "Tylko wewnętrzna" }}</td>
                <td>
                  {% if inv['paid'] %}
                    <span class="badge ok">Opłacona</span>
                    {% if inv['paid_at'] %}<div class="muted small">{{ inv['paid_at'] }}</div>{% endif %}
                  {% else %}
                    <span class="badge danger">Nieopłacona</span>
                    {% if inv['payment_reminder'] %}<span class="badge ok">Przypomnienie wysłane</span>{% endif %}
                  {% endif %}
                </td>
                <td>
                  <div class="flex">
                    <a class="btn" href="{{ url_for('invoice_download_admin', invoice_id=inv['id']) }}" target="_blank">Pobierz PDF</a>
                    <a class="btn" href="{{ url_for('invoice_packing_list_download_admin', invoice_id=inv['id']) }}" target="_blank">Pakuj</a>
                    <form method="post" action="{{ url_for('invoice_regenerate_admin', invoice_id=inv['id']) }}">
                      <button class="btn" type="submit">Regeneruj PDF</button>
                    </form>
                    {% if not inv['sent_to_client'] %}
                      <form method="post" action="{{ url_for('order_invoice_send', order_id=o['id'], invoice_id=inv['id']) }}">
                        <button class="btn primary" type="submit">WyĹ›lij fakturÄ™ klientowi</button>
                      </form>
                    {% else %}
                      <span class="badge">Widoczna w panelu klienta</span>
                    {% endif %}
                    {% if not inv['paid'] %}
                      <form method="post" action="{{ url_for('invoice_payment_reminder_admin', invoice_id=inv['id']) }}">
                        <input type="hidden" name="next" value="{{ request.full_path }}">
                        <button class="btn" type="submit">Przypomnij o płatności</button>
                      </form>
                      <form method="post" action="{{ url_for('invoice_paid_admin', invoice_id=inv['id']) }}">
                        <input type="hidden" name="next" value="{{ request.full_path }}">
                        <button class="btn ok" type="submit">Faktura opłacona</button>
                      </form>
                    {% else %}
                      <form method="post" action="{{ url_for('invoice_unpaid_admin', invoice_id=inv['id']) }}">
                        <input type="hidden" name="next" value="{{ request.full_path }}">
                        <button class="btn" type="submit">Cofnij opłacenie</button>
                      </form>
                    {% endif %}
                    <form method="post" action="{{ url_for('order_invoice_delete', order_id=o['id'], invoice_id=inv['id']) }}" onsubmit="return confirm('UsunÄ…Ä‡ fakturÄ™?')">
                      <button class="btn danger" type="submit">UsuĹ„ fakturÄ™</button>
                    </form>
                  </div>
                </td>
              </tr>
            {% endfor %}
            {% if not invoice_rows %}
              <tr><td colspan="7" class="muted">Brak wystawionych faktur.</td></tr>
            {% endif %}
          </tbody>
        </table>
      </div>
    {% endblock %}
    """
    return render_template_string(tpl, title="Faktura", base_url=BASE_URL, db_path=DB_PATH, o=o, d=data, company=company, items=items, invoice_rows=invoice_rows, msg=msg, canonical_order_no=canonical_order_no)


@app.get("/orders/<int:order_id>/print")
def order_print(order_id):
    c = conn()
    cur = c.cursor()
    cur.execute("SELECT * FROM orders WHERE id=?", (order_id,))
    o = cur.fetchone()
    if not o:
        c.close()
        abort(404)

    cur.execute("""
      SELECT oi.sku, oi.qty, p.model, p.name, COALESCE(s.qty,0) AS stock
      FROM order_items oi
      JOIN products p ON p.id=oi.product_id
      LEFT JOIN stock s ON s.product_id=p.id
      WHERE oi.order_id=?
      ORDER BY oi.id
    """, (order_id,))
    items = cur.fetchall()
    c.close()

    in_stock = []
    missing = []
    total_qty = 0
    total_missing_qty = 0
    for it in items:
        need = int(it["qty"])
        have = int(it["stock"])
        row = {
            "sku": it["sku"],
            "model": it["model"] or "",
            "name": it["name"] or "",
            "qty": need,
            "stock": have,
            "missing": max(0, need - have),
        }
        total_qty += need
        total_missing_qty += row["missing"]
        if have >= need:
            in_stock.append(row)
        else:
            missing.append(row)

    buf = io.BytesIO()
    w = 210 * mm
    h = 297 * mm
    cpdf = canvas.Canvas(buf, pagesize=(w, h))
    pdf_font, pdf_font_bold = get_pdf_font_names()

    y = h - 18 * mm
    cpdf.setFont(pdf_font_bold, 14)
    cpdf.drawString(15 * mm, y, f"Wydruk zamĂłwienia: {order_display_no(o['id'], o['created_at'], o['order_no'], o['note'])}")
    y -= 7 * mm
    cpdf.setFont(pdf_font, 10)
    cpdf.drawString(15 * mm, y, f"Klient: {o['customer_name']}")
    y -= 5 * mm
    cpdf.drawString(15 * mm, y, f"Data: {o['created_at']}")
    y -= 6 * mm
    cpdf.setFont(pdf_font_bold, 10)
    cpdf.drawString(15 * mm, y, f"ĹÄ…czna liczba sztuk w zamĂłwieniu: {total_qty}")
    y -= 5 * mm
    cpdf.setFont(pdf_font, 10)
    cpdf.drawString(15 * mm, y, f"ĹÄ…czny brak na stanie: {total_missing_qty}")

    def draw_section(title, rows, y_pos, show_missing=False):
        cpdf.setFont(pdf_font_bold, 11)
        cpdf.drawString(15 * mm, y_pos, title)
        y_pos -= 6 * mm
        cpdf.setFont(pdf_font_bold, 9)
        cpdf.drawString(15 * mm, y_pos, "SKU")
        cpdf.drawString(55 * mm, y_pos, "Model/Nazwa")
        cpdf.drawString(160 * mm, y_pos, "IloĹ›Ä‡")
        if show_missing:
            cpdf.drawString(176 * mm, y_pos, "Brak")
        y_pos -= 5 * mm
        cpdf.setFont(pdf_font, 9)
        for r in rows:
            label = (r['model'] or r['name'] or "")[:48]
            cpdf.drawString(15 * mm, y_pos, r['sku'])
            cpdf.drawString(55 * mm, y_pos, label)
            cpdf.drawRightString(173 * mm, y_pos, str(r['qty']))
            if show_missing:
                cpdf.drawRightString(195 * mm, y_pos, str(r['missing']))
            y_pos -= 5 * mm
            if y_pos < 20 * mm:
                cpdf.showPage()
                y_pos = h - 20 * mm
                cpdf.setFont(pdf_font, 9)
        return y_pos

    y -= 10 * mm
    y = draw_section("Produkty w magazynie", in_stock, y, show_missing=False)
    y -= 6 * mm
    y = draw_section("Brak na stanie", missing, y, show_missing=True)

    cpdf.showPage()
    cpdf.save()
    buf.seek(0)
    fname = safe_filename(canonical_order_no(o["id"], o["created_at"], o["order_no"])) + "_druk_zamowienia.pdf"
    return send_file(buf, mimetype="application/pdf", as_attachment=True, download_name=fname)


# -------------------------
# LABEL 30x50 (QR + dane)
# -------------------------

@app.get("/orders/<int:order_id>/label")
def order_label(order_id):
    c = conn()
    cur = c.cursor()
    cur.execute("SELECT * FROM orders WHERE id=?", (order_id,))
    o = cur.fetchone()
    c.close()
    if not o:
        abort(404)

    # Etykieta 30x50 ma zawsze uĹĽywaÄ‡ aktualnego QR z poprawnym numerem ZAM-...
    # i nadpisywaÄ‡ stare QR-y wygenerowane kiedy zamĂłwienie miaĹ‚o jeszcze TEMP.
    qr_data_url = make_qr_data_url(canonical_order_no(o["id"], o["created_at"], o["order_no"]))

    c = conn()
    cur = c.cursor()
    cur.execute("UPDATE orders SET qr_data_url=? WHERE id=?", (qr_data_url, order_id))
    c.commit()
    c.close()
    if supabase_enabled():
        try:
            supabase_update_rows("orders", {"qr_data_url": qr_data_url}, {"id": order_id})
        except Exception:
            pass

    # PDF 30x50 mm
    w = 30 * mm
    h = 50 * mm

    buf = io.BytesIO()
    cpdf = canvas.Canvas(buf, pagesize=(w, h))

    # Umieszczenie QR
    qr_bytes = b""
    if qr_data_url.startswith("data:image"):
        try:
            qr_bytes = base64.b64decode(qr_data_url.split(",", 1)[1])
        except Exception:
            qr_bytes = b""

    if not qr_bytes:
        fallback_qr = make_qr_data_url(canonical_order_no(o["id"], o["created_at"], o["order_no"]))
        if fallback_qr.startswith("data:image"):
            qr_bytes = base64.b64decode(fallback_qr.split(",", 1)[1])

    qr_buf = io.BytesIO(qr_bytes)
    qr_img = ImageReader(qr_buf)

    # QR na gĂłrze (wiÄ™kszy), dane poniĹĽej
    margin = 2 * mm
    qr_size = 26 * mm  # zostaje margines
    cpdf.drawImage(qr_img, margin, h - margin - qr_size, width=qr_size, height=qr_size, preserveAspectRatio=True, mask='auto')

    # Dane zamawiajÄ…cego + nr zamĂłwienia
    pdf_font, pdf_font_bold = get_pdf_font_names()
    text_y = h - margin - qr_size - 2*mm
    max_text_width = w - (2 * margin)

    def wrap_pdf_text(value, font_name, font_size, max_width):
        words = str(value or "").split()
        if not words:
            return []
        lines = []
        current = words[0]
        for word in words[1:]:
            test = current + " " + word
            if pdfmetrics.stringWidth(test, font_name, font_size) <= max_width:
                current = test
            else:
                lines.append(current)
                current = word
        lines.append(current)
        return lines

    customer_lines = wrap_pdf_text((o["customer_name"] or "")[:60], pdf_font_bold, 6.2, max_text_width)
    if not customer_lines:
        customer_lines = [""]

    cpdf.setFont(pdf_font_bold, 6.2)
    cpdf.drawString(margin, text_y, customer_lines[0][:60])

    order_no_value = order_display_no(o['id'], o['created_at'], o['order_no'], o['note'])
    order_no_lines = [f"Nr: {order_no_value}"]

    if pdfmetrics.stringWidth(order_no_lines[0], pdf_font_bold, 5.1) > max_text_width:
        order_no_lines = [f"Nr: {order_no_value[:13]}", order_no_value[13:]]

    cpdf.setFont(pdf_font_bold, 5.1)
    cpdf.drawString(margin, text_y - 3.2*mm, order_no_lines[0])
    extra_offset_mm = 0
    if len(order_no_lines) > 1 and order_no_lines[1].strip():
        cpdf.drawString(margin, text_y - 6.0*mm, order_no_lines[1].strip())
        extra_offset_mm = 2.8

    cpdf.setFont(pdf_font, 6.0)
    addr = (o["customer_address"] or "").strip()
    phone = (o["customer_phone"] or "").strip()

    lines = []
    if addr:
        # podziel na linie i dodatkowo Ĺ‚am dĹ‚ugie
        for ln in addr.splitlines():
            ln = ln.strip()
            if not ln:
                continue
            while len(ln) > 42:
                lines.append(ln[:42])
                ln = ln[42:]
            lines.append(ln)
    if phone:
        lines.append(f"Tel: {phone}")

    y = text_y - (6.8 + extra_offset_mm)*mm
    for ln in lines[:6]:
        cpdf.drawString(margin, y, ln)
        y -= 3.2*mm

    cpdf.showPage()
    cpdf.save()
    buf.seek(0)

    fname = safe_filename(canonical_order_no(o["id"], o["created_at"], o["order_no"])) + "_label_30x50.pdf"
    return send_file(buf, mimetype="application/pdf", as_attachment=True, download_name=fname)


def _client_stock_catalog_rows(profile: dict) -> list[dict]:
    """Return live Supabase stock with the server-selected customer price list."""
    price_list = normalize_client_price_list((profile or {}).get("price_list"))
    currency = price_list_currency(price_list)
    rows = supabase_request(
        "/rest/v1/client_stock_catalog_v",
        method="GET",
        params={
            "select": (
                "product_id,sku,model,name,qty_physical,qty_reserved,"
                "qty_on_stock,net_price,gross_price"
            ),
            "order": "sku.asc",
            "limit": 5000,
        },
        timeout=30,
    ) or []
    if not isinstance(rows, list):
        raise RuntimeError("Nieprawidłowa odpowiedź katalogu magazynowego")

    eur_by_sku = {}
    if price_list == "eu_eur":
        eur_rows = supabase_request(
            "/rest/v1/pricing_eur",
            method="GET",
            params={"select": "sku,price_eur,uvp_eur", "order": "sku.asc", "limit": 5000},
            timeout=30,
        ) or []
        if not isinstance(eur_rows, list):
            raise RuntimeError("Nieprawidłowa odpowiedź cennika EUR")
        eur_by_sku = {norm(row.get("sku")).lower(): row for row in eur_rows}

    catalog = []
    for raw in rows:
        row = dict(raw)
        sku_key = norm(row.get("sku")).lower()
        if price_list == "eu_eur":
            price = eur_by_sku.get(sku_key) or {}
            net_price = money_float(price.get("price_eur"))
            # Cennik UE jest cennikiem B2B. Kwota zamówienia brutto jest równa
            # cenie transakcyjnej; UVP pozostaje osobną ceną sugerowaną.
            gross_price = net_price
            retail_price = money_float(price.get("uvp_eur"))
        else:
            net_price = money_float(row.get("net_price"))
            gross_price = money_float(row.get("gross_price"))
            retail_price = money_float(money_dec(net_price) * Decimal("1.45") * Decimal("1.23"))
        row.update({
            "net_price": net_price,
            "gross_price": gross_price,
            "retail_price": retail_price,
            "currency": currency,
            "price_list": price_list,
            "price_available": bool(net_price > 0),
        })
        catalog.append(row)
    return catalog


@app.get("/api/client_stock_catalog")
def api_client_stock_catalog():
    if not supabase_enabled():
        return jsonify(ok=False, error="Brak konfiguracji Supabase na serwerze"), 503

    try:
        profile = _client_profile_for_email(g.client_user.get("email"))
        rows = _client_stock_catalog_rows(profile)
    except Exception as exc:
        app.logger.error(
            "Nie udało się pobrać aktualnych stanów lub cennika bezpośrednio z Supabase: %s",
            type(exc).__name__,
        )
        return jsonify(
            ok=False,
            error="Nie udało się pobrać aktualnych stanów i cen z Supabase",
        ), 503

    response = jsonify(
        ok=True,
        rows=rows,
        source="supabase",
        price_list=profile.get("price_list", "pln"),
        currency=profile.get("currency", "PLN"),
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return response


@app.route("/api/client_search_log", methods=["POST", "OPTIONS"])
def api_client_search_log():
    if request.method == "OPTIONS":
        return ("", 204)

    data = request.get_json(silent=True) or {}
    query = norm(data.get("query"))[:120]
    if len(query) < 2:
        return jsonify(ok=True, skipped=True)

    # Tożsamość pochodzi wyłącznie ze zweryfikowanego tokenu Supabase,
    # nigdy z danych przesłanych przez przeglądarkę.
    email = norm(g.client_user.get("email")).lower()[:180]
    name = ""
    try:
        profile_name = norm(_client_profile_for_email(email).get("name"))
        if profile_name and "@" not in profile_name and not _order_name_is_fallback(profile_name, email):
            name = profile_name
    except Exception as exc:
        app.logger.warning("Nie udalo sie ustalic nazwy klienta dla wyszukiwania %s: %s", email, type(exc).__name__)
    if not name:
        auth_name = norm(g.client_user.get("name"))
        if auth_name and "@" not in auth_name and not _order_name_is_fallback(auth_name, email):
            name = auth_name
    source = norm(data.get("source"))[:40] or "stock"
    results_count = to_int(data.get("results_count"), 0)
    if results_count < 0:
        results_count = 0
    matches = data.get("matches") if isinstance(data.get("matches"), list) else []
    rows_to_save = []
    created_at = now_iso()
    seen_products = set()
    for item in matches[:30]:
        if not isinstance(item, dict):
            continue
        product_sku = norm(item.get("sku"))[:120]
        product_model = norm(item.get("model"))[:120] or product_sku
        product_name = norm(item.get("name"))[:180]
        product_key = (product_model.lower(), product_sku.lower())
        if not product_model or product_key in seen_products:
            continue
        seen_products.add(product_key)
        rows_to_save.append({
            "customer_email": email,
            "customer_name": name,
            "query": query,
            "product_sku": product_sku,
            "product_model": product_model,
            "product_name": product_name,
            "results_count": results_count,
            "source": source,
            "created_at": created_at,
        })

    if not rows_to_save:
        rows_to_save.append({
            "customer_email": email,
            "customer_name": name,
            "query": query,
            "product_sku": "",
            "product_model": "",
            "product_name": "",
            "results_count": results_count,
            "source": source,
            "created_at": created_at,
        })

    cutoff = (app_now() - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    deduped_rows = []
    c = conn()
    cur = c.cursor()
    for row in rows_to_save:
        if row.get("product_sku") or row.get("product_model"):
            cur.execute("""
              SELECT 1
              FROM client_search_logs
              WHERE LOWER(COALESCE(customer_email,''))=?
                AND LOWER(COALESCE(query,''))=?
                AND LOWER(COALESCE(product_sku,''))=?
                AND LOWER(COALESCE(product_model,''))=?
                AND COALESCE(source,'stock')=?
                AND created_at>=?
              LIMIT 1
            """, (
                row.get("customer_email", "").lower(),
                row.get("query", "").lower(),
                row.get("product_sku", "").lower(),
                row.get("product_model", "").lower(),
                row.get("source", "stock"),
                cutoff,
            ))
        else:
            cur.execute("""
              SELECT 1
              FROM client_search_logs
              WHERE LOWER(COALESCE(customer_email,''))=?
                AND LOWER(COALESCE(query,''))=?
                AND COALESCE(product_sku,'')=''
                AND COALESCE(product_model,'')=''
                AND COALESCE(source,'stock')=?
                AND created_at>=?
              LIMIT 1
            """, (
                row.get("customer_email", "").lower(),
                row.get("query", "").lower(),
                row.get("source", "stock"),
                cutoff,
            ))
        if cur.fetchone():
            continue
        deduped_rows.append(row)
    c.close()

    if not deduped_rows:
        return jsonify(ok=True, skipped=True, duplicate=True)

    cloud_ok = False
    cloud_saved = 0
    for row in deduped_rows:
        try:
            if save_client_search_log_supabase(row):
                cloud_saved += 1
        except Exception:
            pass
        save_client_search_log_local(row)

    cloud_ok = cloud_saved == len(deduped_rows)
    return jsonify(ok=True, cloud=bool(cloud_ok), rows=len(deduped_rows))


def _email_event_already_ok(event_key):
    if not event_key:
        return False
    c = conn()
    try:
        cur = c.cursor()
        cur.execute("SELECT ok FROM email_events WHERE event_key=? LIMIT 1", (event_key,))
        row = cur.fetchone()
        return bool(row and to_int(row["ok"], 0) == 1)
    except Exception:
        return False
    finally:
        c.close()


def _record_email_event(event_key, event_type, ref_id, recipient, result):
    if not event_key:
        return
    ok = 1 if isinstance(result, dict) and result.get("ok") else 0
    try:
        payload = json.dumps(result or {}, ensure_ascii=False)[:6000]
    except Exception:
        payload = json.dumps({"raw": str(result)}, ensure_ascii=False)[:6000]
    c = conn()
    try:
        cur = c.cursor()
        cur.execute("SELECT id FROM email_events WHERE event_key=? LIMIT 1", (event_key,))
        row = cur.fetchone()
        if row:
            cur.execute("""
                UPDATE email_events
                SET event_type=?, ref_id=?, recipient=?, ok=?, result_json=?, created_at=?
                WHERE event_key=?
            """, (event_type, str(ref_id or ""), recipient or "", ok, payload, now_iso(), event_key))
        else:
            cur.execute("""
                INSERT INTO email_events(event_key,event_type,ref_id,recipient,ok,result_json,created_at)
                VALUES(?,?,?,?,?,?,?)
            """, (event_key, event_type, str(ref_id or ""), recipient or "", ok, payload, now_iso()))
        c.commit()
    except Exception:
        pass
    finally:
        c.close()


def _partial_packing_order_ids(order_ids, packing_items) -> set[int]:
    """Return orders whose current packing list does not cover every ordered item."""
    clean_ids = sorted({to_int(value, 0) for value in (order_ids or []) if to_int(value, 0) > 0})
    if not clean_ids or not packing_items:
        return set()
    selected_by_item = {}
    for item in packing_items:
        item_id = to_int(item.get("order_item_id") or item.get("id"), 0)
        if item_id <= 0:
            continue
        selected_by_item[item_id] = selected_by_item.get(item_id, 0) + max(0, to_int(item.get("qty"), 0))
    c = conn()
    try:
        placeholders = ",".join(["?"] * len(clean_ids))
        cur = c.cursor()
        cur.execute(
            f"SELECT id, order_id, qty FROM order_items WHERE order_id IN ({placeholders})",
            tuple(clean_ids),
        )
        rows = [dict(row) for row in cur.fetchall()]
    finally:
        c.close()
    rows_by_order = {}
    for row in rows:
        rows_by_order.setdefault(to_int(row.get("order_id"), 0), []).append(row)
    partial_ids = set()
    for order_id in clean_ids:
        order_rows = rows_by_order.get(order_id, [])
        if not order_rows or any(
            selected_by_item.get(to_int(row.get("id"), 0), 0) < max(0, to_int(row.get("qty"), 0))
            for row in order_rows
        ):
            partial_ids.add(order_id)
    return partial_ids


def mark_orders_packed(order_ids, packing_path: str = "", packing_items=None) -> list[int]:
    """Mark selected orders as being packed without issuing stock again."""
    clean_ids = sorted({to_int(value, 0) for value in (order_ids or []) if to_int(value, 0) > 0})
    if not clean_ids:
        return []
    partial_ids = _partial_packing_order_ids(clean_ids, packing_items)
    c = conn()
    try:
        placeholders = ",".join(["?"] * len(clean_ids))
        cur = c.cursor()
        packed_at = now_iso()
        for packed_order_id in clean_ids:
            next_status = "packed_partial" if packed_order_id in partial_ids else "packed"
            cur.execute(
                """UPDATE orders SET status=?, packed_at=?
                   WHERE id=? AND LOWER(COALESCE(status,''))
                   NOT IN ('issued','completed','cancelled','shipped')""",
                (next_status, packed_at, packed_order_id),
            )
        c.commit()
        cur.execute(
            f"SELECT id FROM orders WHERE id IN ({placeholders}) AND status IN ('packed','packed_partial')",
            tuple(clean_ids),
        )
        changed_ids = [int(row["id"]) for row in cur.fetchall()]
    finally:
        c.close()
    if changed_ids and supabase_enabled():
        try:
            sync_local_rows_to_supabase("orders", "id", changed_ids)
        except Exception as exc:
            app.logger.warning("Nie udało się zsynchronizować statusu pakowania: %s", exc)
    if changed_ids:
        c = conn()
        try:
            placeholders = ",".join(["?"] * len(changed_ids))
            cur = c.cursor()
            cur.execute(f"SELECT * FROM orders WHERE id IN ({placeholders})", tuple(changed_ids))
            packed_orders = [dict(row) for row in cur.fetchall()]
        finally:
            c.close()
        pending_orders = [
            order for order in packed_orders
            if not _email_event_already_ok(f"order_packed:{to_int(order.get('id'), 0)}")
        ]
        orders_by_recipient = {}
        for packed_order in pending_orders:
            recipient = _email_key(packed_order.get("customer_email"))
            orders_by_recipient.setdefault(recipient, []).append(packed_order)
        for recipient, recipient_orders in orders_by_recipient.items():
            try:
                result = _send_orders_packed_email(
                    recipient_orders,
                    packing_path=packing_path,
                )
            except Exception as exc:
                result = {"ok": False, "error": str(exc)}
            for packed_order in recipient_orders:
                packed_id = to_int(packed_order.get("id"), 0)
                _record_email_event(
                    f"order_packed:{packed_id}",
                    "order_packed",
                    packed_id,
                    recipient,
                    result,
                )
            if not result.get("ok"):
                app.logger.warning(
                    "Nie udalo sie wyslac zbiorczej informacji o pakowaniu zamowien %s: %s",
                    ",".join(str(to_int(order.get("id"), 0)) for order in recipient_orders),
                    norm(result.get("error")) or "nieznany blad",
                )
    return changed_ids


def _send_orders_packed_email(orders: list[dict], packing_path: str = "") -> dict:
    if not send_email:
        return {"ok": False, "error": "Modul wysylki e-mail nie jest dostepny"}
    orders = [dict(order) for order in (orders or []) if order]
    if not orders:
        return {"ok": False, "error": "Brak zamowien do wyslania"}
    order = orders[0]
    recipient = _email_key(order.get("customer_email"))
    if not recipient:
        return {"ok": False, "error": "Zamowienie nie ma adresu e-mail klienta"}
    try:
        language = normalize_client_language(_client_profile_for_email(recipient).get("language"))
    except Exception:
        language = "pl"
    order_numbers = [
        canonical_order_no(item.get("id"), item.get("created_at"), item.get("order_no"))
        for item in orders
    ]
    multiple = len(order_numbers) > 1
    messages = {
        "pl": (("Rozpoczęliśmy pakowanie zamówień" if multiple else "Rozpoczęliśmy pakowanie zamówienia"), ("Twoje zamówienia są pakowane razem i będą oczekiwać na odbiór przez kuriera." if multiple else "Twoje zamówienie jest w trakcie pakowania i będzie oczekiwać na odbiór przez kuriera.")),
        "de": (("Wir verpacken Ihre Bestellungen" if multiple else "Wir verpacken Ihre Bestellung"), ("Ihre Bestellungen werden gemeinsam verpackt und warten anschließend auf die Abholung durch den Kurier." if multiple else "Ihre Bestellung wird derzeit verpackt und wartet anschließend auf die Abholung durch den Kurier.")),
        "en": (("We are packing your orders" if multiple else "We are packing your order"), ("Your orders are being packed together and will then wait for courier collection." if multiple else "Your order is being packed and will then wait for courier collection.")),
        "es": (("Estamos preparando tus pedidos" if multiple else "Estamos preparando tu pedido"), ("Tus pedidos se están preparando juntos y después quedarán a la espera de la recogida por el transportista." if multiple else "Tu pedido se está preparando y después quedará a la espera de la recogida por el transportista.")),
        "it": (("Stiamo preparando i tuoi ordini" if multiple else "Stiamo preparando il tuo ordine"), ("I tuoi ordini vengono preparati insieme e saranno poi in attesa del ritiro da parte del corriere." if multiple else "Il tuo ordine è in fase di preparazione e sarà poi in attesa del ritiro da parte del corriere.")),
    }
    subject_text, intro = messages.get(language, messages["pl"])
    attachment_note = {
        "pl": "Lista pakowania znajduje się w załączniku.",
        "de": "Die Packliste finden Sie im Anhang.",
        "en": "The packing list is attached.",
        "es": "La lista de embalaje está adjunta.",
        "it": "La lista di imballaggio è allegata.",
    }.get(language, "Lista pakowania znajduje się w załączniku.")
    footer_text = {
        "pl": "Zespół Niedźwieccy", "de": "Ihr Niedźwieccy-Team",
        "en": "The Niedźwieccy Team", "es": "Equipo Niedźwieccy",
        "it": "Il team Niedźwieccy",
    }.get(language, "Zespół Niedźwieccy")
    safe_orders = "".join(
        f"<li style='margin:4px 0'><b>{html.escape(str(order_no), quote=True)}</b></li>"
        for order_no in order_numbers
    )
    html_body = (
        "<div style='margin:0;padding:28px 14px;background:#f3f6fb;font-family:Arial,sans-serif;color:#10203d'>"
        "<div style='max-width:620px;margin:0 auto;background:#fff;border:1px solid #e2e8f2;border-radius:18px;overflow:hidden'>"
        "<div style='padding:30px 34px 28px'>"
        f"<h1 style='margin:0 0 24px;font-size:26px;line-height:1.25'>{html.escape(subject_text)}</h1>"
        f"<p style='margin:0 0 22px;line-height:1.6'>{html.escape(intro)}</p>"
        "<div style='padding:20px;background:#f7f9fd;border:1px solid #e3e9f3;border-radius:14px;line-height:1.8'>"
        f"<div style='color:#62708c'>Numery zamówień:</div><ul style='margin:4px 0;padding-left:22px'>{safe_orders}</ul>"
        "</div>"
        f"<p style='margin:22px 0 0;line-height:1.6;color:#52617c'>{html.escape(attachment_note)}</p>"
        f"<p style='margin:26px 0 0;line-height:1.6;color:#52617c'><b>{html.escape(footer_text)}</b></p>"
        "</div></div></div>"
    )
    text_body = f"{subject_text}\n{intro}\n" + "\n".join(order_numbers) + f"\n{attachment_note}"
    if packing_path and os.path.exists(packing_path):
        with open(packing_path, "rb") as pdf_file:
            packing_content = pdf_file.read()
        if not packing_content:
            return {"ok": False, "error": "Wygenerowana lista pakowania jest pusta"}
        packing_attachment = {
            "filename": f"{safe_filename(order_numbers[0])}_lista_pakowania.pdf",
            "content": packing_content,
        }
    else:
        packing_attachment = _order_packing_list_email_attachment(order)
    subject_orders = ", ".join(order_numbers)
    return send_email(
        recipient,
        f"{subject_text} – {subject_orders}",
        html_body,
        text_body,
        attachments=[packing_attachment],
    )


def _send_order_packed_email(order: dict) -> dict:
    """Compatibility wrapper for callers that still pack one order."""
    return _send_orders_packed_email([order])


def _order_packing_list_email_attachment(order: dict) -> dict:
    """Return the packed PDF, or safely recreate it from the saved order."""
    order_id = to_int(order.get("id"), 0)
    order_no = canonical_order_no(order_id, order.get("created_at"), order.get("order_no"))
    customer_dir = invoice_dir_for_customer(order.get("customer_name") or "Klient")
    expected_path = packing_list_pdf_path_for_invoice(
        os.path.join(customer_dir, f"{safe_filename(order_no)}.pdf"),
        order_no,
    )
    if not os.path.exists(expected_path):
        c = conn()
        try:
            cur = c.cursor()
            cur.execute("""
              SELECT oi.id, oi.product_id, oi.qty,
                     COALESCE(NULLIF(oi.sku,''), p.sku, '') AS sku,
                     COALESCE(p.model, '') AS model,
                     COALESCE(p.name, '') AS name
              FROM order_items oi
              LEFT JOIN products p ON p.id=oi.product_id
              WHERE oi.order_id=? AND COALESCE(oi.qty,0)>0
              ORDER BY oi.id
            """, (order_id,))
            items = [dict(row) for row in cur.fetchall()]
        finally:
            c.close()
        if not items:
            raise ValueError("zamówienie nie ma pozycji do listy pakowania")
        note = norm(order.get("note"))
        for item in items:
            item["source_order_no"] = order_no
            item["source_order_note"] = note
        meta = {
            "invoice_no": order_no,
            "document_label_key": "order",
            "buyer_name": norm(order.get("customer_name")),
            "buyer_email": norm(order.get("customer_email")),
        }
        expected_path = generate_invoice_packing_list_pdf(order, items, meta)
    with open(expected_path, "rb") as pdf_file:
        content = pdf_file.read()
    if not content:
        raise ValueError("wygenerowana lista pakowania jest pusta")
    return {
        "filename": f"{safe_filename(order_no)}_lista_pakowania.pdf",
        "content": content,
    }


def _send_orders_shipped_email(orders: list[dict], tracking_no: str, carrier: str, packing_attachment: dict) -> dict:
    """Send one shipment message for every order included in one packing batch."""
    orders = [dict(order) for order in (orders or []) if order]
    if not orders:
        return {"ok": False, "error": "Brak zamówień w przesyłce"}
    order = orders[0]
    order_numbers = []
    for packed_order in orders:
        order_no = canonical_order_no(
            packed_order.get("id"),
            packed_order.get("created_at"),
            packed_order.get("order_no"),
        )
        if order_no and order_no not in order_numbers:
            order_numbers.append(order_no)
    if not order_numbers:
        return {"ok": False, "error": "Brak numerów zamówień w przesyłce"}
    if len(order_numbers) == 1:
        return _send_order_shipped_email(order, tracking_no, carrier, packing_attachment)

    if not send_email:
        return {"ok": False, "error": "Moduł wysyłki e-mail nie jest dostępny"}
    recipient = _email_key(order.get("customer_email"))
    if not recipient:
        return {"ok": False, "error": "Zamówienia nie mają adresu e-mail klienta"}
    try:
        language = normalize_client_language(_client_profile_for_email(recipient).get("language"))
    except Exception:
        language = "pl"

    messages = {
        "pl": {
            "subject": "Twoje zamówienia {numbers} są już w drodze",
            "title": "Zamówienia zostały wysłane",
            "greeting": "Dzień dobry,",
            "intro": "Z przyjemnością informujemy, że poniższe zamówienia zostały spakowane razem i przekazane kurierowi w jednej przesyłce.",
            "orders": "Numery zamówień", "carrier": "Przewoźnik", "tracking": "Numer przesyłki",
            "button": "Śledź swoją przesyłkę",
            "attachment": "W załączniku znajduje się wspólna lista pakowania ze szczegółami wysłanych produktów.",
            "thanks": "Dziękujemy za zamówienia i życzymy udanego dnia!", "team": "Zespół Niedźwieccy",
        },
        "de": {
            "subject": "Ihre Bestellungen {numbers} sind unterwegs",
            "title": "Ihre Bestellungen wurden versandt",
            "greeting": "Guten Tag,",
            "intro": "Wir freuen uns, Ihnen mitzuteilen, dass die folgenden Bestellungen gemeinsam verpackt und in einer Sendung an den Paketdienst übergeben wurden.",
            "orders": "Bestellnummern", "carrier": "Paketdienst", "tracking": "Sendungsnummer",
            "button": "Sendung verfolgen",
            "attachment": "Im Anhang finden Sie die gemeinsame Packliste mit den Details der versandten Produkte.",
            "thanks": "Vielen Dank für Ihre Bestellungen. Wir wünschen Ihnen einen schönen Tag!", "team": "Ihr Niedźwieccy-Team",
        },
        "en": {
            "subject": "Your orders {numbers} are on their way",
            "title": "Your orders have been shipped",
            "greeting": "Hello,",
            "intro": "We are pleased to let you know that the following orders were packed together and handed over to the courier in one shipment.",
            "orders": "Order numbers", "carrier": "Courier", "tracking": "Tracking number",
            "button": "Track your shipment",
            "attachment": "The attached combined packing list contains the details of the shipped products.",
            "thanks": "Thank you for your orders and have a great day!", "team": "The Niedźwieccy Team",
        },
        "es": {
            "subject": "Tus pedidos {numbers} ya están en camino",
            "title": "Tus pedidos han sido enviados",
            "greeting": "Buenos días,",
            "intro": "Nos complace informarte de que los siguientes pedidos se embalaron juntos y se entregaron al transportista en un solo envío.",
            "orders": "Números de pedido", "carrier": "Transportista", "tracking": "Número de seguimiento",
            "button": "Seguir el envío",
            "attachment": "En el archivo adjunto encontrarás la lista de embalaje conjunta con los detalles de los productos enviados.",
            "thanks": "¡Gracias por tus pedidos y que tengas un buen día!", "team": "Equipo Niedźwieccy",
        },
        "it": {
            "subject": "I tuoi ordini {numbers} sono in viaggio",
            "title": "I tuoi ordini sono stati spediti",
            "greeting": "Buongiorno,",
            "intro": "Siamo lieti di informarti che i seguenti ordini sono stati imballati insieme e affidati al corriere in un'unica spedizione.",
            "orders": "Numeri ordine", "carrier": "Corriere", "tracking": "Numero di tracciamento",
            "button": "Traccia la spedizione",
            "attachment": "In allegato trovi la lista di imballaggio cumulativa con i dettagli dei prodotti spediti.",
            "thanks": "Grazie per i tuoi ordini e buona giornata!", "team": "Il team Niedźwieccy",
        },
    }
    copy = messages.get(language, messages["pl"])
    if any(norm(item.get("status")).lower() == "partially_shipped" for item in orders):
        partial_copy = {
            "pl": ("Częściowa wysyłka zamówień {numbers}", "Zamówienia zostały wysłane częściowo", "Przekazaliśmy kurierowi część produktów z poniższych zamówień. Pozostałe pozycje wyślemy osobno."),
            "de": ("Teillieferung der Bestellungen {numbers}", "Ihre Bestellungen wurden teilweise versandt", "Ein Teil der Produkte aus den folgenden Bestellungen wurde an den Paketdienst übergeben. Die übrigen Positionen werden separat versandt."),
            "en": ("Partial shipment of orders {numbers}", "Your orders have been partially shipped", "Some products from the following orders have been handed over to the courier. The remaining items will be shipped separately."),
            "es": ("Envío parcial de los pedidos {numbers}", "Tus pedidos se han enviado parcialmente", "Hemos entregado al transportista una parte de los productos de los siguientes pedidos. Los artículos restantes se enviarán por separado."),
            "it": ("Spedizione parziale degli ordini {numbers}", "I tuoi ordini sono stati spediti parzialmente", "Abbiamo affidato al corriere una parte dei prodotti dei seguenti ordini. Gli articoli rimanenti saranno spediti separatamente."),
        }.get(language)
        if partial_copy:
            copy = dict(copy, subject=partial_copy[0], title=partial_copy[1], intro=partial_copy[2])
    numbers_text = ", ".join(order_numbers)
    tracking_url = carrier_tracking_url(carrier, tracking_no)
    carrier_names = {"inpost": "InPost", "dpd": "DPD", "fedex": "FedEx", "dhl": "DHL", "ups": "UPS"}
    carrier_name = carrier_names.get(norm(carrier).lower(), norm(carrier) or "—")
    order_list_html = "".join(
        f"<li style='margin:3px 0'><b>{html.escape(str(number), quote=True)}</b></li>"
        for number in order_numbers
    )
    safe_tracking = html.escape(str(tracking_no), quote=True)
    safe_carrier = html.escape(carrier_name, quote=True)
    safe_tracking_url = html.escape(tracking_url, quote=True)
    html_body = (
        "<div style='margin:0;padding:28px 14px;background:#f3f6fb;font-family:Arial,sans-serif;color:#10203d'>"
        "<div style='max-width:620px;margin:0 auto;background:#fff;border:1px solid #e2e8f2;border-radius:18px;overflow:hidden'>"
        "<div style='padding:30px 34px 20px'>"
        f"<h1 style='margin:0 0 24px;font-size:26px;line-height:1.25'>{html.escape(copy['title'])}</h1>"
        f"<p style='margin:0 0 14px'>{html.escape(copy['greeting'])}</p>"
        f"<p style='margin:0 0 24px;line-height:1.6'>{html.escape(copy['intro'])}</p>"
        "<div style='padding:20px;background:#f7f9fd;border:1px solid #e3e9f3;border-radius:14px;line-height:1.8'>"
        f"<div style='color:#62708c'>{html.escape(copy['orders'])}:</div><ul style='margin:3px 0 10px;padding-left:22px'>{order_list_html}</ul>"
        f"<div><span style='color:#62708c'>{html.escape(copy['carrier'])}:</span> <b>{safe_carrier}</b></div>"
        f"<div><span style='color:#62708c'>{html.escape(copy['tracking'])}:</span> <b>{safe_tracking}</b></div>"
        "</div>"
        f"<p style='margin:24px 0'><a href='{safe_tracking_url}' style='display:inline-block;padding:13px 22px;background:#4f70eb;color:#fff;text-decoration:none;font-weight:bold;border-radius:10px'>{html.escape(copy['button'])}</a></p>"
        f"<p style='margin:0 0 24px;line-height:1.6;color:#52617c'>{html.escape(copy['attachment'])}</p>"
        f"<p style='margin:0;line-height:1.6'>{html.escape(copy['thanks'])}<br><br><b>{html.escape(copy['team'])}</b></p>"
        "</div></div></div>"
    )
    text_orders = "\n".join(f"- {number}" for number in order_numbers)
    text_body = (
        f"{copy['greeting']}\n\n{copy['intro']}\n\n{copy['orders']}:\n{text_orders}\n\n"
        f"{copy['carrier']}: {carrier_name}\n{copy['tracking']}: {tracking_no}\n{tracking_url}\n\n"
        f"{copy['attachment']}\n\n{copy['thanks']}\n{copy['team']}"
    )
    return send_email(
        recipient,
        copy["subject"].format(numbers=numbers_text),
        html_body,
        text_body,
        attachments=[packing_attachment],
    )


def _send_order_shipped_email(order: dict, tracking_no: str, carrier: str, packing_attachment: dict) -> dict:
    if not send_email:
        return {"ok": False, "error": "Moduł wysyłki e-mail nie jest dostępny"}
    recipient = _email_key(order.get("customer_email"))
    if not recipient:
        return {"ok": False, "error": "Zamówienie nie ma adresu e-mail klienta"}
    try:
        language = normalize_client_language(_client_profile_for_email(recipient).get("language"))
    except Exception:
        language = "pl"
    order_no = canonical_order_no(order.get("id"), order.get("created_at"), order.get("order_no"))
    messages = {
        "pl": {
            "subject": "Twoje zamówienie {order_no} jest już w drodze",
            "title": "Zamówienie zostało wysłane",
            "greeting": "Dzień dobry,",
            "intro": "Z przyjemnością informujemy, że Twoje zamówienie zostało przekazane kurierowi.",
            "order": "Numer zamówienia", "carrier": "Przewoźnik", "tracking": "Numer przesyłki",
            "button": "Śledź swoją przesyłkę",
            "attachment": "W załączniku znajduje się lista pakowania zawierająca szczegóły wysłanych produktów.",
            "thanks": "Dziękujemy za zamówienie i życzymy udanego dnia!", "team": "Zespół Niedźwieccy",
        },
        "de": {
            "subject": "Ihre Bestellung {order_no} ist unterwegs",
            "title": "Ihre Bestellung wurde versandt",
            "greeting": "Guten Tag,",
            "intro": "Wir freuen uns, Ihnen mitzuteilen, dass Ihre Bestellung an den Paketdienst übergeben wurde.",
            "order": "Bestellnummer", "carrier": "Paketdienst", "tracking": "Sendungsnummer",
            "button": "Sendung verfolgen",
            "attachment": "Im Anhang finden Sie die Packliste mit den Details zu den versandten Produkten.",
            "thanks": "Vielen Dank für Ihre Bestellung. Wir wünschen Ihnen einen schönen Tag!", "team": "Ihr Niedźwieccy-Team",
        },
        "en": {
            "subject": "Your order {order_no} is on its way",
            "title": "Your order has been shipped",
            "greeting": "Hello,",
            "intro": "We are pleased to let you know that your order has been handed over to the courier.",
            "order": "Order number", "carrier": "Courier", "tracking": "Tracking number",
            "button": "Track your shipment",
            "attachment": "The attached packing list contains the details of the shipped products.",
            "thanks": "Thank you for your order and have a great day!", "team": "The Niedźwieccy Team",
        },
        "es": {
            "subject": "Tu pedido {order_no} ya está en camino",
            "title": "Tu pedido ha sido enviado",
            "greeting": "Buenos días,",
            "intro": "Nos complace informarte de que tu pedido ha sido entregado al transportista.",
            "order": "Número de pedido", "carrier": "Transportista", "tracking": "Número de seguimiento",
            "button": "Seguir el envío",
            "attachment": "En el archivo adjunto encontrarás la lista de embalaje con los detalles de los productos enviados.",
            "thanks": "¡Gracias por tu pedido y que tengas un buen día!", "team": "Equipo Niedźwieccy",
        },
        "it": {
            "subject": "Il tuo ordine {order_no} è in viaggio",
            "title": "Il tuo ordine è stato spedito",
            "greeting": "Buongiorno,",
            "intro": "Siamo lieti di informarti che il tuo ordine è stato affidato al corriere.",
            "order": "Numero ordine", "carrier": "Corriere", "tracking": "Numero di tracciamento",
            "button": "Traccia la spedizione",
            "attachment": "In allegato trovi la lista di imballaggio con i dettagli dei prodotti spediti.",
            "thanks": "Grazie per il tuo ordine e buona giornata!", "team": "Il team Niedźwieccy",
        },
    }
    copy = messages.get(language, messages["pl"])
    if norm(order.get("status")).lower() == "partially_shipped":
        partial_copy = {
            "pl": ("Częściowa wysyłka zamówienia {order_no}", "Zamówienie zostało wysłane częściowo", "Przekazaliśmy kurierowi część produktów z Twojego zamówienia. Pozostałe pozycje wyślemy osobno."),
            "de": ("Teillieferung der Bestellung {order_no}", "Ihre Bestellung wurde teilweise versandt", "Ein Teil der Produkte aus Ihrer Bestellung wurde an den Paketdienst übergeben. Die übrigen Positionen werden separat versandt."),
            "en": ("Partial shipment of order {order_no}", "Your order has been partially shipped", "Some products from your order have been handed over to the courier. The remaining items will be shipped separately."),
            "es": ("Envío parcial del pedido {order_no}", "Tu pedido se ha enviado parcialmente", "Hemos entregado al transportista una parte de los productos de tu pedido. Los artículos restantes se enviarán por separado."),
            "it": ("Spedizione parziale dell'ordine {order_no}", "Il tuo ordine è stato spedito parzialmente", "Abbiamo affidato al corriere una parte dei prodotti del tuo ordine. Gli articoli rimanenti saranno spediti separatamente."),
        }.get(language)
        if partial_copy:
            copy = dict(copy, subject=partial_copy[0], title=partial_copy[1], intro=partial_copy[2])
    tracking_url = carrier_tracking_url(carrier, tracking_no)
    carrier_names = {"inpost": "InPost", "dpd": "DPD", "fedex": "FedEx", "dhl": "DHL", "ups": "UPS"}
    carrier_name = carrier_names.get(norm(carrier).lower(), norm(carrier) or "—")
    safe_order = html.escape(str(order_no), quote=True)
    safe_tracking = html.escape(str(tracking_no), quote=True)
    safe_carrier = html.escape(carrier_name, quote=True)
    safe_tracking_url = html.escape(tracking_url, quote=True)
    subject_text = copy["subject"].format(order_no=order_no)
    html_body = (
        "<div style='margin:0;padding:28px 14px;background:#f3f6fb;font-family:Arial,sans-serif;color:#10203d'>"
        "<div style='max-width:620px;margin:0 auto;background:#ffffff;border:1px solid #e2e8f2;border-radius:18px;overflow:hidden'>"
        "<div style='padding:30px 34px 20px'>"
        f"<h1 style='margin:0 0 24px;font-size:26px;line-height:1.25'>{html.escape(copy['title'])}</h1>"
        f"<p style='margin:0 0 14px'>{html.escape(copy['greeting'])}</p>"
        f"<p style='margin:0 0 24px;line-height:1.6'>{html.escape(copy['intro'])}</p>"
        "<div style='padding:20px;background:#f7f9fd;border:1px solid #e3e9f3;border-radius:14px;line-height:1.8'>"
        f"<div><span style='color:#62708c'>{html.escape(copy['order'])}:</span> <b>{safe_order}</b></div>"
        f"<div><span style='color:#62708c'>{html.escape(copy['carrier'])}:</span> <b>{safe_carrier}</b></div>"
        f"<div><span style='color:#62708c'>{html.escape(copy['tracking'])}:</span> <b>{safe_tracking}</b></div>"
        "</div>"
        f"<p style='margin:24px 0'><a href='{safe_tracking_url}' style='display:inline-block;padding:13px 22px;background:#4f70eb;color:#ffffff;text-decoration:none;font-weight:bold;border-radius:10px'>{html.escape(copy['button'])}</a></p>"
        f"<p style='margin:0 0 24px;line-height:1.6;color:#52617c'>{html.escape(copy['attachment'])}</p>"
        f"<p style='margin:0;line-height:1.6'>{html.escape(copy['thanks'])}<br><br><b>{html.escape(copy['team'])}</b></p>"
        "</div></div></div>"
    )
    text_body = (
        f"{copy['greeting']}\n\n{copy['intro']}\n\n"
        f"{copy['order']}: {order_no}\n{copy['carrier']}: {carrier_name}\n"
        f"{copy['tracking']}: {tracking_no}\n{tracking_url}\n\n"
        f"{copy['attachment']}\n\n{copy['thanks']}\n{copy['team']}"
    )
    return send_email(
        recipient,
        subject_text,
        html_body,
        text_body,
        attachments=[packing_attachment],
    )


def _send_saved_order_confirmation(order_id: int, force: bool = False) -> dict:
    """Send a confirmation using the order saved by the warehouse backend."""
    c = conn()
    try:
        cur = c.cursor()
        cur.execute("SELECT * FROM orders WHERE id=? LIMIT 1", (order_id,))
        row = cur.fetchone()
        if not row:
            return {"ok": False, "error": "Nie znaleziono zamówienia"}
        order = dict(row)
        cur.execute("""
          SELECT
            oi.sku,
            oi.qty,
            COALESCE(p.name, fallback_product.name, '') AS name,
            COALESCE(oi.unit_net_price, price.net_price, 0) AS net_price,
            COALESCE(oi.currency, o.currency, 'PLN') AS currency
          FROM order_items oi
          JOIN orders o ON o.id=oi.order_id
          LEFT JOIN products p ON p.id = oi.product_id
          LEFT JOIN products fallback_product ON fallback_product.sku = oi.sku
          LEFT JOIN pricing price ON TRIM(LOWER(price.model)) = TRIM(LOWER(oi.sku))
          WHERE oi.order_id=?
          ORDER BY oi.id
        """, (order_id,))
        items = [dict(x) for x in cur.fetchall()]
    finally:
        c.close()

    try:
        profile = _client_profile_for_email(order.get("customer_email"))
        order["language"] = profile.get("language", "pl")
        order["currency"] = normalize_order_currency(order.get("currency") or profile.get("currency"))
    except Exception:
        order["language"] = "pl"
        order["currency"] = normalize_order_currency(order.get("currency"))

    try:
        admin_email = norm(email_config_summary().get("admin_email"))
    except Exception:
        admin_email = ""
    recipient = ", ".join([x for x in [norm(order.get("customer_email")), admin_email] if x])
    recipient_hash = hashlib.sha1(recipient.lower().encode("utf-8")).hexdigest()[:12] if recipient else "no-recipient"
    event_key = f"order_confirmation:{order_id}:{recipient_hash}"

    if not force and _email_event_already_ok(event_key):
        return {"ok": True, "duplicate": True, "skipped": True, "to": recipient}
    if not send_order_confirmation:
        result = {"ok": False, "skipped": True, "error": "Brak modułu email_module.py"}
    else:
        try:
            result = send_order_confirmation(order, items, admin_email=admin_email)
        except Exception as exc:
            result = {"ok": False, "error": str(exc)}
    _record_email_event(event_key, "order_confirmation", order_id, recipient, result)
    return result


def _safe_saved_order_confirmation(order_id: int, force: bool = False) -> dict:
    """Email is secondary: it must never turn a saved order into an HTTP 500."""
    try:
        result = (
            _send_saved_order_confirmation(order_id, force=True)
            if force
            else _send_saved_order_confirmation(order_id)
        )
        if not isinstance(result, dict):
            return {"ok": False, "pending_retry": True, "error": "Nieprawidłowa odpowiedź modułu e-mail"}
        if not result.get("ok"):
            result["pending_retry"] = True
        return result
    except Exception as exc:
        app.logger.exception("Zamówienie %s zapisane, ale potwierdzenie e-mail nie powiodło się", order_id)
        return {"ok": False, "pending_retry": True, "error": str(exc)}


def _authenticated_client_user() -> dict | None:
    auth = norm(request.headers.get("Authorization"))
    if not auth.lower().startswith("bearer "):
        return None
    token = auth.split(None, 1)[1].strip()
    if not token:
        return None
    req = urllib.request.Request(f"{SUPABASE_URL}/auth/v1/user", method="GET")
    # Endpoint Auth powinien dostać publiczny klucz projektu jako `apikey`.
    # Token użytkownika pozostaje osobno w nagłówku Authorization.
    api_key = SUPABASE_ANON_KEY or SUPABASE_SERVICE_ROLE_KEY
    if not api_key:
        app.logger.error("Weryfikacja klienta niemożliwa: brak SUPABASE_ANON_KEY")
        return None
    req.add_header("apikey", api_key)
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        email = norm(payload.get("email")).lower()
        if not payload.get("id") or not email:
            return None
        metadata = payload.get("user_metadata") if isinstance(payload.get("user_metadata"), dict) else {}
        return {
            "id": str(payload.get("id")),
            "email": email,
            "name": norm(metadata.get("full_name") or metadata.get("name")) or email.split("@")[0],
        }
    except urllib.error.HTTPError as exc:
        app.logger.warning("Supabase odrzucił token klienta: HTTP %s", exc.code)
        return None
    except Exception as exc:
        app.logger.warning("Nie udało się zweryfikować tokenu klienta: %s", type(exc).__name__)
        return None


def _client_profile_for_email(email: str) -> dict:
    email = _email_key(email)
    fallback = {
        "id": None,
        "name": email.split("@")[0] if email else "",
        "address": "",
        "phone": "",
        "email": email,
        "nip": "",
        "language": "pl",
        "price_list": "pln",
        "currency": "PLN",
    }
    if not email or not supabase_enabled():
        return fallback

    params = {
        "select": "id,name,address,phone,email,nip,language,price_list",
        "email": f"ilike.{email}",
        "order": "id.desc",
        "limit": 1,
    }
    try:
        rows = supabase_request("/rest/v1/customers", params=params, timeout=20) or []
    except urllib.error.HTTPError as exc:
        # Bezpieczny fallback podczas wdrażania, zanim kolumna language zostanie dodana.
        if exc.code != 400:
            raise
        fallback_params = dict(params)
        fallback_params["select"] = "id,name,address,phone,email,nip,language"
        rows = supabase_request("/rest/v1/customers", params=fallback_params, timeout=20) or []

    if not isinstance(rows, list) or not rows:
        return fallback
    row = dict(rows[0])
    language = normalize_client_language(row.get("language"))
    price_list = price_list_for_language(language)
    # Reguła jest egzekwowana także dla istniejących kont. Aktualizacja jest
    # wykonywana podczas logowania, więc nie trzeba ręcznie poprawiać klientów.
    stored_price_list = normalize_client_price_list(row.get("price_list"))
    if row.get("id") is not None and stored_price_list != price_list:
        try:
            supabase_update_rows("customers", {"price_list": price_list}, {"id": row["id"]})
            c = conn()
            try:
                c.execute("UPDATE customers SET price_list=? WHERE id=?", (price_list, row["id"]))
                c.commit()
            finally:
                c.close()
        except Exception as exc:
            # Profil nadal zwracamy z poprawnym cennikiem; błąd zapisu jest
            # widoczny w logu Rendera i zostanie ponowiony przy kolejnym odczycie.
            app.logger.warning("Nie udało się automatycznie zapisać cennika klienta id=%s: %s", row.get("id"), exc)
    return {
        "id": row.get("id"),
        "name": norm(row.get("name")) or fallback["name"],
        "address": norm(row.get("address")),
        "phone": norm(row.get("phone")),
        "email": _email_key(row.get("email")) or email,
        "nip": norm(row.get("nip")),
        "language": language,
        "price_list": price_list,
        "currency": price_list_currency(price_list),
    }


@app.route("/api/client/profile", methods=["GET", "PATCH", "OPTIONS"])
def api_client_profile():
    if request.method == "OPTIONS":
        return ("", 204)

    email = _email_key(g.client_user.get("email"))
    try:
        profile = _client_profile_for_email(email)
    except Exception as exc:
        app.logger.error("Nie udało się pobrać profilu klienta: %s", type(exc).__name__)
        return jsonify(ok=False, error="Nie udało się pobrać profilu klienta"), 503

    if request.method == "GET":
        return jsonify(ok=True, customer=profile)
    # Język określa również cennik (PLN albo EUR), dlatego klient nie może
    # zmieniać go samodzielnie. Ustawienie jest dostępne tylko administratorowi
    # w panelu magazynu. Blokada po stronie serwera chroni również przed ręcznym
    # wywołaniem endpointu poza interfejsem.
    return jsonify(ok=False, error="Język i cennik konta może zmienić wyłącznie administrator"), 403


def _order_by_idempotency_key(idempotency_key: str) -> dict | None:
    c = conn()
    try:
        cur = c.cursor()
        cur.execute("SELECT id, order_no, customer_email FROM orders WHERE idempotency_key=? LIMIT 1", (idempotency_key,))
        row = cur.fetchone()
        if row:
            return dict(row)
    finally:
        c.close()
    if not supabase_enabled():
        return None
    rows = supabase_request(
        "/rest/v1/orders",
        params={"select": "id,order_no,customer_email", "idempotency_key": f"eq.{idempotency_key}", "limit": 1},
    )
    return dict(rows[0]) if isinstance(rows, list) and rows else None


def _client_order_origin_allowed() -> bool:
    origin = norm(request.headers.get("Origin")).rstrip("/")
    return not origin or origin in CLIENT_ALLOWED_ORIGINS


@app.route("/api/client/orders", methods=["POST", "OPTIONS"])
def api_client_orders_create():
    """Create the complete order and send its email in one backend request."""
    if not _client_order_origin_allowed():
        return jsonify(ok=False, error="Niedozwolone źródło żądania"), 403
    if request.method == "OPTIONS":
        return ("", 204)

    if not supabase_enabled():
        app.logger.error("Odrzucono zamówienie klienta: brak konfiguracji Supabase")
        return jsonify(ok=False, error="Brak konfiguracji połączenia z Supabase"), 503

    data = request.get_json(silent=True) or {}
    client_user = _authenticated_client_user()
    if not client_user:
        app.logger.warning("Odrzucono zamówienie klienta: brak lub nieważny token")
        return jsonify(ok=False, error="Brak autoryzacji"), 401

    customer_email = client_user["email"]
    customer_name = client_user["name"]
    note = norm(data.get("note"))
    idempotency_key = norm(request.headers.get("Idempotency-Key"))
    try:
        uuid.UUID(idempotency_key)
    except Exception:
        return jsonify(ok=False, error="Brak lub niepoprawny Idempotency-Key"), 400

    try:
        maybe_pull_shared_from_supabase(force=True)
    except Exception as exc:
        app.logger.error("Nie udało się odświeżyć danych Supabase przed zamówieniem: %s", exc)
        return jsonify(ok=False, error="Nie udało się odświeżyć danych produktów"), 503

    existing = _order_by_idempotency_key(idempotency_key)
    if existing:
        if norm(existing.get("customer_email")).lower() != customer_email:
            return jsonify(ok=False, error="Konflikt Idempotency-Key"), 409
        app.logger.info("Ponowiono request user_id=%s order_id=%s idempotency_key=%s", client_user["id"], existing["id"], idempotency_key)
        return jsonify(ok=True, duplicate=True, order={"id": existing["id"], "order_no": existing["order_no"]}, email=_safe_saved_order_confirmation(int(existing["id"])))

    raw_items = data.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        return jsonify(ok=False, error="Zamówienie nie zawiera pozycji"), 400
    if len(raw_items) > 100:
        return jsonify(ok=False, error="Zamówienie może zawierać maksymalnie 100 pozycji"), 400

    try:
        profile = _client_profile_for_email(customer_email)
        catalog_rows = _client_stock_catalog_rows(profile)
    except Exception as exc:
        app.logger.error("Nie udało się ustalić cennika klienta przed zamówieniem: %s", type(exc).__name__)
        return jsonify(ok=False, error="Nie udało się pobrać aktualnego cennika klienta"), 503
    customer_name = norm(profile.get("name")) or customer_name
    catalog_by_product_id = {to_int(row.get("product_id"), 0): row for row in catalog_rows}

    items = []
    c = conn()
    try:
        cur = c.cursor()
        for item in raw_items:
            if not isinstance(item, dict):
                return jsonify(ok=False, error="Nieprawidłowa pozycja zamówienia"), 400
            product_id = item.get("product_id")
            qty = item.get("qty")
            if isinstance(product_id, bool) or not isinstance(product_id, int) or product_id <= 0:
                return jsonify(ok=False, error="Nieprawidłowy identyfikator produktu"), 400
            if isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0:
                label = norm(item.get("sku")) or str(product_id)
                return jsonify(ok=False, error=f"Nieprawidłowa ilość dla produktu {label}"), 400
            cur.execute("SELECT id, sku, name FROM products WHERE id=? AND COALESCE(archived,0)=0 LIMIT 1", (product_id,))
            product = cur.fetchone()
            if not product:
                return jsonify(ok=False, error=f"Nie istnieje produkt ID {product_id}"), 400
            submitted_sku = norm(item.get("sku"))
            if submitted_sku and submitted_sku.lower() != norm(product["sku"]).lower():
                return jsonify(ok=False, error=f"Produkt ID {product_id} nie odpowiada SKU {submitted_sku}"), 400
            catalog_row = catalog_by_product_id.get(product_id) or {}
            if not catalog_row.get("price_available"):
                return jsonify(ok=False, error=f"Brak ceny w cenniku klienta dla SKU {product['sku']}"), 400
            items.append({
                "product_id": product_id,
                "qty": qty,
                "unit_net_price": money_float(catalog_row.get("net_price")),
                "unit_gross_price": money_float(catalog_row.get("gross_price")),
                "unit_retail_price": money_float(catalog_row.get("retail_price")),
                "currency": normalize_order_currency(catalog_row.get("currency")),
            })
    finally:
        c.close()

    try:
        order_id = remote_first_create_order(
            profile.get("id"),
            customer_name,
            norm(profile.get("address")),
            norm(profile.get("phone")),
            customer_email,
            note,
            items,
            idempotency_key=idempotency_key,
            price_list=profile.get("price_list", "pln"),
            currency=profile.get("currency", "PLN"),
        )
        email_result = _safe_saved_order_confirmation(order_id)
        c = conn()
        try:
            cur = c.cursor()
            cur.execute("SELECT order_no FROM orders WHERE id=?", (order_id,))
            row = cur.fetchone()
            order_no = row["order_no"] if row else make_order_no(order_id, now_iso())
        finally:
            c.close()
        app.logger.info("Utworzono zamówienie user_id=%s order_id=%s order_no=%s items=%s email_ok=%s", client_user["id"], order_id, order_no, len(items), bool(email_result.get("ok")))
        return jsonify(ok=True, order={"id": order_id, "order_no": order_no}, email=email_result)
    except Exception as exc:
        # remote_first_create_order może zdążyć zapisać rekord w Supabase, a
        # następnie utracić połączenie podczas lokalnego odświeżenia. Pobieramy
        # stan ponownie i rozpoznajemy zamówienie po niezmiennym kluczu, zamiast
        # zwracać klientowi fałszywy błąd 500.
        try:
            maybe_pull_shared_from_supabase(force=True)
        except Exception:
            pass
        existing = _order_by_idempotency_key(idempotency_key)
        if existing:
            if norm(existing.get("customer_email")).lower() != customer_email:
                return jsonify(ok=False, error="Konflikt Idempotency-Key"), 409
            app.logger.info("Konflikt idempotencji user_id=%s order_id=%s", client_user["id"], existing["id"])
            return jsonify(ok=True, duplicate=True, recovered=True, order={"id": existing["id"], "order_no": existing["order_no"]}, email=_safe_saved_order_confirmation(int(existing["id"])))
        app.logger.exception("Błąd tworzenia zamówienia user_id=%s items=%s", client_user["id"], len(items))
        return jsonify(ok=False, error=str(exc)), 500


@app.route("/api/client_order_email", methods=["POST", "OPTIONS"])
def api_client_order_email():
    if not _client_order_origin_allowed():
        return jsonify(ok=False, error="Niedozwolone źródło żądania"), 403
    if request.method == "OPTIONS":
        return ("", 204)

    return jsonify(
        ok=False,
        error="Ten endpoint został wyłączony. Zamówienie i potwierdzenie obsługuje /api/client/orders.",
    ), 410

    app.logger.warning("Użyto przestarzałego endpointu /api/client_order_email; zaktualizuj panel do /api/client/orders")

    data = request.get_json(silent=True) or {}
    order_id = to_int(data.get("order_id"), 0)
    order_no = norm(data.get("order_no"))
    fallback_email = norm(data.get("customer_email") or data.get("email")).lower()
    fallback_name = norm(data.get("customer_name")) or (fallback_email.split("@")[0] if fallback_email else "")
    fallback_note = norm(data.get("note"))
    fallback_items = data.get("items") if isinstance(data.get("items"), list) else []

    order = {
        "id": order_id,
        "order_no": order_no,
        "customer_email": fallback_email,
        "customer_name": fallback_name,
        "note": fallback_note,
        "created_at": now_iso(),
    }
    items = []

    try:
        maybe_pull_shared_from_supabase(force=True)
    except Exception:
        pass

    c = conn()
    cur = c.cursor()
    try:
        if order_id:
            cur.execute("SELECT * FROM orders WHERE id=? LIMIT 1", (order_id,))
        elif order_no:
            cur.execute("SELECT * FROM orders WHERE order_no=? LIMIT 1", (order_no,))
        else:
            cur.execute("SELECT * FROM orders WHERE 1=0")
        row = cur.fetchone()
        if row:
            db_order = dict(row)
            # Panel klienta jest źródłem prawdy dla adresu odbiorcy maila.
            # Lokalna baza na Renderze może mieć starszy rekord po synchronizacji,
            # więc nie wolno blokować podmiany, jeśli email już istnieje.
            if fallback_email:
                db_order["customer_email"] = fallback_email
            if fallback_name:
                db_order["customer_name"] = fallback_name
            if fallback_note and not norm(db_order.get("note")):
                db_order["note"] = fallback_note
            order = db_order
            cur.execute("""
              SELECT oi.sku, oi.qty, COALESCE(p.name, pr.name, '') AS name
              FROM order_items oi
              LEFT JOIN products p ON p.id = oi.product_id
              LEFT JOIN products pr ON pr.sku = oi.sku
              WHERE oi.order_id=?
              ORDER BY oi.id
            """, (row["id"],))
            items = [dict(x) for x in cur.fetchall()]
    except Exception:
        items = []
    finally:
        c.close()

    if not items:
        for item in fallback_items[:80]:
            if not isinstance(item, dict):
                continue
            items.append({
                "sku": norm(item.get("sku")),
                "name": norm(item.get("name")),
                "qty": to_int(item.get("qty"), 0),
            })

    if fallback_email:
        order["customer_email"] = fallback_email
    if fallback_name:
        order["customer_name"] = fallback_name

    event_ref = norm(order.get("id")) or norm(order_id) or norm(order.get("order_no")) or order_no
    try:
        admin_email = norm(email_config_summary().get("admin_email"))
    except Exception:
        admin_email = ""
    recipient = ", ".join([x for x in [norm(order.get("customer_email")), admin_email] if x])
    recipient_hash = hashlib.sha1(recipient.lower().encode("utf-8")).hexdigest()[:12] if recipient else "no-recipient"
    event_key = f"order_confirmation:{event_ref}:{recipient_hash}" if event_ref else ""

    if _email_event_already_ok(event_key):
        return jsonify(ok=True, email={"ok": True, "duplicate": True, "skipped": True, "to": recipient, "order_email": norm(order.get("customer_email"))})

    if not send_order_confirmation:
        result = {"ok": False, "skipped": True, "error": "Brak modułu email_module.py"}
        _record_email_event(event_key, "order_confirmation", event_ref, recipient, result)
        return jsonify(ok=True, email=result)

    try:
        result = send_order_confirmation(order, items, admin_email=admin_email)
    except Exception as exc:
        result = {"ok": False, "error": str(exc)}
    _record_email_event(event_key, "order_confirmation", event_ref, recipient, result)
    return jsonify(ok=True, email=result, to=recipient, order_email=norm(order.get("customer_email")))


@app.post("/email/order-confirmations/retry-failed")
def retry_failed_order_confirmations():
    supplied_token = norm(request.headers.get("X-Admin-Token"))
    if not ADMIN_ACTION_TOKEN or supplied_token != ADMIN_ACTION_TOKEN:
        return jsonify(ok=False, error="Brak autoryzacji"), 401
    c = conn()
    try:
        cur = c.cursor()
        cur.execute("""
          SELECT ref_id
          FROM email_events
          WHERE event_type='order_confirmation' AND ok=0
          ORDER BY created_at
          LIMIT 50
        """)
        order_ids = [to_int(row["ref_id"], 0) for row in cur.fetchall()]
    finally:
        c.close()

    retried = 0
    sent = 0
    for order_id in order_ids:
        if order_id <= 0:
            continue
        retried += 1
        result = _send_saved_order_confirmation(order_id)
        if result.get("ok"):
            sent += 1
    app.logger.info("Retry potwierdzeń zamówień retried=%s sent=%s", retried, sent)
    return jsonify(ok=True, retried=retried, sent=sent)


@app.route("/email-test", methods=["GET", "POST"])
def email_test():
    cfg = email_config_summary()
    cfg = dict(cfg or {})
    cfg["module_loaded"] = bool(send_email)
    cfg["import_error"] = _EMAIL_IMPORT_ERROR
    cfg["api_key_set"] = "RESEND_API_KEY" not in (cfg.get("missing") or [])
    result = None
    test_to = norm(request.form.get("to")) or norm(cfg.get("admin_email")) or "biuro@niedzwieccy.com"

    if request.method == "POST":
        if not send_email:
            result = {
                "ok": False,
                "error": "Moduł email_module.py nie jest załadowany. Wgraj email_module.py do repo obok app.py i zrób deploy.",
                "import_error": _EMAIL_IMPORT_ERROR,
            }
        else:
            try:
                result = send_email(
                    test_to,
                    "Test maili z Niedźwieccy Orders",
                    "<div style='font-family:Arial,sans-serif'><h2>Test maili działa</h2><p>Jeśli widzisz tę wiadomość, Resend jest poprawnie podpięty do aplikacji.</p></div>",
                    "Test maili działa. Jeśli widzisz tę wiadomość, Resend jest poprawnie podpięty do aplikacji.",
                )
            except Exception as exc:
                result = {"ok": False, "error": str(exc)}

    tpl = r"""
    {% extends "base.html" %}
    {% block content %}
      <div class="card">
        <h1>Test maili</h1>
        <p class="muted">Ta strona sprawdza konfigurację Resend po stronie Rendera. API key nie jest tutaj wyświetlany.</p>
        <div class="kpi">
          <span class="pill">Moduł: <b>{{ 'załadowany' if cfg.module_loaded else 'brak' }}</b></span>
          <span class="pill">Wysyłka: <b>{{ 'włączona' if cfg.enabled else 'wyłączona' }}</b></span>
          <span class="pill">Konfiguracja: <b>{{ 'OK' if cfg.configured else 'brakuje danych' }}</b></span>
          <span class="pill">API key: <b>{{ 'ustawiony' if cfg.api_key_set else 'brak' }}</b></span>
        </div>
        <div class="line"></div>
        <p><b>EMAIL_FROM:</b> {{ cfg['from'] or '-' }}</p>
        <p><b>ADMIN_EMAIL:</b> {{ cfg.admin_email or '-' }}</p>
        {% if cfg.missing %}
          <p class="hint"><b>Brakuje:</b> {{ cfg.missing|join(', ') }}</p>
        {% endif %}
        {% if cfg.import_error %}
          <p class="hint"><b>Błąd importu:</b> {{ cfg.import_error }}</p>
        {% endif %}
        <form method="post" class="flex" style="margin-top:12px">
          <input name="to" value="{{ test_to }}" placeholder="email do testu" style="max-width:420px">
          <button class="btn primary" type="submit">Wyślij test</button>
        </form>
      </div>

      {% if result %}
        <div class="card">
          <h2>Wynik testu</h2>
          {% if result.ok %}
            <p class="badge" style="background:#dcfce7;border-color:#86efac">Mail wysłany</p>
          {% else %}
            <p class="badge" style="background:#fee2e2;border-color:#fecaca">Mail nie wysłany</p>
          {% endif %}
          <pre style="white-space:pre-wrap;background:#111;color:#fff;padding:12px;border-radius:12px;overflow:auto">{{ result|tojson(indent=2) }}</pre>
        </div>
      {% endif %}
    {% endblock %}
    """
    return render_template_string(tpl, title="Test maili", base_url=BASE_URL, db_path=DB_PATH, cfg=cfg, result=result, test_to=test_to)


def _client_order_items_local(c, order: dict) -> list[dict]:
    """Load order lines without assuming that an old SQLite file has new EUR columns.

    Prices saved on an order line always win.  Historical PLN orders fall back to
    the PLN price list, while historical EUR orders fall back to the imported EUR
    price list.  Keeping the optional columns out of SQL also makes this safe on
    Render during a rolling schema upgrade.
    """
    order_id = to_int(order.get("id"), 0)
    if order_id <= 0:
        return []

    cur = c.cursor()
    cur.execute("""
      SELECT
        oi.*,
        COALESCE(p.model, '') AS model,
        COALESCE(p.ean, '') AS ean,
        COALESCE(p.name, '') AS name,
        COALESCE(s.qty, 0) AS stock_qty,
        COALESCE(s.qty, 0) AS stock,
        COALESCE((
          SELECT SUM(ci.qty)
          FROM china_items ci
          JOIN china_packages cp ON cp.id=ci.package_id
          WHERE ci.product_id=oi.product_id
            AND cp.status IN ('planned', 'ordered', 'shipped')
        ), 0) AS in_delivery
      FROM order_items oi
      LEFT JOIN products p ON p.id=oi.product_id
      LEFT JOIN stock s ON s.product_id=oi.product_id
      WHERE oi.order_id=?
      ORDER BY oi.id
    """, (order_id,))
    items = [dict(row) for row in cur.fetchall()]
    if not items:
        return []

    cur.execute("SELECT model, net_price, gross_price FROM pricing")
    pricing = {}
    for row in cur.fetchall():
        key = norm(row["model"]).strip().lower()
        if key:
            pricing[key] = dict(row)

    eur_pricing = {}
    try:
        cur.execute("SELECT sku, price_eur, uvp_eur FROM pricing_eur")
        for row in cur.fetchall():
            key = norm(row["sku"]).strip().lower()
            if key:
                eur_pricing[key] = dict(row)
    except sqlite3.OperationalError:
        # The table is optional only for databases created before EU pricing.
        eur_pricing = {}

    order_currency = normalize_order_currency(order.get("currency"))
    for item in items:
        item_currency = normalize_order_currency(item.get("currency") or order_currency)
        sku_key = norm(item.get("sku")).strip().lower()
        model_key = norm(item.get("model")).strip().lower()

        snapshot_net = item.get("unit_net_price")
        snapshot_gross = item.get("unit_gross_price")
        snapshot_retail = item.get("unit_retail_price")

        if item_currency == "EUR":
            price_row = eur_pricing.get(sku_key) or {}
            fallback_net = money_float(price_row.get("price_eur"))
            fallback_gross = fallback_net
            fallback_retail = money_float(price_row.get("uvp_eur"))
        else:
            price_row = pricing.get(model_key) or pricing.get(sku_key) or {}
            fallback_net = money_float(price_row.get("net_price"))
            fallback_gross = money_float(price_row.get("gross_price"))
            fallback_retail = money_float(
                Decimal(str(fallback_net or 0)) * Decimal("1.45") * Decimal("1.23")
            )

        net_price = money_float(snapshot_net) if snapshot_net is not None else fallback_net
        gross_price = money_float(snapshot_gross) if snapshot_gross is not None else fallback_gross
        retail_price = money_float(snapshot_retail) if snapshot_retail is not None else fallback_retail
        qty = to_int(item.get("qty"), 0)

        item.update({
            "net_price": net_price,
            "gross_price": gross_price,
            "retail_price": retail_price,
            "currency": item_currency,
            "line_value_net": money_float(Decimal(str(net_price or 0)) * qty),
            "line_value_gross": money_float(Decimal(str(gross_price or 0)) * qty),
            "line_value_retail": money_float(Decimal(str(retail_price or 0)) * qty),
        })
    return items


@app.get("/api/order_lookup")
def api_order_lookup():
    try:
        return _api_order_lookup_impl()
    except Exception as exc:
        app.logger.exception("Błąd szczegółów zamówienia klienta: %s", exc)
        return jsonify(ok=False, error="Nie udało się pobrać szczegółów zamówienia"), 500


def _api_order_lookup_impl():
    maybe_pull_shared_from_supabase(force=True)
    token = norm(request.args.get("token"))
    if not token:
        return jsonify(ok=False, error="Brak tokenu"), 400

    c = conn()
    cur = c.cursor()
    cur.execute("SELECT * FROM orders WHERE order_no=? LIMIT 1", (token,))
    o = cur.fetchone()
    if not o:
        cur.execute("SELECT * FROM orders ORDER BY id DESC")
        all_orders = cur.fetchall()
        for row in all_orders:
            if canonical_order_no(row["id"], row["created_at"], row["order_no"]) == norm(token):
                o = row
                break
    if not o:
        c.close()
        return jsonify(ok=False, error="Nie znaleziono zamĂłwienia"), 404
    order = dict(o)
    if _email_key(order.get("customer_email")) != _email_key(g.client_user["email"]):
        c.close()
        return jsonify(ok=False, error="Brak dostępu"), 403

    items = _client_order_items_local(c, order)

    for it in items:
        it["in_delivery_available"] = int(it.get("in_delivery", 0) or 0)
        it["delivery_used"] = 0
        it["line_shortage"] = 0

    order_id = int(order["id"])
    if norm(order.get("status")).lower() in CURRENT_ORDER_STATUSES:
        status_ph = ",".join(["?"] * len(CURRENT_ORDER_STATUSES))
        cur.execute(f"SELECT id FROM orders WHERE LOWER(COALESCE(status,'')) IN ({status_ph}) AND id<=? ORDER BY id", (*sorted(CURRENT_ORDER_STATUSES), order_id))
        scoped_order_ids = [int(r["id"]) for r in cur.fetchall()]
        if scoped_order_ids:
            sph = ",".join(["?"] * len(scoped_order_ids))
            cur.execute(f"""
              SELECT oi.id, oi.order_id, oi.product_id, oi.qty
              FROM order_items oi
              WHERE oi.order_id IN ({sph})
              ORDER BY oi.order_id, oi.id
            """, tuple(scoped_order_ids))
            seq_items = cur.fetchall()

            product_ids = {int(r["product_id"]) for r in seq_items}
            pool_stock = {}
            pool_delivery = {}
            if product_ids:
                pph = ",".join(["?"] * len(product_ids))
                cur.execute(f"""
                  SELECT p.id AS product_id,
                         COALESCE(s.qty,0) AS stock_qty,
                         COALESCE((
                           SELECT SUM(ci.qty)
                           FROM china_items ci
                           JOIN china_packages cp ON cp.id=ci.package_id
                           WHERE ci.product_id=p.id
                             AND cp.status IN ('planned', 'ordered', 'shipped')
                         ),0) AS in_delivery_qty
                  FROM products p
                  LEFT JOIN stock s ON s.product_id=p.id
                  WHERE p.id IN ({pph})
                """, tuple(product_ids))
                for pr in cur.fetchall():
                    pid = int(pr["product_id"])
                    pool_stock[pid] = int(pr["stock_qty"])
                    pool_delivery[pid] = int(pr["in_delivery_qty"])

            item_alloc = {}
            for sr in seq_items:
                pid = int(sr["product_id"])
                need = int(sr["qty"])

                stock_now = pool_stock.get(pid, 0)
                from_stock = min(stock_now, need)
                pool_stock[pid] = stock_now - from_stock
                need_after_stock = need - from_stock

                delivery_now = pool_delivery.get(pid, 0)
                from_delivery = min(delivery_now, need_after_stock)
                pool_delivery[pid] = delivery_now - from_delivery
                shortage = need_after_stock - from_delivery

                if int(sr["order_id"]) == order_id:
                    item_alloc[int(sr["id"])] = {
                        "in_delivery_available": from_delivery,
                        "delivery_used": from_delivery,
                        "line_shortage": shortage,
                    }

            for it in items:
                al = item_alloc.get(int(it["id"]))
                if al:
                    it.update(al)
    c.close()

    invoiced_by_item = invoiced_qty_by_order_item_ids([int(it["id"]) for it in items])
    for it in items:
        ordered_qty = int(it.get("qty") or 0)
        invoiced_qty = int(invoiced_by_item.get(int(it["id"])) or 0)
        it["ordered_qty"] = ordered_qty
        it["invoiced_qty"] = invoiced_qty
        it["remaining_qty"] = max(0, ordered_qty - invoiced_qty)
        stock_qty = int(it.get("stock_qty") or 0)
        delivery_used = int(it.get("delivery_used") or 0)
        line_shortage = int(it.get("line_shortage") or 0)
        if order.get("status") in ("new", "packed", "confirmed", "in_delivery"):
            it["availability_label"] = "dostępne" if stock_qty >= ordered_qty else "10/20 dni"
        else:
            it["availability_label"] = "dostępne" if stock_qty >= ordered_qty else "10/20 dni"
        if ordered_qty > 0 and invoiced_qty >= ordered_qty:
            it["realization_label"] = "w całości"
        elif invoiced_qty > 0:
            it["realization_label"] = f"częściowo: {invoiced_qty}/{ordered_qty} szt."
        else:
            it["realization_label"] = "0 szt."

    total_net = round(sum(float(it.get("line_value_net") or 0) for it in items), 2)
    total_gross = round(sum(float(it.get("line_value_gross") or 0) for it in items), 2)
    total_retail = round(sum(float(it.get("line_value_retail") or 0) for it in items), 2)
    order_currency = normalize_order_currency(order.get("currency"))
    order_price_list = normalize_client_price_list(order.get("price_list"))

    return jsonify(
        ok=True,
        order={
            "id": order.get("id"),
            "order_no": order.get("order_no"),
            "status": order.get("status"),
            "tracking_no": order.get("tracking_no") or "",
            "carrier": order.get("carrier") or "",
            "packed_at": order.get("packed_at") or "",
            "shipped_at": order.get("shipped_at") or "",
            "created_at": order.get("created_at"),
            "customer_name": order.get("customer_name"),
            "customer_address": order.get("customer_address"),
            "customer_phone": order.get("customer_phone"),
            "customer_email": order.get("customer_email"),
            "note": order.get("note"),
            "qr_data_url": order.get("qr_data_url") or "",
            "warehouse_issued": int(order.get("warehouse_issued") or 0),
            "currency": order_currency,
            "price_list": order_price_list,
            "total_net": total_net,
            "total_gross": total_gross,
            "total_retail": total_retail,
        },
        items=items
    )


@app.get("/api/client/orders/<int:order_id>/pdf")
def api_client_order_pdf(order_id: int):
    try:
        return _api_client_order_pdf_impl(order_id)
    except Exception as exc:
        app.logger.exception("Błąd PDF zamówienia klienta order_id=%s: %s", order_id, exc)
        return jsonify(ok=False, error="Nie udało się przygotować PDF zamówienia"), 500


def _api_client_order_pdf_impl(order_id: int, retail_prices: bool = False):
    maybe_pull_shared_from_supabase(force=True)
    email = _email_key(g.client_user.get("email"))

    c = conn()
    try:
        cur = c.cursor()
        cur.execute("SELECT * FROM orders WHERE id=? LIMIT 1", (order_id,))
        row = cur.fetchone()
        if not row:
            return jsonify(ok=False, error="Nie znaleziono zamówienia"), 404
        order = dict(row)
        if _email_key(order.get("customer_email")) != email:
            return jsonify(ok=False, error="Brak dostępu"), 403

        items = _client_order_items_local(c, order)
    finally:
        c.close()

    try:
        language = _client_profile_for_email(email).get("language", "pl")
    except Exception:
        language = "pl"
    pdf_buffer, filename = generate_client_order_pdf(
        order, items, language, retail_prices=retail_prices
    )
    response = send_file(
        pdf_buffer,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=filename,
        max_age=0,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/client/orders/<int:order_id>/pdf-retail")
def api_client_order_pdf_retail(order_id: int):
    try:
        return _api_client_order_pdf_impl(order_id, retail_prices=True)
    except Exception as exc:
        app.logger.exception("Błąd PDF detal zamówienia klienta order_id=%s: %s", order_id, exc)
        return jsonify(ok=False, error="Nie udało się przygotować PDF detal zamówienia"), 500


@app.get("/orders/<int:order_id>/proforma")
def order_proforma(order_id: int):
    """Generate a foreign-customer proforma without creating a Polish invoice/KSeF record."""
    maybe_pull_shared_from_supabase()
    c = conn()
    try:
        cur = c.cursor()
        cur.execute("SELECT * FROM orders WHERE id=? LIMIT 1", (order_id,))
        row = cur.fetchone()
        if not row:
            return "Nie znaleziono zamówienia", 404
        order = dict(row)
        if normalize_order_currency(order.get("currency")) != "EUR":
            return "Proforma EUR jest dostępna wyłącznie dla zamówień zagranicznych", 400
        items = _client_order_items_local(c, order)
        company_row = cur.execute("SELECT * FROM company_profile WHERE id=1").fetchone()
        company = dict(company_row) if company_row else {}
    finally:
        c.close()
    try:
        profile = _client_profile_for_email(order.get("customer_email"))
        language = normalize_client_language(profile.get("language"))
        order["customer_tax_no"] = profile.get("nip") or ""
        order["customer_name"] = profile.get("name") or order.get("customer_name")
        order["customer_address"] = profile.get("address") or order.get("customer_address")
        order["customer_phone"] = profile.get("phone") or order.get("customer_phone")
    except Exception:
        language = "en"
    company["pdf_font"], company["pdf_font_bold"] = get_pdf_font_names()
    pdf_buffer, filename = generate_proforma_pdf(
        order,
        items,
        company,
        language=language,
        logo_path=find_logo_path(),
        iban=norm(os.environ.get("PROFORMA_EUR_IBAN") or company.get("bank_account")),
        bic=norm(os.environ.get("PROFORMA_EUR_BIC")),
        bank_name=norm(os.environ.get("PROFORMA_EUR_BANK")),
        place=norm(os.environ.get("PROFORMA_PLACE") or "Kotusów"),
    )
    return send_file(pdf_buffer, mimetype="application/pdf", as_attachment=True, download_name=filename, max_age=0)


@app.get("/api/client_invoices")
def api_client_invoices():
    maybe_pull_shared_from_supabase()
    email = _email_key(g.client_user["email"])

    c = conn()
    cur = c.cursor()
    cur.execute("""
      SELECT
        i.*,
        m.invoice_id AS meta_invoice_id,
        COALESCE(m.pdf_path,'') AS pdf_path,
        COALESCE(m.sent_to_client,0) AS sent_to_client,
        COALESCE(m.seen_by_client,0) AS seen_by_client,
        COALESCE(m.payment_reminder,0) AS payment_reminder,
        COALESCE(m.paid,0) AS paid,
        COALESCE(m.paid_at,'') AS paid_at,
        COALESCE(m.seen_at,'') AS seen_at,
        COALESCE(k.status,'draft') AS ksef_status,
        COALESCE(k.ksef_number,'') AS ksef_number,
        COALESCE(k.last_error,'') AS ksef_error,
        COALESCE(k.sent_at,'') AS ksef_sent_at,
        o.id AS source_order_id,
        o.order_no,
        o.created_at AS source_order_created_at,
        o.note AS source_order_note,
        o.customer_email AS order_customer_email
      FROM invoices i
      LEFT JOIN invoice_meta m ON m.invoice_id = i.id
      LEFT JOIN ksef_documents k ON k.invoice_id = i.id
      LEFT JOIN orders o ON o.id = i.order_id
      WHERE (
          LOWER(COALESCE(i.buyer_email,'')) = ?
          OR LOWER(COALESCE(o.customer_email,'')) = ?
        )
        AND (
          COALESCE(m.sent_to_client,0)=1
          OR m.invoice_id IS NULL
        )
      ORDER BY i.order_id DESC, i.id DESC
    """, (email, email))
    rows = []
    for r in cur.fetchall():
        d = dict(r)
        if d.get("meta_invoice_id") is None:
            d["sent_to_client"] = 1
        d["order_display"] = order_display_no(
            d.get("source_order_id"),
            d.get("source_order_created_at"),
            d.get("order_no"),
            d.get("source_order_note")
        ) if d.get("source_order_id") else (d.get("order_no") or "")
        d["pdf_exists"] = 1 if d.get("pdf_path") else 0
        api_base = request.url_root.rstrip("/")
        d["download_url"] = f"{api_base}/api/invoices/{d.get('id')}/download?email={urllib.parse.quote_plus(email)}"
        rows.append(d)
    c.close()
    rows.sort(key=lambda x: ((x.get("seen_by_client") or 0), (x.get("issue_date") or ""), int(x.get("id") or 0)), reverse=True)
    return jsonify(ok=True, invoices=rows)


@app.get("/invoices")
def invoices():
    maybe_pull_shared_from_supabase()
    q = norm(request.args.get("q"))
    c = conn()
    cur = c.cursor()
    params = []
    where = ""
    if q:
        like = f"%{q.lower()}%"
        where = """
          WHERE LOWER(COALESCE(i.invoice_no,'')) LIKE ?
             OR LOWER(COALESCE(i.buyer_name,'')) LIKE ?
             OR LOWER(COALESCE(o.customer_name,'')) LIKE ?
             OR LOWER(COALESCE(o.order_no,'')) LIKE ?
             OR LOWER(COALESCE(o.note,'')) LIKE ?
        """
        params = [like, like, like, like, like]

    cur.execute(f"""
      SELECT
        i.*,
        COALESCE(m.pdf_path,'') AS pdf_path,
        COALESCE(m.sent_to_client,0) AS sent_to_client,
        COALESCE(m.seen_by_client,0) AS seen_by_client,
        COALESCE(m.payment_reminder,0) AS payment_reminder,
        COALESCE(m.paid,0) AS paid,
        COALESCE(m.paid_at,'') AS paid_at,
        COALESCE(m.seen_at,'') AS seen_at,
        COALESCE(k.status,'draft') AS ksef_status,
        COALESCE(k.ksef_number,'') AS ksef_number,
        COALESCE(k.last_error,'') AS ksef_error,
        COALESCE(k.sent_at,'') AS ksef_sent_at,
        o.id AS source_order_id,
        o.order_no AS source_order_no,
        o.created_at AS source_order_created_at,
        o.note AS source_order_note,
        o.customer_name AS order_customer_name
      FROM invoices i
      LEFT JOIN invoice_meta m ON m.invoice_id = i.id
      LEFT JOIN ksef_documents k ON k.invoice_id = i.id
      LEFT JOIN orders o ON o.id = i.order_id
      {where}
      ORDER BY LOWER(COALESCE(i.buyer_name, o.customer_name, '')), i.issue_date DESC, i.id DESC
    """, params)
    rows = [dict(r) for r in cur.fetchall()]
    c.close()

    notice = ""
    notice_error = False
    if request.args.get("generated") == "1":
        if request.args.get("email_sent") == "1":
            notice = "Faktura została zapisana i wysłano klientowi wiadomość e-mail."
        elif request.args.get("email_sent") == "0":
            notice = "Faktura została zapisana, ale wiadomość e-mail nie została wysłana: " + (
                norm(request.args.get("email_error")) or "nieznany błąd wysyłki"
            )
            notice_error = True

    groups = []
    groups_by_key = {}
    for inv in rows:
        customer_name = inv.get("buyer_name") or inv.get("order_customer_name") or "Bez klienta"
        display_name = re.sub(r",\s*", ", ", re.sub(r"\s+", " ", customer_name)).strip()
        buyer_tax_no = re.sub(r"\D", "", norm(inv.get("buyer_tax_no")))
        normalized_name = re.sub(r"[\W_]+", "", display_name.casefold(), flags=re.UNICODE)
        key = ("nip", buyer_tax_no) if buyer_tax_no else ("name", normalized_name)
        current = groups_by_key.get(key)
        if current is None:
            current = {"customer_name": display_name, "invoices": [], "months": [], "total_net": 0.0, "total_gross": 0.0}
            groups.append(current)
            groups_by_key[key] = current
        inv["order_display"] = order_display_no(
            inv.get("source_order_id"),
            inv.get("source_order_created_at"),
            inv.get("source_order_no"),
            inv.get("source_order_note")
        ) if inv.get("source_order_id") else "-"
        inv["pdf_ok"] = 1 if (invoice_pdf_exists(inv.get("pdf_path", ""), inv.get("invoice_no", ""))[0] or inv.get("invoice_items_json")) else 0
        current["invoices"].append(inv)
        current["total_net"] += float(inv.get("total_net") or 0)
        current["total_gross"] += float(inv.get("total_gross") or 0)

    for g in groups:
        month_map = {}
        for inv in g["invoices"]:
            issue_date = norm(inv.get("issue_date"))
            month_key = issue_date[:7] if len(issue_date) >= 7 else "bez-daty"
            month_label = month_key if month_key != "bez-daty" else "Bez daty"
            if month_key not in month_map:
                month_map[month_key] = {"month": month_key, "label": month_label, "invoices": [], "total_net": 0.0, "total_gross": 0.0}
                g["months"].append(month_map[month_key])
            month = month_map[month_key]
            month["invoices"].append(inv)
            month["total_net"] += float(inv.get("total_net") or 0)
            month["total_gross"] += float(inv.get("total_gross") or 0)

    tpl = r"""
    {% extends "base.html" %}
    {% block content %}
      <div class="card">
        <div class="flex">
          <h1 style="margin:0;">Faktury</h1>
        </div>
        <form method="get" class="flex" style="margin-top:12px;">
          <input name="q" value="{{ q }}" placeholder="Szukaj: klient, numer faktury, numer zamówienia, notatka">
          <button class="btn primary" type="submit">Szukaj</button>
          <a class="btn" href="{{ url_for('invoices') }}">Wyczyść</a>
        </form>
      </div>

      {% if notice %}
        <div class="card" style="{% if notice_error %}border-color:#fecaca;background:#fff1f2;color:#991b1b;{% endif %}">
          {{ notice }}
        </div>
      {% endif %}

      {% for g in groups %}
        <div class="card">
          <details {% if q %}open{% endif %}>
            <summary class="flex" style="cursor:pointer; align-items:center;">
              <h2 style="margin:0;">{{ g.customer_name }}</h2>
              <span class="badge">{{ g.invoices|length }} faktur</span>
              <span class="badge">Netto: {{ "%.2f"|format(g.total_net) }} PLN</span>
              <span class="badge">Brutto: {{ "%.2f"|format(g.total_gross) }} PLN</span>
              <span class="btn right">Pokaż faktury</span>
            </summary>

            {% for m in g.months %}
              <details style="margin-top:10px;" {% if q %}open{% endif %}>
                <summary class="flex" style="cursor:pointer; align-items:center;">
                  <b>{{ m.label }}</b>
                  <span class="badge">{{ m.invoices|length }} faktur</span>
                  <span class="badge">Netto: {{ "%.2f"|format(m.total_net) }} PLN</span>
                  <span class="badge">Brutto: {{ "%.2f"|format(m.total_gross) }} PLN</span>
                </summary>

                <table style="margin-top:10px;">
                  <thead>
                    <tr>
                      <th>Faktura</th>
                      <th>Data</th>
                      <th>Zamówienie</th>
                      <th>Netto</th>
                      <th>Brutto</th>
                      <th>Status</th>
                      <th>Akcje</th>
                    </tr>
                  </thead>
                  <tbody>
                    {% for inv in m.invoices %}
                      <tr>
                        <td><b>{{ inv.invoice_no }}</b></td>
                        <td>{{ inv.issue_date }}</td>
                        <td>{{ inv.order_display }}</td>
                        <td>{{ "%.2f"|format(inv.total_net) }}</td>
                        <td>{{ "%.2f"|format(inv.total_gross) }}</td>
                        <td>
                          {% if inv.sent_to_client %}
                            <span class="badge ok">Udostępniona klientowi</span>
                          {% else %}
                            <span class="badge">Nieudostępniona</span>
                          {% endif %}
                          {% if inv.sent_to_client %}
                            {% if inv.seen_by_client %}
                              <span class="badge ok">PDF pobrany</span>
                              {% if inv.seen_at %}<div class="muted small">{{ inv.seen_at }}</div>{% endif %}
                            {% else %}
                              <span class="badge">PDF niepobrany</span>
                            {% endif %}
                          {% endif %}
                          {% if not inv.pdf_ok %}
                            <span class="badge danger">Brak PDF</span>
                          {% endif %}
                          {% if inv.paid %}
                            <span class="badge ok">Opłacona</span>
                          {% else %}
                            <span class="badge danger">Nieopłacona</span>
                            {% if inv.payment_reminder %}<span class="badge ok">Przypomnienie wysłane</span>{% endif %}
                          {% endif %}
                          {% if inv.ksef_status == 'sent' %}
                            <span class="badge ok">W KSeF</span>
                            {% if inv.ksef_number %}<div class="muted small">{{ inv.ksef_number }}</div>{% endif %}
                          {% elif inv.ksef_status == 'ready' %}
                            <span class="badge ok">KSeF FA(3) OK</span>
                          {% elif inv.ksef_status == 'error' %}
                            <span class="badge danger">KSeF do poprawy</span>
                            {% if inv.ksef_error %}<div class="muted small">{{ inv.ksef_error }}</div>{% endif %}
                          {% else %}
                            <span class="badge">Nie wysłana do KSeF</span>
                          {% endif %}
                        </td>
                        <td>
                          <div class="flex">
                            <a class="btn" href="{{ url_for('invoice_download_admin', invoice_id=inv.id) }}" target="_blank">Faktura PDF</a>
                            <a class="btn" href="{{ url_for('invoice_packing_list_download_admin', invoice_id=inv.id) }}" target="_blank">Pakuj</a>
                            {% if not inv.sent_to_client %}
                              <form method="post" action="{{ url_for('invoice_send_admin', invoice_id=inv.id) }}">
                                <input type="hidden" name="next" value="{{ request.full_path }}">
                                <button class="btn primary" type="submit">Udostępnij klientowi</button>
                              </form>
                            {% endif %}
                            {% if inv.source_order_id %}
                              <a class="btn" href="{{ url_for('order_view', order_id=inv.source_order_id) }}">Zamówienie</a>
                            {% endif %}
                            {% if not inv.paid %}
                              <form method="post" action="{{ url_for('invoice_payment_reminder_admin', invoice_id=inv.id) }}">
                                <input type="hidden" name="next" value="{{ request.full_path }}">
                                <button class="btn" type="submit">Przypomnij o płatności</button>
                              </form>
                              <form method="post" action="{{ url_for('invoice_paid_admin', invoice_id=inv.id) }}">
                                <input type="hidden" name="next" value="{{ request.full_path }}">
                                <button class="btn ok" type="submit">Faktura opłacona</button>
                              </form>
                            {% else %}
                              <form method="post" action="{{ url_for('invoice_unpaid_admin', invoice_id=inv.id) }}">
                                <input type="hidden" name="next" value="{{ request.full_path }}">
                                <button class="btn" type="submit">Cofnij opłacenie</button>
                              </form>
                            {% endif %}
                            {% if inv.ksef_status != 'sent' %}
                              <a class="btn" href="{{ url_for('invoice_ksef_xml', invoice_id=inv.id) }}">XML KSeF FA(3)</a>
                              <form method="post" action="{{ url_for('invoice_ksef_validate', invoice_id=inv.id) }}">
                                <input type="hidden" name="next" value="{{ request.full_path }}">
                                <button class="btn" type="submit">Sprawdź KSeF</button>
                              </form>
                              <form method="post" action="{{ url_for('invoice_ksef_send', invoice_id=inv.id) }}" onsubmit="return confirm('UWAGA: to jest realna wysyłka faktury do KSeF. Po wysłaniu faktura otrzyma numer KSeF i nie będzie można jej edytować. Kontynuować?');">
                                <input type="hidden" name="next" value="{{ request.full_path }}">
                                <button class="btn primary" type="submit">Wyślij do KSeF</button>
                              </form>
                              <a class="btn" href="{{ url_for('invoice_edit_admin', invoice_id=inv.id) }}">Edytuj</a>
                              <form method="post" action="{{ url_for('invoice_delete_admin', invoice_id=inv.id) }}" onsubmit="return confirm('Usunąć fakturę {{ inv.invoice_no }}? To usunie też PDF i widoczność w panelu klienta.')">
                                <input type="hidden" name="next" value="{{ request.full_path }}">
                                <button class="btn danger" type="submit">Usuń</button>
                              </form>
                            {% else %}
                              <form method="post" action="{{ url_for('invoice_rollback_admin', invoice_id=inv.id) }}" onsubmit="return confirm('AWARYJNIE cofnąć fakturę {{ inv.invoice_no }} w aplikacji? To usunie lokalny zapis faktury, status KSeF, widoczność u klienta i przeliczy zamówienia oraz stany. Używaj tylko przy pomyłce/testach.');">
                                <input type="hidden" name="next" value="{{ request.full_path }}">
                                <button class="btn danger" type="submit">Cofnij fakturę</button>
                              </form>
                            {% endif %}
                          </div>
                        </td>
                      </tr>
                    {% endfor %}
                  </tbody>
                </table>
              </details>
            {% endfor %}
          </details>
        </div>
      {% endfor %}

      {% if not groups %}
        <div class="card muted">Brak faktur.</div>
      {% endif %}
    {% endblock %}
    """
    return render_template_string(
        tpl, title="Faktury", base_url=BASE_URL, db_path=DB_PATH,
        groups=groups, q=q, notice=notice, notice_error=notice_error
    )


@app.get("/payments/overdue")
def overdue_payments():
    maybe_pull_shared_from_supabase()
    c = conn()
    rows = overdue_invoice_rows(c)
    c.close()
    total_gross = sum(float(inv.get("total_gross") or 0) for inv in rows)
    tpl = r"""
    {% extends "base.html" %}
    {% block content %}
      <div class="card">
        <div class="flex" style="align-items:center">
          <div><h1 style="margin:0">Zaległości</h1><div class="muted">Faktury nieopłacone widoczne od godz. 8:00 następnego dnia po terminie płatności.</div></div>
          <a class="btn right" href="{{ url_for('home') }}">← Pulpit</a>
        </div>
        <div class="flex" style="margin-top:16px"><span class="badge danger">Zaległych faktur: {{ rows|length }}</span><span class="badge">Łącznie brutto: {{ "%.2f"|format(total_gross) }} PLN</span></div>
      </div>
      <div class="card">
        <h2>Sprawdź płatności</h2>
        <table><thead><tr><th>Faktura</th><th>Klient</th><th>Zamówienie</th><th>Termin</th><th>Po terminie</th><th>Brutto</th><th>Akcje</th></tr></thead><tbody>
        {% for inv in rows %}<tr>
          <td><b>{{ inv.invoice_no }}</b></td><td>{{ inv.buyer_name or inv.order_customer_name or '-' }}</td><td>{{ inv.order_display }}</td><td>{{ inv.payment_to }}</td>
          <td><span class="badge danger">{{ inv.overdue_days }} dni</span>{% if inv.payment_reminder %} <span class="badge ok">Przypomnienie wysłane</span>{% endif %}</td><td><b>{{ "%.2f"|format(inv.total_gross or 0) }} PLN</b></td>
          <td><div class="flex"><a class="btn" href="{{ url_for('invoice_download_admin', invoice_id=inv.id) }}" target="_blank">Faktura PDF</a>{% if inv.source_order_id %}<a class="btn" href="{{ url_for('order_view', order_id=inv.source_order_id) }}">Zamówienie</a>{% endif %}<form method="post" action="{{ url_for('invoice_payment_reminder_admin', invoice_id=inv.id) }}"><input type="hidden" name="next" value="{{ request.full_path }}"><button class="btn" type="submit">Przypomnij o płatności</button></form><form method="post" action="{{ url_for('invoice_paid_admin', invoice_id=inv.id) }}"><input type="hidden" name="next" value="{{ request.full_path }}"><button class="btn ok" type="submit">Faktura opłacona</button></form></div></td>
        </tr>{% endfor %}
        {% if not rows %}<tr><td colspan="7" class="muted">Brak zaległych faktur. Wszystkie płatności są aktualne.</td></tr>{% endif %}
        </tbody></table>
      </div>
    {% endblock %}
    """
    return render_template_string(tpl, title="Zaległości", base_url=BASE_URL, db_path=DB_PATH, rows=rows, total_gross=total_gross)


def load_invoice_with_meta(invoice_id: int):
    c = conn()
    cur = c.cursor()
    cur.execute("""
      SELECT i.*, COALESCE(m.pdf_path,'') AS pdf_path, COALESCE(m.sent_to_client,0) AS sent_to_client,
             COALESCE(m.invoice_items_json,'') AS invoice_items_json
      FROM invoices i
      LEFT JOIN invoice_meta m ON m.invoice_id = i.id
      WHERE i.id=?
      LIMIT 1
    """, (invoice_id,))
    row = cur.fetchone()
    c.close()
    return dict(row) if row else None

def invoice_meta_payload(invoice_row: dict):
    buyer_address = "\n".join([x for x in [
        invoice_row.get("buyer_street") or "",
        f"{invoice_row.get('buyer_post_code') or ''} {invoice_row.get('buyer_city') or ''}".strip()
    ] if x]).strip()
    ksef_number = invoice_row.get("ksef_number") or ""
    if not ksef_number and invoice_row.get("id"):
        try:
            ksef_number = load_ksef_doc(int(invoice_row.get("id") or 0)).get("ksef_number") or ""
        except Exception:
            ksef_number = ""

    return {
        "invoice_no": invoice_row.get("invoice_no") or "",
        "place": "KotuszĂłw",
        "issue_date": invoice_row.get("issue_date") or app_now().strftime("%Y-%m-%d"),
        "sell_date": invoice_row.get("sell_date") or app_now().strftime("%Y-%m-%d"),
        "payment_type": invoice_row.get("payment_type") or "przelew",
        "payment_to": invoice_row.get("payment_to") or "",
        "buyer_name": invoice_row.get("buyer_name") or "",
        "buyer_tax_no": invoice_row.get("buyer_tax_no") or "",
        "buyer_address": buyer_address,
        "buyer_country": invoice_row.get("buyer_country") or "PL",
        "buyer_email": invoice_row.get("buyer_email") or "",
        "buyer_phone": invoice_row.get("buyer_phone") or "",
        "discount_percent": "0",
        "ksef_number": ksef_number,
    }

def invoice_items_from_saved_json(invoice_id: int):
    meta = load_invoice_meta(invoice_id) or {}
    raw = meta.get("invoice_items_json") or ""
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, list) and data:
                return data
        except Exception:
            pass

    c = conn()
    cur = c.cursor()
    cur.execute("""
      SELECT oi.*, p.model, p.name,
             COALESCE(pr.net_price, 0) AS net_price,
             COALESCE(pr.gross_price, 0) AS gross_price,
             (oi.qty * COALESCE(pr.net_price, 0)) AS line_value_net,
             (oi.qty * COALESCE(pr.gross_price, 0)) AS line_value_gross
      FROM order_items oi
      JOIN products p ON p.id=oi.product_id
      LEFT JOIN pricing pr ON (TRIM(LOWER(pr.model)) = TRIM(LOWER(p.model)) OR TRIM(LOWER(pr.model)) = TRIM(LOWER(p.sku)))
      WHERE oi.order_id=(SELECT order_id FROM invoices WHERE id=?)
      ORDER BY oi.id
    """, (invoice_id,))
    items = [dict(r) for r in cur.fetchall()]
    c.close()
    return items


def load_company_profile() -> dict:
    c = conn()
    cur = c.cursor()
    cur.execute("SELECT * FROM company_profile WHERE id=1")
    row = cur.fetchone()
    c.close()
    return dict(row) if row else {}


def ksef_dir() -> str:
    path = os.path.join(DATA_DIR, "ksef")
    os.makedirs(path, exist_ok=True)
    return path


def ksef_xml_path(invoice_id: int, invoice_no: str) -> str:
    return os.path.join(ksef_dir(), f"{int(invoice_id)}_{xml_filename(invoice_no)}")


def ksef_schema_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "fa3_schemat.xsd")


def load_ksef_doc(invoice_id: int) -> dict:
    c = conn()
    cur = c.cursor()
    cur.execute("SELECT * FROM ksef_documents WHERE invoice_id=?", (invoice_id,))
    row = cur.fetchone()
    c.close()
    return dict(row) if row else {}


def upsert_ksef_doc(invoice_id: int, status: str, xml_path: str = "", last_error: str = "", ksef_number: str = ""):
    current = load_ksef_doc(invoice_id)
    sent_at = current.get("sent_at", "")
    if status == "sent" and not sent_at:
        sent_at = now_iso()
    c = conn()
    cur = c.cursor()
    cur.execute("""
      INSERT INTO ksef_documents(invoice_id, status, ksef_number, xml_path, last_error, validated_at, sent_at, updated_at)
      VALUES(?,?,?,?,?,?,?,?)
      ON CONFLICT(invoice_id) DO UPDATE SET
        status=excluded.status,
        ksef_number=COALESCE(NULLIF(excluded.ksef_number,''), ksef_documents.ksef_number),
        xml_path=COALESCE(NULLIF(excluded.xml_path,''), ksef_documents.xml_path),
        last_error=excluded.last_error,
        validated_at=excluded.validated_at,
        sent_at=COALESCE(ksef_documents.sent_at, excluded.sent_at),
        updated_at=excluded.updated_at
    """, (
        invoice_id,
        status,
        ksef_number or current.get("ksef_number", ""),
        xml_path or current.get("xml_path", ""),
        last_error or "",
        now_iso(),
        sent_at,
        now_iso(),
    ))
    c.commit()
    c.close()
    try:
        sync_local_rows_to_supabase("ksef_documents", "invoice_id", [invoice_id])
    except Exception:
        pass


def regenerate_invoice_pdf_after_ksef_send(invoice_id: int, ksef_number: str) -> bool:
    inv = load_invoice_with_meta(invoice_id)
    if not inv:
        return False
    items = invoice_items_from_saved_json(invoice_id)
    if not items:
        return False

    c = conn()
    cur = c.cursor()
    cur.execute("SELECT * FROM orders WHERE id=?", (inv.get("order_id"),))
    order_row = cur.fetchone()
    c.close()

    meta_payload = invoice_meta_payload(inv)
    meta_payload["ksef_number"] = ksef_number
    pdf_path, total_net, total_gross = generate_order_invoice_pdf(order_row, items, meta_payload)
    packing_pdf_path = generate_invoice_packing_list_pdf(order_row, items, meta_payload, pdf_path)
    stored_pdf_path = upload_invoice_pdfs_to_supabase(invoice_id, inv.get("invoice_no") or f"FV_{invoice_id}", pdf_path, packing_pdf_path)

    c = conn()
    cur = c.cursor()
    cur.execute("UPDATE invoices SET total_net=?, total_gross=? WHERE id=?", (total_net, total_gross, invoice_id))
    c.commit()
    c.close()

    current_meta = load_invoice_meta(invoice_id) or {}
    upsert_invoice_meta(
        invoice_id,
        stored_pdf_path,
        current_meta.get("invoice_items_json") or json.dumps(items, ensure_ascii=False),
        sent_to_client=int(current_meta.get("sent_to_client") or 0),
        seen_by_client=int(current_meta.get("seen_by_client") or 0),
        seen_at=current_meta.get("seen_at"),
        payment_reminder=int(current_meta.get("payment_reminder") or 0),
        paid=int(current_meta.get("paid") or 0),
        paid_at=current_meta.get("paid_at"),
    )

    if supabase_enabled():
        try:
            sync_local_rows_to_supabase("invoices", "id", [invoice_id])
        except Exception:
            pass
        try:
            sync_invoice_meta_to_supabase(invoice_id)
        except Exception:
            pass
    return True


def build_invoice_ksef_payload(invoice_id: int):
    inv = load_invoice_with_meta(invoice_id)
    if not inv:
        return None, {}, [], ["Nie znaleziono faktury."]
    company = load_company_profile()
    items = invoice_items_from_saved_json(invoice_id)
    problems = validate_ksef_invoice(inv, company, items)
    return inv, company, items, problems


@app.get("/ksef")
def ksef_dashboard():
    maybe_pull_shared_from_supabase()
    ksef_cfg = ksef_config_summary()
    c = conn()
    cur = c.cursor()
    cur.execute("""
      SELECT i.*, COALESCE(k.status,'draft') AS ksef_status,
             COALESCE(k.ksef_number,'') AS ksef_number,
             COALESCE(k.last_error,'') AS ksef_error,
             COALESCE(k.validated_at,'') AS ksef_validated_at,
             COALESCE(k.sent_at,'') AS ksef_sent_at
      FROM invoices i
      LEFT JOIN ksef_documents k ON k.invoice_id=i.id
      ORDER BY i.issue_date DESC, i.id DESC
      LIMIT 200
    """)
    rows = [dict(r) for r in cur.fetchall()]
    c.close()

    counts = {"draft": 0, "ready": 0, "error": 0, "sent": 0}
    for r in rows:
        counts[r.get("ksef_status") or "draft"] = counts.get(r.get("ksef_status") or "draft", 0) + 1

    tpl = r"""
    {% extends "base.html" %}
    {% block content %}
      <div class="card">
        <div class="flex">
          <h1 style="margin:0;">KSeF</h1>
          <span class="badge">FA(3)</span>
        </div>
        <div class="hint" style="margin-top:10px;">
          Generator tworzy XML w strukturze FA(3). Przed wysłaniem sprawdź fakturę przyciskiem „Sprawdź” i przetestuj plik w Aplikacji Podatnika KSeF.
        </div>
        {% if not ksef_cfg.configured %}
          <div class="hint" style="margin-top:10px; border-color:#fecaca; background:#fff1f2;">
            Wysyłka bezpośrednia jest gotowa, ale w Render brakuje: <b>{{ ksef_cfg.missing|join(', ') }}</b>.
          </div>
        {% else %}
          <div class="hint" style="margin-top:10px;">
            Wysyłka bezpośrednia aktywna: <b>{{ ksef_cfg.env }}</b>.
          </div>
        {% endif %}
        <div class="kpi" style="margin-top:10px;">
          <div class="pill">Do sprawdzenia: <b>{{ counts.get('draft',0) }}</b></div>
          <div class="pill">FA(3) OK: <b>{{ counts.get('ready',0) }}</b></div>
          <div class="pill">Do poprawy: <b>{{ counts.get('error',0) }}</b></div>
          <div class="pill">Wysłane: <b>{{ counts.get('sent',0) }}</b></div>
        </div>
      </div>

      <div class="card">
        <table>
          <thead>
            <tr><th>Faktura</th><th>Klient</th><th>Data</th><th>Brutto</th><th>Status KSeF</th><th>Akcje</th></tr>
          </thead>
          <tbody>
            {% for inv in rows %}
              <tr>
                <td><b>{{ inv.invoice_no }}</b></td>
                <td>{{ inv.buyer_name or '-' }}</td>
                <td>{{ inv.issue_date }}</td>
                <td>{{ "%.2f"|format(inv.total_gross or 0) }}</td>
                <td>
                  {% if inv.ksef_status == 'ready' %}
                    <span class="badge ok">FA(3) OK</span>
                  {% elif inv.ksef_status == 'error' %}
                    <span class="badge danger">Do poprawy</span>
                  {% elif inv.ksef_status == 'sent' %}
                    <span class="badge ok">Wysłana</span>
                    {% if inv.ksef_number %}<div class="muted">{{ inv.ksef_number }}</div>{% endif %}
                  {% else %}
                    <span class="badge">Do sprawdzenia</span>
                  {% endif %}
                  {% if inv.ksef_error %}<div class="muted">{{ inv.ksef_error }}</div>{% endif %}
                </td>
                <td>
                  <div class="flex">
                    <form method="post" action="{{ url_for('invoice_ksef_validate', invoice_id=inv.id) }}">
                      <button class="btn" type="submit">Sprawdź</button>
                    </form>
                    <a class="btn primary" href="{{ url_for('invoice_ksef_xml', invoice_id=inv.id) }}">Pobierz XML KSeF FA(3)</a>
                    {% if inv.ksef_status != 'sent' %}
                      <form method="post" action="{{ url_for('invoice_ksef_send', invoice_id=inv.id) }}" onsubmit="return confirm('UWAGA: to jest realna wysyłka faktury do KSeF. Po wysłaniu faktura otrzyma numer KSeF i nie będzie można jej edytować. Kontynuować?');">
                        <input type="hidden" name="next" value="{{ request.full_path }}">
                        <button class="btn primary" type="submit">Wyślij do KSeF</button>
                      </form>
                      <form method="post" action="{{ url_for('invoice_ksef_mark_sent', invoice_id=inv.id) }}" onsubmit="return confirm('Oznaczyć fakturę jako wysłaną do KSeF?');" style="display:flex; gap:6px; flex-wrap:wrap; align-items:center;">
                        <input type="hidden" name="next" value="{{ request.full_path }}">
                        <input name="ksef_number" placeholder="Numer KSeF" style="width:220px;">
                        <button class="btn" type="submit">Oznacz wysłaną</button>
                      </form>
                      <a class="btn" href="{{ url_for('invoice_edit_admin', invoice_id=inv.id) }}">Edytuj fakturę</a>
                    {% else %}
                      <span class="badge ok">Wysłana do KSeF — edycja zablokowana</span>
                      <form method="post" action="{{ url_for('invoice_rollback_admin', invoice_id=inv.id) }}" onsubmit="return confirm('AWARYJNIE cofnąć fakturę {{ inv.invoice_no }} w aplikacji? To usunie lokalny zapis faktury, status KSeF, widoczność u klienta i przeliczy zamówienia oraz stany. Nie usuwa faktury z KSeF.');">
                        <input type="hidden" name="next" value="{{ request.full_path }}">
                        <button class="btn danger" type="submit">Cofnij w aplikacji</button>
                      </form>
                    {% endif %}
                  </div>
                </td>
              </tr>
            {% endfor %}
            {% if not rows %}
              <tr><td colspan="6" class="muted">Brak faktur.</td></tr>
            {% endif %}
          </tbody>
        </table>
      </div>
    {% endblock %}
    """
    return render_template_string(tpl, title="KSeF", base_url=BASE_URL, db_path=DB_PATH, rows=rows, counts=counts, ksef_cfg=ksef_cfg)


@app.post("/invoices/<int:invoice_id>/ksef/validate")
def invoice_ksef_validate(invoice_id):
    inv, company, items, problems = build_invoice_ksef_payload(invoice_id)
    if not inv:
        return "Nie znaleziono faktury", 404
    if problems:
        upsert_ksef_doc(invoice_id, "error", last_error="; ".join(problems[:5]))
    else:
        xml = build_ksef_draft_xml(inv, company, items)
        path = ksef_xml_path(invoice_id, inv.get("invoice_no") or f"FV_{invoice_id}")
        schema = ksef_schema_path()
        schema_errors = validate_fa3_xml(xml, schema) if os.path.exists(schema) else []
        if schema_errors:
            upsert_ksef_doc(invoice_id, "error", last_error="; ".join(schema_errors[:3]))
            return redirect(request.form.get("next") or url_for("ksef_dashboard"))
        with open(path, "w", encoding="utf-8") as f:
            f.write(xml)
        upsert_ksef_doc(invoice_id, "ready", xml_path=path)
    return redirect(request.form.get("next") or url_for("ksef_dashboard"))


@app.post("/invoices/<int:invoice_id>/ksef/mark-sent")
def invoice_ksef_mark_sent(invoice_id):
    next_url = request.form.get("next") or url_for("ksef_dashboard")
    ksef_number = (request.form.get("ksef_number") or "").strip()
    if not ksef_number:
        upsert_ksef_doc(invoice_id, "error", last_error="Wpisz numer KSeF, żeby oznaczyć fakturę jako wysłaną.")
        return redirect(next_url)
    upsert_ksef_doc(invoice_id, "sent", ksef_number=ksef_number, last_error="")
    try:
        regenerate_invoice_pdf_after_ksef_send(invoice_id, ksef_number)
    except Exception as exc:
        upsert_ksef_doc(invoice_id, "sent", ksef_number=ksef_number, last_error=f"Oznaczono jako wysłaną, ale nie udało się odświeżyć PDF: {exc}")
    return redirect(next_url)


@app.post("/invoices/<int:invoice_id>/ksef/send")
def invoice_ksef_send(invoice_id):
    next_url = request.form.get("next") or url_for("ksef_dashboard")
    current_ksef = load_ksef_doc(invoice_id)
    if current_ksef.get("status") == "sent":
        return redirect(next_url)
    inv, company, items, problems = build_invoice_ksef_payload(invoice_id)
    if not inv:
        return "Nie znaleziono faktury", 404
    if problems:
        upsert_ksef_doc(invoice_id, "error", last_error="; ".join(problems[:5]))
        return redirect(next_url)

    xml = build_ksef_draft_xml(inv, company, items)
    schema = ksef_schema_path()
    schema_errors = validate_fa3_xml(xml, schema) if os.path.exists(schema) else []
    if schema_errors:
        upsert_ksef_doc(invoice_id, "error", last_error="; ".join(schema_errors[:3]))
        return redirect(next_url)

    path = ksef_xml_path(invoice_id, inv.get("invoice_no") or f"FV_{invoice_id}")
    with open(path, "w", encoding="utf-8") as f:
        f.write(xml)

    if send_invoice_to_ksef is None:
        upsert_ksef_doc(invoice_id, "error", xml_path=path, last_error="Brak modułu ksef_api.py albo zależności requests/cryptography.")
        return redirect(next_url)

    result = send_invoice_to_ksef(xml)
    if result.get("ok"):
        ksef_number = result.get("ksef_number") or (f"ref: {result.get('invoice_reference_number')}" if result.get("invoice_reference_number") else "")
        upsert_ksef_doc(invoice_id, "sent", xml_path=path, ksef_number=ksef_number)
        try:
            regenerate_invoice_pdf_after_ksef_send(invoice_id, ksef_number)
        except Exception as exc:
            upsert_ksef_doc(invoice_id, "sent", xml_path=path, ksef_number=ksef_number, last_error=f"Wysłano do KSeF, ale nie udało się odświeżyć PDF: {exc}")
    else:
        upsert_ksef_doc(invoice_id, "error", xml_path=path, last_error=result.get("message") or "Nie udało się wysłać faktury do KSeF.")
    return redirect(next_url)


@app.get("/invoices/<int:invoice_id>/ksef/xml")
def invoice_ksef_xml(invoice_id):
    inv, company, items, problems = build_invoice_ksef_payload(invoice_id)
    if not inv:
        return "Nie znaleziono faktury", 404
    if problems:
        upsert_ksef_doc(invoice_id, "error", last_error="; ".join(problems[:5]))
        return "Nie można wygenerować XML KSeF:\n- " + "\n- ".join(problems), 400

    xml = build_ksef_draft_xml(inv, company, items)
    schema = ksef_schema_path()
    schema_errors = validate_fa3_xml(xml, schema) if os.path.exists(schema) else []
    if schema_errors:
        upsert_ksef_doc(invoice_id, "error", last_error="; ".join(schema_errors[:3]))
        return "XML nie przeszedł walidacji FA(3):\n- " + "\n- ".join(schema_errors), 400

    path = ksef_xml_path(invoice_id, inv.get("invoice_no") or f"FV_{invoice_id}")
    with open(path, "w", encoding="utf-8") as f:
        f.write(xml)
    upsert_ksef_doc(invoice_id, "ready", xml_path=path)

    return send_file(path, mimetype="application/xml", as_attachment=True, download_name=xml_filename(inv.get("invoice_no") or f"FV_{invoice_id}"))


@app.get("/invoices/<int:invoice_id>/download")
def invoice_download_admin(invoice_id):
    row = load_invoice_with_meta(invoice_id)
    if not row:
        return "Nie znaleziono faktury", 404

    if parse_supabase_storage_ref(row.get("pdf_path", "")):
        try:
            data, filename = supabase_storage_download_bytes(row.get("pdf_path", ""))
            return send_file(io.BytesIO(data), mimetype="application/pdf", as_attachment=True, download_name=filename)
        except Exception:
            pass

    ok_pdf, abs_path = invoice_pdf_exists(row.get("pdf_path", ""), row.get("invoice_no", ""))
    if not ok_pdf:
        c = conn()
        cur = c.cursor()
        cur.execute("SELECT * FROM orders WHERE id=?", (row["order_id"],))
        o = cur.fetchone()
        c.close()
        if not o:
            return "Brak powiązanego zamówienia", 404

        items = invoice_items_from_saved_json(invoice_id)
        if not items:
            return "Brak pozycji faktury", 400

        meta = invoice_meta_payload(row)
        abs_path, total_net, total_gross = generate_order_invoice_pdf(o, items, meta)
        packing_pdf_path = generate_invoice_packing_list_pdf(o, items, meta, abs_path)
        stored_pdf_path = upload_invoice_pdfs_to_supabase(invoice_id, row.get("invoice_no") or f"FV_{invoice_id}", abs_path, packing_pdf_path)

        c = conn()
        cur = c.cursor()
        cur.execute("UPDATE invoices SET total_net=?, total_gross=? WHERE id=?", (total_net, total_gross, invoice_id))
        c.commit()
        c.close()

        current_meta = load_invoice_meta(invoice_id) or {}
        upsert_invoice_meta(
            invoice_id,
            stored_pdf_path,
            current_meta.get("invoice_items_json") or json.dumps(items, ensure_ascii=False),
            sent_to_client=int(current_meta.get("sent_to_client") or 0),
            seen_by_client=int(current_meta.get("seen_by_client") or 0),
            seen_at=current_meta.get("seen_at")
        )

        if supabase_enabled():
            try:
                sync_local_rows_to_supabase("invoices", "id", [invoice_id])
            except Exception:
                pass
            try:
                sync_invoice_meta_to_supabase(invoice_id)
            except Exception:
                pass
        if parse_supabase_storage_ref(stored_pdf_path):
            data, filename = supabase_storage_download_bytes(stored_pdf_path)
            return send_file(io.BytesIO(data), mimetype="application/pdf", as_attachment=True, download_name=filename)

    if supabase_enabled() and abs_path and os.path.exists(abs_path) and not parse_supabase_storage_ref(row.get("pdf_path", "")):
        try:
            items = invoice_items_from_saved_json(invoice_id)
            packing_pdf_path = ""
            if items:
                pack_candidate = packing_list_pdf_path_for_invoice(abs_path, row.get("invoice_no") or f"FV_{invoice_id}")
                if os.path.exists(pack_candidate):
                    packing_pdf_path = pack_candidate
            stored_pdf_path = upload_invoice_pdfs_to_supabase(invoice_id, row.get("invoice_no") or f"FV_{invoice_id}", abs_path, packing_pdf_path)
            current_meta = load_invoice_meta(invoice_id) or {}
            upsert_invoice_meta(
                invoice_id,
                stored_pdf_path,
                current_meta.get("invoice_items_json") or (json.dumps(items, ensure_ascii=False) if items else ""),
                sent_to_client=int(current_meta.get("sent_to_client") or 0),
                seen_by_client=int(current_meta.get("seen_by_client") or 0),
                seen_at=current_meta.get("seen_at")
            )
            sync_invoice_meta_to_supabase(invoice_id)
            data, filename = supabase_storage_download_bytes(stored_pdf_path)
            return send_file(io.BytesIO(data), mimetype="application/pdf", as_attachment=True, download_name=filename)
        except Exception:
            pass
    return send_file(abs_path, mimetype="application/pdf", as_attachment=True, download_name=os.path.basename(abs_path))


@app.get("/orders/<int:order_id>/packing-list")
def order_packing_list_download_admin(order_id):
    """Generuje wspolna liste pakowania dla zamowien tego samego klienta."""
    maybe_pull_shared_from_supabase()
    c = conn()
    cur = c.cursor()
    cur.execute("SELECT * FROM orders WHERE id=?", (order_id,))
    order_row = cur.fetchone()
    if not order_row:
        c.close()
        return "Nie znaleziono zamowienia", 404
    candidate_orders = [dict(order_row)]
    recipient = _email_key(order_row["customer_email"])
    if recipient:
        # Jedno klikniecie „Pakuj” obejmuje pozostale potwierdzone zamowienia
        # tego samego klienta. Dzieki temu powstaje jeden dokument i jeden
        # zbiorczy e-mail, zamiast osobnej wiadomosci dla kazdego zamowienia.
        cur.execute("""
          SELECT *
          FROM orders
          WHERE id<>?
            AND LOWER(TRIM(COALESCE(customer_email,'')))=?
            AND LOWER(COALESCE(status,''))='confirmed'
          ORDER BY created_at, id
        """, (order_id, recipient))
        candidate_orders.extend(dict(row) for row in cur.fetchall())

    candidate_by_id = {int(order["id"]): order for order in candidate_orders}
    candidate_ids = sorted(candidate_by_id)
    placeholders = ",".join(["?"] * len(candidate_ids))
    cur.execute(f"""
      SELECT oi.id, oi.order_id, oi.product_id, oi.qty, p.sku, p.model, p.name,
             COALESCE(s.qty,0) AS stock_qty
      FROM order_items oi
      JOIN products p ON p.id=oi.product_id
      LEFT JOIN stock s ON s.product_id=oi.product_id
      WHERE oi.order_id IN ({placeholders})
      ORDER BY oi.order_id, oi.id
    """, tuple(candidate_ids))
    order_items = [dict(row) for row in cur.fetchall()]
    c.close()
    if not order_items:
        return "Brak pozycji zamowienia", 400

    # Lista robocza ma zawierac wyłącznie to, co można teraz fizycznie
    # spakować z magazynu. Wspólna pula dla product_id zapobiega pokazaniu
    # tego samego stanu kilka razy, gdy produkt występuje w kilku pozycjach.
    stock_pool = {}
    items = []
    for item in order_items:
        product_id = int(item.get("product_id") or 0)
        available = stock_pool.setdefault(product_id, max(0, int(item.get("stock_qty") or 0)))
        pack_qty = min(max(0, int(item.get("qty") or 0)), available)
        stock_pool[product_id] = available - pack_qty
        if pack_qty <= 0:
            continue
        item["qty"] = pack_qty
        source_order = candidate_by_id.get(int(item.get("order_id") or 0), {})
        item["source_order_id"] = int(item.get("order_id") or 0)
        item["source_order_no"] = canonical_order_no(
            source_order.get("id"), source_order.get("created_at"), source_order.get("order_no")
        )
        item["source_order_note"] = norm(source_order.get("note"))
        items.append(item)

    if not items:
        return "Brak pozycji dostępnych obecnie na magazynie do spakowania", 400

    packed_order_ids = sorted({int(item["source_order_id"]) for item in items})
    order_no = canonical_order_no(order_row["id"], order_row["created_at"], order_row["order_no"])
    meta = {
        "invoice_no": order_no,
        "document_label_key": "order",
        "buyer_name": norm(order_row["customer_name"]),
        "buyer_email": norm(order_row["customer_email"]),
    }
    pack_path = generate_invoice_packing_list_pdf(order_row, items, meta)
    mark_orders_packed(packed_order_ids, packing_path=pack_path, packing_items=items)
    filename_suffix = "_zbiorcza" if len(packed_order_ids) > 1 else ""
    return send_file(
        pack_path,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"{safe_filename(order_no)}{filename_suffix}_lista_pakowania.pdf",
    )


@app.get("/invoices/<int:invoice_id>/packing-list")
def invoice_packing_list_download_admin(invoice_id):
    inv = load_invoice_with_meta(invoice_id)
    if not inv:
        return "Nie znaleziono faktury", 404

    c = conn()
    cur = c.cursor()
    cur.execute("SELECT * FROM orders WHERE id=?", (inv["order_id"],))
    o = cur.fetchone()
    c.close()
    if not o:
        return "Brak powiązanego zamówienia", 404

    items = invoice_items_from_saved_json(invoice_id)
    if not items:
        return "Brak pozycji faktury", 400

    ok_pdf, invoice_abs_path = invoice_pdf_exists(inv.get("pdf_path", ""), inv.get("invoice_no", ""))
    pack_path = packing_list_pdf_path_for_invoice(invoice_abs_path if ok_pdf else "", inv.get("invoice_no") or f"FV_{invoice_id}")
    pack_path = generate_invoice_packing_list_pdf(o, items, invoice_meta_payload(inv), invoice_abs_path if ok_pdf else "")
    mark_orders_packed([
        int(item.get("source_order_id") or item.get("order_id") or inv.get("order_id") or 0)
        for item in items
    ], packing_path=pack_path, packing_items=items)
    if supabase_enabled():
        try:
            packing_ref = supabase_storage_upload_file(
                pack_path,
                invoice_packing_storage_object_path(invoice_id, inv.get("invoice_no") or f"FV_{invoice_id}"),
                content_type="application/pdf",
            )
            data, filename = supabase_storage_download_bytes(packing_ref)
            return send_file(io.BytesIO(data), mimetype="application/pdf", as_attachment=True, download_name=filename)
        except Exception:
            pass

    return send_file(pack_path, mimetype="application/pdf", as_attachment=True, download_name=os.path.basename(pack_path))

@app.post("/invoices/<int:invoice_id>/regenerate")
def invoice_regenerate_admin(invoice_id):
    inv = load_invoice_with_meta(invoice_id)
    if not inv:
        return "Nie znaleziono faktury", 404

    c = conn()
    cur = c.cursor()
    cur.execute("SELECT * FROM orders WHERE id=?", (inv["order_id"],))
    o = cur.fetchone()
    c.close()
    if not o:
        return "Brak powiÄ…zanego zamĂłwienia", 404

    items = invoice_items_from_saved_json(invoice_id)
    if not items:
        return "Brak pozycji faktury", 400

    meta = invoice_meta_payload(inv)
    pdf_path, total_net, total_gross = generate_order_invoice_pdf(o, items, meta)
    packing_pdf_path = generate_invoice_packing_list_pdf(o, items, meta, pdf_path)
    stored_pdf_path = upload_invoice_pdfs_to_supabase(invoice_id, inv["invoice_no"], pdf_path, packing_pdf_path)

    c = conn()
    cur = c.cursor()
    cur.execute("UPDATE invoices SET total_net=?, total_gross=? WHERE id=?", (total_net, total_gross, invoice_id))
    c.commit()
    c.close()

    current_meta = load_invoice_meta(invoice_id) or {}
    upsert_invoice_meta(
        invoice_id,
        stored_pdf_path,
        current_meta.get("invoice_items_json") or json.dumps(items, ensure_ascii=False),
        sent_to_client=int(current_meta.get("sent_to_client") or 0),
        seen_by_client=int(current_meta.get("seen_by_client") or 0),
        seen_at=current_meta.get("seen_at")
    )

    if supabase_enabled():
        try:
            sync_local_rows_to_supabase("invoices", "id", [invoice_id])
        except Exception:
            pass
        try:
            sync_invoice_meta_to_supabase(invoice_id)
        except Exception:
            pass

    return redirect(request.referrer or url_for("orders"))


def _redirect_after_invoice_action(default_endpoint="invoices"):
    target = norm(request.values.get("next")) or request.referrer or url_for(default_endpoint)
    return redirect(target)


def _invoice_json_order_ids(raw_items_json: str) -> set[int]:
    """Odczytuje powiązania wielu zamówień ze starszego zapisu faktury."""
    try:
        items = json.loads(raw_items_json or "[]")
    except Exception:
        return set()
    if not isinstance(items, list):
        return set()
    order_ids = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            order_id = int(item.get("source_order_id") or item.get("order_id") or 0)
        except (TypeError, ValueError):
            order_id = 0
        if order_id > 0:
            order_ids.add(order_id)
    return order_ids


def _invoice_source_order_ids(cur, invoice_id: int) -> list[int]:
    """Zwraca wszystkie zamówienia rozliczane daną fakturą."""
    cur.execute("""
      SELECT DISTINCT order_id
      FROM invoice_allocations
      WHERE invoice_id=? AND order_id IS NOT NULL
    """, (invoice_id,))
    order_ids = {int(row["order_id"]) for row in cur.fetchall() if row["order_id"]}

    # Starsze faktury mogły powstać przed tabelą invoice_allocations.
    cur.execute("SELECT order_id FROM invoices WHERE id=?", (invoice_id,))
    invoice_row = cur.fetchone()
    if invoice_row and invoice_row["order_id"]:
        order_ids.add(int(invoice_row["order_id"]))

    # Stare faktury zbiorcze przechowywały dodatkowe zamówienia wyłącznie
    # w JSON-ie pozycji; invoices.order_id wskazywało tylko pierwsze z nich.
    cur.execute("SELECT invoice_items_json FROM invoice_meta WHERE invoice_id=?", (invoice_id,))
    meta_row = cur.fetchone()
    if meta_row:
        order_ids.update(_invoice_json_order_ids(meta_row["invoice_items_json"]))
    return sorted(order_ids)


def _order_invoice_ids(cur, order_id: int) -> list[int]:
    """Zwraca faktury rozliczające zamówienie, także rekordy historyczne."""
    cur.execute("""
      SELECT DISTINCT invoice_id
      FROM invoice_allocations
      WHERE order_id=? AND invoice_id IS NOT NULL
    """, (order_id,))
    invoice_ids = {int(row["invoice_id"]) for row in cur.fetchall() if row["invoice_id"]}
    cur.execute("SELECT id FROM invoices WHERE order_id=?", (order_id,))
    invoice_ids.update(int(row["id"]) for row in cur.fetchall())
    cur.execute("SELECT invoice_id, invoice_items_json FROM invoice_meta WHERE TRIM(COALESCE(invoice_items_json,''))<>''")
    for row in cur.fetchall():
        if order_id in _invoice_json_order_ids(row["invoice_items_json"]):
            invoice_ids.add(int(row["invoice_id"]))
    return sorted(invoice_ids)


def _all_order_invoices_paid(cur, order_id: int) -> bool:
    invoice_ids = _order_invoice_ids(cur, order_id)
    if not invoice_ids:
        return False
    placeholders = ",".join(["?"] * len(invoice_ids))
    cur.execute(f"""
      SELECT i.id AS invoice_id, m.invoice_id AS meta_invoice_id,
             COALESCE(m.sent_to_client,0) AS sent_to_client,
             COALESCE(m.paid,0) AS paid
      FROM invoices i
      LEFT JOIN invoice_meta m ON m.invoice_id=i.id
      WHERE i.id IN ({placeholders})
    """, tuple(invoice_ids))
    rows = [dict(row) for row in cur.fetchall()]
    # Niewysłany szkic starej faktury nie jest należnością klienta i nie może
    # blokować zamknięcia zamówienia. Rekordy bez invoice_meta są historycznie
    # widoczne, więc nadal wymagają jawnego oznaczenia płatności.
    payable = [row for row in rows if row["meta_invoice_id"] is None or int(row["sent_to_client"] or 0) == 1 or int(row["paid"] or 0) == 1]
    return bool(payable) and all(int(row["paid"] or 0) == 1 for row in payable)


def _order_fully_invoiced_for_payment(cur, order_id: int) -> bool:
    """Obsługuje również pełne faktury sprzed tabeli invoice_allocations.

    Jeżeli zamówienie ma choć jedną alokację, obowiązuje dokładne sprawdzenie
    ilości pozycji. Brak alokacji jest uznawany za historyczną pełną fakturę
    tylko wtedy, gdy faktura wskazuje zamówienie bezpośrednio przez order_id.
    """
    cur.execute("SELECT 1 FROM invoice_allocations WHERE order_id=? LIMIT 1", (order_id,))
    if cur.fetchone():
        return order_fully_invoiced(cur, order_id)
    cur.execute("SELECT 1 FROM invoices WHERE order_id=? LIMIT 1", (order_id,))
    if cur.fetchone() is not None:
        return True
    cur.execute("SELECT invoice_items_json FROM invoice_meta WHERE TRIM(COALESCE(invoice_items_json,''))<>''")
    return any(order_id in _invoice_json_order_ids(row["invoice_items_json"]) for row in cur.fetchall())


def reconcile_paid_order_statuses():
    """Uzupełnia status completed dla wcześniej opłaconych zamówień."""
    c = conn()
    cur = c.cursor()
    cur.execute("SELECT invoice_id FROM invoice_meta WHERE COALESCE(paid,0)=1")
    paid_invoice_ids = [int(row["invoice_id"]) for row in cur.fetchall()]
    order_ids = sorted({
        order_id
        for invoice_id in paid_invoice_ids
        for order_id in _invoice_source_order_ids(cur, invoice_id)
    })
    changed_order_ids = []
    for order_id in order_ids:
        cur.execute("SELECT status, warehouse_issued FROM orders WHERE id=?", (order_id,))
        order_row = cur.fetchone()
        if not order_row:
            continue
        current_status = norm(order_row["status"]).lower()
        if current_status in {"completed", "cancelled"}:
            continue
        historically_fulfilled = int(order_row["warehouse_issued"] or 0) == 1
        fully_invoiced = _order_fully_invoiced_for_payment(cur, order_id)
        if (historically_fulfilled or fully_invoiced) and _all_order_invoices_paid(cur, order_id):
            cur.execute("UPDATE orders SET status='completed' WHERE id=?", (order_id,))
            changed_order_ids.append(order_id)
    c.commit()
    c.close()

    if changed_order_ids and supabase_enabled():
        try:
            sync_local_rows_to_supabase("orders", "id", changed_order_ids)
        except Exception:
            app.logger.exception("Nie udało się zsynchronizować statusów opłaconych zamówień")
    return changed_order_ids


def reconcile_legacy_orders_by_age(days: int = 14):
    """Twardo zamyka nieanulowane zamówienia sprzed co najmniej ``days`` dni."""
    cutoff = (app_now() - timedelta(days=max(1, int(days)))).strftime("%Y-%m-%d")
    c = conn()
    cur = c.cursor()
    cur.execute("""
      SELECT id
      FROM orders
      WHERE TRIM(COALESCE(created_at,''))<>''
        AND SUBSTR(TRIM(created_at),1,10) <= ?
        AND LOWER(COALESCE(status,'')) NOT IN ('completed','cancelled')
    """, (cutoff,))
    changed_order_ids = [int(row["id"]) for row in cur.fetchall()]
    if changed_order_ids:
        placeholders = ",".join(["?"] * len(changed_order_ids))
        cur.execute(
            f"UPDATE orders SET status='completed' WHERE id IN ({placeholders})",
            tuple(changed_order_ids),
        )
    c.commit()
    c.close()

    if changed_order_ids and supabase_enabled():
        try:
            for offset in range(0, len(changed_order_ids), 250):
                sync_local_rows_to_supabase("orders", "id", changed_order_ids[offset:offset + 250])
        except Exception:
            app.logger.exception("Nie udało się zsynchronizować zamówień zamkniętych regułą 14 dni")
    return changed_order_ids


def _set_invoice_payment_state(invoice_id: int, *, reminder: int | None = None, paid: int | None = None):
    meta = load_invoice_meta(invoice_id) or {}
    pdf_path = meta.get("pdf_path", "")
    items_json = meta.get("invoice_items_json", "")
    sent_to_client = int(meta.get("sent_to_client") or 0)
    seen_by_client = int(meta.get("seen_by_client") or 0)
    seen_at = meta.get("seen_at")
    current_reminder = int(meta.get("payment_reminder") or 0)
    current_paid = int(meta.get("paid") or 0)
    current_paid_at = meta.get("paid_at")

    next_paid = current_paid if paid is None else int(paid)
    next_reminder = current_reminder if reminder is None else int(reminder)
    next_paid_at = current_paid_at
    if next_paid:
        next_reminder = 0
        state_changed_at = now_iso()
        next_paid_at = state_changed_at
        # Opłacona faktura nie powinna nadal oczekiwać na potwierdzenie
        # klienta. Zachowujemy jedną prawdę w invoice_meta/Supabase.
        if not seen_by_client:
            seen_by_client = 1
            seen_at = state_changed_at
    elif paid == 0:
        next_paid_at = None

    upsert_invoice_meta(
        invoice_id,
        pdf_path,
        items_json,
        sent_to_client=sent_to_client,
        seen_by_client=seen_by_client,
        seen_at=seen_at,
        payment_reminder=next_reminder,
        paid=next_paid,
        paid_at=next_paid_at
    )

    changed_order_ids = []
    c = conn()
    cur = c.cursor()
    for order_id in _invoice_source_order_ids(cur, invoice_id):
        cur.execute("SELECT status, tracking_no, packed_at FROM orders WHERE id=?", (order_id,))
        order_row = cur.fetchone()
        if not order_row:
            continue

        current_status = norm(order_row["status"]).lower()
        fully_invoiced = _order_fully_invoiced_for_payment(cur, order_id)

        # Zakończenie następuje dopiero po pełnym zafakturowaniu zamówienia
        # i opłaceniu wszystkich faktur, które je rozliczają.
        if next_paid and fully_invoiced and _all_order_invoices_paid(cur, order_id):
            if current_status not in {"completed", "cancelled"}:
                cur.execute("UPDATE orders SET status='completed' WHERE id=?", (order_id,))
                changed_order_ids.append(order_id)
        elif paid == 0 and current_status in {"completed", "issued"}:
            if norm(order_row["tracking_no"]):
                next_status = "shipped" if fully_invoiced else "partially_shipped"
            elif fully_invoiced:
                next_status = "packed"
            else:
                next_status = "packed_partial" if order_row["packed_at"] else "confirmed"
            cur.execute("UPDATE orders SET status=? WHERE id=?", (next_status, order_id))
            changed_order_ids.append(order_id)

    c.commit()
    c.close()

    if supabase_enabled():
        try:
            sync_invoice_meta_to_supabase(invoice_id)
        except Exception:
            pass
        if changed_order_ids:
            try:
                sync_local_rows_to_supabase("orders", "id", changed_order_ids)
            except Exception:
                pass


@app.post("/invoices/<int:invoice_id>/payment-reminder")
def invoice_payment_reminder_admin(invoice_id):
    _set_invoice_payment_state(invoice_id, reminder=1, paid=0)
    try:
        if send_payment_reminder:
            invoice_row, pdf_url = _invoice_email_context(invoice_id)
            send_payment_reminder(invoice_row, pdf_url=pdf_url)
    except Exception:
        pass
    return _redirect_after_invoice_action()


@app.post("/invoices/<int:invoice_id>/paid")
def invoice_paid_admin(invoice_id):
    _set_invoice_payment_state(invoice_id, reminder=0, paid=1)
    return _redirect_after_invoice_action()


@app.post("/invoices/<int:invoice_id>/unpaid")
def invoice_unpaid_admin(invoice_id):
    _set_invoice_payment_state(invoice_id, reminder=0, paid=0)
    return _redirect_after_invoice_action()

@app.post("/api/invoices/<int:invoice_id>/seen")
def api_invoice_seen(invoice_id):
    email = _email_key(g.client_user["email"])
    c = conn()
    cur = c.cursor()
    cur.execute("""
      SELECT
        i.id,
        m.invoice_id AS meta_invoice_id,
        i.buyer_email,
        o.customer_email AS order_customer_email,
        COALESCE(m.pdf_path,'') AS pdf_path,
        COALESCE(m.sent_to_client,0) AS sent_to_client,
        i.invoice_no
      FROM invoices i
      LEFT JOIN invoice_meta m ON m.invoice_id = i.id
      LEFT JOIN orders o ON o.id = i.order_id
      WHERE i.id=?
      LIMIT 1
    """, (invoice_id,))
    row = cur.fetchone()
    c.close()
    if not row:
        return jsonify(ok=False, error="Nie znaleziono faktury"), 404

    if email:
        buyer_ok = _email_key(row["buyer_email"]) == email
        order_ok = _email_key(row["order_customer_email"]) == email
        has_meta = row["meta_invoice_id"] is not None
        if (has_meta and int(row["sent_to_client"] or 0) != 1) or not (buyer_ok or order_ok):
            return jsonify(ok=False, error="Brak dostÄ™pu"), 403

    meta = load_invoice_meta(invoice_id) or {}
    ts = now_iso()
    upsert_invoice_meta(
        invoice_id,
        meta.get("pdf_path",""),
        meta.get("invoice_items_json",""),
        sent_to_client=int(meta.get("sent_to_client") or 0),
        seen_by_client=1,
        seen_at=ts
    )

    if supabase_enabled():
        try:
            sync_invoice_meta_to_supabase(invoice_id)
        except Exception:
            pass

    return jsonify(ok=True, seen_at=ts)

@app.get("/api/invoices/<int:invoice_id>/download")
def api_invoice_download(invoice_id):
    maybe_pull_shared_from_supabase()
    email = _email_key(g.client_user["email"])
    c = conn()
    cur = c.cursor()
    cur.execute("""
      SELECT
        i.*,
        m.invoice_id AS meta_invoice_id,
        COALESCE(m.pdf_path,'') AS pdf_path,
        COALESCE(m.sent_to_client,0) AS sent_to_client,
        o.customer_email AS order_customer_email
      FROM invoices i
      LEFT JOIN invoice_meta m ON m.invoice_id = i.id
      LEFT JOIN orders o ON o.id = i.order_id
      WHERE i.id=?
      LIMIT 1
    """, (invoice_id,))
    row = cur.fetchone()
    c.close()
    if not row:
        return "Nie znaleziono faktury", 404

    if email:
        buyer_ok = _email_key(row["buyer_email"]) == email
        order_ok = _email_key(row["order_customer_email"]) == email
        has_meta = row["meta_invoice_id"] is not None
        if (has_meta and int(row["sent_to_client"] or 0) != 1) or not (buyer_ok or order_ok):
            return "Brak dostÄ™pu", 403

    def mark_downloaded_by_client():
        if not email:
            return
        meta = load_invoice_meta(invoice_id) or {}
        upsert_invoice_meta(
            invoice_id,
            meta.get("pdf_path", ""),
            meta.get("invoice_items_json", ""),
            sent_to_client=int(meta.get("sent_to_client") or 0),
            seen_by_client=1,
            seen_at=now_iso(),
            payment_reminder=int(meta.get("payment_reminder") or 0),
            paid=int(meta.get("paid") or 0),
            paid_at=meta.get("paid_at")
        )
        if supabase_enabled():
            try:
                sync_invoice_meta_to_supabase(invoice_id)
            except Exception:
                pass

    if parse_supabase_storage_ref(row["pdf_path"]):
        try:
            data, filename = supabase_storage_download_bytes(row["pdf_path"])
            mark_downloaded_by_client()
            return send_file(io.BytesIO(data), mimetype="application/pdf", as_attachment=True, download_name=filename)
        except Exception:
            pass

    ok_pdf, abs_path = invoice_pdf_exists(row["pdf_path"], row["invoice_no"])
    if not ok_pdf:
        cur_order = None
        c = conn()
        cur = c.cursor()
        cur.execute("SELECT * FROM orders WHERE id=?", (row["order_id"],))
        cur_order = cur.fetchone()
        c.close()
        if not cur_order:
            return "Brak powiązanego zamówienia", 404
        items = invoice_items_from_saved_json(invoice_id)
        if not items:
            return "Brak pozycji faktury", 400
        meta = invoice_meta_payload(dict(row))
        abs_path, total_net, total_gross = generate_order_invoice_pdf(cur_order, items, meta)
        packing_pdf_path = generate_invoice_packing_list_pdf(cur_order, items, meta, abs_path)
        stored_pdf_path = upload_invoice_pdfs_to_supabase(invoice_id, row["invoice_no"], abs_path, packing_pdf_path)
        current_meta = load_invoice_meta(invoice_id) or {}
        upsert_invoice_meta(
            invoice_id,
            stored_pdf_path,
            current_meta.get("invoice_items_json") or json.dumps(items, ensure_ascii=False),
            sent_to_client=int(current_meta.get("sent_to_client") or 0),
            seen_by_client=int(current_meta.get("seen_by_client") or 0),
            seen_at=current_meta.get("seen_at")
        )
        if supabase_enabled():
            try:
                sync_invoice_meta_to_supabase(invoice_id)
            except Exception:
                pass
        if parse_supabase_storage_ref(stored_pdf_path):
            data, filename = supabase_storage_download_bytes(stored_pdf_path)
            mark_downloaded_by_client()
            return send_file(io.BytesIO(data), mimetype="application/pdf", as_attachment=True, download_name=filename)

    if supabase_enabled() and abs_path and os.path.exists(abs_path) and not parse_supabase_storage_ref(row["pdf_path"]):
        try:
            items = invoice_items_from_saved_json(invoice_id)
            packing_pdf_path = ""
            if items:
                pack_candidate = packing_list_pdf_path_for_invoice(abs_path, row["invoice_no"])
                if os.path.exists(pack_candidate):
                    packing_pdf_path = pack_candidate
            stored_pdf_path = upload_invoice_pdfs_to_supabase(invoice_id, row["invoice_no"], abs_path, packing_pdf_path)
            current_meta = load_invoice_meta(invoice_id) or {}
            upsert_invoice_meta(
                invoice_id,
                stored_pdf_path,
                current_meta.get("invoice_items_json") or (json.dumps(items, ensure_ascii=False) if items else ""),
                sent_to_client=int(current_meta.get("sent_to_client") or 0),
                seen_by_client=int(current_meta.get("seen_by_client") or 0),
                seen_at=current_meta.get("seen_at")
            )
            sync_invoice_meta_to_supabase(invoice_id)
            data, filename = supabase_storage_download_bytes(stored_pdf_path)
            mark_downloaded_by_client()
            return send_file(io.BytesIO(data), mimetype="application/pdf", as_attachment=True, download_name=filename)
        except Exception:
            pass

    try:
        mark_downloaded_by_client()
        return send_file(abs_path, mimetype="application/pdf", as_attachment=True, download_name=os.path.basename(abs_path))
    except Exception as e:
        return f"BĹ‚Ä…d pobierania PDF: {e}", 500

def _delete_invoice_everywhere(invoice_id: int):
    inv = load_invoice_with_meta(invoice_id)
    if not inv:
        abort(404)

    c = conn()
    cur = c.cursor()
    cur.execute("SELECT DISTINCT order_id FROM invoice_allocations WHERE invoice_id=?", (invoice_id,))
    touched_order_ids = [int(r["order_id"]) for r in cur.fetchall()]
    meta_items_raw = inv.get("invoice_items_json") or ""
    if meta_items_raw:
        try:
            meta_items = json.loads(meta_items_raw)
            if isinstance(meta_items, list):
                for it in meta_items:
                    oid = int(it.get("source_order_id") or it.get("order_id") or 0)
                    if oid and oid not in touched_order_ids:
                        touched_order_ids.append(oid)
        except Exception:
            pass
    if int(inv.get("order_id") or 0) and int(inv.get("order_id") or 0) not in touched_order_ids:
        touched_order_ids.append(int(inv.get("order_id") or 0))
    c.close()

    ok_pdf, abs_path = invoice_pdf_exists(inv.get("pdf_path", ""), inv.get("invoice_no", ""))
    try:
        pack_path = packing_list_pdf_path_for_invoice(abs_path if ok_pdf else "", inv.get("invoice_no", ""))
        if pack_path and os.path.exists(pack_path):
            os.remove(pack_path)
        if ok_pdf and abs_path and os.path.exists(abs_path):
            os.remove(abs_path)
    except Exception:
        pass

    c = conn()
    cur = c.cursor()
    cur.execute("DELETE FROM invoice_allocations WHERE invoice_id=?", (invoice_id,))
    cur.execute("DELETE FROM invoice_meta WHERE invoice_id=?", (invoice_id,))
    cur.execute("DELETE FROM ksef_documents WHERE invoice_id=?", (invoice_id,))
    cur.execute("DELETE FROM invoices WHERE id=?", (invoice_id,))
    c.commit()
    c.close()

    changed_order_ids, changed_product_ids = reconcile_orders_after_invoice_change(touched_order_ids)

    if supabase_enabled():
        try:
            supabase_delete_rows("invoice_allocations", {"invoice_id": invoice_id})
        except Exception:
            pass
        try:
            supabase_delete_rows("invoice_meta", {"invoice_id": invoice_id})
        except Exception:
            pass
        try:
            supabase_delete_rows("ksef_documents", {"invoice_id": invoice_id})
        except Exception:
            pass
        try:
            supabase_delete_rows("invoices", {"id": invoice_id})
        except Exception:
            pass
        if changed_order_ids:
            try:
                sync_local_rows_to_supabase("orders", "id", changed_order_ids)
            except Exception:
                pass
        if changed_product_ids:
            try:
                sync_local_rows_to_supabase("stock", "product_id", changed_product_ids)
            except Exception:
                pass

    return inv


@app.post("/invoices/<int:invoice_id>/delete")
def invoice_delete_admin(invoice_id):
    _delete_invoice_everywhere(invoice_id)
    return _redirect_after_invoice_action()


@app.post("/invoices/<int:invoice_id>/rollback")
def invoice_rollback_admin(invoice_id):
    _delete_invoice_everywhere(invoice_id)
    return _redirect_after_invoice_action()


@app.post("/orders/<int:order_id>/invoice/<int:invoice_id>/delete")
def order_invoice_delete(order_id, invoice_id):
    inv = load_invoice_with_meta(invoice_id)
    if not inv or int(inv.get("order_id") or 0) != int(order_id):
        abort(404)
    _delete_invoice_everywhere(invoice_id)

    return redirect(url_for("order_invoice", order_id=order_id, deleted="1"))


def _invoice_email_context(invoice_id: int):
    c = conn()
    cur = c.cursor()
    cur.execute("""
      SELECT
        i.*,
        o.order_no AS order_no,
        o.customer_email AS customer_email,
        o.customer_name AS customer_name,
        m.pdf_path AS pdf_path,
        m.payment_reminder AS payment_reminder,
        m.paid AS paid,
        m.seen_by_client AS seen_by_client,
        m.seen_at AS seen_at
      FROM invoices i
      LEFT JOIN orders o ON o.id = i.order_id
      LEFT JOIN invoice_meta m ON m.invoice_id = i.id
      WHERE i.id=?
      LIMIT 1
    """, (invoice_id,))
    row = cur.fetchone()
    c.close()
    if not row:
        abort(404)

    invoice = dict(row)
    # Wiadomość otwiera panel. Sam PDF pozostaje chroniony tokenem klienta.
    panel_url = (
        f"{CLIENT_PANEL_URL}/?"
        + urllib.parse.urlencode({"section": "invoices", "invoice": invoice_id})
    )
    return invoice, panel_url


def send_automatic_payment_reminders(reference_time=None) -> dict:
    """Wysyła jedno przypomnienie dzień po terminie płatności.

    Harmonogram uruchamia tę funkcję o 12:00 czasu Europe/Warsaw. Znacznik
    payment_reminder jest zapisywany dopiero po udanej wysyłce, dzięki czemu
    ponowne uruchomienie jest bezpieczne i nie dubluje wiadomości.
    """
    now = reference_time or app_now()
    due_date = (now.date() - timedelta(days=1)).isoformat()
    c = conn()
    cur = c.cursor()
    cur.execute("""
      SELECT i.id
      FROM invoices i
      LEFT JOIN invoice_meta m ON m.invoice_id=i.id
      WHERE SUBSTR(TRIM(COALESCE(i.payment_to,'')),1,10)=?
        AND COALESCE(m.paid,0)=0
        AND COALESCE(m.payment_reminder,0)=0
      ORDER BY i.id
    """, (due_date,))
    invoice_ids = [int(row["id"]) for row in cur.fetchall()]
    c.close()

    sent_ids = []
    failed = []
    for invoice_id in invoice_ids:
        try:
            if not send_payment_reminder:
                raise RuntimeError("Moduł wysyłki przypomnień nie jest dostępny")
            invoice_row, pdf_url = _invoice_email_context(invoice_id)
            result = send_payment_reminder(invoice_row, pdf_url=pdf_url)
            if not result.get("ok"):
                raise RuntimeError(norm(result.get("error")) or "Wysyłka nie powiodła się")
            _set_invoice_payment_state(invoice_id, reminder=1, paid=None)
            sent_ids.append(invoice_id)
        except Exception as exc:
            failed.append({"invoice_id": invoice_id, "error": str(exc)[:300]})
            app.logger.exception("Automatyczne przypomnienie nie zostało wysłane dla faktury %s", invoice_id)
    return {
        "ok": not failed,
        "due_date": due_date,
        "eligible": len(invoice_ids),
        "sent": len(sent_ids),
        "sent_ids": sent_ids,
        "failed": failed,
    }


def _send_invoice_to_client(invoice_id: int) -> tuple[int, bool, str]:
    c = conn()
    cur = c.cursor()
    cur.execute("""
      SELECT i.id, i.order_id, i.buyer_email, i.invoice_no, o.customer_email
      FROM invoices i
      LEFT JOIN orders o ON o.id = i.order_id
      WHERE i.id=?
      LIMIT 1
    """, (invoice_id,))
    row = cur.fetchone()
    if not row:
        c.close()
        abort(404)

    buyer_email = _email_key(row["buyer_email"])
    order_email = _email_key(row["customer_email"])
    if not buyer_email and order_email:
        cur.execute("UPDATE invoices SET buyer_email=? WHERE id=?", (order_email, invoice_id))
        c.commit()
        if supabase_enabled():
            try:
                supabase_update_rows("invoices", {"buyer_email": order_email}, {"id": invoice_id})
            except Exception:
                pass
    c.close()

    meta = load_invoice_meta(invoice_id) or {}
    pdf_path = norm(meta.get("pdf_path"))
    stored_pdf_path = pdf_path

    if not parse_supabase_storage_ref(stored_pdf_path):
        local_pdf_path = ""
        if pdf_path:
            candidate = pdf_path if os.path.isabs(pdf_path) else invoice_pdf_abspath(pdf_path)
            if os.path.exists(candidate):
                local_pdf_path = candidate

        if not local_pdf_path:
            fallback = find_invoice_pdf_fallback(row["invoice_no"])
            if fallback:
                local_pdf_path = fallback

        items = invoice_items_from_saved_json(invoice_id)
        if not local_pdf_path and items:
            c = conn()
            cur = c.cursor()
            cur.execute("SELECT * FROM orders WHERE id=?", (row["order_id"],))
            order_row = cur.fetchone()
            c.close()
            if order_row:
                meta_payload = invoice_meta_payload(dict(row))
                local_pdf_path, _total_net, _total_gross = generate_order_invoice_pdf(order_row, items, meta_payload)

        if local_pdf_path:
            packing_pdf_path = ""
            if items:
                pack_candidate = packing_list_pdf_path_for_invoice(local_pdf_path, row["invoice_no"])
                if os.path.exists(pack_candidate):
                    packing_pdf_path = pack_candidate
                else:
                    c = conn()
                    cur = c.cursor()
                    cur.execute("SELECT * FROM orders WHERE id=?", (row["order_id"],))
                    order_row = cur.fetchone()
                    c.close()
                    if order_row:
                        packing_pdf_path = generate_invoice_packing_list_pdf(order_row, items, invoice_meta_payload(dict(row)), local_pdf_path)
            stored_pdf_path = upload_invoice_pdfs_to_supabase(invoice_id, row["invoice_no"], local_pdf_path, packing_pdf_path)

    upsert_invoice_meta(invoice_id, stored_pdf_path, meta.get("invoice_items_json",""), sent_to_client=1, seen_by_client=0, seen_at=None)

    if supabase_enabled():
        try:
            sync_invoice_meta_to_supabase(invoice_id)
        except Exception:
            pass

    email_ok = False
    email_error = ""
    try:
        if not send_invoice_available:
            email_error = "Brak modułu wysyłki e-mail."
        else:
            invoice_row, pdf_url = _invoice_email_context(invoice_id)
            result = send_invoice_available(invoice_row, pdf_url=pdf_url) or {}
            email_ok = bool(result.get("ok"))
            if not email_ok:
                email_error = norm(result.get("error")) or "Usługa pocztowa odrzuciła wiadomość."
    except Exception as exc:
        email_error = str(exc) or type(exc).__name__
        app.logger.exception("Nie udało się wysłać e-maila z fakturą %s", invoice_id)

    if not email_ok:
        app.logger.error("Nie wysłano e-maila z fakturą %s: %s", invoice_id, email_error)

    return int(row["order_id"] or 0), email_ok, email_error


@app.post("/invoices/<int:invoice_id>/send")
def invoice_send_admin(invoice_id):
    _order_id, email_ok, email_error = _send_invoice_to_client(invoice_id)
    target = norm(request.values.get("next")) or request.referrer or url_for("invoices")
    separator = "&" if "?" in target else "?"
    if email_ok:
        return redirect(target + separator + "email_sent=1")
    return redirect(target + separator + "email_sent=0&email_error=" + urllib.parse.quote_plus(email_error[:300]))


@app.post("/orders/<int:order_id>/invoice/<int:invoice_id>/send")
def order_invoice_send(order_id, invoice_id):
    _order_id, email_ok, email_error = _send_invoice_to_client(invoice_id)
    if email_ok:
        return redirect(url_for("order_invoice", order_id=order_id, sent="1", invoice_id=invoice_id))
    return redirect(url_for(
        "order_invoice", order_id=order_id, invoice_id=invoice_id,
        email_error=email_error[:300]
    ))


@app.route("/invoices/<int:invoice_id>/edit", methods=["GET", "POST"])
def invoice_edit_admin(invoice_id):
    inv = load_invoice_with_meta(invoice_id)
    if not inv:
        return "Nie znaleziono faktury", 404
    ksef_doc = load_ksef_doc(invoice_id)
    if ksef_doc.get("status") == "sent":
        tpl = r"""
        {% extends "base.html" %}
        {% block content %}
          <div class="card">
            <div class="flex">
              <h1 style="margin:0;">Faktura wysłana do KSeF</h1>
              <a class="btn right" href="{{ url_for('invoices') }}">← Faktury</a>
            </div>
            <div class="hint" style="margin-top:10px;">
              Ta faktura ma już numer KSeF i jej edycja została zablokowana, żeby nie powstała różnica między aplikacją a KSeF.
            </div>
            {% if ksef_doc.ksef_number %}
              <p><b>Numer KSeF:</b> {{ ksef_doc.ksef_number }}</p>
            {% endif %}
            <div class="flex" style="margin-top:12px;">
              <a class="btn" href="{{ url_for('invoice_download_admin', invoice_id=inv.id) }}" target="_blank">Faktura PDF</a>
              <a class="btn" href="{{ url_for('invoice_ksef_xml', invoice_id=inv.id) }}">XML KSeF FA(3)</a>
              <a class="btn" href="{{ url_for('invoices') }}">Wróć do faktur</a>
            </div>
          </div>
        {% endblock %}
        """
        return render_template_string(tpl, title="Faktura wysłana do KSeF", base_url=BASE_URL, db_path=DB_PATH, inv=inv, ksef_doc=ksef_doc)

    c = conn()
    cur = c.cursor()
    cur.execute("SELECT * FROM orders WHERE id=?", (inv["order_id"],))
    order_row = cur.fetchone()
    c.close()

    edit_items = invoice_edit_items(invoice_id, dict(inv))

    msg = ""
    if request.method == "POST":
        data = {k: norm(request.form.get(k)) for k in [
            "invoice_no", "issue_date", "sell_date", "payment_type", "payment_to",
            "buyer_name", "buyer_tax_no", "buyer_address", "buyer_country",
            "buyer_email", "buyer_phone"
        ]}
        invoice_items = prepare_invoice_edit_items(edit_items, request.form)
        existing_invoice_id = invoice_no_exists(data["invoice_no"], invoice_id)
        if not data["invoice_no"]:
            msg = "Numer faktury jest wymagany."
        elif existing_invoice_id:
            msg = f"Faktura o takim numerze już istnieje! Numer: {data['invoice_no']}. Wybierz inny numer faktury."
        elif not invoice_items:
            msg = "Faktura musi zawierać co najmniej jedną pozycję."
        else:
            old_order_ids = sorted({int(x.get("source_order_id") or x.get("order_id") or 0) for x in edit_items if int(x.get("current_invoice_qty") or 0) > 0})
            st, pc, city = split_address(data.get("buyer_address", ""))
            c = conn()
            cur = c.cursor()
            cur.execute("""
              UPDATE invoices
              SET invoice_no=?, issue_date=?, sell_date=?, payment_type=?, payment_to=?,
                  buyer_name=?, buyer_tax_no=?, buyer_street=?, buyer_post_code=?, buyer_city=?,
                  buyer_country=?, buyer_email=?, buyer_phone=?
              WHERE id=?
            """, (
                data["invoice_no"], data["issue_date"], data["sell_date"], data["payment_type"], data["payment_to"],
                data["buyer_name"], data["buyer_tax_no"], st, pc, city,
                data["buyer_country"], data["buyer_email"], data["buyer_phone"], invoice_id
            ))
            c.commit()
            c.close()

            updated = load_invoice_with_meta(invoice_id)
            if invoice_items and updated:
                order_for_pdf = order_row
                if not order_for_pdf:
                    first_order_id = int(invoice_items[0].get("source_order_id") or invoice_items[0].get("order_id") or 0)
                    if first_order_id:
                        c = conn()
                        cur = c.cursor()
                        cur.execute("SELECT * FROM orders WHERE id=?", (first_order_id,))
                        order_for_pdf = cur.fetchone()
                        c.close()

                pdf_path, total_net, total_gross = generate_order_invoice_pdf(order_for_pdf, invoice_items, invoice_meta_payload(updated))
                packing_pdf_path = generate_invoice_packing_list_pdf(order_for_pdf, invoice_items, invoice_meta_payload(updated), pdf_path)
                stored_pdf_path = upload_invoice_pdfs_to_supabase(invoice_id, data["invoice_no"], pdf_path, packing_pdf_path)
                allocation_ids = replace_invoice_allocations(invoice_id, invoice_items)
                new_order_ids = sorted({int(x.get("source_order_id") or x.get("order_id") or 0) for x in invoice_items})
                touched_order_ids = sorted(set(old_order_ids + new_order_ids))
                changed_order_ids, changed_product_ids = reconcile_orders_after_invoice_change(touched_order_ids)

                c = conn()
                cur = c.cursor()
                cur.execute("UPDATE invoices SET total_net=?, total_gross=? WHERE id=?", (total_net, total_gross, invoice_id))
                c.commit()
                c.close()

                meta = load_invoice_meta(invoice_id) or {}
                upsert_invoice_meta(
                    invoice_id,
                    stored_pdf_path,
                    json.dumps(invoice_items, ensure_ascii=False),
                    sent_to_client=int(meta.get("sent_to_client") or 0),
                    seen_by_client=0,
                    seen_at=None,
                    payment_reminder=int(meta.get("payment_reminder") or 0),
                    paid=int(meta.get("paid") or 0),
                    paid_at=meta.get("paid_at")
                )

            if supabase_enabled():
                try:
                    sync_local_rows_to_supabase("invoices", "id", [invoice_id])
                except Exception:
                    pass
                try:
                    sync_invoice_meta_to_supabase(invoice_id)
                except Exception:
                    pass
                try:
                    supabase_delete_rows("invoice_allocations", {"invoice_id": invoice_id})
                except Exception:
                    pass
                if allocation_ids:
                    try:
                        sync_local_rows_to_supabase("invoice_allocations", "id", allocation_ids)
                    except Exception:
                        pass
                if changed_order_ids:
                    try:
                        sync_local_rows_to_supabase("orders", "id", changed_order_ids)
                    except Exception:
                        pass
                if changed_product_ids:
                    try:
                        sync_local_rows_to_supabase("stock", "product_id", changed_product_ids)
                    except Exception:
                        pass

            return redirect(url_for("invoices", edited="1", invoice_id=invoice_id))

    buyer_address = "\n".join([x for x in [
        inv.get("buyer_street") or "",
        " ".join([inv.get("buyer_post_code") or "", inv.get("buyer_city") or ""]).strip()
    ] if x])

    tpl = r"""
    {% extends "base.html" %}
    {% block content %}
      <div class="card">
        <div class="flex">
          <h1 style="margin:0;">Edytuj fakturę {{ inv.invoice_no }}</h1>
          <a class="btn right" href="{{ url_for('invoices') }}">← Faktury</a>
        </div>
        {% if msg %}<div class="hint" style="margin-top:10px;">{{ msg }}</div>{% endif %}
      </div>

      <div class="card">
        <form method="post" class="row">
          <div><label class="muted small">Numer faktury</label><input name="invoice_no" value="{{ inv.invoice_no }}" required></div>
          <div><label class="muted small">Data wystawienia</label><input name="issue_date" type="date" value="{{ inv.issue_date }}"></div>
          <div><label class="muted small">Data sprzedaży</label><input name="sell_date" type="date" value="{{ inv.sell_date }}"></div>
          <div><label class="muted small">Forma płatności</label>
            <select name="payment_type">
              <option value="gotowka" {% if inv.payment_type in ['cash','gotowka'] %}selected{% endif %}>gotówka</option>
              <option value="przelew" {% if inv.payment_type in ['transfer','przelew'] %}selected{% endif %}>przelew</option>
              <option value="karta" {% if inv.payment_type in ['card','karta'] %}selected{% endif %}>karta</option>
            </select>
          </div>
          <div><label class="muted small">Termin płatności</label><input name="payment_to" type="date" value="{{ inv.payment_to }}"></div>
          <div><label class="muted small">Nabywca</label><input name="buyer_name" value="{{ inv.buyer_name }}"></div>
          <div><label class="muted small">NIP nabywcy</label><input name="buyer_tax_no" value="{{ inv.buyer_tax_no }}"></div>
          <div><label class="muted small">Adres nabywcy</label><textarea name="buyer_address" placeholder="Ulica&#10;Kod pocztowy Miasto">{{ buyer_address }}</textarea></div>
          <div><label class="muted small">Kraj</label><input name="buyer_country" value="{{ inv.buyer_country or 'PL' }}"></div>
          <div><label class="muted small">Email</label><input name="buyer_email" value="{{ inv.buyer_email }}"></div>
          <div><label class="muted small">Telefon</label><input name="buyer_phone" value="{{ inv.buyer_phone }}"></div>
          <div style="grid-column:1/-1;">
            <h2>Pozycje faktury</h2>
            <div class="hint" style="margin-bottom:10px;">
              Zmień ilości pozycji na tej fakturze. Wpisanie 0 usuwa pozycję z faktury.
            </div>
            <table>
              <thead>
                <tr>
                  <th>Zamówienie</th>
                  <th>Notatka</th>
                  <th>SKU</th>
                  <th>Model / Nazwa</th>
                  <th>Zamówiono</th>
                  <th>Na innych fakturach</th>
                  <th>Maks. na tej fakturze</th>
                  <th>Ilość na fakturze</th>
                  <th>Netto/szt</th>
                  <th>Brutto/szt</th>
                </tr>
              </thead>
              <tbody>
                {% for it in edit_items %}
                  <tr>
                    <td><b>{{ it.source_order_no }}</b></td>
                    <td>{{ it.source_order_note or '-' }}</td>
                    <td>{{ it.sku }}</td>
                    <td>{{ it.model or '' }}{% if it.name %}<div class="muted small">{{ it.name }}</div>{% endif %}</td>
                    <td>{{ it.ordered_qty }}</td>
                    <td>{{ it.invoiced_other_qty }}</td>
                    <td><b>{{ it.remaining_qty }}</b></td>
                    <td><input type="number" min="0" max="{{ it.remaining_qty }}" name="invoice_qty_{{ it.id }}" value="{{ it.current_invoice_qty }}" style="width:110px;"></td>
                    <td>{{ "%.2f"|format(it.net_price) }}</td>
                    <td>{{ "%.2f"|format(it.gross_price) }}</td>
                  </tr>
                {% endfor %}
              </tbody>
            </table>
          </div>
          <div style="grid-column:1/-1;" class="flex">
            <button class="btn primary" type="submit">Zapisz i regeneruj PDF</button>
            <a class="btn" href="{{ url_for('invoice_download_admin', invoice_id=inv.id) }}" target="_blank">Podgląd PDF</a>
          </div>
        </form>
      </div>
    {% endblock %}
    """
    return render_template_string(tpl, title="Edytuj fakturę", base_url=BASE_URL, db_path=DB_PATH, inv=inv, buyer_address=buyer_address, msg=msg, edit_items=edit_items)

@app.get("/orders/by-code/<path:token>")
def order_by_code(token):
    maybe_pull_shared_from_supabase()
    c = conn()
    cur = c.cursor()
    cur.execute("SELECT id, order_no, created_at FROM orders WHERE order_no=? LIMIT 1", (norm(token),))
    row = cur.fetchone()
    if not row:
        cur.execute("SELECT id, order_no, created_at FROM orders ORDER BY id DESC")
        all_rows = cur.fetchall()
        for r in all_rows:
            if canonical_order_no(r["id"], r["created_at"], r["order_no"]) == norm(token):
                row = r
                break
    c.close()
    if not row:
        return "Nie znaleziono zamĂłwienia", 404
    return redirect(url_for("order_view", order_id=row["id"]))


@app.get("/orders/scan")
def order_scan():
    tpl = r"""
    {% extends "base.html" %}
    {% block content %}
      <style>
        #qrScannerModal.hidden{display:none}
      </style>

      <div class="card">
        <h1 id="openQrScanner" style="cursor:pointer;text-decoration:underline;">Skanuj QR zamĂłwienia</h1>
        <div>
          <input id="orderCodeInput" placeholder="Wklej kod zamĂłwienia ZAM-... albo kliknij â€žSkanuj QR zamĂłwieniaâ€ť" />
          <button id="btnShowOrderByCode" class="btn primary" type="button" style="margin-top:8px">PokaĹĽ zamĂłwienie</button>
        </div>
        <div id="orderScanOut" class="muted" style="margin-top:10px"></div>
      </div>

      <div id="qrScannerModal" class="hidden" style="position:fixed;inset:0;background:rgba(0,0,0,.72);z-index:9999;padding:16px;box-sizing:border-box;">
        <div style="max-width:420px;margin:4vh auto;background:#fff;border-radius:16px;padding:12px;">
          <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:8px;">
            <b>Skanuj QR</b>
            <button id="closeQrScanner" class="btn" type="button">Zamknij</button>
          </div>
          <div class="muted" style="margin-bottom:8px;">Uruchamia siÄ™ tylko tylny aparat.</div>
          <div id="orderQrReader" style="width:100%;min-height:280px;"></div>
        </div>
      </div>

      <script src="https://unpkg.com/html5-qrcode" type="text/javascript"></script>
      <script>
        const $ = (id)=>document.getElementById(id);

        function parseOrderCode(value){
          const raw = String(value || '').trim();
          if(!raw) return '';

          const match = raw.match(/ZAM-[0-9]{6}[0-9]+|ZAM-[0-9]{8}-[A-Z0-9-]+/i);
          if(match) return match[0].toUpperCase();

          try {
            const url = new URL(raw);
            const fromPath = url.pathname.match(/ZAM-[0-9]{6}[0-9]+|ZAM-[0-9]{8}-[A-Z0-9-]+/i);
            if(fromPath) return fromPath[0].toUpperCase();
          } catch(e) {}

          return raw.toUpperCase();
        }

        function showOrderByCode(rawCode){
          const code = parseOrderCode(rawCode || $('orderCodeInput').value || '');
          if(!code){
            $('orderScanOut').innerHTML = '<div class="muted">Wpisz albo zeskanuj kod ZAM-...</div>';
            return;
          }
          window.location.href = '/orders/by-code/' + encodeURIComponent(code);
        }

        $('btnShowOrderByCode').onclick = () => showOrderByCode();

        let orderQrScanner = null;
        let orderQrScannerRunning = false;

        async function startOrderQrScanner(){
          if(!window.Html5Qrcode) return;
          if(!orderQrScanner){
            orderQrScanner = new Html5Qrcode('orderQrReader');
          }
          if(orderQrScannerRunning) return;

          const onSuccess = async (decodedText) => {
            const parsedCode = parseOrderCode(decodedText);
            $('orderCodeInput').value = parsedCode || decodedText;
            await stopOrderQrScanner();
            $('qrScannerModal').classList.add('hidden');
            showOrderByCode(parsedCode || decodedText);
          };

          try {
            await orderQrScanner.start(
              { facingMode: { exact: 'environment' } },
              { fps: 10, qrbox: 220, aspectRatio: 1.0 },
              onSuccess,
              () => {}
            );
            orderQrScannerRunning = true;
          } catch (e1) {
            try {
              await orderQrScanner.start(
                { facingMode: 'environment' },
                { fps: 10, qrbox: 220, aspectRatio: 1.0 },
                onSuccess,
                () => {}
              );
              orderQrScannerRunning = true;
            } catch (e2) {
              $('orderScanOut').innerHTML = '<div class="muted">Nie udaĹ‚o siÄ™ uruchomiÄ‡ tylnego aparatu.</div>';
              $('qrScannerModal').classList.add('hidden');
            }
          }
        }

        async function stopOrderQrScanner(){
          try {
            if(orderQrScanner && orderQrScannerRunning){
              await orderQrScanner.stop();
              await orderQrScanner.clear();
            }
          } catch (e) {}
          orderQrScannerRunning = false;
        }

        $('openQrScanner').onclick = async () => {
          $('qrScannerModal').classList.remove('hidden');
          await startOrderQrScanner();
        };

        $('closeQrScanner').onclick = async () => {
          await stopOrderQrScanner();
          $('qrScannerModal').classList.add('hidden');
        };

        $('qrScannerModal').onclick = async (e) => {
          if(e.target && e.target.id === 'qrScannerModal'){
            await stopOrderQrScanner();
            $('qrScannerModal').classList.add('hidden');
          }
        };

        window.addEventListener('beforeunload', () => {
          stopOrderQrScanner();
        });
      </script>
    {% endblock %}
    """
    return render_template_string(tpl, title="Skan QR", base_url=BASE_URL, db_path=DB_PATH)



# -------------------------
# CHINA (prosty start)
# -------------------------

@app.get("/china")
def china():
    # WyĹ‚Ä…czony pull z Supabase tylko dla moduĹ‚u Chiny.
    # Tu pracujemy na lokalnej bazie, ĹĽeby POST -> redirect nie cofaĹ‚ zmian.
    c = conn()
    cur = c.cursor()
    cur.execute("SELECT * FROM china_packages ORDER BY id DESC LIMIT 200")
    packs = cur.fetchall()
    c.close()

    tpl = r"""
    {% extends "base.html" %}
    {% block content %}
      <div class="card">
        <div class="flex">
          <h1 style="margin:0;">Chiny (P/O)</h1>
        </div>
        <div class="muted">ZarzÄ…dzaj przesyĹ‚kami: status, tracking i zawartoĹ›Ä‡ paczki. Tracking otwiera 17TRACK.</div>
      </div>

      <div class="card">
        <h2>Nowa paczka</h2>
        <form method="post" action="{{ url_for('china_create') }}" class="row">
          <div>
            <label class="muted small">Numer paczki / P/O</label>
            <input name="package_no" placeholder="np. PO-2026-02-01" required>
          </div>
          <div>
            <label class="muted small">Tracking</label>
            <input name="tracking" placeholder="UPS / DHL...">
          </div>
          <div>
            <label class="muted small">Status</label>
            <select name="status">
              <option value="planned">planned</option>
              <option value="ordered">ordered</option>
              <option value="shipped">shipped</option>
              <option value="arrived">arrived</option>
            </select>
          </div>
          <div>
            <label class="muted small">Notatka</label>
            <input name="note">
          </div>
          <div>
            <label class="muted small">Koszt paczki / P/O (PLN)</label>
            <input type="number" name="cost_amount" min="0.01" step="0.01" required>
          </div>
          <div>
            <label class="muted small">Numer dokumentu kosztowego</label>
            <input name="cost_document_no" placeholder="domyślnie numer P/O">
          </div>
          <div class="flex" style="align-items:flex-end;">
            <button class="btn primary" type="submit">Zapisz</button>
          </div>
        </form>
      </div>

      <div class="card">
        <h2>Paczki (max 200)</h2>
        <table>
          <thead>
            <tr><th>Nr</th><th>Status</th><th>Tracking</th><th>Koszt</th><th>Dokument</th><th>Notatka</th><th>Data</th><th>Akcje</th></tr>
          </thead>
          <tbody>
            {% for p in packs %}
              <tr>
                <td><b>{{ p['package_no'] }}</b></td>
                <td>
                  <form method="post" action="{{ url_for('china_status', package_id=p['id']) }}" class="flex">
                    <select name="status" style="width:140px;">
                      <option value="planned" {% if p['status']=='planned' %}selected{% endif %}>planned</option>
                      <option value="ordered" {% if p['status']=='ordered' %}selected{% endif %}>ordered</option>
                      <option value="shipped" {% if p['status']=='shipped' %}selected{% endif %}>shipped</option>
                      <option value="arrived" {% if p['status']=='arrived' %}selected{% endif %}>arrived</option>
                    </select>
                    <button class="btn" type="submit">ZmieĹ„</button>
                  </form>
                </td>
                <td>
                  <form method="post" action="{{ url_for('china_tracking', package_id=p['id']) }}" class="flex">
                    <input name="tracking" value="{{ p['tracking'] or '' }}" placeholder="nr trackingu" style="width:180px;">
                    <button class="btn" type="submit">Zapisz</button>
                    {% if p['tracking'] %}
                      <a class="btn" target="_blank" href="https://t.17track.net/en#nums={{ p['tracking']|urlencode }}">17TRACK</a>
                    {% endif %}
                  </form>
                </td>
                <td><b>{{ "%.2f"|format(p['cost_amount'] or 0) }} PLN</b></td>
                <td>{{ p['cost_document_no'] or p['package_no'] }}</td>
                <td>{{ p['note'] or "-" }}</td>
                <td class="muted">{{ p['created_at'] }}</td>
                <td class="flex">
                  <a class="btn primary" href="{{ url_for('china_package', package_id=p['id']) }}">ZawartoĹ›Ä‡</a>
                  <form method="post" action="{{ url_for('china_delete', package_id=p['id']) }}" onsubmit="return confirm('UsunÄ…Ä‡ paczkÄ™?')">
                    <button class="btn danger" type="submit">UsuĹ„</button>
                  </form>
                </td>
              </tr>
            {% endfor %}
            {% if not packs %}
              <tr><td colspan="8" class="muted">Brak paczek.</td></tr>
            {% endif %}
          </tbody>
        </table>
      </div>
    {% endblock %}
    """
    return render_template_string(tpl, title="Chiny (P/O)", base_url=BASE_URL, db_path=DB_PATH, packs=packs)

@app.post("/china/create")
def china_create():
    package_no = norm(request.form.get("package_no"))
    status = norm(request.form.get("status")) or "planned"
    tracking = norm(request.form.get("tracking"))
    note = norm(request.form.get("note"))
    cost_amount = to_float(request.form.get("cost_amount"), 0)
    cost_document_no = norm(request.form.get("cost_document_no")) or package_no

    if not package_no or cost_amount <= 0:
        return "Podaj numer P/O oraz koszt większy od zera", 400

    c = conn()
    cur = c.cursor()
    try:
        cur.execute("""
          INSERT INTO china_packages(package_no, status, tracking, note, cost_amount, cost_document_no, created_at)
          VALUES(?,?,?,?,?,?,?)
        """, (package_no, status, tracking, note, cost_amount, cost_document_no, now_iso()))
        c.commit()
    except sqlite3.IntegrityError:
        pass
    finally:
        c.close()

    return redirect(url_for("china"))

@app.post("/china/<int:package_id>/status")
def china_status(package_id):
    status = norm(request.form.get("status"))
    if status not in {"planned", "ordered", "shipped", "arrived"}:
        return "NieprawidĹ‚owy status", 400

    c = conn()
    cur = c.cursor()

    cur.execute("SELECT status FROM china_packages WHERE id=?", (package_id,))
    pack = cur.fetchone()
    if not pack:
        c.close()
        abort(404)

    old_status = pack["status"]

    cur.execute("SELECT product_id, qty FROM china_items WHERE package_id=?", (package_id,))
    items = cur.fetchall()

    # PrzejĹ›cie NA arrived: fizycznie przyjÄ™to towar -> dodaj na stan.
    if old_status != "arrived" and status == "arrived":
        for it in items:
            pid = it["product_id"]
            qty = int(it["qty"])
            cur.execute("INSERT OR IGNORE INTO stock(product_id, qty) VALUES (?, 0)", (pid,))
            cur.execute("UPDATE stock SET qty = qty + ? WHERE product_id=?", (qty, pid))

    # CofniÄ™cie Z arrived na inny status: towar wraca jako "w drodze" -> odejmij ze stanu.
    elif old_status == "arrived" and status != "arrived":
        for it in items:
            pid = it["product_id"]
            qty = int(it["qty"])
            cur.execute("INSERT OR IGNORE INTO stock(product_id, qty) VALUES (?, 0)", (pid,))
            cur.execute("UPDATE stock SET qty = qty - ? WHERE product_id=?", (qty, pid))

    cur.execute("UPDATE china_packages SET status=? WHERE id=?", (status, package_id))
    c.commit()
    c.close()
    return redirect(url_for("china"))

@app.post("/china/<int:package_id>/tracking")
def china_tracking(package_id):
    tracking = norm(request.form.get("tracking"))

    c = conn()
    cur = c.cursor()
    cur.execute("SELECT id FROM china_packages WHERE id=?", (package_id,))
    if not cur.fetchone():
        c.close()
        abort(404)

    cur.execute("UPDATE china_packages SET tracking=? WHERE id=?", (tracking, package_id))
    c.commit()
    c.close()

    ref = request.referrer or ""
    if ref.endswith(f"/china/{package_id}"):
        return redirect(url_for("china_package", package_id=package_id))
    return redirect(url_for("china"))

@app.post("/china/<int:package_id>/cost")
def china_cost(package_id):
    cost_amount = to_float(request.form.get("cost_amount"), 0)
    cost_document_no = norm(request.form.get("cost_document_no"))
    if cost_amount <= 0 or not cost_document_no:
        return redirect(url_for("china_package", package_id=package_id, cost_error=1))

    c = conn()
    cur = c.cursor()
    cur.execute("SELECT * FROM china_packages WHERE id=?", (package_id,))
    pack = cur.fetchone()
    if not pack:
        c.close()
        abort(404)
    cur.execute(
        "UPDATE china_packages SET cost_amount=?, cost_document_no=? WHERE id=?",
        (cost_amount, cost_document_no, package_id),
    )
    c.commit()
    cur.execute("SELECT * FROM china_packages WHERE id=?", (package_id,))
    cloud_row = dict(cur.fetchone())
    c.close()
    if supabase_enabled():
        supabase_upsert_rows("china_packages", [cloud_row], "id")
    return redirect(url_for("china_package", package_id=package_id, cost_saved=1))

@app.get("/china/<int:package_id>")
def china_package(package_id):
    # WyĹ‚Ä…czony pull z Supabase tylko dla moduĹ‚u Chiny.
    c = conn()
    cur = c.cursor()
    cur.execute("SELECT * FROM china_packages WHERE id=?", (package_id,))
    pack = cur.fetchone()
    if not pack:
        c.close()
        abort(404)

    cur.execute("SELECT id, sku, model, name FROM products WHERE COALESCE(archived,0)=0 ORDER BY sku LIMIT 5000")
    products_rows = cur.fetchall()

    cur.execute("""
      SELECT ci.*, p.model, p.name
      FROM china_items ci
      JOIN products p ON p.id=ci.product_id
      WHERE ci.package_id=?
      ORDER BY ci.id DESC
    """, (package_id,))
    items = cur.fetchall()
    c.close()

    tpl = r"""
    {% extends "base.html" %}
    {% block content %}
      <div class="card">
        <div class="flex">
          <h1 style="margin:0;">Paczka {{ pack['package_no'] }}</h1>
          <span class="badge">{{ pack['status'] }}</span>
          <a class="btn right" href="{{ url_for('china') }}">â† Lista paczek</a>
        </div>
        <div class="muted">Tracking: {{ pack['tracking'] or '-' }}</div>
        <form method="post" action="{{ url_for('china_tracking', package_id=pack['id']) }}" class="flex" style="margin-top:10px;">
          <input name="tracking" value="{{ pack['tracking'] or '' }}" placeholder="nr trackingu" style="width:260px;">
          <button class="btn" type="submit">ZmieĹ„ tracking</button>
          {% if pack['tracking'] %}
            <a class="btn" target="_blank" href="https://t.17track.net/en#nums={{ pack['tracking']|urlencode }}">OtwĂłrz 17TRACK</a>
          {% endif %}
        </form>
        <form method="post" action="{{ url_for('china_cost', package_id=pack['id']) }}" class="flex" style="margin-top:10px;align-items:flex-end;">
          <div>
            <label class="muted small">Koszt paczki / P/O (PLN)</label>
            <input type="number" name="cost_amount" min="0.01" step="0.01" value="{{ pack['cost_amount'] or '' }}" required style="width:220px;">
          </div>
          <div>
            <label class="muted small">Numer dokumentu kosztowego</label>
            <input name="cost_document_no" value="{{ pack['cost_document_no'] or pack['package_no'] }}" required style="width:280px;">
          </div>
          <button class="btn primary" type="submit">Zapisz koszt</button>
          {% if request.args.get('cost_saved') %}<span class="badge">Koszt zapisany</span>{% endif %}
          {% if request.args.get('cost_error') %}<span class="muted" style="color:#b00020;">Podaj kwotę większą od zera i numer dokumentu.</span>{% endif %}
        </form>
      </div>

      <div class="card">
        <h2>Dodaj zawartoĹ›Ä‡ paczki</h2>
        <form method="post" action="{{ url_for('china_item_add', package_id=pack['id']) }}" class="items-row">
          <div>
            <label class="muted small">Produkt</label>
            <select name="product_id" required>
              <option value="">-- wybierz --</option>
              {% for p in products %}
                <option value="{{ p['id'] }}">{{ p['sku'] }}{% if p['model'] %} â€˘ {{ p['model'] }}{% endif %}{% if p['name'] %} â€˘ {{ p['name'] }}{% endif %}</option>
              {% endfor %}
            </select>
          </div>
          <div>
            <label class="muted small">IloĹ›Ä‡</label>
            <input name="qty" value="1" required>
          </div>
          <div class="flex" style="align-items:flex-end;">
            <button class="btn primary" type="submit">Dodaj</button>
          </div>
        </form>
      </div>

      <div class="card">
        <h2>ZawartoĹ›Ä‡ paczki</h2>
        <table>
          <thead>
            <tr><th>SKU</th><th>Model / Nazwa</th><th>IloĹ›Ä‡</th><th>Data</th><th>Akcje</th></tr>
          </thead>
          <tbody>
            {% for it in items %}
              <tr>
                <td><b>{{ it['sku'] }}</b></td>
                <td>{{ it['model'] or '' }}{% if it['name'] %}<div class="muted">{{ it['name'] }}</div>{% endif %}</td>
                <td><span class="badge">{{ it['qty'] }}</span></td>
                <td class="muted">{{ it['created_at'] }}</td>
                <td>
                  <form method="post" action="{{ url_for('china_item_delete', package_id=pack['id'], item_id=it['id']) }}" onsubmit="return confirm('UsunÄ…Ä‡ pozycjÄ™?')">
                    <button class="btn danger" type="submit">UsuĹ„</button>
                  </form>
                </td>
              </tr>
            {% endfor %}
            {% if not items %}
              <tr><td colspan="5" class="muted">Brak pozycji w paczce.</td></tr>
            {% endif %}
          </tbody>
        </table>
      </div>
    {% endblock %}
    """
    return render_template_string(tpl, title=f"Paczka {pack['package_no']}", base_url=BASE_URL, db_path=DB_PATH,
                                  pack=pack, products=products_rows, items=items)


@app.post("/china/<int:package_id>/delete")
def china_delete(package_id):
    c = conn()
    cur = c.cursor()
    cur.execute("SELECT status FROM china_packages WHERE id=?", (package_id,))
    pack = cur.fetchone()
    if not pack:
        c.close()
        abort(404)

    if norm(pack["status"]).lower() == "arrived":
        c.close()
        return "Nie moĹĽna usunÄ…Ä‡ paczki ARRIVED", 400

    if supabase_enabled():
        try:
            cur.execute("SELECT id FROM china_items WHERE package_id=?", (package_id,))
            item_ids = [int(r["id"]) for r in cur.fetchall()]
            for iid in item_ids:
                supabase_delete_rows("china_items", {"id": iid})
            supabase_delete_rows("china_packages", {"id": package_id})
        except Exception:
            pass

    cur.execute("DELETE FROM china_items WHERE package_id=?", (package_id,))
    cur.execute("DELETE FROM china_packages WHERE id=?", (package_id,))
    c.commit()
    c.close()
    return redirect(url_for("china"))

@app.post("/china/<int:package_id>/items/add")
def china_item_add(package_id):
    product_id = to_int(request.form.get("product_id"), 0)
    qty = to_int(request.form.get("qty"), 0)
    if product_id <= 0 or qty <= 0:
        return "NieprawidĹ‚owy produkt lub iloĹ›Ä‡", 400

    c = conn()
    cur = c.cursor()
    cur.execute("SELECT sku FROM products WHERE id=? AND COALESCE(archived,0)=0", (product_id,))
    p = cur.fetchone()
    if not p:
        c.close()
        return "Produkt nie istnieje", 404

    cur.execute("SELECT id FROM china_packages WHERE id=?", (package_id,))
    if not cur.fetchone():
        c.close()
        return "Paczka nie istnieje", 404

    cur.execute(
        "INSERT INTO china_items(package_id, product_id, sku, qty, created_at) VALUES (?,?,?,?,?)",
        (package_id, product_id, p["sku"], qty, now_iso())
    )
    c.commit()
    c.close()
    return redirect(url_for("china_package", package_id=package_id))

@app.post("/china/<int:package_id>/items/<int:item_id>/delete")
def china_item_delete(package_id, item_id):
    if supabase_enabled():
        supabase_delete_rows("china_items", {"id": item_id})

    c = conn()
    cur = c.cursor()
    cur.execute("DELETE FROM china_items WHERE id=? AND package_id=?", (item_id, package_id))
    c.commit()
    c.close()
    return redirect(url_for("china_package", package_id=package_id))


# =========================
# RUN
# =========================
if __name__ == "__main__":
    # debug=True moĹĽesz zostawiÄ‡ na czas budowy
    app.run(host="0.0.0.0", port=5000, debug=True)
