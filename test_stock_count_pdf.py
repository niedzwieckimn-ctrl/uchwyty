"""Counting sheets read every active physical-stock row without business writes."""
from datetime import datetime
from io import BytesIO
import re
import time

import pdfplumber
from pypdf import PdfReader
import pytest

import app as backend
import routes.inventory as inventory_routes
from stock_count_pdf import render_stock_count_pdf
from test_business_operations import isolated, _actor


PATH = "/stock/count-sheet.pdf"
STAMP = datetime.fromisoformat("2026-10-19T14:05:06+02:00")


def synthetic_items(count=137):
    return [
        {"sku": f"TEST-SKU-{i:04d}", "model": f"Model {i:04d}",
         "name": "Uchwyt Łódź, żółty mat", "ean": f"5901234{i:06d}",
         "stock_qty": i % 37}
        for i in range(1, count + 1)
    ]


def seed(items, *, archived=()):
    db = backend.conn()
    try:
        for pid, item in enumerate(items, 1):
            db.execute(
                "INSERT INTO products(id,sku,model,name,ean,archived,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (pid, item["sku"], item.get("model", ""), item.get("name", ""),
                 item.get("ean", ""), int(pid in archived), STAMP.isoformat()),
            )
            if item.get("stock_qty") is not None:
                db.execute("INSERT INTO stock(product_id,qty) VALUES(?,?)",
                           (pid, item["stock_qty"]))
        db.commit()
    finally:
        db.close()


def database_dump():
    db = backend.conn()
    try:
        # Freshness may lazily create/update its own metadata table. Every other
        # schema and business-data statement must remain byte-for-byte identical.
        return tuple(statement for statement in db.iterdump()
                     if not statement.startswith(("CREATE TABLE local_sync_state(",
                                                  'INSERT INTO "local_sync_state"')))
    finally:
        db.close()


@pytest.fixture
def sheet(isolated, monkeypatch):
    monkeypatch.setattr(backend, "supabase_enabled", lambda: False)
    monkeypatch.setattr(backend, "app_now", lambda: STAMP)

    def forbidden(*args, **kwargs):
        pytest.fail("A counting PDF must not use broad sync, analytics or remote writes")

    for name in ("maybe_pull_shared_from_supabase", "build_replenishment_analysis",
                 "pull_shared_tables_from_supabase", "supabase_upsert_rows",
                 "supabase_update_rows", "supabase_delete_rows"):
        monkeypatch.setattr(backend, name, forbidden)
    with isolated.session_transaction() as state:
        state["admin_authenticated"] = True
        state["csrf_token"] = "stock-sheet-test"
    return isolated


def pdf_text(response):
    assert response.status_code == 200
    assert response.mimetype == "application/pdf"
    assert response.data.startswith(b"%PDF-")
    return "\n".join(page.extract_text() for page in PdfReader(BytesIO(response.data)).pages)


def test_all_active_skus_ignore_filters_and_pagination_without_business_writes(sheet, monkeypatch):
    items = synthetic_items()
    items[0]["stock_qty"] = 0
    items[1]["stock_qty"] = None  # A catalog entry without a stock row still belongs in the sheet.
    items[2]["stock_qty"] = -3  # The report must not silently clamp recorded quantities.
    seed(items + [{"sku": "ARCHIVED-EXCLUDED", "stock_qty": 0}], archived={138})
    captured = []
    original = inventory_routes.render_stock_count_pdf

    def render(rows, *args, **kwargs):
        captured.extend(dict(row) for row in rows)
        return original(rows, *args, **kwargs)

    monkeypatch.setattr(inventory_routes, "render_stock_count_pdf", render)
    before = database_dump()
    response = sheet.get(PATH + "?q=does-not-exist&filter=missing&page=999&per_page=1")
    text = pdf_text(response)

    assert len(captured) == 137
    assert [row["sku"] for row in captured] == [item["sku"] for item in items]
    assert [row["stock_qty"] for row in captured[:3]] == [0, 0, -3]
    assert all(item["sku"] in text for item in items)
    assert "ARCHIVED-EXCLUDED" not in text
    assert "Liczba pozycji: 137" in text
    assert "attachment" in response.headers["Content-Disposition"]
    assert "spis-z-natury-2026-10-19_14-05-06.pdf" in response.headers["Content-Disposition"]
    assert response.headers["Cache-Control"] == "private, no-store"
    assert database_dump() == before


def test_empty_local_catalog_is_a_valid_blank_sheet(sheet):
    response = sheet.get(PATH)
    text = pdf_text(response)
    assert "Brak aktywnych pozycji." in text
    assert "Liczba pozycji: 0" in text


def test_anonymous_download_redirects_to_login_before_any_freshness_read(sheet, monkeypatch):
    monkeypatch.setattr(backend, "ensure_business_operation_freshness",
                        lambda *args: pytest.fail("Anonymous request reached stock read"))
    response = backend.app.test_client().get(PATH)
    assert response.status_code == 302
    assert "/login?next=" in response.headers["Location"]
    assert response.mimetype != "application/pdf"


def test_accounting_role_cannot_download_or_escalate_permission(sheet, monkeypatch):
    actor = _actor("HUMAN", "KSIEGOWOSC")
    with sheet.session_transaction() as state:
        state["internal_actor_id"] = actor.actor_id
    monkeypatch.setattr(backend, "ensure_business_operation_freshness",
                        lambda *args: pytest.fail("Denied actor reached stock read"))
    response = sheet.get(PATH + "?permission=inventory.read&role=OWNER", headers={"X-Role": "OWNER"})
    assert response.status_code == 403
    assert response.mimetype != "application/pdf"
    assert backend.app.view_functions["stock_count_sheet_pdf"].required_permission == "inventory.read"


