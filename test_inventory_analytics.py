import sqlite3
from datetime import date

from inventory_analytics import build_replenishment_analysis, recommended_replenishments


TODAY = date(2026, 8, 8)


def make_db(tmp_path):
    path = tmp_path / "analytics.db"

    def connect():
        c = sqlite3.connect(path)
        c.row_factory = sqlite3.Row
        return c

    c = connect()
    c.executescript("""
      CREATE TABLE products(id INTEGER PRIMARY KEY, sku TEXT, model TEXT, name TEXT, ean TEXT, archived INTEGER DEFAULT 0);
      CREATE TABLE stock(product_id INTEGER PRIMARY KEY, qty INTEGER);
      CREATE TABLE orders(id INTEGER PRIMARY KEY, customer_id INTEGER, customer_email TEXT, status TEXT, warehouse_issued INTEGER, created_at TEXT);
      CREATE TABLE order_items(id INTEGER PRIMARY KEY, order_id INTEGER, product_id INTEGER, qty INTEGER);
      CREATE TABLE invoice_allocations(id INTEGER PRIMARY KEY, invoice_id INTEGER, order_id INTEGER, order_item_id INTEGER, product_id INTEGER, qty INTEGER);
      CREATE TABLE china_packages(id INTEGER PRIMARY KEY, status TEXT);
      CREATE TABLE china_items(id INTEGER PRIMARY KEY, package_id INTEGER, product_id INTEGER, qty INTEGER);
      CREATE TABLE client_search_logs(customer_email TEXT, query TEXT, product_sku TEXT, product_model TEXT, product_name TEXT, results_count INTEGER, created_at TEXT);
    """)
    c.commit()
    c.close()
    return connect


def add_product(connect, product_id=1, sku="CH010", stock=0):
    c = connect()
    c.execute("INSERT INTO products(id,sku,model,name,ean) VALUES(?,?,?,?,?)", (product_id, sku, sku, "Winsor", ""))
    c.execute("INSERT INTO stock VALUES(?,?)", (product_id, stock))
    c.commit()
    c.close()


def add_sale(connect, order_id, qty, created_at, product_id=1, customer="a@example.com"):
    c = connect()
    c.execute("INSERT INTO orders VALUES(?,?,?,?,?,?)", (order_id, order_id, customer, "issued", 1, created_at))
    c.execute("INSERT INTO order_items VALUES(?,?,?,?)", (order_id, order_id, product_id, qty))
    c.commit()
    c.close()


def test_dead_zero_stock_sku_is_ignored(tmp_path):
    connect = make_db(tmp_path)
    add_product(connect)
    row = build_replenishment_analysis(connect, TODAY, 60)[0]
    assert row["reorder_score"] == 0
    assert row["suggested_qty"] == 0
    assert recommended_replenishments([row]) == []


def test_recent_sales_create_reorder_recommendation(tmp_path):
    connect = make_db(tmp_path)
    add_product(connect)
    add_sale(connect, 1, 10, "2026-08-02 10:00:00")
    row = build_replenishment_analysis(connect, TODAY, 60)[0]
    assert row["sales_30"] == 10
    assert row["sales_90"] == 10
    assert row["suggested_qty"] > 0
    assert row["reorder_score"] >= 25
    assert recommended_replenishments([row]) == [row]


def test_available_incoming_reduces_suggestion_and_priority(tmp_path):
    connect = make_db(tmp_path)
    add_product(connect)
    add_sale(connect, 1, 12, "2026-08-02 10:00:00")
    before = build_replenishment_analysis(connect, TODAY, 60)[0]
    c = connect()
    c.execute("INSERT INTO china_packages VALUES(1,'shipped')")
    c.execute("INSERT INTO china_items VALUES(1,1,1,50)")
    c.commit()
    c.close()
    after = build_replenishment_analysis(connect, TODAY, 60)[0]
    assert after["suggested_qty"] < before["suggested_qty"]
    assert after["reorder_score"] < before["reorder_score"]


def test_reservations_reduce_available_stock(tmp_path):
    connect = make_db(tmp_path)
    add_product(connect, stock=8)
    c = connect()
    c.execute("INSERT INTO orders VALUES(1,1,'a@example.com','confirmed',0,'2026-08-08 10:00:00')")
    c.execute("INSERT INTO order_items VALUES(1,1,1,5)")
    c.commit()
    c.close()
    row = build_replenishment_analysis(connect, TODAY, 60)[0]
    assert row["reserved_qty"] == 5
    assert row["available_qty"] == 3


