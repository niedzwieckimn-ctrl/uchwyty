"""Audyt i naprawa wysłanych zamówień, które nie zdjęły magazynu.

Najpierw uruchom bez parametrów (tylko raport), a po weryfikacji z --apply.
"""
import argparse

from app import (
    conn,
    issue_order_stock,
    maybe_pull_shared_from_supabase,
    supabase_enabled,
    sync_local_rows_to_supabase,
)


def candidates(cur):
    cur.execute(
        """
        SELECT o.id, o.order_no, o.created_at, o.shipped_at, o.status,
               COALESCE(SUM(oi.qty), 0) AS item_qty
        FROM orders o
        JOIN order_items oi ON oi.order_id=o.id
        WHERE COALESCE(o.warehouse_issued, 0)=0
          AND TRIM(COALESCE(o.shipped_at, ''))<>''
          AND LOWER(COALESCE(o.status, '')) IN ('shipped', 'issued', 'completed')
        GROUP BY o.id
        ORDER BY o.shipped_at, o.id
        """
    )
    return [dict(row) for row in cur.fetchall()]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="odejmij wykazane zamówienia")
    args = parser.parse_args()

    maybe_pull_shared_from_supabase(force=True)
    database = conn()
    cur = database.cursor()
    rows = candidates(cur)

    print(f"Podejrzane wysłane zamówienia: {len(rows)}")
    if rows:
        print(f"Zakres wysyłek: {rows[0]['shipped_at']} -> {rows[-1]['shipped_at']}")
        for row in rows:
            print(
                f"- id={row['id']} nr={row['order_no']} status={row['status']} "
                f"wysłano={row['shipped_at']} sztuk={row['item_qty']}"
            )

    if not args.apply:
        database.close()
        print("Tryb raportu: niczego nie zmieniono. Użyj --apply po sprawdzeniu listy.")
        return

    changed_orders = []
    changed_products = []
    try:
        for row in rows:
            product_ids = issue_order_stock(cur, int(row["id"]))
            if product_ids:
                changed_orders.append(int(row["id"]))
                changed_products.extend(product_ids)
        database.commit()
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()

    if supabase_enabled():
        if changed_orders:
            sync_local_rows_to_supabase("orders", "id", changed_orders)
        if changed_products:
            sync_local_rows_to_supabase("stock", "product_id", sorted(set(changed_products)))
    print(f"Naprawiono zamówienia: {len(changed_orders)}")


if __name__ == "__main__":
    main()
