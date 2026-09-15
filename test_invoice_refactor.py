import copy
import json
import xml.etree.ElementTree as ET
from datetime import timedelta

import pytest
from pypdf import PdfReader

import app as backend
import invoice_numbering
from invoice_types import resolve_invoice_type
import ksef_foreign
from ksef_module import FA3_NS, validate_fa3_xml
import routes.invoices as invoice_routes


COMPANY = {
    "company_name": "Sprzedawca Test", "nip": "1234567890",
    "address": "Testowa 1", "city": "Warszawa", "bank_account": "",
}
ITEMS = [{
    "name": "Uchwyt", "model": "M1", "sku": "SKU-1", "qty": 2,
    "net_price": 10, "line_value_net": 20, "vat_rate": 0, "currency": "EUR",
}]


@pytest.fixture()
def numbering_db(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "DB_PATH", str(tmp_path / "numbering.db"))
    monkeypatch.setattr(backend, "supabase_enabled", lambda: False)
    backend.init_db()
    db = backend.conn()
    db.execute(
        "INSERT INTO orders(id,order_no,customer_name,status,created_at) VALUES(1,'ZAM-NUM','Test','new',?)",
        (backend.now_iso(),),
    )
    db.commit()
    db.close()
    return tmp_path


def _store_number(number, *, invoice_id=None):
    db = backend.conn()
    columns = "id,order_id," if invoice_id is not None else "order_id,"
    placeholders = "?,?," if invoice_id is not None else "?,"
    values = (invoice_id, 1) if invoice_id is not None else (1,)
    db.execute(
        f"INSERT INTO invoices({columns}invoice_no,issue_date,sell_date,payment_type,total_net,total_gross,created_at) "
        f"VALUES({placeholders}?,?,?,?,?,?,?)",
        values + (number, "2026-09-15", "2026-09-15", "przelew", 0, 0, backend.now_iso()),
    )
    db.commit()
    db.close()


@pytest.mark.parametrize(("used", "expected"), [
    ([1, 2, 3], 4),
    ([1, 2, 5], 6),
    ([1, 2, 5, 9], 10),
])
def test_next_invoice_number_uses_highest_sequence_not_record_count(numbering_db, used, expected):
    for sequence in used:
        _store_number(f"FVAT {sequence}/09/2026")
    assert invoice_numbering.preview(backend, "2026-09-15") == f"FVAT {expected}/09/2026"


def test_deleted_highest_invoice_number_is_not_reused(numbering_db):
    for sequence in (1, 2, 5):
        _store_number(f"FVAT {sequence}/09/2026")
    db = backend.conn()
    db.execute("DELETE FROM invoices WHERE invoice_no='FVAT 5/09/2026'")
    db.commit()
    db.close()
    assert invoice_numbering.preview(backend, "2026-09-15") == "FVAT 6/09/2026"


def test_manual_standard_number_is_exact_and_advances_next_auto_number(numbering_db):
    number = invoice_numbering.reserve(
        backend, "2026-09-15", "FVAT 20/09/2026", manual=True,
    )
    assert number == "FVAT 20/09/2026"
    _store_number(number)
    db = backend.conn()
    assert db.execute("SELECT invoice_no FROM invoices").fetchone()[0] == "FVAT 20/09/2026"
    db.close()
    assert invoice_numbering.reserve(backend, "2026-09-15") == "FVAT 21/09/2026"


def test_duplicate_manual_number_is_rejected_without_second_invoice(numbering_db):
    _store_number("FVAT 20/09/2026")
    with pytest.raises(ValueError, match="już wykorzystany"):
        invoice_numbering.reserve(
            backend, "2026-09-15", "FVAT 20/09/2026", manual=True,
        )
    db = backend.conn()
    assert db.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == 1
    db.close()


def test_manual_standard_number_is_sent_to_supabase_as_exact_claim(numbering_db, monkeypatch):
    captured = {}

    def rpc(path, method="GET", payload=None, **_kwargs):
        captured.update(path=path, method=method, payload=payload)
        return payload["p_custom"]

    monkeypatch.setattr(backend, "supabase_enabled", lambda: True)
    monkeypatch.setattr(backend, "supabase_request", rpc)
    assert invoice_numbering.reserve(
        backend, "2026-09-15", "FVAT 20/09/2026", manual=True,
    ) == "FVAT 20/09/2026"
    assert captured == {
        "path": "/rest/v1/rpc/reserve_invoice_number",
        "method": "POST",
        "payload": {
            "p_period": "09/2026",
            "p_custom": "FVAT 20/09/2026",
            "p_requested_min": 0,
        },
    }


