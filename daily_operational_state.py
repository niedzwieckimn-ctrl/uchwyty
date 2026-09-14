"""Application service composing the existing read-only operational states."""

from __future__ import annotations

from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo

from cash_flow_module import cash_flow_overdue_invoices
from china_delivery_attention import delivery_attention_states
from fulfillment_readiness import calculate_fulfillment_readiness
from inventory_analytics import build_replenishment_analysis, inventory_business_status


STATE_KEYS = (
    "ready_to_ship",
    "overdue_payments",
    "uncovered_order_shortages",
    "covered_order_shortages",
    "deliveries_requiring_attention",
    "other_urgent_exceptions",
)


def _text(value) -> str:
    return str(value or "").strip()


def _read_sources(connection_factory: Callable, current_time: datetime):
    db = connection_factory()
    try:
        readiness = calculate_fulfillment_readiness(db)
        overdue = cash_flow_overdue_invoices(db, current_time=current_time)
        packages = [dict(row) for row in db.execute("""
            SELECT cp.*, COALESCE(SUM(MAX(0, ci.qty)), 0) AS total_units
            FROM china_packages cp
            LEFT JOIN china_items ci ON ci.package_id=cp.id
            WHERE LOWER(COALESCE(cp.status,'')) IN ('planned','ordered','shipped','problem')
            GROUP BY cp.id
            ORDER BY cp.created_at, cp.id
        """).fetchall()]
    finally:
        db.close()
    inventory = build_replenishment_analysis(connection_factory, today=current_time.date())
    return readiness, overdue, packages, inventory


def build_daily_operational_state(
    connection_factory: Callable, *, current_time: datetime | None = None,
) -> dict:
    """Compose existing readiness, coverage, overdue and delivery attention states."""
    now = current_time or datetime.now(ZoneInfo("Europe/Warsaw"))
    readiness, overdue, packages, inventory = _read_sources(connection_factory, now)
    state = {key: [] for key in STATE_KEYS}

    coverage_by_product = {}
    for row in inventory:
        product_id = int(row.get("id") or 0)
        if product_id:
            coverage_by_product[product_id] = (row, inventory_business_status(row))

    for order in readiness:
        order_id = int(order["order_id"])
        order_number = _text(order.get("order_number"))
        customer_name = _text(order.get("customer_name"))
        if order.get("ready"):
            packed = _text(order.get("order_status")).lower() == "packed"
            state["ready_to_ship"].append({
                "entity_type": "order", "entity_id": order_id,
                "human_label": order_number, "customer_name": customer_name,
                "quantity": int(order.get("total_units") or 0),
                "action_required": (
                    "Nadaj gotowe zamówienie." if packed
                    else "Spakuj i nadaj gotowe zamówienie."
                ),
                "urgency": "high",
                "source_state": {
                    "fulfillment_ready": True,
                    "order_status": _text(order.get("order_status")),
                },
            })
            continue

        for shortage in order.get("missing_items") or []:
            product_id = int(shortage.get("product_id") or 0)
            inventory_row, coverage = coverage_by_product.get(product_id, ({}, {
                "status_label": "Brak danych", "covered_by_stock_and_confirmed_incoming": False,
            }))
            covered = bool(coverage["covered_by_stock_and_confirmed_incoming"])
            quantity = int(shortage.get("shortage_quantity") or 0)
            sku = _text(shortage.get("sku"))
            model = _text(shortage.get("model"))
            product_name = _text(shortage.get("name"))
            product_label = model or product_name or sku
            entry = {
                "entity_type": "order_shortage", "entity_id": order_id,
                "human_label": f"{order_number} — {product_label}",
                "order_number": order_number, "customer_name": customer_name,
                "product_id": product_id, "sku": sku, "model": model,
                "product_name": product_name, "quantity": quantity,
                "action_required": (
                    "Monitoruj potwierdzoną dostawę pokrywającą brak."
                    if covered else "Zamów brakującą ilość produktu."
                ),
                "urgency": "medium" if covered else "high",
                "source_state": {
                    "fulfillment_ready": False,
                    "coverage_status": coverage["status_label"],
                    "covered_by_stock_and_confirmed_incoming": covered,
                    "confirmed_incoming_quantity": int(inventory_row.get("incoming_qty") or 0),
                },
            }
            target = "covered_order_shortages" if covered else "uncovered_order_shortages"
            state[target].append(entry)

    for invoice in overdue:
        invoice_id = int(invoice["id"])
        currency = _text(invoice.get("currency") or "PLN").upper()
        customer_name = _text(invoice.get("buyer_name") or invoice.get("order_customer_name"))
        state["overdue_payments"].append({
            "entity_type": "invoice", "entity_id": invoice_id,
            "human_label": _text(invoice.get("invoice_no")),
            "customer_name": customer_name, "quantity": None,
            "amount_outstanding": round(float(invoice.get("total_gross") or 0), 2),
            "currency": currency,
            "action_required": "Skontaktuj się z klientem w sprawie zaległej płatności.",
            "urgency": "high",
            "source_state": {
                "payment_status": "overdue",
                "due_date": _text(invoice.get("payment_to")),
                "overdue_days": int(invoice.get("overdue_days") or 0),
            },
        })

    for package in packages:
        attention = delivery_attention_states(package, current_time=now)
        if not attention:
            continue
        actions = list(dict.fromkeys(item["action_required"] for item in attention))
        state["deliveries_requiring_attention"].append({
            "entity_type": "purchase_order", "entity_id": int(package["id"]),
            "human_label": _text(package.get("package_no")),
            "company_name": _text(package.get("supplier")),
            "quantity": int(package.get("total_units") or 0),
            "action_required": " ".join(actions),
            "urgency": "high" if any(item["urgency"] == "high" for item in attention) else "medium",
            "source_state": {
                "delivery_status": _text(package.get("status")),
                "attention_codes": [item["code"] for item in attention],
                "attention_labels": [item["label"] for item in attention],
            },
        })

    return state
