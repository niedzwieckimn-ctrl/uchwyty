from datetime import datetime
import logging

import pytest

import app as backend
import business_operations as operations
import internal_rbac as rbac


@pytest.fixture()
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "DB_PATH", str(tmp_path / "prompt9.db"))
    backend.init_db()
    monkeypatch.setattr(operations, "_business_now", lambda: datetime.fromisoformat("2026-09-10T12:00:00+02:00"))
    db = backend.conn()
    now = "2026-09-10T09:00:00+02:00"
    db.execute("INSERT INTO customers(id,name,address,phone,email,nip,language,price_list,created_at) VALUES(1,'Firma Avery','Warszawa','500600700','avery@example.pl','1234567890','pl','pln',?)", (now,))
    db.execute("INSERT INTO customers(id,name,address,phone,email,nip,language,price_list,created_at) VALUES(2,'Euro Client','Berlin','111','euro@example.de','DE123','de','eu_eur',?)", (now,))
    db.execute("INSERT INTO products(id,sku,model,name,archived,created_at) VALUES(1,'CH034-BB-128160','CH034','Avery',0,?)", (now,))
    db.execute("INSERT INTO stock(product_id,qty) VALUES(1,40)")
    db.execute("INSERT INTO orders(id,order_no,customer_id,customer_name,status,created_at,currency,price_list) VALUES(1,'ZAM-2609101',1,'Firma Avery','confirmed','2026-09-10T08:00:00+02:00','PLN','pln')")
    db.execute("INSERT INTO orders(id,order_no,customer_id,customer_name,status,created_at,currency,price_list) VALUES(2,'ZAM-2608311',2,'Euro Client','completed','2026-08-31T08:00:00+02:00','EUR','eu_eur')")
    db.execute("INSERT INTO order_items(id,order_id,product_id,sku,qty,unit_net_price,unit_gross_price,currency,created_at) VALUES(1,1,1,'CH034-BB-128160',2,100,123,'PLN',?)", (now,))
    db.execute("INSERT INTO order_items(id,order_id,product_id,sku,qty,unit_net_price,unit_gross_price,currency,created_at) VALUES(2,2,1,'CH034-BB-128160',1,50,50,'EUR',?)", (now,))
    db.execute("INSERT INTO invoices(id,order_id,invoice_no,issue_date,sell_date,payment_type,payment_to,buyer_name,buyer_tax_no,total_net,total_gross,created_at,currency) VALUES(1,1,'FV/1/09/2026','2026-09-01','2026-09-01','transfer','2026-09-08','Firma Avery','1234567890',200,246,?,'PLN')", (now,))
    db.execute("INSERT INTO invoices(id,order_id,invoice_no,issue_date,sell_date,payment_type,payment_to,buyer_name,buyer_tax_no,total_net,total_gross,created_at,currency) VALUES(2,2,'FV/2/09/2026','2026-09-09','2026-09-09','transfer','2026-09-20','Euro Client','DE123',50,50,?,'EUR')", (now,))
    db.execute("INSERT INTO invoice_meta(invoice_id,invoice_items_json,paid,paid_at,updated_at) VALUES(1,'[{\"sku\":\"CH034-BB-128160\",\"qty\":2,\"currency\":\"PLN\"}]',0,NULL,?)", (now,))
    db.execute("INSERT INTO invoice_meta(invoice_id,invoice_items_json,paid,paid_at,updated_at) VALUES(2,'[{\"sku\":\"CH034-BB-128160\",\"qty\":1,\"currency\":\"EUR\"}]',1,'2026-09-10',?)", (now,))
    db.execute("INSERT INTO china_packages(id,package_no,status,created_at) VALUES(1,'PO-1','ordered',?)", (now,))
    db.execute("INSERT INTO china_packages(id,package_no,status,created_at) VALUES(2,'PO-2','arrived',?)", (now,))
    db.execute("INSERT INTO china_items(id,package_id,product_id,sku,qty,created_at) VALUES(1,1,1,'CH034-BB-128160',12,?)", (now,))
    db.execute("INSERT INTO china_items(id,package_id,product_id,sku,qty,created_at) VALUES(2,2,1,'CH034-BB-128160',5,?)", (now,))
    db.commit(); db.close()
    return rbac.load_actor_context(rbac.AI_OWNER_ASSISTANT_ACTOR_ID, request_id="prompt9")


