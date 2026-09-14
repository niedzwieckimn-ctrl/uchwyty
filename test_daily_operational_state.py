import json
from datetime import datetime

import pytest

import app as backend
import business_operations as operations
import business_query
import daily_operational_state as daily
import internal_rbac as rbac
import orders_operational_state as orders_state


NOW = datetime(2026, 9, 14, 10, 0, 0)
NOW_TEXT = "2026-09-14T10:00:00+02:00"


@pytest.fixture()
def operational_fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "DB_PATH", str(tmp_path / "daily-state.db"))
    monkeypatch.setenv(business_query.FEATURE_FLAG, "1")
    backend.init_db()
    db = backend.conn()
    db.executemany(
        "INSERT INTO products(id,sku,model,name,archived,created_at) VALUES(?,?,?,?,0,?)",
        [
            (1, "SKU-READY", "Ready", "Ready product", NOW_TEXT),
            (2, "SKU-COVERED", "Hugo", "Covered product", NOW_TEXT),
            (3, "SKU-PLANNED", "Victor", "Uncovered product", NOW_TEXT),
        ],
    )
    db.executemany("INSERT INTO stock(product_id,qty) VALUES(?,?)", [(1, 5), (2, 0), (3, 0)])
    db.executemany(
        """INSERT INTO orders(id,order_no,customer_id,customer_name,status,currency,created_at)
           VALUES(?,?,?,?,?,?,?)""",
        [
            (1, "ZAM-READY", None, "MAGMAR", "confirmed", "PLN", "2026-09-10T08:00:00+02:00"),
            (2, "ZAM-COVERED", None, "Firma Pokryta", "confirmed", "PLN", "2026-09-11T08:00:00+02:00"),
            (3, "ZAM-UNCOVERED", None, "Firma Pilna", "confirmed", "PLN", "2026-09-12T08:00:00+02:00"),
        ],
    )
    db.executemany(
        """INSERT INTO order_items(id,order_id,product_id,sku,qty,unit_net_price,currency,created_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        [
            (1, 1, 1, "SKU-READY", 2, 10, "PLN", NOW_TEXT),
            (2, 2, 2, "SKU-COVERED", 3, 10, "PLN", NOW_TEXT),
            (3, 3, 3, "SKU-PLANNED", 4, 10, "PLN", NOW_TEXT),
        ],
    )
    db.executemany(
        """INSERT INTO china_packages(
             id,package_no,status,supplier,tracking,tracking_error,cost_amount,created_at
           ) VALUES(?,?,?,?,?,?,?,?)""",
        [
            (1, "PO-ORDERED", "ordered", "Dostawca B", "TRACK-B", "", 100, NOW_TEXT),
            (2, "PO-PLANNED", "planned", "Dostawca C", "", "", 100, NOW_TEXT),
            (3, "PO-PROBLEM", "problem", "Dostawca X", "TRACK-X", "Odprawa celna", 100, NOW_TEXT),
        ],
    )
    db.executemany(
        "INSERT INTO china_items(id,package_id,product_id,sku,qty,created_at) VALUES(?,?,?,?,?,?)",
        [
            (1, 1, 2, "SKU-COVERED", 3, NOW_TEXT),
            (2, 2, 3, "SKU-PLANNED", 100, NOW_TEXT),
        ],
    )
    db.commit()
    db.close()
    return rbac.load_actor_context(rbac.BOOTSTRAP_OWNER_ACTOR_ID, request_id="daily-owner")


def test_daily_state_composes_production_case_and_separates_coverage(operational_fixture):
    state = daily.build_daily_operational_state(backend.conn, current_time=NOW)

    assert [item["human_label"] for item in state["ready_to_ship"]] == ["ZAM-READY"]
    assert state["overdue_payments"] == []
    assert [(item["customer_name"], item["model"], item["quantity"])
            for item in state["covered_order_shortages"]] == [
                ("Firma Pokryta", "Hugo", 3),
            ]
    assert [(item["customer_name"], item["model"], item["quantity"])
            for item in state["uncovered_order_shortages"]] == [
                ("Firma Pilna", "Victor", 4),
            ]
    assert state["covered_order_shortages"][0]["source_state"][
        "covered_by_stock_and_confirmed_incoming"] is True
    # The 100 planned units are visible as a P/O, but the existing inventory
    # source of truth excludes them from confirmed incoming coverage.
    uncovered = state["uncovered_order_shortages"][0]
    assert uncovered["source_state"]["confirmed_incoming_quantity"] == 0
    assert uncovered["source_state"]["covered_by_stock_and_confirmed_incoming"] is False
    assert [(item["human_label"], item["company_name"])
            for item in state["deliveries_requiring_attention"]] == [
                ("PO-PROBLEM", "Dostawca X"),
            ]
    for entries in state.values():
        for entry in entries:
            assert entry["entity_id"] and entry["human_label"]
            assert entry["action_required"] and entry["urgency"] and entry["source_state"]


def test_daily_state_delegates_business_rules_to_existing_helpers(monkeypatch):
    calls = []

    class Result:
        @staticmethod
        def fetchall():
            return []

    class DB:
        def execute(self, _sql):
            calls.append("packages")
            return Result()

        def close(self):
            calls.append("close")

    monkeypatch.setattr(daily, "calculate_fulfillment_readiness", lambda _db: calls.append("readiness") or [{
        "order_id": 9, "order_number": "ZAM-9", "customer_name": "Klient",
        "order_status": "confirmed", "ready": False,
        "missing_items": [{"product_id": 7, "sku": "SKU-7", "model": "Model 7",
                           "shortage_quantity": 2}],
    }])
    monkeypatch.setattr(daily, "cash_flow_overdue_invoices", lambda _db, current_time: calls.append("overdue") or [{
        "id": 4, "invoice_no": "FVAT 4", "buyer_name": "Płatnik",
        "currency": "PLN", "total_gross": 120.5, "payment_to": "2026-09-10",
        "overdue_days": 4,
    }])
    monkeypatch.setattr(daily, "build_replenishment_analysis", lambda factory, today: calls.append("inventory") or [{
        "id": 7, "incoming_qty": 2,
    }])
    monkeypatch.setattr(orders_state, "inventory_business_status", lambda row: calls.append("coverage") or {
        "status_label": "Tylko w drodze",
        "covered_by_stock_and_confirmed_incoming": True,
    })

    state = daily.build_daily_operational_state(lambda: DB(), current_time=NOW)

    assert calls == ["readiness", "overdue", "packages", "close", "inventory", "coverage"]
    assert state["uncovered_order_shortages"] == []
    assert state["covered_order_shortages"][0]["entity_id"] == 9
    assert state["covered_order_shortages"][0]["source_state"][
        "covered_by_stock_and_confirmed_incoming"] is True
    assert state["overdue_payments"] == [{
        "entity_type": "invoice", "entity_id": 4, "human_label": "FVAT 4",
        "customer_name": "Płatnik", "quantity": None,
        "amount_outstanding": 120.5, "currency": "PLN",
        "action_required": "Skontaktuj się z klientem w sprawie zaległej płatności.",
        "urgency": "high", "source_state": {
            "payment_status": "overdue", "due_date": "2026-09-10", "overdue_days": 4,
        },
    }]


def test_business_query_daily_view_returns_curated_state_without_raw_tables(operational_fixture):
    result = operations.execute_business_operation(
        operational_fixture, "business.query", {"view": "daily_operational_state"})

    assert result.status == "SUCCESS", (result.error_code, result.safe_error_message)
    view = result.data["results"][0]
    assert view["entity"] == "daily_operational_state"
    assert set(view["rows"][0]) == set(daily.STATE_KEYS)
    encoded = json.dumps(result.data, ensure_ascii=False).lower()
    assert "china_packages" not in encoded
    assert "order_items" not in encoded
    assert "invoice_meta" not in encoded


def test_daily_state_empty_sources_return_bounded_empty_contract(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "DB_PATH", str(tmp_path / "empty-daily.db"))
    backend.init_db()

    state = daily.build_daily_operational_state(backend.conn, current_time=NOW)

    assert state == {key: [] for key in daily.STATE_KEYS}
