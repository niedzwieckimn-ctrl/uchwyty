"""Canonical read model shared by the admin dashboard and the AI agent.

The dashboard tiles are business facts, so the agent must consume the exact
same calculations instead of recreating them from lower-level operations.
Dependencies are injected to keep this module independent from Flask routes.
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo


def build_dashboard_read(
    connection_factory: Callable[[], object],
    *,
    current_time: datetime | None = None,
    overdue_invoice_rows: Callable[..., list] | None = None,
    build_replenishment_analysis: Callable[..., list] | None = None,
    recommended_replenishments: Callable[..., list] | None = None,
    calculate_fulfillment_readiness: Callable[..., list] | None = None,
    order_display_no: Callable[..., str] | None = None,
    include_items: bool = True,
) -> dict:
    """Return the structural values rendered by the main dashboard tiles."""
    if not callable(connection_factory):
        raise TypeError("connection_factory musi być wywoływalne")
    if not all(callable(item) for item in (
        overdue_invoice_rows, build_replenishment_analysis,
        recommended_replenishments, calculate_fulfillment_readiness,
    )):
        raise TypeError("Brak zależności dashboard read")
    now = current_time or datetime.now(ZoneInfo("Europe/Warsaw"))
    db = connection_factory()
    try:
        cur = db.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM products WHERE COALESCE(archived,0)=0")
        products_count = int(cur.fetchone()["n"] or 0)
        cur.execute("""SELECT COUNT(*) AS n FROM orders
                       WHERE status IN ('new','packed','packed_partial','confirmed',
                                        'in_delivery','shipped','partially_shipped')""")
        current_orders = int(cur.fetchone()["n"] or 0)
        cur.execute("""SELECT COUNT(*) AS n FROM china_packages
                       WHERE status IN ('planned','ordered','shipped','problem')""")
        active_china_orders = int(cur.fetchone()["n"] or 0)
        cur.execute("SELECT COALESCE(SUM(qty),0) AS n FROM stock")
        stock_units = int(cur.fetchone()["n"] or 0)
        cur.execute("""SELECT COALESCE(SUM(ci.qty),0) AS n
                       FROM china_items ci JOIN china_packages cp ON cp.id=ci.package_id
                       WHERE cp.status IN ('ordered','shipped','problem')""")
        in_delivery_units = int(cur.fetchone()["n"] or 0)
        cur.execute("""SELECT COALESCE(SUM(
                    (COALESCE(s.qty,0) + COALESCE(d.in_delivery_qty,0)) * COALESCE(
                    (SELECT pr.net_price FROM pricing pr
                     WHERE TRIM(LOWER(pr.model)) = TRIM(LOWER(p.sku))
                     ORDER BY pr.created_at DESC LIMIT 1),
                    (SELECT pr.net_price FROM pricing pr
                     WHERE TRIM(LOWER(pr.model)) = TRIM(LOWER(p.model))
                     ORDER BY pr.created_at DESC LIMIT 1), 0)), 0) AS v
                    FROM products p
                    LEFT JOIN stock s ON s.product_id=p.id
                    LEFT JOIN (
                      SELECT ci.product_id, SUM(ci.qty) AS in_delivery_qty
                      FROM china_items ci
                      JOIN china_packages cp ON cp.id=ci.package_id
                      WHERE cp.status IN ('ordered', 'shipped', 'problem')
                      GROUP BY ci.product_id
                    ) d ON d.product_id=p.id
                    WHERE COALESCE(p.archived,0)=0""")
        inventory_value_net = float(cur.fetchone()["v"] or 0)
        cur.execute("SELECT COUNT(*) AS n FROM orders WHERE date(created_at)=date('now','localtime')")
        new_orders = int(cur.fetchone()["n"] or 0)
        cur.execute("""SELECT COUNT(DISTINCT issued.order_id) AS n
                       FROM (
                         SELECT ia.order_id, ia.created_at AS issued_at
                         FROM invoice_allocations ia
                         UNION ALL
                         SELECT i.order_id, i.created_at AS issued_at
                         FROM invoices i
                         WHERE i.order_id IS NOT NULL
                           AND NOT EXISTS (
                             SELECT 1 FROM invoice_allocations ia2
                             WHERE ia2.invoice_id=i.id
                           )
                       ) issued
                       JOIN orders o ON o.id=issued.order_id
                       WHERE COALESCE(o.warehouse_issued,0)=1
                         AND date(issued.issued_at)=date('now','localtime')""")
        issued_today = int(cur.fetchone()["n"] or 0)
        readiness = calculate_fulfillment_readiness(db)
        issuable_orders = [row for row in readiness if row["ready"]]
        ready_to_issue_today = len(issuable_orders)
        if callable(order_display_no):
            ready_to_issue_labels = [
                order_display_no(row["order_id"], row.get("created_at"),
                                 row.get("order_number"), row.get("note") or "")
                for row in issuable_orders[:3]
            ]
        else:
            # The BO handler is intentionally independent from Flask route helpers.
            ready_to_issue_labels = [
                str(row.get("order_number") or row.get("order_id") or "")
                for row in issuable_orders[:3]
            ]
        overdue = overdue_invoice_rows(db)
        overdue_count = len(overdue)
        overdue_amount = sum(float(row.get("total_gross") or 0) for row in overdue)
        horizon = 60
        try:
            cur.execute("SELECT value FROM cash_flow_settings WHERE key='reorder_horizon_days'")
            row = cur.fetchone()
            horizon = int(float(row["value"])) if row else 60
        except Exception:
            horizon = 60
        if horizon not in (45, 60, 90):
            horizon = 60
        cur.execute("""SELECT o.id,o.order_no,o.customer_name,o.created_at,o.status,o.currency,
                             COALESCE(SUM(oi.qty * COALESCE(oi.unit_net_price,pr.net_price,0)),0) AS total_net
                      FROM orders o
                      LEFT JOIN order_items oi ON oi.order_id=o.id
                      LEFT JOIN products p ON p.id=oi.product_id
                      LEFT JOIN pricing pr ON (TRIM(LOWER(pr.model))=TRIM(LOWER(p.model))
                                               OR TRIM(LOWER(pr.model))=TRIM(LOWER(p.sku)))
                      GROUP BY o.id ORDER BY o.id DESC LIMIT 8""")
        recent_orders = [dict(row) for row in cur.fetchall()]
        cur.execute("SELECT status,COUNT(*) AS n FROM orders GROUP BY status")
        status_counts = {str(row["status"] or "").strip().lower(): int(row["n"] or 0)
                         for row in cur.fetchall()}
        status_new = sum(status_counts.get(value, 0) for value in ("new", "pending", "unconfirmed"))
        status_work = sum(status_counts.get(value, 0) for value in (
            "confirmed", "packed", "packed_partial", "in_delivery", "shipped",
            "partially_shipped", "issued"))
        status_done = status_counts.get("completed", 0)
        status_cancelled = status_counts.get("cancelled", 0)
        status_total = status_new + status_work + status_done + status_cancelled
    finally:
        db.close()

    replenishment = build_replenishment_analysis(
        connection_factory, today=now.date(), horizon_days=horizon
    )
    ranked = recommended_replenishments(replenishment, limit=max(10, len(replenishment)))
    replenishment_items = ranked[:50] if include_items else []
    truncated = len(ranked) > len(replenishment_items)
    return {
        "ok": True,
        "read_model": "dashboard",
        "as_of": now.isoformat(),
        "complete": not truncated,
        "truncated": truncated,
        "scope": {
            "kind": "operational_view",
            "entity_existence_authoritative": False,
            "empty_means": "no_matching_rows_in_this_operational_view",
        },
        "inventory_value": inventory_value_net,
        "new_orders": new_orders,
        "issued_today": issued_today,
        "ready_to_issue_today": ready_to_issue_today,
        "overdue_count": overdue_count,
        "overdue_amount": overdue_amount,
        "replenishment_count": len(ranked),
        "replenishment_items": replenishment_items,
        "products_count": products_count,
        "current_orders": current_orders,
        "active_china_orders": active_china_orders,
        "stock_units": stock_units,
        "in_delivery_units": in_delivery_units,
        "ready_to_issue_labels": ready_to_issue_labels,
        "recent_orders": recent_orders,
        "status_counts": {
            "new": status_new, "work": status_work, "completed": status_done,
            "cancelled": status_cancelled, "total": status_total,
        },
    }
