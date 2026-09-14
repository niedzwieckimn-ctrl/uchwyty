"""Compact China delivery state composed from existing P/O data and attention rules."""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from china_delivery_attention import delivery_attention_states


ACTIVE_STATUSES = frozenset({"planned", "ordered", "shipped", "problem"})


def _text(value) -> str:
    return str(value or "").strip()


def build_deliveries_operational_state(
    connection_factory: Callable, *, current_time: datetime,
) -> dict:
    db = connection_factory()
    try:
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        packages = [dict(row) for row in db.execute(f"""
            SELECT cp.*
            FROM china_packages cp
            WHERE LOWER(COALESCE(cp.status,'')) IN ({placeholders})
            ORDER BY COALESCE(NULLIF(TRIM(cp.ordered_at),''),cp.created_at) DESC,cp.id DESC
        """, tuple(sorted(ACTIVE_STATUSES))).fetchall()]
        package_ids = [int(row["id"]) for row in packages]
        items_by_package: dict[int, list[dict]] = {package_id: [] for package_id in package_ids}
        if package_ids:
            item_placeholders = ",".join("?" for _ in package_ids)
            for row in db.execute(f"""
                SELECT ci.package_id,ci.product_id,ci.sku,ci.qty,p.model,p.name
                FROM china_items ci
                LEFT JOIN products p ON p.id=ci.product_id
                WHERE ci.package_id IN ({item_placeholders})
                ORDER BY ci.package_id,ci.id
            """, package_ids).fetchall():
                items_by_package[int(row["package_id"])].append({
                    "product_id": int(row["product_id"]),
                    "sku": _text(row["sku"]),
                    "model": _text(row["model"]),
                    "product_name": _text(row["name"]),
                    "quantity": int(row["qty"] or 0),
                })
    finally:
        db.close()

    sections = {
        "requiring_attention": [],
        "ordered_not_shipped": [],
        "in_transit": [],
        "planned": [],
    }
    for package in packages:
        status = _text(package.get("status")).lower()
        attention = delivery_attention_states(package, current_time=current_time)
        items = items_by_package.get(int(package["id"]), [])
        record = {
            "entity_type": "purchase_order",
            "entity_id": int(package["id"]),
            "human_label": _text(package.get("package_no")),
            "supplier": _text(package.get("supplier")),
            "status": status,
            "tracking_number": _text(package.get("tracking")),
            "delivery_stage": _text(package.get("tracking_status")),
            "quantity": sum(item["quantity"] for item in items),
            "items": items,
            "attention_codes": [item["code"] for item in attention],
            "action_required": " ".join(dict.fromkeys(
                item["action_required"] for item in attention
            )),
            "urgency": ("high" if any(item["urgency"] == "high" for item in attention)
                        else "medium" if attention else "low"),
            "source_state": {
                "delivery_status": status,
                "attention_labels": [item["label"] for item in attention],
            },
        }
        if attention:
            sections["requiring_attention"].append(record)
        if status == "ordered":
            sections["ordered_not_shipped"].append(record)
        elif status == "shipped":
            sections["in_transit"].append(record)
        elif status == "planned":
            sections["planned"].append(record)
    return sections
