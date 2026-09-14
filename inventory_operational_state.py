"""Compact inventory state composed from the existing inventory analytics."""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from inventory_analytics import (
    build_replenishment_analysis,
    inventory_business_status,
    recommended_replenishments,
)


def _entry(row: dict, status: dict, *, action: str) -> dict:
    return {
        "entity_type": "product",
        "entity_id": int(row["id"]),
        "human_label": str(row.get("model") or row.get("name") or row.get("sku") or "").strip(),
        "sku": str(row.get("sku") or "").strip(),
        "model": str(row.get("model") or "").strip(),
        "product_name": str(row.get("name") or "").strip(),
        "quantity": int(row.get("suggested_qty") or 0),
        "action_required": action,
        "urgency": str(row.get("priority") or "low"),
        "source_state": {
            "stock_quantity": int(row.get("stock_qty") or 0),
            "reserved_quantity": int(row.get("reserved_qty") or 0),
            "available_quantity": int(row.get("available_qty") or 0),
            "confirmed_incoming_quantity": int(row.get("incoming_qty") or 0),
            "coverage_status": status["status_label"],
            "covered_by_stock_and_confirmed_incoming": bool(
                status["covered_by_stock_and_confirmed_incoming"]
            ),
            "reorder_score": int(row.get("reorder_score") or 0),
        },
    }


def build_inventory_operational_state(
    connection_factory: Callable, *, current_time: datetime,
) -> dict:
    rows = build_replenishment_analysis(connection_factory, today=current_time.date())
    sections = {
        "uncovered_demand": [],
        "covered_demand": [],
        "low_or_critical": [],
        "replenishment_priorities": [],
    }
    prepared: dict[int, tuple[dict, dict]] = {}
    for row in rows:
        status = inventory_business_status(row)
        prepared[int(row["id"])] = (row, status)
        stock = int(row.get("stock_qty") or 0)
        reserved = int(row.get("reserved_qty") or 0)
        if reserved > stock:
            target = "covered_demand" if status["covered_by_stock_and_confirmed_incoming"] else "uncovered_demand"
            sections[target].append(_entry(
                row, status,
                action=("Monitoruj potwierdzoną dostawę pokrywającą popyt."
                        if target == "covered_demand" else "Domów niepokrytą ilość produktu."),
            ))
        if status["status_label"] in {"Problem", "Brak", "Niski stan", "Tylko w drodze"}:
            sections["low_or_critical"].append(_entry(
                row, status, action="Sprawdź stan i priorytet uzupełnienia produktu.",
            ))

    for row in recommended_replenishments(rows, limit=max(10, len(rows))):
        _, status = prepared[int(row["id"])]
        sections["replenishment_priorities"].append(_entry(
            row, status, action="Zamów sugerowaną ilość produktu.",
        ))
    return sections
