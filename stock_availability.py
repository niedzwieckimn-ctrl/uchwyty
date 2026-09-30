"""Small, shared stock/read model. No analytics, network or writes."""
ACTIVE_ORDER_STATUSES = {
    "new", "pending", "unconfirmed", "confirmed", "packed", "packed_partial",
    "in_delivery", "shipped", "partially_shipped",
}


def read_stock_availability(connection, sku=None):
    statuses = tuple(sorted(ACTIVE_ORDER_STATUSES))
    placeholders = ','.join('?' for _ in statuses)
    # Filtering the product set first also restricts allocation aggregation for one SKU.
    rows = connection.execute(f"""
        WITH selected AS (
          SELECT id,sku,model,name,ean FROM products
          WHERE COALESCE(archived,0)=0 AND (? IS NULL OR sku=?)
        ), eligible AS (
          SELECT i.id,i.product_id,i.qty FROM order_items i
          JOIN selected p ON p.id=i.product_id JOIN orders o ON o.id=i.order_id
          WHERE COALESCE(o.warehouse_issued,0)=0
            AND lower(COALESCE(o.status,'')) IN ({placeholders})
        ), allocated AS (
          SELECT a.order_item_id,SUM(a.qty) AS qty
          FROM invoice_allocations a JOIN eligible i ON i.id=a.order_item_id
          GROUP BY a.order_item_id
        ), reserved AS (
          SELECT i.product_id,SUM(MAX(0,i.qty-COALESCE(a.qty,0))) AS qty
          FROM eligible i
          LEFT JOIN allocated a ON a.order_item_id=i.id
          GROUP BY i.product_id
        )
        SELECT p.*,COALESCE(s.qty,0) AS stock_qty,
          MAX(0,COALESCE(r.qty,0)) AS reserved_qty,
          MAX(0,COALESCE(s.qty,0)-MAX(0,COALESCE(r.qty,0))) AS available_qty
        FROM selected p LEFT JOIN stock s ON s.product_id=p.id
        LEFT JOIN reserved r ON r.product_id=p.id ORDER BY p.sku
    """, (sku, sku, *statuses)).fetchall()
    return [dict(row) for row in rows]