def test_warehouse_role_can_download(sheet):
    actor = _actor("HUMAN", "MAGAZYN")
    with sheet.session_transaction() as state:
        state["internal_actor_id"] = actor.actor_id
    seed([{"sku": "WAREHOUSE-ALLOWED", "stock_qty": 5}])
    assert "WAREHOUSE-ALLOWED" in pdf_text(sheet.get(PATH))


def remote_catalog(monkeypatch):
    monkeypatch.setattr(backend, "supabase_enabled", lambda: True)
    monkeypatch.setattr(backend, "BUSINESS_FRESHNESS_TTL_SECONDS", 45)
    calls = []
    rows = {
        "products": [{"id": 1, "sku": "REMOTE-ONLY", "model": "Model żółty",
                      "name": "Łódź", "ean": "5900000000001", "archived": 0,
                      "created_at": STAMP.isoformat()}],
        "stock": [{"product_id": 1, "qty": 29}],
    }

    def select(table, **kwargs):
        calls.append(table)
        assert table in rows, "The stock sheet read an unrelated remote table"
        return [dict(row) for row in rows[table]]

    monkeypatch.setattr(backend, "supabase_select_rows", select)
    return calls


def test_cold_remote_read_fetches_exactly_products_and_stock_then_uses_fresh_cache(sheet, monkeypatch):
    calls = remote_catalog(monkeypatch)
    first = sheet.get(PATH)
    assert "REMOTE-ONLY" in pdf_text(first)
    assert calls == ["products", "stock"]
    db = backend.conn()
    try:
        assert db.execute("SELECT qty FROM stock WHERE product_id=1").fetchone()[0] == 29
    finally:
        db.close()
    assert "REMOTE-ONLY" in pdf_text(sheet.get(PATH))
    assert calls == ["products", "stock"]


def test_failed_warm_refresh_refuses_pdf_and_preserves_existing_stock(sheet, monkeypatch):
    remote_catalog(monkeypatch)
    assert sheet.get(PATH).status_code == 200
    backend._mark_business_freshness("inventory_catalog", time.time() - 600)
    before = database_dump()

    def offline(*args, **kwargs):
        raise RuntimeError("Synthetic offline error")

    monkeypatch.setattr(backend, "supabase_select_rows", offline)
    monkeypatch.setattr(inventory_routes, "render_stock_count_pdf",
                        lambda *args, **kwargs: pytest.fail("Stale stock was sent to the PDF renderer"))
    response = sheet.get(PATH)
    assert response.status_code == 503
    assert response.mimetype == "text/html"
    assert "Nie udało się pobrać aktualnych stanów" in response.get_data(as_text=True)
    assert response.headers["Cache-Control"] == "private, no-store"
    assert not response.data.startswith(b"%PDF-")
    assert database_dump() == before


def test_failed_cold_refresh_refuses_pdf_instead_of_reporting_zero_stock(sheet, monkeypatch):
    remote_catalog(monkeypatch)

    def offline(*args, **kwargs):
        raise RuntimeError("Synthetic offline error")

    monkeypatch.setattr(backend, "supabase_select_rows", offline)
    monkeypatch.setattr(inventory_routes, "render_stock_count_pdf",
                        lambda *args, **kwargs: pytest.fail("Unavailable stock was rendered as zero"))
    response = sheet.get(PATH)
    assert response.status_code == 503
    assert response.mimetype == "text/html"
    assert "Nie udało się pobrać aktualnych stanów" in response.get_data(as_text=True)
    assert "Content-Disposition" not in response.headers
    db = backend.conn()
    try:
        assert db.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM stock").fetchone()[0] == 0
    finally:
        db.close()


def test_renderer_wraps_long_text_and_repeats_readable_polish_headers_on_pages():
    items = synthetic_items(61)
    items[0]["name"] = "Zażółć gęślą jaźń <bezpieczne> & Łódź " * 5
    items[0]["model"] = "Bardzo długi opis wariantu do sprawdzenia zawijania wierszy"
    items[1]["sku"] = "DŁUGIE-SKU-" + "ABC123" * 11
    stream = render_stock_count_pdf(items, STAMP, backend.get_pdf_font_names(),
                                   {"company_name": "Próbna firma Łódź"})
    with pdfplumber.open(stream) as pdf:
        assert len(pdf.pages) >= 3
        joined = "\n".join(page.extract_text() for page in pdf.pages)
        assert "Zażółć gęślą jaźń <bezpieczne> & Łódź" in re.sub(r"\s+", " ", joined)
        assert "Próbna firma Łódź" in joined
        assert "Liczba pozycji: 61" in joined
        assert "\u25a0" not in joined and "\ufffd" not in joined
        sku_cells = []
        for index, page in enumerate(pdf.pages, 1):
            text = page.extract_text()
            assert "ARKUSZ SPISU Z NATURY" in text
            assert "19.10.2026 14:05:06 (czas polski)" in text
            assert f"Strona {index}" in text
            assert "Arkusz do liczenia - wszystkie aktywne pozycje magazynu" in text
            assert all(30 <= char["x0"] <= char["x1"] <= page.width - 30
                       for char in page.chars if char["text"].strip())
            if re.search(r"TEST-SKU-\d{4}", text):
                tables = page.extract_tables()
                assert tables
                assert tables[0][0] == ["Lp.", "SKU / EAN", "Model / nazwa", "J.m.",
                                         "Stan\nsystemowy", "Policzono", "Uwagi"]
                sku_cells.extend(re.sub(r"\s+", "", row[1] or "") for row in tables[0][1:])
        # Read within cells so wrapped SKU text is not interleaved with the
        # adjacent description by the PDF's page-level reading order.
        assert all(any(item["sku"] in cell for cell in sku_cells) for item in items)