def test_cancelled_order_does_not_reserve_stock(tmp_path):
    connect = make_db(tmp_path)
    add_product(connect, stock=8)
    c = connect()
    c.execute("INSERT INTO orders VALUES(1,1,'a@example.com','cancelled',0,'2026-08-08 10:00:00')")
    c.execute("INSERT INTO order_items VALUES(1,1,1,5)")
    c.commit()
    c.close()
    row = build_replenishment_analysis(connect, TODAY, 60)[0]
    assert row["reserved_qty"] == 0
    assert row["available_qty"] == 8


def test_partial_shipment_reserves_only_uninvoiced_quantity(tmp_path):
    connect = make_db(tmp_path)
    add_product(connect, stock=20)
    c = connect()
    c.execute("INSERT INTO orders VALUES(1,1,'a@example.com','partially_shipped',0,'2026-08-08 10:00:00')")
    c.execute("INSERT INTO order_items VALUES(1,1,1,10)")
    c.execute("INSERT INTO invoice_allocations VALUES(1,1,1,1,1,6)")
    c.commit()
    c.close()
    row = build_replenishment_analysis(connect, TODAY, 60)[0]
    assert row["reserved_qty"] == 4
    assert row["available_qty"] == 16


def test_fully_invoiced_packed_order_is_not_reserved(tmp_path):
    connect = make_db(tmp_path)
    add_product(connect, stock=20)
    c = connect()
    c.execute("INSERT INTO orders VALUES(1,1,'a@example.com','packed',0,'2026-08-08 10:00:00')")
    c.execute("INSERT INTO order_items VALUES(1,1,1,10)")
    c.execute("INSERT INTO invoice_allocations VALUES(1,1,1,1,1,10)")
    c.commit()
    c.close()
    row = build_replenishment_analysis(connect, TODAY, 60)[0]
    assert row["reserved_qty"] == 0
    assert row["available_qty"] == 20


def test_single_search_does_not_trigger_purchase(tmp_path):
    connect = make_db(tmp_path)
    add_product(connect)
    c = connect()
    c.execute("INSERT INTO client_search_logs VALUES(?,?,?,?,?,?,?)", ("a@example.com", "CH010", "CH010", "CH010", "Winsor", 1, "2026-08-08 10:00:00"))
    c.commit()
    c.close()
    row = build_replenishment_analysis(connect, TODAY, 60)[0]
    assert row["searches_30"] == 1
    assert row["suggested_qty"] == 0
    assert recommended_replenishments([row]) == []


def test_repeated_no_result_searches_can_suggest_test_quantity(tmp_path):
    connect = make_db(tmp_path)
    add_product(connect)
    c = connect()
    c.execute("INSERT INTO client_search_logs VALUES(?,?,?,?,?,?,?)", ("a@example.com", "windsor", "", "", "", 0, "2026-08-07 10:00:00"))
    c.execute("INSERT INTO client_search_logs VALUES(?,?,?,?,?,?,?)", ("b@example.com", "winsor", "", "", "", 0, "2026-08-08 10:00:00"))
    c.commit()
    c.close()
    row = build_replenishment_analysis(connect, TODAY, 60)[0]
    assert row["searches_30"] == 2
    assert row["search_clients_30"] == 2
    assert row["suggested_qty"] >= 1
    assert row["confidence"] == "low"


def test_numeric_family_search_can_match_product_spacing(tmp_path):
    connect = make_db(tmp_path)
    add_product(connect, sku="CH010-BB-320")
    c = connect()
    c.execute("INSERT INTO client_search_logs VALUES(?,?,?,?,?,?,?)", ("a@example.com", "320", "", "", "", 0, "2026-08-07 10:00:00"))
    c.execute("INSERT INTO client_search_logs VALUES(?,?,?,?,?,?,?)", ("b@example.com", "320", "", "", "", 0, "2026-08-08 10:00:00"))
    c.commit()
    c.close()
    row = build_replenishment_analysis(connect, TODAY, 60)[0]
    assert row["searches_30"] == 2
    assert row["unresolved_searches_30"] == 2
    assert row["suggested_qty"] >= 1


def test_horizon_changes_suggested_quantity(tmp_path):
    connect = make_db(tmp_path)
    add_product(connect)
    add_sale(connect, 1, 15, "2026-08-02 10:00:00")
    row_45 = build_replenishment_analysis(connect, TODAY, 45)[0]
    row_90 = build_replenishment_analysis(connect, TODAY, 90)[0]
    assert row_90["target_days"] == 90
    assert row_90["suggested_qty"] > row_45["suggested_qty"]
