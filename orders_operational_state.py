"""Application-level order state composed from existing fulfillment sources."""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from fulfillment_readiness import calculate_fulfillment_readiness
from inventory_analytics import build_replenishment_analysis, inventory_business_status


def _text(value) -> str:
    return str(value or "").strip()


def compose_order_actions(readiness: list[dict], inventory: list[dict]) -> dict:
    """Present existing readiness and coverage decisions without recalculating them."""
    coverage_by_product = {}
    for row in inventory:
        product_id = int(row.get("id") or 0)
        if product_id:
            coverage_by_product[product_id] = (row, inventory_business_status(row))

    ready_to_ship = []
    covered_shortages = []
    uncovered_shortages = []
    for order in readiness:
        order_id = int(order["order_id"])
        order_number = _text(order.get("order_number"))
        customer_name = _text(order.get("customer_name"))
        if order.get("ready"):
            packed = _text(order.get("order_status")).lower() == "packed"
            ready_to_ship.append({
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
            sku = _text(shortage.get("sku"))
            model = _text(shortage.get("model"))
            product_name = _text(shortage.get("name"))
            product_label = model or product_name or sku
            entry = {
                "entity_type": "order_shortage", "entity_id": order_id,
                "human_label": f"{order_number} — {product_label}",
                "order_number": order_number, "customer_name": customer_name,
                "product_id": product_id, "sku": sku, "model": model,
                "product_name": product_name,
                "quantity": int(shortage.get("shortage_quantity") or 0),
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
            (covered_shortages if covered else uncovered_shortages).append(entry)
    return {
        "ready_to_ship": ready_to_ship,
        "covered_order_shortages": covered_shortages,
        "uncovered_order_shortages": uncovered_shortages,
    }


def build_orders_operational_state(
    connection_factory: Callable, *, current_time: datetime,
    order_id: int | None = None, customer_id: int | None = None,
) -> dict:
    db = connection_factory()
    try:
        readiness = calculate_fulfillment_readiness(db)
    finally:
        db.close()
    if order_id:
        readiness = [row for row in readiness if int(row["order_id"]) == int(order_id)]
    if customer_id:
        readiness = [row for row in readiness if int(row.get("customer_id") or 0) == int(customer_id)]
    inventory = build_replenishment_analysis(connection_factory, today=current_time.date())
    actions = compose_order_actions(readiness, inventory)
    shortages_by_order: dict[int, list[dict]] = {}
    for item in actions["uncovered_order_shortages"] + actions["covered_order_shortages"]:
        shortages_by_order.setdefault(int(item["entity_id"]), []).append(item)
    blocked = []
    for order in readiness:
        if order.get("ready"):
            continue
        missing_items = shortages_by_order.get(int(order["order_id"]), [])
        has_uncovered = any(not item["source_state"]["covered_by_stock_and_confirmed_incoming"]
                            for item in missing_items)
        blocked.append({
            "entity_type": "order", "entity_id": int(order["order_id"]),
            "human_label": _text(order.get("order_number")),
            "customer_name": _text(order.get("customer_name")),
            "quantity": int(order.get("total_units") or 0),
            "action_required": (
                "Zamów niepokryte braki zamówienia." if has_uncovered
                else "Monitoruj potwierdzone dostawy pokrywające braki."
            ),
            "urgency": "high" if has_uncovered else "medium",
            "source_state": {
                "fulfillment_ready": False,
                "order_status": _text(order.get("order_status")),
            },
            "missing_items": missing_items,
        })
    return {
        "ready_to_ship": actions["ready_to_ship"],
        "blocked": blocked,
        "active_complete": list(actions["ready_to_ship"]),
        "active_incomplete": list(blocked),
    }
