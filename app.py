# -*- coding: utf-8 -*-
import os
import io
import csv
import base64
import re
import json
import glob
import sqlite3
import socket
import time
import threading
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from flask import (
    Flask, request, redirect, url_for, jsonify,
    send_file, abort
)
from flask import render_template_string
from jinja2 import DictLoader

import qrcode
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont


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
    # Dla QR preferuj adres LAN; jeśli aplikacja jest otwarta lokalnie,
    # spróbuj wykryć LAN IP automatycznie (bardziej niezawodne niż stały BASE_URL).
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
        created_at TEXT NOT NULL,
        FOREIGN KEY(order_id) REFERENCES orders(id),
        FOREIGN KEY(product_id) REFERENCES products(id)
    )
    """)

    # Paczki z Chin (prosty moduł na start)
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

    # migracja: starsze bazy mogą nie mieć kolumny NIP u klientów
    cur.execute("PRAGMA table_info(customers)")
    customer_cols = {r[1] for r in cur.fetchall()}
    if "nip" not in customer_cols:
        cur.execute("ALTER TABLE customers ADD COLUMN nip TEXT")

    # migracja: QR zamówień
    cur.execute("PRAGMA table_info(orders)")
    order_cols = {r[1] for r in cur.fetchall()}
    if "qr_data_url" not in order_cols:
        cur.execute("ALTER TABLE orders ADD COLUMN qr_data_url TEXT")

    # Ułatwia agregowanie "w dostawie" po statusach paczek
    cur.execute("CREATE INDEX IF NOT EXISTS idx_china_items_package_id ON china_items(package_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_china_items_product_id ON china_items(product_id)")

    c.commit()
    c.close()

init_db()


# =========================
# UTILS
# =========================

def now_iso():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def make_order_no(order_id: int) -> str:
    # np. ZAM-20260220-000123
    d = datetime.now().strftime("%Y%m%d")
    return f"ZAM-{d}-{order_id:06d}"


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
        "cash": "gotówka",
        "gotowka": "gotówka",
        "transfer": "przelew",
        "card": "karta",
        "karta": "karta",
    }
    return mapping.get(v, v or "-")


def find_logo_path() -> str:
    for fn in ("logo.png", "logo.jpg", "logo.jpeg", "logo.webp"):
        pth = os.path.join(DATA_DIR, fn)
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
        "packed": "W dostawie",
        "in_delivery": "W dostawie",
        "issued": "Wydane",
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
        "in_delivery": "st-delivery",
        "issued": "st-issued",
    }
    return mapping.get(v, "")

def guess_col(headers, candidates):
    h = [x.strip().lower() for x in headers]
    for cand in candidates:
        cand = cand.lower()
        if cand in h:
            return h.index(cand)
    # luźne dopasowanie: np. "model" w "Model uchwytu"
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
SUPABASE_SERVICE_ROLE_KEY = (os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InFmemF3emt5bm1xa2JqbGJ0a2pkIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc3NDUyNDgxMCwiZXhwIjoyMDkwMTAwODEwfQ.DcyQuZL4atOlbsgSWBmgl-nvQ0eJOTrcu6ciU59O7zU").strip()
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
    ("company_profile", "id"),
    ("invoices", "id"),
]

# Kolejność PULL jest ważna: najpierw rodzice, potem dzieci.
SUPABASE_PULL_TABLES = [
    ("company_profile", "id"),
    ("pricing", "model"),
    ("customers", "id"),
    ("products", "id"),
    ("orders", "id"),
    ("china_packages", "id"),
    ("stock", "product_id"),
    ("order_items", "id"),
    ("china_items", "id"),
    ("invoices", "id"),
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
    with urllib.request.urlopen(req, timeout=60) as resp:
        if resp.status >= 300:
            raise RuntimeError(f"Supabase HTTP {resp.status}")

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
            raise RuntimeError(f"Nieprawidłowa odpowiedź Supabase dla tabeli {table}")
        rows.extend(chunk)
        if len(chunk) < page_size:
            break
        offset += page_size
    return rows


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

    # 1) pobierz wszystko z Supabase
    for table, conflict_col in SUPABASE_PULL_TABLES:
        try:
            fetched[(table, conflict_col)] = supabase_select_rows(table, order_by=conflict_col)
        except Exception as e:
            result["ok"] = False
            result["tables"][table] = {"status": "error", "stage": "fetch", "error": str(e)}

    # 2) upsert do lokalnego SQLite
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

    # 3) usuń lokalne rekordy, których już nie ma w Supabase
    for table, conflict_col in reversed(SUPABASE_PULL_TABLES):
        if (table, conflict_col) not in fetched:
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

    return result


def maybe_pull_shared_from_supabase(force: bool = False):
    try:
        if request.method == "GET":
            pull_shared_tables_from_supabase(force=force)
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


def sync_order_to_supabase(order_id: int):
    sync_local_rows_to_supabase("orders", "id", [order_id])
    c = conn()
    cur = c.cursor()
    cur.execute("SELECT id FROM order_items WHERE order_id=?", (order_id,))
    item_ids = [int(r["id"]) for r in cur.fetchall()]
    c.close()
    if item_ids:
        sync_local_rows_to_supabase("order_items", "id", item_ids)


def remote_first_create_customer(name: str, address: str, phone: str, email: str, nip: str):
    created = supabase_insert_row("customers", {
        "name": name,
        "address": address,
        "phone": phone,
        "email": email,
        "nip": nip,
        "created_at": now_iso(),
    })
    if not created or "id" not in created:
        raise RuntimeError("Supabase nie zwrócił ID dla klienta")

    customer_id = int(created["id"])
    c = conn()
    cur = c.cursor()
    cur.execute(
        "INSERT INTO customers(id, name, address, phone, email, nip, created_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name, address=excluded.address, phone=excluded.phone, email=excluded.email, nip=excluded.nip, created_at=excluded.created_at",
        (customer_id, name, address, phone, email, nip, created.get("created_at") or now_iso())
    )
    c.commit()
    c.close()
    return customer_id


def remote_first_create_order(customer_id, customer_name, customer_address, customer_phone, customer_email, note, items):
    created_at = now_iso()
    created_order = supabase_insert_row("orders", {
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
    })
    if not created_order or "id" not in created_order:
        raise RuntimeError("Supabase nie zwrócił ID dla zamówienia")

    order_id = int(created_order["id"])
    order_no = make_order_no(order_id)
    qr_data_url = ""
    supabase_update_rows("orders", {"order_no": order_no, "qr_data_url": qr_data_url}, {"id": order_id})

    c = conn()
    cur = c.cursor()
    cur.execute(
        "INSERT INTO orders(id, order_no, customer_id, customer_name, customer_address, customer_phone, customer_email, status, note, created_at, qr_data_url) VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET order_no=excluded.order_no, customer_id=excluded.customer_id, customer_name=excluded.customer_name, customer_address=excluded.customer_address, customer_phone=excluded.customer_phone, customer_email=excluded.customer_email, status=excluded.status, note=excluded.note, created_at=excluded.created_at, qr_data_url=excluded.qr_data_url",
        (order_id, order_no, customer_id if customer_id else None, customer_name, customer_address, customer_phone, customer_email, "new", note, created_at, qr_data_url)
    )

    for pid, qty in items:
        cur.execute("SELECT sku FROM products WHERE id=?", (pid,))
        p = cur.fetchone()
        if not p:
            continue
        created_item = supabase_insert_row("order_items", {
            "order_id": order_id,
            "product_id": pid,
            "sku": p["sku"],
            "qty": qty,
            "created_at": now_iso(),
        })
        if not created_item or "id" not in created_item:
            raise RuntimeError("Supabase nie zwrócił ID dla pozycji zamówienia")
        cur.execute(
            "INSERT INTO order_items(id, order_id, product_id, sku, qty, created_at) VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET order_id=excluded.order_id, product_id=excluded.product_id, sku=excluded.sku, qty=excluded.qty, created_at=excluded.created_at",
            (int(created_item["id"]), order_id, pid, p["sku"], qty, created_item.get("created_at") or now_iso())
        )

    c.commit()
    c.close()
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

    # Szukaj czcionek Unicode także po wildcardach i lokalnym katalogu app/fonts.
    regular_candidates = [
        # Lokalne fonty aplikacji (najwyższy priorytet)
        ("AppFont-Regular", os.path.join(APP_DIR, "fonts", "regular.ttf")),

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

    # Dodatkowe wildcardy gdy ścieżki systemowe różnią się między maszynami.
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


def generate_sales_invoice(order_row, items):
    customer_dir = invoice_dir_for_customer(order_row["customer_name"])
    fname = f"FV_{safe_filename(order_row['order_no'])}.pdf"
    fpath = os.path.join(customer_dir, fname)

    c = conn()
    cur = c.cursor()
    cur.execute("SELECT * FROM company_profile WHERE id=1")
    company = cur.fetchone()
    cur.execute("SELECT model, net_price, gross_price FROM pricing")
    pricing_rows = cur.fetchall()
    c.close()

    pricing_map = {norm(r["model"]): r for r in pricing_rows}

    w = 210 * mm
    h = 297 * mm
    cpdf = canvas.Canvas(fpath, pagesize=(w, h))

    pdf_font, pdf_font_bold = get_pdf_font_names()

    y = h - 18 * mm
    cpdf.setFont(pdf_font_bold, 14)
    cpdf.drawString(15 * mm, y, f"Faktura sprzedażowa: {order_row['order_no']}")

    y -= 8 * mm
    cpdf.setFont(pdf_font, 10)
    cpdf.drawString(15 * mm, y, f"Data: {order_row['created_at']}")

    y -= 9 * mm
    cpdf.setFont(pdf_font_bold, 10)
    cpdf.drawString(15 * mm, y, "Sprzedawca:")
    y -= 6 * mm
    cpdf.setFont(pdf_font, 9)
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
        cpdf.drawString(15 * mm, y, "Brak danych firmy (uzupełnij w zakładce: Dane mojej firmy)")

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
    cpdf.drawString(95 * mm, y, "Ilość")
    cpdf.drawString(112 * mm, y, "Netto/szt")
    cpdf.drawString(140 * mm, y, "Brutto/szt")
    cpdf.drawString(170 * mm, y, "Wartość brutto")
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
    cpdf.drawString(15 * mm, y, "Ceny pobrane z zakładki Cennik (model, netto, brutto).")

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
    c.close()

    pricing_map = {norm(r["model"]): r for r in pricing_rows}

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
    cpdf.drawString(15 * mm, y, f"Miejsce: {meta.get('place') or '-'}")
    cpdf.drawString(85 * mm, y, f"Data wystawienia: {meta['issue_date']}")
    cpdf.drawString(150 * mm, y, f"Data sprzedaży: {meta['sell_date']}")

    y -= 7 * mm
    cpdf.drawString(15 * mm, y, f"Forma płatności: {payment_type_pl(meta.get('payment_type'))}")
    cpdf.drawString(85 * mm, y, f"Termin płatności: {meta.get('payment_to') or '-'}")

    y -= 10 * mm
    cpdf.setFont(pdf_font_bold, 10)
    cpdf.drawString(15 * mm, y, "Sprzedawca")
    cpdf.drawString(110 * mm, y, "Nabywca")

    y -= 6 * mm
    cpdf.setFont(pdf_font, 9)
    seller_name = (company["company_name"] if company else "") or "-"
    seller_nip = (company["nip"] if company else "") or "-"
    seller_addr = (company["address"] if company else "") or "-"
    seller_phone = (company["phone"] if company else "") or ""
    seller_email = (company["email"] if company else "") or ""
    seller_bank = (company["bank_account"] if company else "") or ""

    buyer_name = meta.get("buyer_name") or (order_row["customer_name"] if order_row and "customer_name" in order_row.keys() else "") or "-"
    buyer_tax_no = meta.get("buyer_tax_no") or "-"
    buyer_street = meta.get("buyer_street") or "-"
    buyer_post = meta.get("buyer_post_code") or ""
    buyer_city = meta.get("buyer_city") or ""
    buyer_country = meta.get("buyer_country") or "PL"
    buyer_email = meta.get("buyer_email") or ""
    buyer_phone = meta.get("buyer_phone") or ""

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

    max_len = max(len(seller_lines), len(buyer_lines))
    for i in range(max_len):
        if i < len(seller_lines):
            cpdf.drawString(15 * mm, y, seller_lines[i][:55])
        if i < len(buyer_lines):
            cpdf.drawString(110 * mm, y, buyer_lines[i][:55])
        y -= 5 * mm

    y -= 3 * mm
    table_left = 15 * mm
    table_right = 198 * mm
    row_h = 9 * mm
    # L.p. | Nazwa/SKU | Ilość | Netto/szt | Brutto/szt | Wartość netto | VAT
    col_x = [15 * mm, 23 * mm, 96 * mm, 110 * mm, 134 * mm, 158 * mm, 180 * mm, 198 * mm]

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
    header_font = 8.0
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
    total_gross = 0.0
    discount_pct = max(0.0, to_float(meta.get("discount_percent"), 0.0))
    body_font = 8.2
    cpdf.setFont(pdf_font, body_font)

    lp = 1
    for it in items:
        model = norm(it.get("model"))
        sku = norm(it.get("sku"))
        pr = pricing_map.get(model) or pricing_map.get(sku)
        net = float(pr["net_price"]) if pr else 0.0
        gross = float(pr["gross_price"]) if pr else round(net * 1.23, 2)
        qty = int(it["qty"])
        line_net = round(net * qty, 2)
        line_gross = round(gross * qty, 2)
        if discount_pct > 0:
            line_net = round(line_net * (100.0 - discount_pct) / 100.0, 2)
            line_gross = round(line_gross * (100.0 - discount_pct) / 100.0, 2)
        line_tax = round(line_gross - line_net, 2)

        total_net += line_net
        total_gross += line_gross

        text_y = cell_baseline(y, row_h, pdf_font, body_font)
        cpdf.drawCentredString(cell_center(col_x[0], col_x[1]), text_y, str(lp))
        cpdf.drawString(col_x[1] + 1.5 * mm, text_y, (sku or model or "-")[:24])
        cpdf.drawCentredString(cell_center(col_x[2], col_x[3]), text_y, str(qty))
        cpdf.drawRightString(col_x[4] - 1.5 * mm, text_y, f"{net:.2f}")
        cpdf.drawRightString(col_x[5] - 1.5 * mm, text_y, f"{gross:.2f}")
        cpdf.drawRightString(col_x[6] - 1.5 * mm, text_y, f"{line_net:.2f}")
        cpdf.drawRightString(col_x[7] - 1.5 * mm, text_y, f"{line_tax:.2f}")
        cpdf.line(table_left, y - row_h + 1, table_right, y - row_h + 1)
        for cx in col_x:
            cpdf.line(cx, y + 1, cx, y - row_h + 1)
        y -= row_h
        lp += 1
        if y < 26 * mm:
            cpdf.showPage()
            y = h - 20 * mm
            cpdf.setFont(pdf_font, 8.8)

    total_tax = round(total_gross - total_net, 2)
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

    cpdf.save()
    return fpath, round(total_net,2), round(total_gross,2)



# =========================
# TEMPLATES (BASE as "file")
# =========================

BASE = r"""
<!doctype html>
<html lang="pl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ title or "Niedźwieccy Orders" }}</title>
  <style>
    body{font-family:Arial, sans-serif; margin:0; background:#f6f7fb; color:#111;}
    .top{background:#111; color:#fff; padding:12px 14px; display:flex; gap:10px; flex-wrap:wrap; align-items:center;}
    .brand{font-weight:700; letter-spacing:.2px;}
    .nav a{color:#fff; text-decoration:none; padding:7px 10px; border:1px solid rgba(255,255,255,.25); border-radius:10px;}
    .nav a:hover{background:rgba(255,255,255,.08)}
    .wrap{max-width:1100px; margin:16px auto; padding:0 12px;}
    .card{background:#fff; border:1px solid #e7e7ee; border-radius:14px; padding:14px; box-shadow:0 8px 22px rgba(0,0,0,.04); margin-bottom:12px;}
    .row{display:grid; grid-template-columns:1fr 1fr; gap:12px;}
    @media (max-width:860px){ .row{grid-template-columns:1fr;} }
    h1{font-size:22px; margin:0 0 12px;}
    h2{font-size:16px; margin:0 0 10px;}
    .muted{color:#666; font-size:12px;}
    .btn{display:inline-block; padding:9px 12px; border:1px solid #ddd; border-radius:10px; background:#fff; color:#111; text-decoration:none; cursor:pointer;}
    .btn.primary{background:#111; color:#fff; border-color:#111;}
    .btn.danger{background:#b00020; color:#fff; border-color:#b00020;}
    .btn.ok{background:#0a7a34; color:#fff; border-color:#0a7a34;}
    input, select, textarea{width:100%; padding:10px; border:1px solid #ddd; border-radius:10px; font-size:14px;}
    textarea{min-height:90px;}
    table{width:100%; border-collapse:collapse;}
    th,td{border-bottom:1px solid #eee; padding:10px; text-align:left; vertical-align:top;}
    th{background:#fafafa;}
    .badge{display:inline-block; padding:4px 8px; border-radius:999px; border:1px solid #ddd; font-size:12px;}
    .flex{display:flex; gap:10px; flex-wrap:wrap; align-items:center;}
    .right{margin-left:auto;}
    .small{font-size:12px;}
    .grid3{display:grid; grid-template-columns: 2fr 1fr 1fr; gap:12px;}
    @media (max-width:860px){ .grid3{grid-template-columns:1fr;} }
    .line{height:1px; background:#eee; margin:12px 0;}
    .hint{background:#fff7d6; border:1px solid #ffe08a; padding:10px; border-radius:12px; font-size:13px;}
    .kpi{display:flex; gap:12px; flex-wrap:wrap;}
    .kpi .pill{background:#fafafa; border:1px solid #eee; padding:8px 10px; border-radius:999px; font-size:13px;}
    .items-row{display:grid; grid-template-columns: 2fr 120px 120px 120px; gap:10px; align-items:center;}
    @media (max-width:860px){ .items-row{grid-template-columns:1fr 1fr;} }
  </style>
</head>
<body>
  <div class="top">
    <div class="brand">Niedźwieccy Orders</div>
    <div class="nav flex">
      <a href="{{ url_for('home') }}">Start</a>
      <a href="{{ url_for('orders') }}">Zamówienia</a>
      <a href="{{ url_for('order_new') }}">Nowe zamówienie</a>
      <a href="{{ url_for('products') }}">Produkty</a>
      <a href="{{ url_for('customers') }}">Klienci</a>
      <a href="{{ url_for('pricing') }}">Cennik</a>
      <a href="{{ url_for('company') }}">Dane mojej firmy</a>
      <a href="{{ url_for('stock') }}">Magazyn</a>
      <a href="{{ url_for('china') }}">Chiny (P/O)</a>
      <a href="{{ url_for('order_scan') }}">Skan QR</a>
      <a href="{{ url_for('cloud_supabase') }}">Chmura</a>
    </div>
    <div class="right muted">Lokalnie • {{ base_url }}</div>
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

# loader: BASE dostępny jako "base.html"
app.jinja_loader = DictLoader({"base.html": BASE})


# =========================
# PAGES
# =========================

@app.get("/")
def home():
    maybe_pull_shared_from_supabase()
    c = conn()
    cur = c.cursor()
    cur.execute("SELECT COUNT(*) AS n FROM products")
    n_products = cur.fetchone()["n"]
    cur.execute("SELECT COUNT(*) AS n FROM orders WHERE status IN ('new','packed','confirmed','in_delivery')")
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
    """)
    inventory_value_net = float(cur.fetchone()["v"] or 0)
    c.close()

    tpl = r"""
    {% extends "base.html" %}
    {% block content %}
      <style>
        .start-kpi .pill{
          font-size:20px;
          padding:14px 18px;
          line-height:1.35;
        }
        .start-kpi .pill b{
          font-size:28px;
        }
      </style>

      <div class="card">
        <h1>Start</h1>
        <div class="muted">Szybki podgląd najważniejszych, bieżących danych.</div>
      </div>

      <div class="card">
        <h2>Zamówienia i paczki (aktualne)</h2>
        <div class="kpi start-kpi">
          <div class="pill">Zamówienia aktualne (new/packed/confirmed/in_delivery): <b>{{ n_orders_current }}</b></div>
          <div class="pill">Paczki Chiny aktywne (planned/ordered/shipped): <b>{{ n_china_active }}</b></div>
        </div>
      </div>

      <div class="card">
        <h2>Stan asortymentu</h2>
        <div class="kpi start-kpi">
          <div class="pill">Produkty: <b>{{ n_products }}</b></div>
          <div class="pill">Uchwyty na stanie: <b>{{ n_stock_qty }}</b> szt.</div>
          <div class="pill">Uchwyty w drodze: <b>{{ n_in_delivery_qty }}</b> szt.</div>
          <div class="pill">Wartość magazynu + w drodze (netto): <b>{{ "%.2f"|format(inventory_value_net) }} PLN</b></div>
        </div>
      </div>
    {% endblock %}
    """
    return render_template_string(tpl, title="Start", base_url=BASE_URL, db_path=DB_PATH,
                                  n_products=n_products, n_orders_current=n_orders_current, n_china_active=n_china_active,
                                  n_stock_qty=n_stock_qty, n_in_delivery_qty=n_in_delivery_qty,
                                  inventory_value_net=inventory_value_net)


@app.get("/cloud/supabase")
def cloud_supabase():
    tpl = r"""
    {% extends "base.html" %}
    {% block content %}
      <style>
        .st-unconfirmed{background:#ef4444;color:#fff;border-color:#ef4444;}
        .st-confirmed{background:#16a34a;color:#fff;border-color:#16a34a;}
        .st-delivery{background:#2563eb;color:#fff;border-color:#2563eb;}
        .st-issued{background:#6b7280;color:#fff;border-color:#6b7280;}
      </style>

      <div class="card">
        <h1>Supabase Cloud Sync</h1>
        <div class="muted">Przerzucanie danych z lokalnego SQLite do tabel w Supabase (REST upsert).</div>
      </div>

      <div class="card">
        <div><b>Status konfiguracji:</b> {% if enabled %}<span class="badge">AKTYWNA</span>{% else %}<span class="badge">BRAK</span>{% endif %}</div>
        <div class="muted" style="margin-top:8px;">Wymagane zmienne środowiskowe: <code>SUPABASE_URL</code>, <code>SUPABASE_SERVICE_ROLE_KEY</code>.</div>
        <div class="muted" style="margin-top:6px;">Auto sync po zapisie: <b>{{ "ON" if auto_sync else "OFF" }}</b> (co najmniej co {{ min_interval }} s).</div>
      </div>

      <div class="card">
        <h2>Ręczna synchronizacja</h2>
        <div class="flex">
          <form method="post" action="{{ url_for('cloud_supabase_sync') }}">
            <button class="btn primary" type="submit">Push do Supabase</button>
          </form>
          <form method="post" action="{{ url_for('cloud_supabase_pull') }}">
            <button class="btn" type="submit">Pull z Supabase</button>
          </form>
        </div>
      </div>
    {% endblock %}
    """
    return render_template_string(
        tpl,
        title="Chmura / Supabase",
        base_url=BASE_URL,
        db_path=DB_PATH,
        enabled=supabase_enabled(),
        auto_sync=SUPABASE_AUTO_SYNC_ON_WRITE,
        min_interval=SUPABASE_MIN_SYNC_INTERVAL_SEC
    )


@app.post("/cloud/supabase/sync")
def cloud_supabase_sync():
    result = sync_all_to_supabase()
    return jsonify(result), (200 if result.get("ok") else 500)


@app.post("/cloud/supabase/pull")
def cloud_supabase_pull():
    result = pull_shared_tables_from_supabase(force=True)
    return jsonify(result), (200 if result.get("ok") else 500)


@app.after_request
def auto_sync_after_write(response):
    try:
        if response.status_code >= 400:
            return response
        if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
            return response
        # endpoint syncu manualnego nie powinien odpalać sam siebie
        if request.path.startswith("/cloud/supabase"):
            return response
        trigger_background_supabase_sync(reason=f"{request.method} {request.path}")
    except Exception:
        pass
    return response


# -------------------------
# COMPANY
# -------------------------

@app.get("/company")
def company():
    maybe_pull_shared_from_supabase()
    c = conn()
    cur = c.cursor()
    cur.execute("SELECT * FROM company_profile WHERE id=1")
    row = cur.fetchone()
    c.close()

    tpl = r"""
    {% extends "base.html" %}
    {% block content %}
      <div class="card">
        <h1>Dane mojej firmy</h1>
        <div class="muted">Te dane trafią na fakturę sprzedażową.</div>
      </div>

      <div class="card">
        <form method="post" action="{{ url_for('company_save') }}" class="row">
          <div><label class="muted small">Nazwa firmy</label><input name="company_name" value="{{ row['company_name'] if row else '' }}"></div>
          <div><label class="muted small">NIP</label><input name="nip" value="{{ row['nip'] if row else '' }}"></div>
          <div><label class="muted small">Telefon</label><input name="phone" value="{{ row['phone'] if row else '' }}"></div>
          <div><label class="muted small">Email</label><input name="email" value="{{ row['email'] if row else '' }}"></div>
          <div><label class="muted small">Konto bankowe</label><input name="bank_account" value="{{ row['bank_account'] if row else '' }}"></div>
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
    bank_account = norm(request.form.get("bank_account"))

    c = conn()
    cur = c.cursor()
    cur.execute("""
      INSERT INTO company_profile(id, company_name, address, nip, phone, email, bank_account, updated_at)
      VALUES(1,?,?,?,?,?,?,?)
      ON CONFLICT(id) DO UPDATE SET
        company_name=excluded.company_name,
        address=excluded.address,
        nip=excluded.nip,
        phone=excluded.phone,
        email=excluded.email,
        bank_account=excluded.bank_account,
        updated_at=excluded.updated_at
    """, (company_name, address, nip, phone, email, bank_account, now_iso()))
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
    c = conn()
    cur = c.cursor()
    if q:
        like = f"%{q}%"
        cur.execute("SELECT * FROM pricing WHERE model LIKE ? ORDER BY model LIMIT 2000", (like,))
    else:
        cur.execute("SELECT * FROM pricing ORDER BY model LIMIT 2000")
    rows = cur.fetchall()
    c.close()

    tpl = r"""
    {% extends "base.html" %}
    {% block content %}
      <div class="card">
        <h1>Cennik</h1>
        <div class="muted">Import pliku cen (kolumny: model, netto, brutto). Obsługa CSV i XLSX (jeśli dostępny openpyxl).</div>
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
        <form method="get" class="grid3" style="margin-bottom:10px;">
          <input name="q" value="{{ q }}" placeholder="Szukaj modelu">
          <button class="btn primary" type="submit">Szukaj</button>
          <a class="btn" href="{{ url_for('pricing') }}">Wyczyść</a>
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
    {% endblock %}
    """
    return render_template_string(tpl, title="Cennik", base_url=BASE_URL, db_path=DB_PATH, rows=rows, q=q)

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
            return "Brak biblioteki openpyxl do odczytu XLSX. Użyj CSV albo doinstaluj openpyxl.", 400

        wb = load_workbook(f, data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            return "Pusty plik", 400
        headers = [norm(x) for x in rows[0]]
        data = rows[1:]
        i_model = guess_col(headers, ["model"])
        i_net = guess_col(headers, ["netto", "net", "cena netto"])
        i_gross = guess_col(headers, ["brutto", "gross", "cena brutto"])
        if i_model is None or i_net is None or i_gross is None:
            return "Plik musi mieć kolumny: model, netto, brutto", 400
        for r in data:
            if not r:
                continue
            model = norm(r[i_model]) if len(r) > i_model else ""
            if not model:
                continue
            net = to_float(r[i_net] if len(r) > i_net else "", 0.0)
            gross = to_float(r[i_gross] if len(r) > i_gross else "", 0.0)
            parsed_rows.append((model, net, gross))

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
        i_net = guess_col(headers, ["netto", "net", "cena netto"])
        i_gross = guess_col(headers, ["brutto", "gross", "cena brutto"])
        if i_model is None or i_net is None or i_gross is None:
            return "Plik musi mieć kolumny: model, netto, brutto", 400
        for r in data:
            if not r:
                continue
            model = norm(r[i_model]) if len(r) > i_model else ""
            if not model:
                continue
            net = to_float(r[i_net] if len(r) > i_net else "", 0.0)
            gross = to_float(r[i_gross] if len(r) > i_gross else "", 0.0)
            parsed_rows.append((model, net, gross))

    c = conn()
    cur = c.cursor()
    for model, net, gross in parsed_rows:
        cur.execute("""
          INSERT INTO pricing(model, net_price, gross_price, created_at)
          VALUES(?,?,?,?)
          ON CONFLICT(model) DO UPDATE SET
            net_price=excluded.net_price,
            gross_price=excluded.gross_price,
            created_at=excluded.created_at
        """, (model, net, gross, now_iso()))
    c.commit()
    c.close()
    return redirect(url_for("pricing"))


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
          <a class="btn" href="{{ url_for('customers') }}">Wyczyść</a>
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
          <div class="flex" style="align-items:flex-end;">
            <button class="btn primary" type="submit">Zapisz klienta</button>
          </div>
        </form>
      </div>

      <div class="card">
        <h2>Lista klientów</h2>
        <table>
          <thead>
            <tr><th>Nazwa</th><th>Telefon</th><th>Email</th><th>NIP</th><th>Adres</th><th>Akcje</th></tr>
          </thead>
          <tbody>
            {% for r in rows %}
              <tr>
                <td><b>{{ r['name'] }}</b></td>
                <td>{{ r['phone'] or '-' }}</td>
                <td>{{ r['email'] or '-' }}</td>
                <td>{{ r['nip'] or '-' }}</td>
                <td style="white-space:pre-line;">{{ r['address'] or '-' }}</td>
                <td>
                  <div class="flex">
                    <a class="btn" href="{{ url_for('customers_edit', customer_id=r['id']) }}">Edytuj</a>
                    <form method="post" action="{{ url_for('customers_delete', customer_id=r['id']) }}" onsubmit="return confirm('Usunąć klienta?')">
                      <button class="btn danger" type="submit">Usuń</button>
                    </form>
                  </div>
                </td>
              </tr>
            {% endfor %}
            {% if not rows %}
              <tr><td colspan="6" class="muted">Brak klientów.</td></tr>
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
    if not name:
        return "Brak nazwy klienta", 400

    if supabase_enabled():
        remote_first_create_customer(name, address, phone, email, nip)
    else:
        c = conn()
        cur = c.cursor()
        cur.execute(
            "INSERT INTO customers(name, address, phone, email, nip, created_at) VALUES (?,?,?,?,?,?)",
            (name, address, phone, email, nip, now_iso())
        )
        c.commit()
        c.close()
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
        <div class="muted">Zmień dane zapisane dla stałego klienta.</div>
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
          <div class="flex" style="align-items:flex-end;">
            <button class="btn primary" type="submit">Zapisz zmiany</button>
            <a class="btn" href="{{ url_for('customers') }}">Powrót</a>
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
    if not name:
        return "Brak nazwy klienta", 400

    c = conn()
    cur = c.cursor()
    cur.execute("""
      UPDATE customers
      SET name=?, address=?, phone=?, email=?, nip=?
      WHERE id=?
    """, (name, address, phone, email, nip, customer_id))
    c.commit()
    c.close()

    if supabase_enabled():
        supabase_update_rows("customers", {
            "name": name,
            "address": address,
            "phone": phone,
            "email": email,
            "nip": nip,
        }, {"id": customer_id})

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
          WHERE p.sku LIKE ? OR p.model LIKE ? OR p.ean LIKE ? OR p.name LIKE ?
          ORDER BY p.sku
          LIMIT 1000
        """, (like, like, like, like))
    else:
        cur.execute("""
          SELECT p.*, COALESCE(s.qty,0) AS stock
          FROM products p
          LEFT JOIN stock s ON s.product_id=p.id
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
        <form method="get" class="grid3" style="margin-top:10px;">
          <input name="q" value="{{ q }}" placeholder="Szukaj: SKU / model / EAN / nazwa">
          <button class="btn primary" type="submit">Szukaj</button>
          <a class="btn" href="{{ url_for('products') }}">Wyczyść</a>
        </form>
      </div>

      <div class="card">
        <h2>Import CSV (478 pozycji)</h2>
        <div class="muted">Wybierz plik CSV z Excela. Minimalnie: kolumna SKU (unikalna). Pozostałe: model, ean, name/nazwa.</div>
        <form method="post" action="{{ url_for('products_import') }}" enctype="multipart/form-data" class="row" style="margin-top:10px;">
          <div>
            <input type="file" name="file" accept=".csv,text/csv" required>
            <div class="muted small" style="margin-top:6px;">Kodowanie: najlepiej UTF-8. Separator zwykle „;” lub „,” – program sam spróbuje.</div>
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
            </tr>
            {% endfor %}
            {% if not rows %}
              <tr><td colspan="5" class="muted">Brak produktów. Zrób import CSV.</td></tr>
            {% endif %}
          </tbody>
        </table>
      </div>
    {% endblock %}
    """
    return render_template_string(tpl, title="Produkty", base_url=BASE_URL, db_path=DB_PATH, rows=rows, q=q)

@app.post("/products/import")
def products_import():
    f = request.files.get("file")
    if not f:
        return "Brak pliku", 400

    raw = f.read()
    # Spróbuj UTF-8, jak nie pójdzie to latin2
    try:
        text = raw.decode("utf-8-sig")
    except:
        text = raw.decode("latin2", errors="replace")

    # Spróbuj wykryć delimiter
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
        return "CSV musi mieć kolumnę SKU / Symbol / Indeks", 400

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
            cur.execute("UPDATE products SET model=?, ean=?, name=? WHERE sku=?", (model, ean, name, sku))
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
    c.close()

    return redirect(url_for("products", q=""))


# -------------------------
# STOCK
# -------------------------

@app.get("/stock")
def stock():
    maybe_pull_shared_from_supabase()
    q = norm(request.args.get("q"))
    c = conn()
    cur = c.cursor()

    if q:
        like = f"%{q}%"
        cur.execute("""
          SELECT x.*,
                 CASE WHEN x.ordered_new - x.qty > 0 THEN x.ordered_new - x.qty ELSE 0 END AS reserved_in_delivery,
                 CASE WHEN x.in_delivery - (CASE WHEN x.ordered_new - x.qty > 0 THEN x.ordered_new - x.qty ELSE 0 END) > 0
                      THEN x.in_delivery - (CASE WHEN x.ordered_new - x.qty > 0 THEN x.ordered_new - x.qty ELSE 0 END)
                      ELSE 0
                 END AS available_in_delivery
          FROM (
            SELECT p.id, p.sku, p.model, p.ean, p.name,
                   COALESCE(s.qty,0) AS qty,
                   COALESCE((
                      SELECT SUM(ci.qty)
                      FROM china_items ci
                      JOIN china_packages cp ON cp.id=ci.package_id
                      WHERE ci.product_id=p.id
                        AND cp.status IN ('planned', 'ordered', 'shipped')
                   ), 0) AS in_delivery,
                   COALESCE((
                      SELECT SUM(oi.qty)
                      FROM order_items oi
                      JOIN orders o ON o.id=oi.order_id
                      WHERE oi.product_id=p.id
                        AND o.status='new'
                   ), 0) AS ordered_new
            FROM products p
            LEFT JOIN stock s ON s.product_id=p.id
            WHERE p.sku LIKE ? OR p.model LIKE ? OR p.ean LIKE ? OR p.name LIKE ?
          ) x
          ORDER BY x.sku
          LIMIT 1000
        """, (like, like, like, like))
    else:
        cur.execute("""
          SELECT x.*,
                 CASE WHEN x.ordered_new - x.qty > 0 THEN x.ordered_new - x.qty ELSE 0 END AS reserved_in_delivery,
                 CASE WHEN x.in_delivery - (CASE WHEN x.ordered_new - x.qty > 0 THEN x.ordered_new - x.qty ELSE 0 END) > 0
                      THEN x.in_delivery - (CASE WHEN x.ordered_new - x.qty > 0 THEN x.ordered_new - x.qty ELSE 0 END)
                      ELSE 0
                 END AS available_in_delivery
          FROM (
            SELECT p.id, p.sku, p.model, p.ean, p.name,
                   COALESCE(s.qty,0) AS qty,
                   COALESCE((
                      SELECT SUM(ci.qty)
                      FROM china_items ci
                      JOIN china_packages cp ON cp.id=ci.package_id
                      WHERE ci.product_id=p.id
                        AND cp.status IN ('planned', 'ordered', 'shipped')
                   ), 0) AS in_delivery,
                   COALESCE((
                      SELECT SUM(oi.qty)
                      FROM order_items oi
                      JOIN orders o ON o.id=oi.order_id
                      WHERE oi.product_id=p.id
                        AND o.status='new'
                   ), 0) AS ordered_new
            FROM products p
            LEFT JOIN stock s ON s.product_id=p.id
          ) x
          ORDER BY x.sku
          LIMIT 1000
        """)
    rows = cur.fetchall()
    c.close()

    tpl = r"""
    {% extends "base.html" %}
    {% block content %}
      <div class="card">
        <div class="flex">
          <h1 style="margin:0;">Magazyn</h1>
        </div>
        <form method="get" class="grid3" style="margin-top:10px;">
          <input name="q" value="{{ q }}" placeholder="Szukaj produktu: SKU / model / EAN / nazwa">
          <button class="btn primary" type="submit">Szukaj</button>
          <a class="btn" href="{{ url_for('stock') }}">Wyczyść</a>
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
          <button class="btn ok" onclick="applyDelta(); return false;">Zapisz korektę</button>
          <div class="muted" id="deltaMsg"></div>
        </div>
      </div>

      <div class="card">
        <h2>Stany (max 1000)</h2>
        <div class="muted" style="margin-bottom:8px;">
          Najpierw realizowane są ilości z magazynu. Niedobory z otwartych zamówień (status <b>new</b>) rezerwują towar „w drodze”.
        </div>
        <table>
          <thead>
            <tr><th>SKU</th><th>Model</th><th>EAN</th><th>Nazwa</th><th>Stan</th><th>W drodze</th><th>Zarezerwowane w drodze</th><th>Dostępne w drodze</th></tr>
          </thead>
          <tbody>
            {% for r in rows %}
              <tr>
                <td><b>{{ r['sku'] }}</b></td>
                <td>{{ r['model'] or "" }}</td>
                <td>{{ r['ean'] or "" }}</td>
                <td>{{ r['name'] or "" }}</td>
                <td><span class="badge">{{ r['qty'] }}</span></td>
                <td><span class="badge">{{ r['in_delivery'] }}</span></td>
                <td><span class="badge">{{ r['reserved_in_delivery'] }}</span></td>
                <td><span class="badge">{{ r['available_in_delivery'] }}</span></td>
              </tr>
            {% endfor %}
            {% if not rows %}
              <tr><td colspan="8" class="muted">Brak produktów.</td></tr>
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
  if(!delta){ msg.innerText = "Podaj zmianę"; return; }

  const r = await fetch("/api/stock_delta", {
    method:"POST",
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({sku, delta})
  });
  const j = await r.json();
  if(!j.ok){ msg.innerText = "Błąd: " + (j.error || ""); return; }
  msg.innerText = "OK. Nowy stan: " + j.new_qty;
  setTimeout(()=>location.reload(), 500);
}
</script>

    {% endblock %}
    """
    return render_template_string(tpl, title="Magazyn", base_url=BASE_URL, db_path=DB_PATH, rows=rows, q=q)

@app.post("/api/stock_delta")
def api_stock_delta():
    data = request.get_json(force=True, silent=True) or {}
    sku = norm(data.get("sku"))
    delta_raw = norm(data.get("delta"))

    if not sku:
        return jsonify(ok=False, error="Brak SKU"), 400

    delta = to_int(delta_raw, None)
    if delta is None:
        # spróbuj +10 / -3
        try:
            delta = int(delta_raw)
        except:
            return jsonify(ok=False, error="Nieprawidłowa zmiana (np. +10 lub -3)"), 400

    c = conn()
    cur = c.cursor()
    cur.execute("SELECT id FROM products WHERE sku=?", (sku,))
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
      WHERE p.id=?
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
    q = norm(request.args.get("q"))
    tab = norm(request.args.get("tab")) or "new"
    if tab not in {"new", "issued", "all"}:
        tab = "new"

    c = conn()
    cur = c.cursor()

    where_parts = []
    params = []

    if tab == "new":
        where_parts.append("status IN ('new', 'packed', 'confirmed', 'in_delivery')")
    elif tab == "issued":
        where_parts.append("status='issued'")

    if q:
        where_parts.append("(order_no LIKE ? OR customer_name LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like])

    where_sql = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""
    sql = f"""
      SELECT o.*,
             COALESCE((
               SELECT SUM(oi.qty * COALESCE(pr.net_price, 0))
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

    visible_open_ids = sorted([r["id"] for r in rows if r["status"] in ("new", "packed", "confirmed", "in_delivery")])
    if visible_open_ids:
        cur.execute("SELECT id FROM orders WHERE status IN ('new','packed','confirmed','in_delivery') AND id<=? ORDER BY id", (visible_open_ids[-1],))
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

    c.close()

    tpl = r"""
    {% extends "base.html" %}
    {% block content %}
      <div class="card">
        <div class="flex">
          <h1 style="margin:0;">Zamówienia</h1>
          <a class="btn primary right" href="{{ url_for('order_new') }}">+ Nowe zamówienie</a>
        </div>
        <div class="flex" style="margin-top:10px;">
          <a class="btn {% if tab=='new' %}primary{% endif %}" href="{{ url_for('orders', tab='new', q=q) }}">Do wydania</a>
          <a class="btn {% if tab=='issued' %}primary{% endif %}" href="{{ url_for('orders', tab='issued', q=q) }}">Wydane z magazynu</a>
          <a class="btn {% if tab=='all' %}primary{% endif %}" href="{{ url_for('orders', tab='all', q=q) }}">Wszystkie</a>
        </div>
        <form method="get" class="grid3" style="margin-top:10px;">
          <input type="hidden" name="tab" value="{{ tab }}">
          <input name="q" value="{{ q }}" placeholder="Szukaj: numer zamówienia lub klient">
          <button class="btn primary" type="submit">Szukaj</button>
          <a class="btn" href="{{ url_for('orders', tab=tab) }}">Wyczyść</a>
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
            <tr><th>Nr</th><th>Klient</th><th>Status</th><th>Wartość netto</th><th>Data</th><th>Akcje</th></tr>
          </thead>
          <tbody>
            {% for r in rows %}
              <tr {% if r['has_shortage'] or r['status'] in ['new','pending','unconfirmed'] %}style="background:#ffe7e7;"{% endif %}>
                <td><b>{{ r['order_no'] }}</b></td>
                <td>{{ r['customer_name'] }}</td>
                <td><span class="badge {{ order_status_css(r['status']) }}">{{ order_status_label(r['status']) }}</span></td>
                <td><span class="badge">{{ "%.2f"|format(r['order_value_net']) }} PLN</span></td>
                <td class="muted">{{ r['created_at'] }}</td>
                <td class="flex">
                  <a class="btn" href="{{ url_for('order_view', order_id=r['id']) }}">Szczegóły</a>
                  {% if r['status'] != 'issued' %}
                    <a class="btn" href="{{ url_for('order_label', order_id=r['id']) }}">Etykieta 30x50</a>
                    <form method="post" action="{{ url_for('order_delete', order_id=r['id']) }}" onsubmit="return confirm('Usunąć zamówienie?')">
                      <button class="btn danger" type="submit">Usuń</button>
                    </form>
                  {% else %}
                    <span class="muted">Podgląd</span>
                  {% endif %}
                </td>
              </tr>
            {% endfor %}
            {% if not rows %}
              <tr><td colspan="5" class="muted">Brak zamówień.</td></tr>
            {% endif %}
          </tbody>
        </table>
      </div>
    {% endblock %}
    """
    return render_template_string(tpl, title="Zamówienia", base_url=BASE_URL, db_path=DB_PATH, rows=rows, q=q, tab=tab, order_status_label=order_status_label, order_status_css=order_status_css)

@app.get("/orders/new")
def order_new():
    maybe_pull_shared_from_supabase()
    c = conn()
    cur = c.cursor()
    cur.execute("SELECT id, sku, model, name FROM products ORDER BY sku LIMIT 5000")
    products_rows = cur.fetchall()
    cur.execute("SELECT id, name, address, phone, email, nip FROM customers ORDER BY name")
    customers_rows = cur.fetchall()
    c.close()

    tpl = r"""
    {% extends "base.html" %}
    {% block content %}
      <div class="card">
        <h1>Nowe zamówienie</h1>
        <div class="muted">Produkty wybierasz z bazy. Przy wyborze pokazuje stan magazynowy.</div>
      </div>

      <div class="card">
        <form method="post" action="{{ url_for('order_create') }}">
          <div class="row">
            <div>
              <label class="muted small">Wybierz stałego klienta (opcjonalnie)</label>
              <select id="customerSelect" name="customer_id" onchange="fillCustomer(this.value)">
                <option value="">-- ręcznie / nowy klient --</option>
                {% for c in customers %}
                  <option value="{{ c['id'] }}">{{ c['name'] }}</option>
                {% endfor %}
              </select>
            </div>
            <div class="muted">Po wyborze pola klienta zostaną automatycznie uzupełnione.</div>
          </div>

          <div class="row">
            <div>
              <label class="muted small">Zamawiający (nazwa firmy / osoba)</label>
              <input name="customer_name" required>
            </div>
            <div>
              <label class="muted small">Telefon</label>
              <input name="customer_phone">
            </div>
          </div>

          <div class="row" style="margin-top:10px;">
            <div>
              <label class="muted small">Adres (na etykietę)</label>
              <textarea name="customer_address" placeholder="Ulica, kod, miasto, kraj"></textarea>
            </div>
            <div>
              <label class="muted small">Email</label>
              <input name="customer_email">
              <div style="height:10px;"></div>
              <label class="muted small">Adres Wysyłki</label>
              <input name="note">
            </div>
          </div>

          <div class="line"></div>

          <div class="flex">
            <h2 style="margin:0;">Pozycje zamówienia</h2>
            <button class="btn" onclick="addItemRow(); return false;">+ Dodaj pozycję</button>
          </div>

          <div id="itemsContainer" style="margin-top:10px;"></div>

          <template id="itemRowTpl">
            <div class="items-row card" style="margin:10px 0;">
              <div>
                <label class="muted small">Produkt (SKU)</label>
                <select name="product_id[]" onchange="refreshStock(this.value, this.dataset.stockTarget)" data-stock-target="">
                  <option value="">-- wybierz --</option>
                  {% for p in products %}
                    <option value="{{ p['id'] }}">{{ p['sku'] }}{% if p['model'] %} • {{ p['model'] }}{% endif %}{% if p['name'] %} • {{ p['name'] }}{% endif %}</option>
                  {% endfor %}
                </select>
              </div>
              <div>
                <label class="muted small">Ilość</label>
                <input name="qty[]" value="1">
              </div>
              <div>
                <label class="muted small">Stan</label>
                <div class="badge" id="">-</div>
              </div>
              <div class="flex" style="align-items:flex-end;">
                <button class="btn danger" onclick="removeRow(this); return false;">Usuń</button>
              </div>
            </div>
          </template>

          <div class="line"></div>
          <button class="btn primary" type="submit">Zapisz zamówienie</button>
          <a class="btn" href="{{ url_for('orders') }}">Anuluj</a>
        </form>
      </div>

<script>
// po dodaniu wiersza trzeba podpiąć ID na badge (stan)
function addItemRow(){
  const tpl = document.getElementById("itemRowTpl");
  const container = document.getElementById("itemsContainer");
  const node = tpl.content.cloneNode(true);

  // znajdź select i badge w nowo wstawionym wierszu
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
        title="Nowe zamówienie",
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
        return "Brak zamawiającego", 400

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
        return "Dodaj minimum 1 pozycję", 400

    if supabase_enabled():
        oid = remote_first_create_order(customer_id if customer_id > 0 else None, customer_name, customer_address, customer_phone, customer_email, note, items)
    else:
        c = conn()
        cur = c.cursor()
        cur.execute("""
          INSERT INTO orders(order_no, customer_id, customer_name, customer_address, customer_phone, customer_email, status, note, created_at, qr_data_url)
          VALUES(?,?,?,?,?,?,?,?,?,?)
        """, ("TEMP", customer_id if customer_id > 0 else None, customer_name, customer_address, customer_phone, customer_email, "new", note, now_iso(), ""))
        oid = cur.lastrowid

        order_no = make_order_no(oid)
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

    return redirect(url_for("order_view", order_id=oid))

@app.get("/orders/<int:order_id>")
def order_view(order_id):
    maybe_pull_shared_from_supabase()
    c = conn()
    cur = c.cursor()
    cur.execute("SELECT * FROM orders WHERE id=?", (order_id,))
    o = cur.fetchone()
    if not o:
        c.close()
        abort(404)

    cur.execute("""
      SELECT oi.*, p.model, p.ean, p.name,
             COALESCE(pr.net_price, 0) AS net_price,
             COALESCE(pr.gross_price, 0) AS gross_price,
             (oi.qty * COALESCE(pr.net_price, 0)) AS line_value_net,
             (oi.qty * COALESCE(pr.gross_price, 0)) AS line_value_gross,
             COALESCE(s.qty,0) AS stock,
             COALESCE((
                SELECT SUM(ci.qty)
                FROM china_items ci
                JOIN china_packages cp ON cp.id=ci.package_id
                WHERE ci.product_id=oi.product_id
                  AND cp.status IN ('planned', 'ordered', 'shipped')
             ), 0) AS in_delivery
      FROM order_items oi
      JOIN products p ON p.id=oi.product_id
      LEFT JOIN pricing pr ON (TRIM(LOWER(pr.model)) = TRIM(LOWER(p.model)) OR TRIM(LOWER(pr.model)) = TRIM(LOWER(p.sku)))
      LEFT JOIN stock s ON s.product_id=p.id
      WHERE oi.order_id=?
      ORDER BY oi.id
    """, (order_id,))
    items = [dict(r) for r in cur.fetchall()]

    for it in items:
        it["in_delivery_available"] = int(it.get("in_delivery", 0))
        it["delivery_used"] = 0
        it["line_shortage"] = 0

    if o["status"] in ("new", "packed", "confirmed", "in_delivery"):
        cur.execute("SELECT id FROM orders WHERE status IN ('new','packed','confirmed','in_delivery') AND id<=? ORDER BY id", (order_id,))
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

    cur.execute("SELECT id, sku, model, name FROM products ORDER BY sku LIMIT 5000")
    products_rows = cur.fetchall()
    c.close()

    order_url = build_public_url(url_for("order_view", order_id=order_id))

    tpl = r"""
    {% extends "base.html" %}
    {% block content %}
      <div class="card">
        <div class="flex">
          <h1 style="margin:0;">{{ o['order_no'] }}</h1>
          <span class="badge {{ order_status_css(o['status']) }}">{{ order_status_label(o['status']) }}</span>
          <div class="right flex">
            <a class="btn" href="{{ url_for('orders') }}">← Lista</a>
            <a class="btn" href="{{ url_for('order_print', order_id=o['id']) }}">Drukuj zamówienie</a>
            <a class="btn primary" href="{{ url_for('order_invoice', order_id=o['id']) }}">Faktura</a>
            {% if not locked %}
              <form method="post" action="{{ url_for('order_status_update', order_id=o['id']) }}" class="flex">
                <select name="status" style="width:190px;">
                  <option value="new" {% if o['status'] in ['new','pending','unconfirmed'] %}selected{% endif %}>Niepotwierdzone</option>
                  <option value="confirmed" {% if o['status']=='confirmed' %}selected{% endif %}>Potwierdzone</option>
                  <option value="in_delivery" {% if o['status'] in ['packed','in_delivery'] %}selected{% endif %}>W dostawie</option>
                </select>
                <button class="btn" type="submit">Zmień status</button>
              </form>
              <a class="btn primary" href="{{ url_for('order_label', order_id=o['id']) }}">Etykieta 30x50</a>
              <a class="btn ok" href="{{ url_for('order_issue', order_id=o['id']) }}">Wydaj z magazynu</a>
              <form method="post" action="{{ url_for('order_delete', order_id=o['id']) }}" onsubmit="return confirm('Usunąć zamówienie?')">
                <button class="btn danger" type="submit">Usuń zamówienie</button>
              </form>
            {% endif %}
          </div>
        </div>
        <div class="muted" style="margin-top:6px;">{{ o['created_at'] }}</div>
      </div>

      <div class="row">
        <div class="card">
          <h2>Zamawiający</h2>
          <div><b>{{ o['customer_name'] }}</b></div>
          <div class="muted" style="white-space:pre-line; margin-top:6px;">{{ o['customer_address'] or "-" }}</div>
          <div class="muted" style="margin-top:6px;">Tel: {{ o['customer_phone'] or "-" }}</div>
          <div class="muted">Email: {{ o['customer_email'] or "-" }}</div>
          <div class="line"></div>
          <div class="muted small">Kod zamówienia do skanowania: <b>{{ o['order_no'] }}</b></div>
          {% if o['qr_data_url'] %}
            <div style="margin-top:10px;">
              <img src="{{ o['qr_data_url'] }}" alt="QR zamówienia" style="width:180px;height:180px;border:1px solid #eee;border-radius:12px;padding:8px;background:#fff;">
            </div>
          {% else %}
            <div class="muted small" style="margin-top:10px;">QR wygeneruje się po ustawieniu statusu na <b>Potwierdzone</b>.</div>
          {% endif %}
        </div>

        <div class="card">
          <h2>Notatka</h2>
          <div>{{ o['note'] or "-" }}</div>
          <div class="line"></div>
          <div class="hint">
            <b>Wydaj z magazynu</b> odejmie ilości z magazynu (przycisk zielony).<br>
            Jeśli brakuje stanu, pozycja może być realizowana z <b>towaru w drodze z Chin</b> (kolumna „W dostawie” poniżej).
          </div>
        </div>
      </div>

      {% if not locked %}
      <div class="card">
        <h2>Dodaj produkt do zamówienia</h2>
        <form method="post" action="{{ url_for('order_item_add', order_id=o['id']) }}" class="items-row">
          <div>
            <select name="product_id" required>
              <option value="">-- wybierz produkt --</option>
              {% for p in products %}
                <option value="{{ p['id'] }}">{{ p['sku'] }}{% if p['model'] %} • {{ p['model'] }}{% endif %}{% if p['name'] %} • {{ p['name'] }}{% endif %}</option>
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
            <tr><th>SKU</th><th>Model / Nazwa</th><th>Ilość</th><th>Cena netto</th><th>Cena brutto</th><th>Wartość netto</th><th>Wartość brutto</th><th>Stan teraz</th><th>W dostawie (dostępne)</th><th>Realizacja</th><th>Akcje</th></tr>
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
                      <button class="btn" type="submit">Zmień</button>
                    </form>
                  {% endif %}
                </td>
                <td><span class="badge">{{ "%.2f"|format(it['net_price']) }} PLN</span></td>
                <td><span class="badge">{{ "%.2f"|format(it['gross_price']) }} PLN</span></td>
                <td><span class="badge">{{ "%.2f"|format(it['line_value_net']) }} PLN</span></td>
                <td><span class="badge">{{ "%.2f"|format(it['line_value_gross']) }} PLN</span></td>
                <td><span class="badge">{{ it['stock'] }}</span></td>
                <td><span class="badge">{{ it['in_delivery_available'] }}</span></td>
                <td>
                  {% if it['line_shortage'] <= 0 and it['delivery_used'] == 0 %}
                    <span class="badge">Z magazynu</span>
                  {% elif it['line_shortage'] <= 0 %}
                    <span class="badge">Część / całość z Chin</span>
                  {% else %}
                    <span class="badge">Brak towaru</span>
                  {% endif %}
                </td>
                <td>
                  {% if not locked %}
                    <form method="post" action="{{ url_for('order_item_delete', order_id=o['id'], item_id=it['id']) }}" onsubmit="return confirm('Usunąć pozycję?')">
                      <button class="btn danger" type="submit">Usuń</button>
                    </form>
                  {% else %}
                    <span class="muted">Podgląd</span>
                  {% endif %}
                </td>
              </tr>
            {% endfor %}
            {% if items %}
              <tr>
                <td colspan="5" style="text-align:right;"><b>Suma netto:</b></td>
                <td><span class="badge"><b>{{ "%.2f"|format(ns.total_net) }} PLN</b></span></td>
                <td colspan="5"></td>
              </tr>
              <tr>
                <td colspan="6" style="text-align:right;"><b>Suma brutto:</b></td>
                <td><span class="badge"><b>{{ "%.2f"|format(ns.total_gross) }} PLN</b></span></td>
                <td colspan="4"></td>
              </tr>
            {% else %}
              <tr><td colspan="11" class="muted">Brak pozycji w zamówieniu.</td></tr>
            {% endif %}
          </tbody>
        </table>
      </div>
    {% endblock %}
    """
    return render_template_string(tpl, title=o["order_no"], base_url=BASE_URL, db_path=DB_PATH, o=o, items=items, order_url=order_url, products=products_rows, locked=(o["status"]=="issued"), order_status_label=order_status_label, order_status_css=order_status_css)

@app.post("/orders/<int:order_id>/items/add")
def order_item_add(order_id):
    product_id = to_int(request.form.get("product_id"), 0)
    qty = to_int(request.form.get("qty"), 0)
    if product_id <= 0 or qty <= 0:
        return "Nieprawidłowy produkt lub ilość", 400

    c = conn()
    cur = c.cursor()
    cur.execute("SELECT status FROM orders WHERE id=?", (order_id,))
    o = cur.fetchone()
    if not o:
        c.close()
        abort(404)
    if o["status"] == "issued":
        c.close()
        return "Zamówienie wydane z magazynu jest tylko do podglądu", 400

    cur.execute("SELECT sku FROM products WHERE id=?", (product_id,))
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
            return "Nie udało się dodać pozycji do Supabase", 500
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
        return "Ilość musi być > 0", 400
    c = conn()
    cur = c.cursor()
    cur.execute("SELECT status FROM orders WHERE id=?", (order_id,))
    o = cur.fetchone()
    if not o:
        c.close()
        abort(404)
    if o["status"] == "issued":
        c.close()
        return "Zamówienie wydane z magazynu jest tylko do podglądu", 400
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
    cur.execute("SELECT status FROM orders WHERE id=?", (order_id,))
    o = cur.fetchone()
    if not o:
        c.close()
        abort(404)
    if o["status"] == "issued":
        c.close()
        return "Zamówienie wydane z magazynu jest tylko do podglądu", 400

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
    cur.execute("SELECT status FROM orders WHERE id=?", (order_id,))
    o = cur.fetchone()
    if not o:
        c.close()
        abort(404)
    if o["status"] == "issued":
        c.close()
        return "Zamówienie wydane z magazynu jest tylko do podglądu", 400

    if supabase_enabled():
        supabase_delete_rows("order_items", {"order_id": order_id})
        supabase_delete_rows("orders", {"id": order_id})

    cur.execute("DELETE FROM order_items WHERE order_id=?", (order_id,))
    cur.execute("DELETE FROM orders WHERE id=?", (order_id,))
    c.commit()
    c.close()
    return redirect(url_for("orders"))

@app.post("/orders/<int:order_id>/status")
def order_status_update(order_id):
    new_status = norm(request.form.get("status")).lower()
    allowed = {"new", "confirmed", "in_delivery"}
    if new_status not in allowed:
        return "Nieprawidłowy status", 400

    c = conn()
    cur = c.cursor()
    cur.execute("SELECT id, order_no, qr_data_url, status FROM orders WHERE id=?", (order_id,))
    o = cur.fetchone()
    if not o:
        c.close()
        abort(404)
    if o["status"] == "issued":
        c.close()
        return "Zamówienie wydane z magazynu jest tylko do podglądu", 400

    qr_data_url = o["qr_data_url"] or ""
    if new_status == "confirmed" and not qr_data_url:
        qr_data_url = make_qr_data_url(o["order_no"])

    cur.execute("UPDATE orders SET status=?, qr_data_url=? WHERE id=?", (new_status, qr_data_url, order_id))
    c.commit()
    c.close()

    if supabase_enabled():
        supabase_update_rows("orders", {"status": new_status, "qr_data_url": qr_data_url}, {"id": order_id})

    return redirect(url_for("order_view", order_id=order_id))


@app.get("/orders/<int:order_id>/issue")
def order_issue(order_id):
    # odejmij z magazynu wg pozycji i oznacz jako wydane
    c = conn()
    cur = c.cursor()
    cur.execute("SELECT * FROM orders WHERE id=?", (order_id,))
    o = cur.fetchone()
    if not o:
        c.close()
        abort(404)

    if o["status"] == "issued":
        c.close()
        return redirect(url_for("orders", tab="issued"))

    cur.execute("""
      SELECT oi.*, p.model, p.name
      FROM order_items oi
      JOIN products p ON p.id=oi.product_id
      WHERE oi.order_id=?
      ORDER BY oi.id
    """, (order_id,))
    its = cur.fetchall()

    changed_product_ids = []
    for it in its:
        pid = it["product_id"]
        qty = int(it["qty"])
        cur.execute("INSERT OR IGNORE INTO stock(product_id, qty) VALUES (?, 0)", (pid,))
        cur.execute("UPDATE stock SET qty = qty - ? WHERE product_id=?", (qty, pid))
        changed_product_ids.append(int(pid))

    cur.execute("UPDATE orders SET status='issued' WHERE id=?", (order_id,))
    c.commit()
    c.close()

    if supabase_enabled():
        supabase_update_rows("orders", {"status": "issued"}, {"id": order_id})
        sync_local_rows_to_supabase("stock", "product_id", changed_product_ids)

    return redirect(url_for("orders", tab="issued"))

@app.route("/orders/<int:order_id>/invoice", methods=["GET", "POST"])
def order_invoice(order_id):
    maybe_pull_shared_from_supabase()
    c = conn()
    cur = c.cursor()
    cur.execute("SELECT * FROM orders WHERE id=?", (order_id,))
    o = cur.fetchone()
    if not o:
        c.close()
        abort(404)

    cur.execute("""
      SELECT oi.*, p.model, p.name
      FROM order_items oi
      JOIN products p ON p.id=oi.product_id
      WHERE oi.order_id=?
      ORDER BY oi.id
    """, (order_id,))
    items = [dict(r) for r in cur.fetchall()]

    cur.execute("SELECT * FROM company_profile WHERE id=1")
    company = cur.fetchone()

    customer_row = None
    if o["customer_id"]:
        cur.execute("SELECT * FROM customers WHERE id=?", (o["customer_id"],))
        customer_row = cur.fetchone()
    if not customer_row:
        cur.execute("SELECT * FROM customers WHERE name=? ORDER BY id DESC LIMIT 1", (o["customer_name"],))
        customer_row = cur.fetchone()
    c.close()

    default_issue = datetime.now().strftime("%Y-%m-%d")
    # adres na fakturze zawsze preferuj z kartoteki klienta
    buyer_address_source = ""
    if customer_row:
        buyer_address_source = customer_row["address"] or ""
    if not buyer_address_source:
        buyer_address_source = (o["customer_address"] or "")
    st, pc, city = split_address(buyer_address_source)
    buyer_tax_no = ""
    if customer_row:
        buyer_tax_no = customer_row["nip"] or ""
    buyer_address_default = "\n".join([x for x in [st, f"{pc} {city}".strip()] if x]).strip()

    if request.method == "GET":
        data = {
            "invoice_no": next_invoice_no(default_issue),
            "place": "Kotuszów",
            "issue_date": default_issue,
            "sell_date": default_issue,
            "payment_type": "gotowka",
            "payment_to": (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d"),
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

        pdf_path, total_net, total_gross = generate_order_invoice_pdf(o, items, data)

        c = conn()
        cur = c.cursor()
        cur.execute("""
          INSERT INTO invoices(order_id, invoice_no, issue_date, sell_date, payment_type, payment_to,
                               buyer_name, buyer_tax_no, buyer_street, buyer_post_code, buyer_city, buyer_country,
                               buyer_email, buyer_phone, total_net, total_gross, created_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(invoice_no) DO UPDATE SET
            order_id=excluded.order_id,
            issue_date=excluded.issue_date,
            sell_date=excluded.sell_date,
            payment_type=excluded.payment_type,
            payment_to=excluded.payment_to,
            buyer_name=excluded.buyer_name,
            buyer_tax_no=excluded.buyer_tax_no,
            buyer_street=excluded.buyer_street,
            buyer_post_code=excluded.buyer_post_code,
            buyer_city=excluded.buyer_city,
            buyer_country=excluded.buyer_country,
            buyer_email=excluded.buyer_email,
            buyer_phone=excluded.buyer_phone,
            total_net=excluded.total_net,
            total_gross=excluded.total_gross,
            created_at=excluded.created_at
        """, (
            order_id, data["invoice_no"], data["issue_date"], data["sell_date"], data["payment_type"], data["payment_to"],
            data["buyer_name"], data["buyer_tax_no"], data["buyer_street"], data["buyer_post_code"], data["buyer_city"], data["buyer_country"],
            data["buyer_email"], data["buyer_phone"], total_net, total_gross, now_iso()
        ))
        c.commit()
        c.close()

        return send_file(pdf_path, mimetype="application/pdf", as_attachment=True, download_name=os.path.basename(pdf_path))

    tpl = r"""
    {% extends "base.html" %}
    {% block content %}
      <div class="card">
        <div class="flex">
          <h1 style="margin:0;">Faktura do zamówienia {{ o['order_no'] }}</h1>
          <a class="btn right" href="{{ url_for('order_view', order_id=o['id']) }}">← Szczegóły</a>
        </div>
      </div>

      <div class="card">
        <form method="post" class="row">
          <div><label class="muted small">Numer faktury</label><input name="invoice_no" value="{{ d['invoice_no'] }}" required></div>
          <div><label class="muted small">Miejsce</label><input name="place" value="{{ d['place'] }}"></div>
          <div><label class="muted small">Data wystawienia</label><input name="issue_date" type="date" value="{{ d['issue_date'] }}"></div>
          <div><label class="muted small">Data sprzedaży</label><input name="sell_date" type="date" value="{{ d['sell_date'] }}"></div>
          <div><label class="muted small">Forma płatności</label>
            <select name="payment_type">
              <option value="gotowka" {% if d['payment_type'] in ['cash','gotowka'] %}selected{% endif %}>gotówka</option>
              <option value="przelew" {% if d['payment_type'] in ['transfer','przelew'] %}selected{% endif %}>przelew</option>
              <option value="karta" {% if d['payment_type'] in ['card','karta'] %}selected{% endif %}>karta</option>
            </select>
          </div>
          <div><label class="muted small">Termin płatności</label><input name="payment_to" type="date" value="{{ d['payment_to'] }}"></div>
          <div><label class="muted small">Rabat %</label><input name="discount_percent" value="{{ d['discount_percent'] or "0" }}"></div>

          <div><label class="muted small">Nabywca</label><input name="buyer_name" value="{{ d['buyer_name'] }}" required></div>
          <div><label class="muted small">NIP nabywcy</label><input name="buyer_tax_no" value="{{ d['buyer_tax_no'] }}"></div>
          <div><label class="muted small">Adres nabywcy</label><textarea name="buyer_address" placeholder="Ulica&#10;Kod pocztowy Miasto">{{ d['buyer_address'] }}</textarea></div>
          <div><label class="muted small">Kraj</label><input name="buyer_country" value="{{ d['buyer_country'] }}"></div>
          <div><label class="muted small">Email</label><input name="buyer_email" value="{{ d['buyer_email'] }}"></div>
          <div><label class="muted small">Telefon</label><input name="buyer_phone" value="{{ d['buyer_phone'] }}"></div>

          <div class="flex" style="align-items:flex-end;">
            <button class="btn primary" type="submit">Generuj fakturę PDF</button>
          </div>
        </form>
      </div>
    {% endblock %}
    """
    return render_template_string(tpl, title="Faktura", base_url=BASE_URL, db_path=DB_PATH, o=o, d=data, company=company)


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
    cpdf.drawString(15 * mm, y, f"Wydruk zamówienia: {o['order_no']}")
    y -= 7 * mm
    cpdf.setFont(pdf_font, 10)
    cpdf.drawString(15 * mm, y, f"Klient: {o['customer_name']}")
    y -= 5 * mm
    cpdf.drawString(15 * mm, y, f"Data: {o['created_at']}")
    y -= 6 * mm
    cpdf.setFont(pdf_font_bold, 10)
    cpdf.drawString(15 * mm, y, f"Łączna liczba sztuk w zamówieniu: {total_qty}")
    y -= 5 * mm
    cpdf.setFont(pdf_font, 10)
    cpdf.drawString(15 * mm, y, f"Łączny brak na stanie: {total_missing_qty}")

    def draw_section(title, rows, y_pos, show_missing=False):
        cpdf.setFont(pdf_font_bold, 11)
        cpdf.drawString(15 * mm, y_pos, title)
        y_pos -= 6 * mm
        cpdf.setFont(pdf_font_bold, 9)
        cpdf.drawString(15 * mm, y_pos, "SKU")
        cpdf.drawString(55 * mm, y_pos, "Model/Nazwa")
        cpdf.drawString(160 * mm, y_pos, "Ilość")
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
    fname = safe_filename(o["order_no"]) + "_druk_zamowienia.pdf"
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

    # QR ma prowadzić do szczegółów zamówienia
    url = build_public_url(url_for("order_view", order_id=order_id))

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=1
    )
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")

    # PDF 30x50 mm
    w = 30 * mm
    h = 50 * mm

    buf = io.BytesIO()
    cpdf = canvas.Canvas(buf, pagesize=(w, h))

    # Umieszczenie QR
    qr_buf = io.BytesIO()
    img.save(qr_buf, format="PNG")
    qr_buf.seek(0)
    qr_img = ImageReader(qr_buf)

    # QR na górze (większy), dane poniżej
    margin = 2 * mm
    qr_size = 26 * mm  # zostaje margines
    cpdf.drawImage(qr_img, margin, h - margin - qr_size, width=qr_size, height=qr_size, preserveAspectRatio=True, mask='auto')

    # Dane zamawiającego + nr zamówienia
    pdf_font, pdf_font_bold = get_pdf_font_names()
    text_y = h - margin - qr_size - 2*mm
    cpdf.setFont(pdf_font_bold, 6.8)
    cpdf.drawString(margin, text_y, (o["customer_name"] or "")[:40])

    cpdf.setFont(pdf_font_bold, 6.3)
    cpdf.drawString(margin, text_y - 3.2*mm, f"Nr: {o['order_no']}")

    cpdf.setFont(pdf_font, 6.3)
    addr = (o["customer_address"] or "").strip()
    phone = (o["customer_phone"] or "").strip()

    lines = []
    if addr:
        # podziel na linie i dodatkowo łam długie
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

    y = text_y - 6.8*mm
    for ln in lines[:6]:
        cpdf.drawString(margin, y, ln)
        y -= 3.2*mm

    cpdf.showPage()
    cpdf.save()
    buf.seek(0)

    fname = safe_filename(o["order_no"]) + "_label_30x50.pdf"
    return send_file(buf, mimetype="application/pdf", as_attachment=True, download_name=fname)


@app.get("/api/order_lookup")
def api_order_lookup():
    maybe_pull_shared_from_supabase()
    token = norm(request.args.get("token"))
    if not token:
        return jsonify(ok=False, error="Brak tokenu"), 400

    c = conn()
    cur = c.cursor()
    cur.execute("SELECT * FROM orders WHERE order_no=? LIMIT 1", (token,))
    o = cur.fetchone()
    if not o:
        c.close()
        return jsonify(ok=False, error="Nie znaleziono zamówienia"), 404

    cur.execute("""
      SELECT oi.*, p.model, p.ean, p.name,
             COALESCE(pr.net_price, 0) AS net_price,
             COALESCE(pr.gross_price, 0) AS gross_price,
             (oi.qty * COALESCE(pr.net_price, 0)) AS line_value_net,
             (oi.qty * COALESCE(pr.gross_price, 0)) AS line_value_gross
      FROM order_items oi
      JOIN products p ON p.id=oi.product_id
      LEFT JOIN pricing pr ON (TRIM(LOWER(pr.model)) = TRIM(LOWER(p.model)) OR TRIM(LOWER(pr.model)) = TRIM(LOWER(p.sku)))
      WHERE oi.order_id=?
      ORDER BY oi.id
    """, (o["id"],))
    items = [dict(r) for r in cur.fetchall()]
    c.close()

    total_net = round(sum(float(it.get("line_value_net") or 0) for it in items), 2)
    total_gross = round(sum(float(it.get("line_value_gross") or 0) for it in items), 2)

    return jsonify(
        ok=True,
        order={
            "id": o["id"],
            "order_no": o["order_no"],
            "status": o["status"],
            "created_at": o["created_at"],
            "customer_name": o["customer_name"],
            "customer_address": o["customer_address"],
            "customer_phone": o["customer_phone"],
            "customer_email": o["customer_email"],
            "note": o["note"],
            "qr_data_url": o["qr_data_url"] or "",
            "total_net": total_net,
            "total_gross": total_gross,
        },
        items=items
    )


@app.get("/orders/by-code/<path:token>")
def order_by_code(token):
    maybe_pull_shared_from_supabase()
    c = conn()
    cur = c.cursor()
    cur.execute("SELECT id FROM orders WHERE order_no=? LIMIT 1", (norm(token),))
    row = cur.fetchone()
    c.close()
    if not row:
        return "Nie znaleziono zamówienia", 404
    return redirect(url_for("order_view", order_id=row["id"]))


@app.get("/orders/scan")
def order_scan():
    tpl = r"""
    {% extends "base.html" %}
    {% block content %}
      <div class="card">
        <h1>Skan QR zamówienia</h1>
        <div class="muted">Zeskanuj kod aparatem albo wklej numer zamówienia ZAM-...</div>
      </div>

      <div class="card">
        <div class="row">
          <div>
            <label class="muted small">Numer / kod zamówienia</label>
            <input id="manualToken" placeholder="np. ZAM-20260329-000018">
          </div>
          <div class="flex" style="align-items:flex-end;">
            <button class="btn primary" onclick="openOrderByCode(); return false;">Pokaż zamówienie</button>
          </div>
        </div>
      </div>

      <div class="card">
        <h2>Kamera</h2>
        <div id="reader" style="width:100%;max-width:520px;"></div>
        <div class="muted" id="scanMsg" style="margin-top:8px;"></div>
      </div>

      <script src="https://unpkg.com/html5-qrcode" type="text/javascript"></script>
      <script>
        function openOrderByCode(raw){
          const value = (raw || document.getElementById('manualToken').value || '').trim();
          if(!value){
            document.getElementById('scanMsg').innerText = 'Wpisz albo zeskanuj kod.';
            return;
          }
          window.location.href = '/orders/by-code/' + encodeURIComponent(value);
        }

        function onScanSuccess(decodedText){
          document.getElementById('scanMsg').innerText = 'Odczytano: ' + decodedText;
          openOrderByCode(decodedText);
        }

        window.addEventListener('load', function(){
          if(window.Html5QrcodeScanner){
            const scanner = new Html5QrcodeScanner('reader', { fps: 10, qrbox: 220 });
            scanner.render(onScanSuccess, function(){});
          } else {
            document.getElementById('scanMsg').innerText = 'Brak biblioteki skanera.';
          }
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
    maybe_pull_shared_from_supabase()
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
        <div class="muted">Zarządzaj przesyłkami: status, tracking i zawartość paczki. Tracking otwiera 17TRACK.</div>
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
          <div class="flex" style="align-items:flex-end;">
            <button class="btn primary" type="submit">Zapisz</button>
          </div>
        </form>
      </div>

      <div class="card">
        <h2>Paczki (max 200)</h2>
        <table>
          <thead>
            <tr><th>Nr</th><th>Status</th><th>Tracking</th><th>Notatka</th><th>Data</th><th>Akcje</th></tr>
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
                    <button class="btn" type="submit">Zmień</button>
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
                <td>{{ p['note'] or "-" }}</td>
                <td class="muted">{{ p['created_at'] }}</td>
                <td><a class="btn primary" href="{{ url_for('china_package', package_id=p['id']) }}">Zawartość</a></td>
              </tr>
            {% endfor %}
            {% if not packs %}
              <tr><td colspan="6" class="muted">Brak paczek.</td></tr>
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

    if not package_no:
        return "Brak numeru paczki", 400

    c = conn()
    cur = c.cursor()
    try:
        cur.execute("""
          INSERT INTO china_packages(package_no, status, tracking, note, created_at)
          VALUES(?,?,?,?,?)
        """, (package_no, status, tracking, note, now_iso()))
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
        return "Nieprawidłowy status", 400

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

    # Przejście NA arrived: fizycznie przyjęto towar -> dodaj na stan.
    if old_status != "arrived" and status == "arrived":
        for it in items:
            pid = it["product_id"]
            qty = int(it["qty"])
            cur.execute("INSERT OR IGNORE INTO stock(product_id, qty) VALUES (?, 0)", (pid,))
            cur.execute("UPDATE stock SET qty = qty + ? WHERE product_id=?", (qty, pid))

    # Cofnięcie Z arrived na inny status: towar wraca jako "w drodze" -> odejmij ze stanu.
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

@app.get("/china/<int:package_id>")
def china_package(package_id):
    maybe_pull_shared_from_supabase()
    c = conn()
    cur = c.cursor()
    cur.execute("SELECT * FROM china_packages WHERE id=?", (package_id,))
    pack = cur.fetchone()
    if not pack:
        c.close()
        abort(404)

    cur.execute("SELECT id, sku, model, name FROM products ORDER BY sku LIMIT 5000")
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
          <a class="btn right" href="{{ url_for('china') }}">← Lista paczek</a>
        </div>
        <div class="muted">Tracking: {{ pack['tracking'] or '-' }}</div>
        <form method="post" action="{{ url_for('china_tracking', package_id=pack['id']) }}" class="flex" style="margin-top:10px;">
          <input name="tracking" value="{{ pack['tracking'] or '' }}" placeholder="nr trackingu" style="width:260px;">
          <button class="btn" type="submit">Zmień tracking</button>
          {% if pack['tracking'] %}
            <a class="btn" target="_blank" href="https://t.17track.net/en#nums={{ pack['tracking']|urlencode }}">Otwórz 17TRACK</a>
          {% endif %}
        </form>
      </div>

      <div class="card">
        <h2>Dodaj zawartość paczki</h2>
        <form method="post" action="{{ url_for('china_item_add', package_id=pack['id']) }}" class="items-row">
          <div>
            <label class="muted small">Produkt</label>
            <select name="product_id" required>
              <option value="">-- wybierz --</option>
              {% for p in products %}
                <option value="{{ p['id'] }}">{{ p['sku'] }}{% if p['model'] %} • {{ p['model'] }}{% endif %}{% if p['name'] %} • {{ p['name'] }}{% endif %}</option>
              {% endfor %}
            </select>
          </div>
          <div>
            <label class="muted small">Ilość</label>
            <input name="qty" value="1" required>
          </div>
          <div class="flex" style="align-items:flex-end;">
            <button class="btn primary" type="submit">Dodaj</button>
          </div>
        </form>
      </div>

      <div class="card">
        <h2>Zawartość paczki</h2>
        <table>
          <thead>
            <tr><th>SKU</th><th>Model / Nazwa</th><th>Ilość</th><th>Data</th><th>Akcje</th></tr>
          </thead>
          <tbody>
            {% for it in items %}
              <tr>
                <td><b>{{ it['sku'] }}</b></td>
                <td>{{ it['model'] or '' }}{% if it['name'] %}<div class="muted">{{ it['name'] }}</div>{% endif %}</td>
                <td><span class="badge">{{ it['qty'] }}</span></td>
                <td class="muted">{{ it['created_at'] }}</td>
                <td>
                  <form method="post" action="{{ url_for('china_item_delete', package_id=pack['id'], item_id=it['id']) }}" onsubmit="return confirm('Usunąć pozycję?')">
                    <button class="btn danger" type="submit">Usuń</button>
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

@app.post("/china/<int:package_id>/items/add")
def china_item_add(package_id):
    product_id = to_int(request.form.get("product_id"), 0)
    qty = to_int(request.form.get("qty"), 0)
    if product_id <= 0 or qty <= 0:
        return "Nieprawidłowy produkt lub ilość", 400

    c = conn()
    cur = c.cursor()
    cur.execute("SELECT sku FROM products WHERE id=?", (product_id,))
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
    # debug=True możesz zostawić na czas budowy
    app.run(host="0.0.0.0", port=5000, debug=True)