def run(actor, name, payload):
    result = operations.execute_business_operation(actor, name, payload)
    assert result.status == "SUCCESS", (result.error_code, result.safe_error_message)
    return result.data


def test_orders_search_and_get_with_amounts(data):
    found = run(data, "orders.search", {"query": "  firma AVERY ", "period": "today", "limit": 10})
    assert found["count"] == 1 and found["results"][0]["order_number"] == "ZAM-2609101"
    assert found["results"][0]["totals"]["PLN"] == {"net": 200.0, "gross": 246.0}
    detail = run(data, "orders.get", {"number": "zam-2609101"})["record"]
    assert detail["items"][0]["sku"] == "CH034-BB-128160" and detail["items"][0]["qty"] == 2
    assert run(data, "orders.search", {"status": "confirmed"})["count"] == 1
    assert run(data, "orders.search", {"status": "not_shipped"})["count"] == 1


def test_invoice_search_get_and_payment_status(data):
    unpaid = run(data, "invoices.search", {"query": " FIRMA avery ", "payment_status": "unpaid"})
    assert unpaid["count"] == 1 and unpaid["results"][0]["payment_status"] == "overdue"
    paid = run(data, "invoices.search", {"payment_status": "paid"})
    assert paid["results"][0]["currency"] == "EUR" and paid["results"][0]["paid_at"] == "2026-09-10"
    detail = run(data, "invoices.get", {"number": "fv/1/09/2026"})["record"]
    assert detail["items"][0]["qty"] == 2 and detail["total_gross"] == 246.0


def test_overdue_reuses_cash_flow_rule(data):
    result = run(data, "invoices.overdue", {"as_of": "2026-09-10"})
    assert result["count"] == 1
    assert result["results"][0]["invoice_number"] == "FV/1/09/2026"
    assert result["results"][0]["overdue_days"] == 2
    assert run(data, "invoices.overdue", {"as_of": "2026-09-09"})["count"] == 1
    assert run(data, "invoices.overdue", {"as_of": "2026-09-08"})["count"] == 0


def test_invoice_due_date_period_filter(data):
    due = run(data, "invoices.search", {"period": "this_week", "date_field": "due_date"})
    assert [row["invoice_number"] for row in due["results"]] == ["FV/1/09/2026"]


def test_customer_search_and_get(data):
    found = run(data, "customers.search", {"query": "  avery@EXAMPLE.pl "})
    assert found["count"] == 1 and found["results"][0]["nip"] == "1234567890"
    detail = run(data, "customers.get", {"customer_id": 1})["record"]
    assert detail["order_count"] == 1 and detail["invoice_totals"][0]["total_gross"] == 246.0
    assert "email" not in detail and "phone" not in detail and "address" not in detail


@pytest.mark.parametrize("query", ["AM Interiors", "am interiors", "AM", "Interiors", "  AM   Interiors  "])
def test_customer_search_matches_order_identity_casefold_contains_and_spaces(data, query):
    db = backend.conn()
    db.execute("INSERT INTO orders(id,order_no,customer_name,customer_email,status,created_at,currency,price_list) VALUES(3,'ZAM-AM','AM Interiors','am@example.com','confirmed','2026-09-10T10:00:00+02:00','PLN','pln')")
    db.commit(); db.close()
    found = run(data, "customers.search", {"query": query})
    assert any(row["name"] == "AM Interiors" for row in found["results"])


def test_inventory_summary_matches_inventory_analysis(data):
    from inventory_analytics import build_replenishment_analysis
    rows = build_replenishment_analysis(backend.conn, today=datetime.fromisoformat("2026-09-10").date())
    result = run(data, "inventory.summary", {})
    assert result == {
        "ok": True, "product_count": len(rows),
        "stock_units": sum(row["stock_qty"] for row in rows),
        "available_units": sum(row["available_qty"] for row in rows),
        "reserved_units": sum(row["reserved_qty"] for row in rows),
        "incoming_units": sum(row["incoming_qty"] for row in rows),
    }


def test_china_orders_summary_uses_po_tables_and_ui_active_statuses(data):
    active = run(data, "china.orders.summary", {})
    assert active["scope"] == "active" and active["order_count"] == 1
    assert active["item_units"] == 12 and active["by_status"] == {"ordered": 1}
    assert run(data, "china.orders.summary", {"scope": "all"})["order_count"] == 2