def foreign_invoice(kind="wdt"):
    return {
        "invoice_no": "FV/TEST/1", "issue_date": "2026-09-03",
        "sell_date": "2026-09-03", "place": "Kotuszów",
        "buyer_name": "Test Buyer", "buyer_street": "Street 1",
        "buyer_post_code": "10115", "buyer_city": "Berlin",
        "buyer_country": "DE", "buyer_tax_no": "DE123456789",
        "currency": "EUR", "invoice_type": kind, "payment_type": "transfer",
        "payment_to": "2026-09-10", "paid": 0,
    }


def xml_values(xml):
    root = ET.fromstring(xml)
    ns = {"f": FA3_NS}
    return root, ns


def test_legacy_type_resolution_does_not_mutate_record():
    old = {"invoice_no": "FV/HISTORY", "currency": "PLN"}
    before = copy.deepcopy(old)
    assert resolve_invoice_type(old, [{"vat_rate": 23}]) == "domestic"
    assert old == before
    assert resolve_invoice_type({"currency": "EUR"}, [{"vat_rate": 0}]) == "wdt"


def test_wdt_fa3_currency_rate_identity_and_totals():
    xml = ksef_foreign.generate(foreign_invoice(), COMPANY, ITEMS)
    root, ns = xml_values(xml)
    assert root.findtext(".//f:KodWaluty", namespaces=ns) == "EUR"
    assert root.findtext(".//f:P_1M", namespaces=ns) == "Kotuszów"
    assert root.findtext(".//f:P_12", namespaces=ns) == "0 WDT"
    assert root.findtext(".//f:KodUE", namespaces=ns) == "DE"
    assert root.findtext(".//f:NrVatUE", namespaces=ns) == "123456789"
    assert root.find(".//f:Podmiot2/f:DaneIdentyfikacyjne/f:NIP", ns) is None
    assert root.findtext(".//f:P_13_6_2", namespaces=ns) == "20.00"
    assert root.findtext(".//f:P_15", namespaces=ns) == "20.00"
    assert validate_fa3_xml(xml, backend.ksef_schema_path()) == []


def test_export_fa3_uses_0_ex_and_foreign_tax_id():
    inv = foreign_invoice("export")
    inv.update(buyer_country="US", buyer_tax_no="US-99-123", currency="USD")
    items = [dict(ITEMS[0], currency="USD")]
    xml = ksef_foreign.generate(inv, COMPANY, items)
    root, ns = xml_values(xml)
    assert root.findtext(".//f:KodWaluty", namespaces=ns) == "USD"
    assert root.findtext(".//f:P_12", namespaces=ns) == "0 EX"
    assert "0 WDT" not in xml
    assert root.findtext(".//f:Podmiot2/f:DaneIdentyfikacyjne/f:KodKraju", namespaces=ns) == "US"
    assert root.findtext(".//f:Podmiot2/f:DaneIdentyfikacyjne/f:NrID", namespaces=ns) == "US99123"
    assert root.find(".//f:Podmiot2/f:DaneIdentyfikacyjne/f:NIP", ns) is None
    assert root.find(".//f:Podmiot2/f:DaneIdentyfikacyjne/f:KodUE", ns) is None
    assert root.findtext(".//f:P_13_6_3", namespaces=ns) == "20.00"
    assert validate_fa3_xml(xml, backend.ksef_schema_path()) == []


def test_paid_foreign_invoice_has_no_active_payment_section():
    inv = foreign_invoice()
    inv["paid"] = 1
    xml = ksef_foreign.generate(inv, COMPANY, ITEMS)
    root, ns = xml_values(xml)
    assert root.find(".//f:Platnosc", ns) is None


@pytest.mark.parametrize("kind,country,tax_id", [
    ("wdt", "US", "US123456"),
    ("export", "DE", "DE123456789"),
])
def test_foreign_type_country_mismatch_is_rejected(kind, country, tax_id):
    inv = foreign_invoice(kind)
    inv.update(buyer_country=country, buyer_tax_no=tax_id)
    assert ksef_foreign.validate(inv, COMPANY, ITEMS)


def test_xml_generation_is_pure_for_inputs():
    inv, company, items = foreign_invoice(), copy.deepcopy(COMPANY), copy.deepcopy(ITEMS)
    before = copy.deepcopy((inv, company, items))
    ksef_foreign.generate(inv, company, items)
    assert (inv, company, items) == before


