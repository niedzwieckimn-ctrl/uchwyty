"""Compact inventory state composed from the existing inventory analytics."""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from inventory_analytics import (
    build_replenishment_analysis,
    inventory_business_status,
    recommended_replenishments,
)


def demand_coverage(row: dict, status: dict | None = None) -> dict:
    """Project quantities already calculated by inventory analytics."""
    status = status or inventory_business_status(row)
    stock_qty = max(0, int(row.get("stock_qty") or 0))
    reserved_qty = max(0, int(row.get("reserved_qty") or 0))
    missing_qty = max(0, reserved_qty - stock_qty)
    covered_qty = max(0, int(row.get("reserved_incoming") or 0))
    return {
        "missing_qty_against_stock": missing_qty,
        "confirmed_incoming_qty": max(0, int(row.get("incoming_qty") or 0)),
        "confirmed_incoming_used": covered_qty,
        "covered_qty": covered_qty,
        "uncovered_qty": max(0, missing_qty - covered_qty),
        "fully_covered": bool(status["covered_by_stock_and_confirmed_incoming"]),
        "planned_ignored": True,
    }


def product_coverage_entry(row: dict, status: dict | None = None) -> dict:
    return {
        "product_id": int(row["id"]),
        "sku": str(row.get("sku") or "").strip(),
        "model": str(row.get("model") or "").strip(),
        **demand_coverage(row, status),
    }


def product_demand_entry(row: dict, status: dict | None = None) -> dict:
    status = status or inventory_business_status(row)
    return {
        **product_coverage_entry(row, status),
        "product_name": str(row.get("name") or "").strip(),
        "stock_qty": max(0, int(row.get("stock_qty") or 0)),
        "reserved_qty": max(0, int(row.get("reserved_qty") or 0)),
        "available_qty": max(0, int(row.get("available_qty") or 0)),
        "available_incoming_qty": max(0, int(row.get("available_incoming") or 0)),
        "coverage_status": status["status_label"],
        "suggested_qty": max(0, int(row.get("suggested_qty") or 0)),
        "reorder_score": max(0, int(row.get("reorder_score") or 0)),
        "urgency": str(row.get("priority") or "low"),
    }


def build_inventory_operational_state(
    connection_factory: Callable, *, current_time: datetime,
) -> dict:
    rows = build_replenishment_analysis(connection_factory, today=current_time.date())
    recommended_ids = {
        int(row["id"])
        for row in recommended_replenishments(rows, limit=max(10, len(rows)))
    }
    products = []
    for row in rows:
        status = inventory_business_status(row)
        coverage = demand_coverage(row, status)
        low_or_critical = status["status_label"] in {
            "Problem", "Brak", "Niski stan", "Tylko w drodze",
        }
        replenishment_priority = int(row["id"]) in recommended_ids
        if not coverage["missing_qty_against_stock"] and not low_or_critical and not replenishment_priority:
            continue
        entry = product_demand_entry(row, status)
        entry.update({
            "demand_status": (
                "covered" if coverage["missing_qty_against_stock"] and coverage["fully_covered"]
                else "uncovered" if coverage["missing_qty_against_stock"] else "none"
            ),
            "low_or_critical": low_or_critical,
            "replenishment_priority": replenishment_priority,
        })
        products.append(entry)
    return {"products": products}
