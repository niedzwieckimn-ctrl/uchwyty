"""Shared read-only order fulfillment calculation used by UI and Business Operations."""

ISSUABLE_ORDER_STATUSES = frozenset({
    "new", "pending", "unconfirmed", "confirmed", "packed",
    "packed_partial", "partially_shipped",
})


def calculate_fulfillment_readiness(db):
    """Allocate physical stock to complete orders in oldest-first order.

    This preserves the dashboard rule: invoiced/allocated quantities no longer
    need stock, a ready order consumes its pool, and an incomplete order does
    not partially consume stock that can satisfy a later complete order.
    """
    status_ph = ",".join("?" for _ in ISSUABLE_ORDER_STATUSES)
    rows = db.execute(f"""
      SELECT o.id, o.order_no, o.customer_id, o.customer_name, o.status,
             o.created_at, o.note, oi.product_id,
             COALESCE(NULLIF(TRIM(oi.sku),''), p.sku, '') AS sku,
             p.model, p.name,
             SUM(MAX(0, oi.qty - COALESCE((
               SELECT SUM(ia.qty) FROM invoice_allocations ia
               WHERE ia.order_item_id=oi.id
             ),0))) AS required_qty,
             SUM(MAX(0, oi.qty)) AS ordered_qty
      FROM orders o
      JOIN order_items oi ON oi.order_id=o.id
      LEFT JOIN products p ON p.id=oi.product_id
      WHERE LOWER(COALESCE(o.status,'')) IN ({status_ph})
        AND COALESCE(o.warehouse_issued,0)=0
      GROUP BY o.id, oi.product_id
      HAVING required_qty > 0
      ORDER BY o.created_at, o.id, oi.product_id
    """, tuple(sorted(ISSUABLE_ORDER_STATUSES))).fetchall()
    stock_pool = {
        int(row["product_id"]): max(0, int(row["qty"] or 0))
        for row in db.execute("SELECT product_id, MAX(0, COALESCE(qty,0)) AS qty FROM stock").fetchall()
    }
    grouped = {}
    for row in rows:
        record = dict(row)
        grouped.setdefault(int(row["id"]), {"order": record, "items": []})["items"].append(record)

    results = []
    for candidate in grouped.values():
        order, items = candidate["order"], candidate["items"]
        shortages = []
        for item in items:
            product_id = int(item["product_id"])
            required = max(0, int(item["required_qty"] or 0))
            available = stock_pool.get(product_id, 0)
            shortage = max(0, required - available)
            if shortage:
                shortages.append({
                    "product_id": product_id, "sku": item.get("sku") or "",
                    "model": item.get("model"), "name": item.get("name"),
                    "required_quantity": required, "available_quantity": available,
                    "shortage_quantity": shortage,
                })
        ready = bool(items) and not shortages
        if ready:
            for item in items:
                product_id = int(item["product_id"])
                stock_pool[product_id] = stock_pool.get(product_id, 0) - max(0, int(item["required_qty"] or 0))
        results.append({
            "order_id": int(order["id"]), "order_number": order.get("order_no") or "",
            "customer_id": order.get("customer_id"), "customer_name": order.get("customer_name") or "",
            "order_status": order.get("status") or "", "created_at": order.get("created_at") or "",
            "note": order.get("note") or "", "total_items": len(items),
            "total_units": sum(max(0, int(item["required_qty"] or 0)) for item in items),
            "ready": ready, "missing_items": shortages, "shortages": list(shortages),
        })
    return results
