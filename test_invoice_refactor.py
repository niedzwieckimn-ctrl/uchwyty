import copy
import json
import xml.etree.ElementTree as ET

import pytest
from pypdf import PdfReader

import app as backend
from invoice_types import resolve_invoice_type
import ksef_foreign
from ksef_module import FA3_NS, validate_fa3_xml


COMPANY = {
    "company_name": "Sprzedawca Test", "nip": "1234567890",
    "address": "Testowa 1", "city": "Warszawa", "bank_account": "",
}
ITEMS = [{
    "name": "Uchwyt", "model": "M1", "sku": "SKU-1", "qty": 2,
    "net_price": 10, "line_value_net": 20, "vat_rate": 0, "currency": "EUR",
}]


def foreign_invoice(kind="wdt"):
    return {
        "invoice_no": "FV/TEST/1", "issue_date": "2026-09-03",
        "sell_date": "2026-09-03", "place": "Kotusów",
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
    # Historical row intentionally has NULL invoice_type.
    c.execute("INSERT INTO invoices(id,order_id,invoice_no,issue_date,sell_date,payment_type,payment_to,buyer_name,buyer_tax_no,buyer_street,buyer_post_code,buyer_city,buyer_country,buyer_email,total_net,total_gross,created_at) VALUES(1,1,'FV/HIST/1','2026-01-01','2026-01-01','przelew','2026-01-08','Kunde','DE123456789','Street 1','10115','Berlin','DE','buyer@example.com',20,20,?)", (backend.now_iso(),))
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
