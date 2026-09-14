from datetime import datetime

import app as backend
import deliveries_operational_state as deliveries
import finance_operational_state as finance
import inventory_operational_state as inventory


NOW = datetime(2026, 9, 14, 10, 0, 0)


def test_inventory_state_uses_existing_coverage_and_replenishment(monkeypatch):
    rows = [
        {"id": 1, "sku": "A", "model": "A", "name": "A", "stock_qty": 2,
         "reserved_qty": 7, "available_qty": 0, "incoming_qty": 3,
         "reserved_incoming": 3, "available_incoming": 0,
         "suggested_qty": 2, "reorder_score": 80, "priority": "critical",
         "sales_90": 10, "searches_30": 0},
        {"id": 2, "sku": "B", "model": "B", "name": "B", "stock_qty": 2,
         "reserved_qty": 5, "available_qty": 0, "incoming_qty": 3,
         "reserved_incoming": 3, "available_incoming": 0,
         "suggested_qty": 0, "reorder_score": 20, "priority": "low",
         "sales_90": 2, "searches_30": 0},
        {"id": 3, "sku": "C", "model": "C", "name": "C", "stock_qty": 1,
         "reserved_qty": 0, "available_qty": 1, "incoming_qty": 0,
         "reserved_incoming": 0, "available_incoming": 0,
         "suggested_qty": 0, "reorder_score": 0, "priority": "low",
         "sales_90": 0, "searches_30": 0},
    ]
    monkeypatch.setattr(inventory, "build_replenishment_analysis", lambda *_args, **_kwargs: rows)

    state = inventory.build_inventory_operational_state(lambda: None, current_time=NOW)

    assert [item["sku"] for item in state["products"]] == ["A", "B", "C"]
    by_sku = {item["sku"]: item for item in state["products"]}
    assert by_sku["A"]["demand_status"] == "uncovered"
    assert by_sku["A"]["covered_qty"] == 3 and by_sku["A"]["uncovered_qty"] == 2
    assert by_sku["A"]["suggested_qty"] == 2
    assert by_sku["B"]["demand_status"] == "covered"
    assert by_sku["B"]["covered_qty"] == 3 and by_sku["B"]["uncovered_qty"] == 0
    assert by_sku["C"]["low_or_critical"] is True
    assert by_sku["A"]["replenishment_priority"] is True


def test_finance_state_reuses_controlled_invoice_and_sales_results():
    state = finance.compose_finance_operational_state(
        {"results": [{"id": 1, "invoice_number": "FV-1", "buyer_name": "A",
                       "amount_outstanding": 120, "currency": "PLN", "due_date": "2026-09-10",
                       "overdue_days": 4}]},
        {"results": [
            {"id": 1, "payment_status": "overdue", "amount_outstanding": 120},
            {"id": 2, "invoice_number": "FV-2", "buyer_name": "B", "payment_status": "unpaid",
             "amount_outstanding": 50, "currency": "EUR", "due_date": "2026-09-20"},
        ]},
        {"ok": True, "date_from": "2026-09-01", "date_to": "2026-09-30",
         "order_count": 2, "invoice_count": 1, "by_currency": [], "top_customers": []},
    )

    assert [item["human_label"] for item in state["overdue_payments"]] == ["FV-1"]
    assert [item["human_label"] for item in state["current_receivables"]] == ["FV-2"]
    assert state["sales_summary"]["order_count"] == 2
    assert "ok" not in state["sales_summary"]


def test_deliveries_state_includes_items_and_keeps_planned_separate(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "DB_PATH", str(tmp_path / "deliveries-state.db"))
    backend.init_db()
    db = backend.conn()
    db.execute("INSERT INTO products(id,sku,model,name,archived,created_at) VALUES(1,'SKU-1','Hugo','Hugo',0,?)",
               (NOW.isoformat(),))
    db.executemany(
        """INSERT INTO china_packages(id,package_no,status,supplier,tracking,tracking_error,cost_amount,created_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        [
            (1, "PO-ORDERED", "ordered", "A", "T1", "", 100, NOW.isoformat()),
            (2, "PO-SHIPPED", "shipped", "B", "T2", "", 100, NOW.isoformat()),
            (3, "PO-PLANNED", "planned", "C", "", "", 100, NOW.isoformat()),
            (4, "PO-PROBLEM", "problem", "D", "T4", "Odprawa", 100, NOW.isoformat()),
        ],
    )
    db.executemany(
        "INSERT INTO china_items(id,package_id,product_id,sku,qty,created_at) VALUES(?,?,?,?,?,?)",
        [(1, 1, 1, "SKU-1", 3, NOW.isoformat()), (2, 3, 1, "SKU-1", 20, NOW.isoformat())],
    )
    db.commit(); db.close()

    state = deliveries.build_deliveries_operational_state(backend.conn, current_time=NOW)

    assert set(state) == {"purchase_orders"}
    assert [item["number"] for item in state["purchase_orders"]] == [
        "PO-PROBLEM", "PO-PLANNED", "PO-SHIPPED", "PO-ORDERED",
    ]
    by_number = {item["number"]: item for item in state["purchase_orders"]}
    assert by_number["PO-ORDERED"]["items"][0]["quantity"] == 3
    assert by_number["PO-SHIPPED"]["status"] == "shipped"
    assert by_number["PO-PLANNED"]["status"] == "planned"
    assert by_number["PO-PROBLEM"]["requires_attention"] is True