def test_unpaid_and_overdue_semantics_include_no_partial_amount_model(data):
    # invoice_meta stores a paid boolean, exactly as the Faktury and Cash Flow UI.
    # There is no persisted partial-payment amount; paid=0 therefore remains fully open.
    unpaid = run(data, "invoices.search", {"payment_status": "unpaid"})
    invoice = next(row for row in unpaid["results"] if row["invoice_number"] == "FV/1/09/2026")
    assert invoice["amount_outstanding"] == invoice["total_gross"] == 246.0
    assert run(data, "invoices.overdue", {"as_of": "2026-09-10"})["count"] == 1


def test_business_operation_diagnostics_are_bounded(data, caplog):
    with caplog.at_level(logging.INFO, logger="business_operations"):
        run(data, "customers.search", {"query": "AM Interiors"})
    assert "BUSINESS_OPERATION_INPUT" in caplog.text
    assert "BUSINESS_OPERATION_RESULT" in caplog.text
    assert '"operation": "customers.search"' in caplog.text
    assert '"status": "SUCCESS"' in caplog.text
    assert "AM Interiors" not in caplog.text


def test_ambiguous_customer_returns_candidates(data):
    db = backend.conn(); now = backend.now_iso()
    db.execute("INSERT INTO customers(name,nip,language,price_list,created_at) VALUES('Firma Avery Druga','999','pl','pln',?)", (now,)); db.commit(); db.close()
    found = run(data, "customers.search", {"query": "firma avery"})
    assert found["count"] == 2


def test_sales_summary_keeps_currencies_separate(data):
    summary = run(data, "business.sales.summary", {"period": "this_month"})
    assert summary["order_count"] == 1 and summary["invoice_count"] == 2
    amounts = {row["currency"]: row for row in summary["by_currency"]}
    assert amounts["PLN"]["invoice_gross"] == 246.0
    assert amounts["EUR"]["invoice_gross"] == 50.0 and amounts["EUR"]["paid_gross"] == 50.0
    assert amounts["PLN"]["order_gross"] == 246.0 and amounts["PLN"]["average_order_gross"] == 246.0
    day = run(data, "business.sales.summary", {"period": "today"})
    assert day["order_count"] == 1 and day["invoice_count"] == 0


def test_search_limit_is_enforced(data):
    db = backend.conn(); now = backend.now_iso()
    for index in range(3, 10):
        db.execute("INSERT INTO customers(id,name,language,price_list,created_at) VALUES(?,?,'pl','pln',?)", (index, f"Limit {index}", now))
    db.commit(); db.close()
    result = run(data, "customers.search", {"query": "Limit", "limit": 2})
    assert result["count"] == 2 and result["truncated"] is True


def test_read_operations_do_not_mutate_business_tables(data):
    tables = ("orders", "order_items", "customers", "invoices", "invoice_meta", "invoice_allocations", "products", "stock", "china_packages", "china_items")
    db = backend.conn(); before = {t: [tuple(r) for r in db.execute(f"SELECT * FROM {t} ORDER BY 1")] for t in tables}; db.close()
    for name, payload in (
        ("orders.search", {}), ("orders.get", {"id": 1}), ("invoices.search", {}),
        ("invoices.get", {"id": 1}), ("invoices.overdue", {}),
        ("customers.search", {"query": "Firma"}), ("customers.get", {"customer_id": 1}),
        ("inventory.summary", {}), ("china.orders.summary", {}),
        ("business.sales.summary", {"period": "this_month"}),
    ):
        run(data, name, payload)
    db = backend.conn(); after = {t: [tuple(r) for r in db.execute(f"SELECT * FROM {t} ORDER BY 1")] for t in tables}; db.close()
    assert after == before


def test_rbac_blocks_role_without_order_permission(data):
    db = backend.conn(); now = backend.now_iso()
    db.execute("INSERT INTO internal_actors(actor_id,actor_type,display_name,status,created_at,updated_at) VALUES('ai-limited','AI_AGENT','Limited','active',?,?)", (now, now))
    db.execute("INSERT INTO internal_actor_roles(actor_id,role_key,assigned_at) VALUES('ai-limited','AI_WAREHOUSE',?)", (now,)); db.commit(); db.close()
    limited = rbac.load_actor_context("ai-limited", request_id="limited")
    result = operations.execute_business_operation(limited, "orders.search", {})
    assert result.status == "DENIED" and result.error_code == "PERMISSION_DENIED"
