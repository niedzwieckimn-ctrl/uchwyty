"""Shared read-only order fulfillment calculation used by UI and Business Operations."""

import re

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


def customer_shipping_options(db, customer, limit=20):
    """Read remaining order lines and physical stock without changing reservations.

    The quantities are a provisional packing ceiling for this customer, not a
    reservation or a confirmation that a parcel can be sent.
    """
    query = " ".join(str(customer or "").casefold().split())
    if not query:
        return {"ok": True, "customer": "", "ambiguous": False,
                "matches": [], "orders": [], "truncated": False}
    status_ph = ",".join("?" for _ in ISSUABLE_ORDER_STATUSES)
    names = [str(row[0] or "").strip() for row in db.execute(
        f"SELECT DISTINCT customer_name FROM orders WHERE LOWER(COALESCE(status,'')) IN ({status_ph}) "
        "AND COALESCE(warehouse_issued,0)=0", tuple(sorted(ISSUABLE_ORDER_STATUSES))
    ).fetchall()]
    query_words = re.findall(r"\w+", query)
    matches = sorted({name for name in names if
                      query in " ".join(name.casefold().split()) or
                      (query_words and all(len(word) >= 5 and any(
                          candidate.startswith(word[:5]) for candidate in re.findall(r"\w+", name.casefold())
                      ) for word in query_words))})
    if len(matches) != 1:
        return {"ok": True, "customer": "", "ambiguous": len(matches) > 1,
                "matches": matches[:5], "orders": [], "truncated": len(matches) > 5}
    name = matches[0]
    rows = db.execute(f"""
      SELECT o.id, o.order_no, o.created_at, oi.product_id,
             COALESCE(NULLIF(TRIM(oi.sku),''),p.sku,'') AS sku,
             p.model, p.name,
             SUM(MAX(0,oi.qty-COALESCE((
               SELECT SUM(ia.qty) FROM invoice_allocations ia
               WHERE ia.order_item_id=oi.id
             ),0))) AS remaining_qty
      FROM orders o
      JOIN order_items oi ON oi.order_id=o.id
      LEFT JOIN products p ON p.id=oi.product_id
      WHERE o.customer_name=? AND LOWER(COALESCE(o.status,'')) IN ({status_ph})
        AND COALESCE(o.warehouse_issued,0)=0
      GROUP BY o.id,oi.product_id
      HAVING remaining_qty>0
      ORDER BY o.created_at,o.id,oi.product_id
    """, (name, *sorted(ISSUABLE_ORDER_STATUSES))).fetchall()
    order_ids = list(dict.fromkeys(int(row["id"]) for row in rows))
    selected_ids = set(order_ids[:limit])
    selected = [row for row in rows if int(row["id"]) in selected_ids]
    product_ids = sorted({int(row["product_id"]) for row in selected})
    stock = {}
    if product_ids:
        placeholders = ",".join("?" for _ in product_ids)
        stock = {int(row["product_id"]): max(0,int(row["qty"] or 0))
                 for row in db.execute(
                     f"SELECT product_id,qty FROM stock WHERE product_id IN ({placeholders})",
                     product_ids).fetchall()}
    pool = dict(stock)
    orders = []
    for row in selected:
        order_id = int(row["id"])
        if not orders or orders[-1]["order_id"] != order_id:
            orders.append({"order_id": order_id, "order_number": row["order_no"] or "",
                           "items": [], "remaining_units": 0, "potential_units_now": 0})
        product_id = int(row["product_id"])
        remaining = max(0,int(row["remaining_qty"] or 0))
        potential = min(remaining,pool.get(product_id,0))
        pool[product_id] = pool.get(product_id,0)-potential
        orders[-1]["items"].append({
            "sku": row["sku"] or "", "model": row["model"] or row["name"] or "",
            "remaining_qty": remaining, "on_hand_qty": stock.get(product_id,0),
            "potential_now_qty": potential, "missing_now_qty": remaining-potential,
        })
        orders[-1]["remaining_units"] += remaining
        orders[-1]["potential_units_now"] += potential
    return {"ok": True, "customer": name, "ambiguous": False, "matches": [],
            "orders": orders, "truncated": len(order_ids) > limit,
            "stock_basis": "physical_snapshot_not_reserved"}
