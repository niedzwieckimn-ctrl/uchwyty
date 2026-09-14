"""Application-level order state composed from existing fulfillment sources."""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from fulfillment_readiness import calculate_fulfillment_readiness
from inventory_analytics import build_replenishment_analysis, inventory_business_status
from inventory_operational_state import product_coverage_entry


def _text(value) -> str:
    return str(value or "").strip()


def compose_order_actions(readiness: list[dict], inventory: list[dict]) -> dict:
    """Project order readiness plus product-level demand coverage without allocation."""
    inventory_by_product = {
        int(row["id"]): row for row in inventory if int(row.get("id") or 0)
    }
    orders = []
    shortage_product_ids = set()
    for order in readiness:
        missing_items = []
        for shortage in order.get("missing_items") or []:
            product_id = int(shortage.get("product_id") or 0)
            shortage_product_ids.add(product_id)
            missing_items.append({
                "product_id": product_id,
                "sku": _text(shortage.get("sku")),
                "model": _text(shortage.get("model")),
                "missing_qty_against_stock": int(shortage.get("shortage_quantity") or 0),
            })
        orders.append({
            "order_id": int(order["order_id"]),
            "order_number": _text(order.get("order_number")),
            "customer_name": _text(order.get("customer_name")),
            "status": _text(order.get("order_status")),
            "units": int(order.get("total_units") or 0),
            "fulfillment_ready": bool(order.get("ready")),
            "missing_items": missing_items,
        })

    coverage = []
    for product_id in sorted(shortage_product_ids):
        row = inventory_by_product.get(product_id)
        if row:
            coverage.append(product_coverage_entry(row, inventory_business_status(row)))
    return {"orders": orders, "product_demand_coverage": coverage}


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
    return compose_order_actions(readiness, inventory)