@pytest.fixture()
def historical_db(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "DB_PATH", str(tmp_path / "history.db"))
    monkeypatch.setattr(backend, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(backend, "supabase_enabled", lambda: False)
    backend.init_db()
    c = backend.conn()
    c.execute("INSERT INTO products(id,sku,model,name,created_at) VALUES(1,'SKU-1','M1','Uchwyt',?)", (backend.now_iso(),))
    c.execute("INSERT INTO stock(product_id,qty) VALUES(1,17)")
    c.execute("INSERT INTO customers(id,name,address,email,language,price_list,created_at) VALUES(1,'Kunde','Street 1','buyer@example.com','de','eu_eur',?)", (backend.now_iso(),))
    c.execute("INSERT INTO orders(id,order_no,customer_id,customer_name,customer_address,customer_email,status,created_at,warehouse_issued,currency,tracking_no) VALUES(1,'ZAM-1',1,'Kunde','Street 1','buyer@example.com','shipped',?,1,'EUR','TRACK-1')", (backend.now_iso(),))
    c.execute("INSERT INTO order_items(id,order_id,product_id,sku,qty,unit_net_price,currency,created_at) VALUES(1,1,1,'SKU-1',2,10,'EUR',?)", (backend.now_iso(),))
    c.execute("INSERT INTO company_profile(id,company_name,address,nip,updated_at) VALUES(1,'Sprzedawca','Testowa 1','1234567890',?)", (backend.now_iso(),))
    issue_date = backend.app_now().date()
    # Historical row intentionally has NULL invoice_type.
    c.execute("INSERT INTO invoices(id,order_id,invoice_no,issue_date,sell_date,payment_type,payment_to,buyer_name,buyer_tax_no,buyer_street,buyer_post_code,buyer_city,buyer_country,buyer_email,total_net,total_gross,created_at) VALUES(1,1,'FV/HIST/1',?,?, 'przelew',?,'Kunde','DE123456789','Street 1','10115','Berlin','DE','buyer@example.com',20,20,?)", (issue_date.isoformat(), issue_date.isoformat(), (issue_date + timedelta(days=7)).isoformat(), backend.now_iso()))
    saved_items = [dict(ITEMS[0], order_id=1, source_order_id=1, order_item_id=1)]
    c.execute("INSERT INTO invoice_meta(invoice_id,pdf_path,invoice_items_json,sent_to_client,seen_by_client,payment_reminder,paid,paid_at,seen_at,updated_at) VALUES(1,'',?,1,1,0,1,?, ?, ?)", (json.dumps(saved_items), backend.now_iso(), backend.now_iso(), backend.now_iso()))
    c.execute("INSERT INTO invoice_allocations(id,invoice_id,order_id,order_item_id,product_id,sku,qty,created_at) VALUES(1,1,1,1,1,'SKU-1',2,?)", (backend.now_iso(),))
    c.execute("INSERT INTO packing_batches(id,root_order_id,invoice_id,created_at) VALUES(1,1,1,?)", (backend.now_iso(),))
    c.execute("INSERT INTO packing_allocations(id,batch_id,order_id,order_item_id,qty,created_at) VALUES(1,1,1,1,2,?)", (backend.now_iso(),))
    c.commit(); c.close()
    return tmp_path


PROTECTED_TABLES = ("stock", "orders", "order_items", "invoice_allocations", "packing_batches", "packing_allocations")


def protected_snapshot():
    c = backend.conn()
    result = {}
    for table in PROTECTED_TABLES:
        result[table] = [tuple(row) for row in c.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()]
    c.close()
    return result


def _invoice_edit_form(invoice_no):
    return {
        "csrf_token": "test",
        "invoice_no": invoice_no,
        "issue_date": "2026-09-15",
        "sell_date": "2026-09-15",
        "payment_type": "przelew",
        "payment_to": "2026-09-22",
        "buyer_name": "Kunde",
        "buyer_tax_no": "DE123456789",
        "buyer_address": "Street 1\n10115 Berlin",
        "buyer_country": "DE",
        "buyer_email": "buyer@example.com",
        "buyer_phone": "",
        "invoice_type": "wdt",
        "currency": "EUR",
        "invoice_qty_1": "2",
    }


def _invoice_create_form(invoice_no, *, manual="1"):
    data = _invoice_edit_form(invoice_no)
    data.update({
        "place": "Kotuszów",
        "discount_percent": "0",
        "invoice_no_manual": manual,
        "suggested_invoice_no": "FVAT 1/09/2026",
        "submit_action": "invoice",
        "invoice_qty_2": "1",
    })
    data.pop("invoice_qty_1")
    return data


def _invoice_edit_client(monkeypatch):
    monkeypatch.setattr(backend.app, "secret_key", "invoice-number-test")
    monkeypatch.setattr(invoice_routes, "resume_invoice_job", lambda _invoice_id: None)
    monkeypatch.setattr(invoice_routes, "reconcile_orders_after_invoice_change", lambda _order_ids: None)
    client = backend.app.test_client()
    with client.session_transaction() as session:
        session["admin_authenticated"] = True
        session["csrf_token"] = "test"
    return client


def test_manual_number_from_invoice_form_is_saved_exactly(historical_db, monkeypatch):
    db = backend.conn()
    db.execute(
        "INSERT INTO orders(id,order_no,customer_id,customer_name,customer_address,customer_email,status,created_at,currency) "
        "VALUES(2,'ZAM-2',1,'Kunde','Street 1','buyer@example.com','new',?,'EUR')",
        (backend.now_iso(),),
    )
    db.execute(
        "INSERT INTO order_items(id,order_id,product_id,sku,qty,unit_net_price,currency,created_at) "
        "VALUES(2,2,1,'SKU-1',1,10,'EUR',?)",
        (backend.now_iso(),),
    )
    db.commit()
    db.close()
    client = _invoice_edit_client(monkeypatch)
    monkeypatch.setattr(invoice_routes, "finalize_fully_invoiced_orders", lambda _order_ids: ([], []))

    page = client.get("/orders/2/invoice").get_data(as_text=True)
    assert 'name="invoice_no_manual" value="0"' in page
    assert "document.getElementById('invoice_no_manual').value='1'" in page

    response = client.post(
        "/orders/2/invoice", data=_invoice_create_form("FVAT 20/09/2026"),
    )
    assert response.status_code == 302
    db = backend.conn()
    saved = db.execute("SELECT invoice_no FROM invoices WHERE order_id=2").fetchone()
    db.close()
    assert saved[0] == "FVAT 20/09/2026"


def test_pdf_and_ksef_use_invoice_number_stored_on_record(historical_db):
    stored_number = "FVAT 20/09/2026"
    db = backend.conn()
    db.execute("UPDATE invoices SET invoice_no=? WHERE id=1", (stored_number,))
    db.commit()
    order = db.execute("SELECT * FROM orders WHERE id=1").fetchone()
    db.close()

    invoice = backend.load_invoice_with_meta(1)
    items = backend.invoice_items_from_saved_json(1)
    pdf_path, _net, _gross = backend.generate_order_invoice_pdf(
        order, items, backend.invoice_meta_payload(invoice),
    )
    pdf_text = "\n".join(page.extract_text() or "" for page in PdfReader(pdf_path).pages)
    assert stored_number in pdf_text

    ksef_invoice, company, ksef_items, problems = backend.build_invoice_ksef_payload(1)
    assert problems == []
    root, namespaces = xml_values(backend.build_ksef_draft_xml(ksef_invoice, company, ksef_items))
    assert root.findtext(".//f:P_2", namespaces=namespaces) == stored_number


def test_edit_without_number_change_preserves_own_number(historical_db, monkeypatch):
    client = _invoice_edit_client(monkeypatch)
    response = client.post("/invoices/1/edit", data=_invoice_edit_form("FV/HIST/1"))
    assert response.status_code == 302
    assert backend.load_invoice_with_meta(1)["invoice_no"] == "FV/HIST/1"


def test_allowed_manual_invoice_edit_is_exact_and_not_overwritten(historical_db, monkeypatch):
    client = _invoice_edit_client(monkeypatch)
    response = client.post("/invoices/1/edit", data=_invoice_edit_form("FVAT 20/09/2026"))
    assert response.status_code == 302
    assert backend.load_invoice_with_meta(1)["invoice_no"] == "FVAT 20/09/2026"
    assert invoice_numbering.preview(backend, "2026-09-15") == "FVAT 21/09/2026"


def test_duplicate_manual_invoice_edit_is_clear_and_does_not_save(historical_db, monkeypatch):
    _store_number("FVAT 20/09/2026", invoice_id=2)
    client = _invoice_edit_client(monkeypatch)
    response = client.post("/invoices/1/edit", data=_invoice_edit_form("FVAT 20/09/2026"))
    assert response.status_code == 200
    assert "Faktura o takim numerze już istnieje" in response.get_data(as_text=True)
    assert backend.load_invoice_with_meta(1)["invoice_no"] == "FV/HIST/1"


def test_historical_pdf_generation_preserves_stock_status_and_links(historical_db):
    before = protected_snapshot()
    inv = backend.load_invoice_with_meta(1)
    items = backend.invoice_items_from_saved_json(1)
    c = backend.conn(); order = c.execute("SELECT * FROM orders WHERE id=1").fetchone(); c.close()
    path, net, gross = backend.generate_order_invoice_pdf(order, items, backend.invoice_meta_payload(inv))
    assert path.endswith(".pdf") and net == gross == 20
    assert protected_snapshot() == before
    unchanged = backend.load_invoice_with_meta(1)
    assert unchanged["invoice_no"] == "FV/HIST/1"
    assert unchanged["order_id"] == 1
    assert unchanged["paid"] == 1


def test_historical_ksef_generation_preserves_operational_tables(historical_db):
    before = protected_snapshot()
    inv, company, items, problems = backend.build_invoice_ksef_payload(1)
    assert problems == []
    xml = backend.build_ksef_draft_xml(inv, company, items)
    assert "0 WDT" in xml and "EUR" in xml
    assert protected_snapshot() == before


def test_export_pdf_is_foreign_and_not_wdt(historical_db):
    c = backend.conn()
    c.execute("UPDATE invoices SET invoice_type='export', buyer_country='US', buyer_tax_no='US-99-123' WHERE id=1")
    c.execute("UPDATE orders SET currency='USD' WHERE id=1")
    c.commit()
    inv = backend.load_invoice_with_meta(1)
    items = [dict(backend.invoice_items_from_saved_json(1)[0], currency="USD")]
    order = c.execute("SELECT * FROM orders WHERE id=1").fetchone(); c.close()
    path, _, _ = backend.generate_order_invoice_pdf(order, items, backend.invoice_meta_payload(inv))
    text = "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
    assert "Ausfuhrlieferung" in text
    assert "innergemeinschaftliche Lieferung" not in text
    assert "USD" in text


def test_invoice_edit_keeps_saved_eur_price_and_zero_vat(historical_db):
    rows = backend.invoice_edit_items(1, backend.load_invoice_with_meta(1))
    assert rows[0]["net_price"] == 10
    assert rows[0]["currency"] == "EUR"
    form = {f"invoice_qty_{rows[0]['id']}": "2"}
    prepared = backend.prepare_invoice_edit_items(rows, form, "wdt", "EUR")
    assert prepared[0]["net_price"] == 10
    assert prepared[0]["gross_price"] == 10
    assert prepared[0]["vat_rate"] == 0
    assert prepared[0]["currency"] == "EUR"


def test_eur_order_automatically_becomes_wdt_without_language_rule():
    order = {"currency": "EUR", "price_list": "eu_eur"}
    kind, currency, country = backend.automatic_invoice_tax_context(
        order, "DE368333559", "Deutschland"
    )
    assert (kind, currency, country) == ("wdt", "EUR", "DE")


def test_domain_routes_keep_names_and_read_pages_are_side_effect_free(historical_db, monkeypatch):
    monkeypatch.setattr(backend, "maybe_pull_shared_from_supabase", lambda *a, **k: None)
    backend.app.secret_key = "test-secret-key"
    rules = {(rule.rule, rule.endpoint) for rule in backend.app.url_map.iter_rules()}
    for expected in {
        ("/", "home"), ("/customers", "customers"), ("/orders", "orders"),
        ("/stock", "stock"), ("/invoices", "invoices"), ("/ksef", "ksef_dashboard"),
        ("/inpost/dispatch", "inpost_dispatch_order"), ("/china", "china"),
        ("/api/client_stock_catalog", "api_client_stock_catalog"),
        ("/api/client_search_log", "api_client_search_log"),
        ("/api/client/profile", "api_client_profile"),
        ("/api/client/search-aliases", "search_aliases"),
        ("/api/client/orders", "api_client_orders_create"),
        ("/api/client/orders/<int:order_id>/pdf", "api_client_order_pdf"),
        ("/api/client/orders/<int:order_id>/pdf-retail", "api_client_order_pdf_retail"),
        ("/api/client/product-images/<int:image_id>", "client_product_image"),
        ("/api/client_invoices", "api_client_invoices"),
        ("/api/invoices/<int:invoice_id>/seen", "api_invoice_seen"),
        ("/api/invoices/<int:invoice_id>/download", "api_invoice_download"),
        ("/api/order_lookup", "api_order_lookup"),
        ("/api/client_order_email", "api_client_order_email"),
    }:
        assert expected in rules

    c = backend.conn()
    tables = [row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    before = {table: [tuple(row) for row in c.execute(f"SELECT * FROM {table} ORDER BY 1")] for table in tables}
    c.close()
    client = backend.app.test_client()
    with client.session_transaction() as sess:
        sess["admin_authenticated"] = True
        sess["csrf_token"] = "test"
    for path in ("/", "/customers", "/customers/1/edit", "/orders", "/orders/1", "/stock", "/invoices", "/invoices/1/edit", "/ksef", "/china"):
        response = client.get(path)
        assert response.status_code == 200, path
    c = backend.conn()
    after = {table: [tuple(row) for row in c.execute(f"SELECT * FROM {table} ORDER BY 1")] for table in tables}
    c.close()
    assert after == before


def test_invoice_list_uses_invoice_currency_instead_of_hardcoded_pln(historical_db, monkeypatch):
    monkeypatch.setattr(backend, "maybe_pull_shared_from_supabase", lambda *a, **k: None)
    backend.app.secret_key = "test-secret-key"
    c = backend.conn()
    c.execute("UPDATE invoices SET currency='EUR', invoice_type='wdt' WHERE id=1")
    c.commit()
    c.close()
    client = backend.app.test_client()
    with client.session_transaction() as sess:
        sess["admin_authenticated"] = True
        sess["csrf_token"] = "test"
    response = client.get("/invoices")
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert "20,00 EUR" in page
    assert "20,00 PLN" not in page


def test_dashboard_metric_tiles_link_to_details(historical_db, monkeypatch):
    monkeypatch.setattr(backend, "maybe_pull_shared_from_supabase", lambda *a, **k: None)
    backend.app.secret_key = "test-secret-key"
    client = backend.app.test_client()
    with client.session_transaction() as sess:
        sess["admin_authenticated"] = True
        sess["csrf_token"] = "test"
    page = client.get("/").get_data(as_text=True).replace("&amp;", "&")
    for href in (
        "/orders?tab=all&created_today=1",
        "/orders?tab=all&issued_today=1",
        "/orders?tab=new&ready_today=1",
        "/payments/overdue",
        "/cash-flow#replenishment-ranking",
        "/stock",
    ):
        assert f'href="{href}"' in page
    assert client.get("/orders?tab=all&created_today=1").status_code == 200
    assert client.get("/orders?tab=all&issued_today=1").status_code == 200


def test_invoice_redesign_filters_are_read_only_and_keep_actions(historical_db, monkeypatch):
    monkeypatch.setattr(backend, "maybe_pull_shared_from_supabase", lambda *a, **k: None)
    backend.app.secret_key = "test-secret-key"
    c = backend.conn()
    c.execute("UPDATE invoices SET currency='EUR', invoice_type='wdt' WHERE id=1")
    c.commit()
    tables = [row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    before = {table: [tuple(row) for row in c.execute(f"SELECT * FROM {table} ORDER BY 1")] for table in tables}
    c.close()
    client = backend.app.test_client()
    with client.session_transaction() as sess:
        sess["admin_authenticated"] = True
        sess["csrf_token"] = "test"
    urls = (
        "/invoices",
        "/invoices?view=customers",
        "/invoices?q=Kunde&customer=Kunde&month=2026-01&payment=paid&document_type=wdt&currency=EUR&ksef=none&sent=sent",
    )
    for url in urls:
        response = client.get(url)
        assert response.status_code == 200
    page = client.get("/invoices").get_data(as_text=True)
    assert "Wszystkie faktury" in page and "Wed\u0142ug klient\u00f3w" in page
    assert "KRAJOWA" in page or "WDT" in page
    assert "/invoices/1/download" in page
    assert "/invoices/1/payment-reminder" in page or "/invoices/1/unpaid" in page
    assert "/invoices/1/ksef/xml" in page
    c = backend.conn()
    after = {table: [tuple(row) for row in c.execute(f"SELECT * FROM {table} ORDER BY 1")] for table in tables}
    c.close()
    assert after == before
