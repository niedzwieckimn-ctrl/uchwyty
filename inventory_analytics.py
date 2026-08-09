# -*- coding: utf-8 -*-
"""Wspólne obliczenia popytu i uzupełniania magazynu.

Moduł nie zapisuje wyniku w bazie. Reorder score jest wyliczany z aktualnych
danych, dzięki czemu dashboard, cash flow i przyszła karta produktu mogą
korzystać z jednej definicji.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher


ACTIVE_ORDER_STATUSES = {
    "new", "pending", "unconfirmed", "confirmed", "packed", "in_delivery", "shipped"
}
INCOMING_PACKAGE_STATUSES = {"planned", "ordered", "shipped"}
CANCELLED_ORDER_STATUSES = {"cancelled", "canceled", "deleted", "usuniete", "anulowane"}


def _text(value) -> str:
    return str(value or "").strip()


def _key(value) -> str:
    return re.sub(r"[^a-z0-9]+", "", _text(value).lower())


def _iso_day(value) -> date | None:
    raw = _text(value)[:10]
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except Exception:
        return None


def _level(score: int) -> tuple[str, str]:
    if score >= 75:
        return "critical", "Krytyczny"
    if score >= 50:
        return "high", "Wysoki"
    if score >= 25:
        return "medium", "Średni"
    return "low", "Niski"


def _confidence(sales_90: int, sale_days_90: int, buyers_90: int, searches_30: int, search_clients_30: int):
    evidence = 0
    evidence += min(3, sales_90 // 4)
    evidence += min(2, sale_days_90 // 3)
    evidence += min(2, buyers_90)
    evidence += min(2, searches_30 // 3)
    evidence += min(1, search_clients_30 // 2)
    if evidence >= 7:
        return "high", "Wysoka"
    if evidence >= 3:
        return "medium", "Średnia"
    return "low", "Niska"


def _match_unresolved_query(query: str, products: list[dict], exact_aliases: dict[str, set[int]]) -> set[int]:
    """Ostrożne dopasowanie zapytania bez wyników do produktu/rodziny."""
    query_key = _key(query)
    if len(query_key) < 3:
        return set()
    if query_key in exact_aliases:
        return set(exact_aliases[query_key])

    # Krótkie oznaczenia liczbowe, np. "320", są często rozstawem lub częścią
    # serii. Traktujemy je jako dopasowanie rodziny, ale dopiero powtarzające się
    # wyszukiwania mogą wywołać sugestię zakupu.
    if len(query_key) == 3 and query_key.isdigit():
        family_ids = set()
        for product in products:
            if any(query_key in _key(product.get(field)) for field in ("sku", "model", "name")):
                family_ids.add(int(product["id"]))
        if family_ids:
            return family_ids

    contained = set()
    if len(query_key) >= 4:
        for alias, product_ids in exact_aliases.items():
            if len(alias) >= 4 and (alias in query_key or query_key in alias):
                contained.update(product_ids)
        if contained:
            return contained

    if len(query_key) >= 5:
        best_ratio = 0.0
        best_ids = set()
        for alias, product_ids in exact_aliases.items():
            if len(alias) < 5 or abs(len(alias) - len(query_key)) > 2:
                continue
            ratio = SequenceMatcher(None, query_key, alias).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_ids = set(product_ids)
            elif ratio == best_ratio:
                best_ids.update(product_ids)
        if best_ratio >= 0.90:
            return best_ids
    return set()


def build_replenishment_analysis(conn_factory, today: date | None = None, horizon_days: int = 60) -> list[dict]:
    """Zwraca analizę dla każdego SKU, posortowaną wg reorder_score."""
    today = today or date.today()
    horizon_days = int(horizon_days or 60)
    if horizon_days not in (45, 60, 90):
        horizon_days = 60
    start_30 = today - timedelta(days=29)
    start_prev_30 = today - timedelta(days=59)
    start_90 = today - timedelta(days=89)

    c = conn_factory()
    cur = c.cursor()
    cur.execute("""
      SELECT p.id, p.sku, p.model, p.name, p.ean, COALESCE(s.qty,0) AS stock_qty
      FROM products p
      LEFT JOIN stock s ON s.product_id=p.id
      ORDER BY p.sku
    """)
    products = [dict(row) for row in cur.fetchall()]

    active_statuses = tuple(sorted(ACTIVE_ORDER_STATUSES))
    active_placeholders = ",".join("?" for _ in active_statuses)
    cur.execute(f"""
      SELECT oi.product_id, COALESCE(SUM(oi.qty),0) AS reserved_qty
      FROM order_items oi
      JOIN orders o ON o.id=oi.order_id
      WHERE COALESCE(o.warehouse_issued,0)=0
        AND lower(COALESCE(o.status,'')) IN ({active_placeholders})
      GROUP BY oi.product_id
    """, active_statuses)
    reservations = {int(row["product_id"]): int(row["reserved_qty"] or 0) for row in cur.fetchall()}

    incoming_statuses = tuple(sorted(INCOMING_PACKAGE_STATUSES))
    incoming_placeholders = ",".join("?" for _ in incoming_statuses)
    cur.execute(f"""
      SELECT ci.product_id, COALESCE(SUM(ci.qty),0) AS incoming_qty
      FROM china_items ci
      JOIN china_packages cp ON cp.id=ci.package_id
      WHERE lower(COALESCE(cp.status,'')) IN ({incoming_placeholders})
      GROUP BY ci.product_id
    """, incoming_statuses)
    incoming = {int(row["product_id"]): int(row["incoming_qty"] or 0) for row in cur.fetchall()}

    cancelled_statuses = tuple(sorted(CANCELLED_ORDER_STATUSES))
    cancelled_placeholders = ",".join("?" for _ in cancelled_statuses)
    cur.execute(f"""
      SELECT oi.product_id, oi.qty, o.created_at,
             COALESCE(NULLIF(lower(trim(o.customer_email)),''), 'customer:' || COALESCE(o.customer_id, o.id)) AS buyer_key
      FROM order_items oi
      JOIN orders o ON o.id=oi.order_id
      WHERE (COALESCE(o.warehouse_issued,0)=1 OR lower(COALESCE(o.status,''))='issued')
        AND lower(COALESCE(o.status,'')) NOT IN ({cancelled_placeholders})
        AND date(o.created_at) >= date(?)
    """, (*cancelled_statuses, start_90.isoformat()))
    sales_rows = [dict(row) for row in cur.fetchall()]

    cur.execute("""
      SELECT customer_email, query, product_sku, product_model, product_name,
             results_count, created_at
      FROM client_search_logs
      WHERE date(created_at) >= date(?)
      ORDER BY created_at
    """, (start_prev_30.isoformat(),))
    search_rows = [dict(row) for row in cur.fetchall()]
    c.close()

    sales = defaultdict(lambda: {
        "sales_30": 0, "sales_prev_30": 0, "sales_90": 0,
        "sale_days": set(), "buyers": set(), "last_sale_at": "",
    })
    for row in sales_rows:
        product_id = int(row.get("product_id") or 0)
        sold_day = _iso_day(row.get("created_at"))
        if not product_id or not sold_day:
            continue
        qty = max(0, int(row.get("qty") or 0))
        stat = sales[product_id]
        stat["sales_90"] += qty
        stat["sale_days"].add(sold_day.isoformat())
        stat["buyers"].add(_text(row.get("buyer_key")) or "unknown")
        stat["last_sale_at"] = max(stat["last_sale_at"], _text(row.get("created_at")))
        if sold_day >= start_30:
            stat["sales_30"] += qty
        elif sold_day >= start_prev_30:
            stat["sales_prev_30"] += qty

    product_by_id = {int(p["id"]): p for p in products}
    exact_aliases = defaultdict(set)
    product_ids_by_sku = defaultdict(set)
    for product in products:
        product_id = int(product["id"])
        for value in (product.get("sku"), product.get("model"), product.get("name"), product.get("ean")):
            alias = _key(value)
            if len(alias) >= 3:
                exact_aliases[alias].add(product_id)
        for value in (product.get("sku"), product.get("model")):
            alias = _key(value)
            if alias:
                product_ids_by_sku[alias].add(product_id)

    search_events = defaultdict(lambda: {"current": set(), "previous": set(), "clients": set(), "unresolved": 0})
    for row in search_rows:
        searched_day = _iso_day(row.get("created_at"))
        if not searched_day:
            continue
        product_ids = set()
        for value in (row.get("product_sku"), row.get("product_model")):
            product_ids.update(product_ids_by_sku.get(_key(value), set()))
        is_unresolved = int(row.get("results_count") or 0) == 0 or not product_ids
        if not product_ids and is_unresolved:
            product_ids = _match_unresolved_query(row.get("query"), products, exact_aliases)
        if not product_ids:
            continue
        client = _text(row.get("customer_email")).lower() or "unknown"
        event_key = (client, _key(row.get("query")), _text(row.get("created_at")))
        for product_id in product_ids:
            stat = search_events[product_id]
            if searched_day >= start_30:
                stat["current"].add(event_key)
                stat["clients"].add(client)
                if is_unresolved:
                    stat["unresolved"] += 1
            elif searched_day >= start_prev_30:
                stat["previous"].add(event_key)

    result = []
    for product_id, product in product_by_id.items():
        sold = sales[product_id]
        searched = search_events[product_id]
        sales_30 = int(sold["sales_30"])
        sales_prev_30 = int(sold["sales_prev_30"])
        sales_90 = int(sold["sales_90"])
        sale_days_90 = len(sold["sale_days"])
        buyers_90 = len(sold["buyers"])
        searches_30 = len(searched["current"])
        searches_prev_30 = len(searched["previous"])
        search_clients_30 = len(searched["clients"])

        stock_qty = max(0, int(product.get("stock_qty") or 0))
        reserved_qty = max(0, reservations.get(product_id, 0))
        available_qty = max(0, stock_qty - reserved_qty)
        incoming_qty = max(0, incoming.get(product_id, 0))
        reserved_incoming = min(incoming_qty, max(0, reserved_qty - stock_qty))
        available_incoming = max(0, incoming_qty - reserved_incoming)

        recent_daily = sales_30 / 30.0
        long_daily = sales_90 / 90.0
        if sales_90:
            forecast_daily = (recent_daily * 0.65) + (long_daily * 0.35)
        else:
            forecast_daily = 0.0
        # Wyszukiwania nie są sprzedażą. Przy powtarzalnym zainteresowaniu mogą
        # jednak uzasadnić małą partię testową, zawsze z niską pewnością.
        if sales_90 == 0 and searches_30 >= 2:
            search_monthly_proxy = min(searches_30, max(1, search_clients_30) * 3) * 0.10
            forecast_daily = max(forecast_daily, search_monthly_proxy / 30.0)
        sales_trend = (sales_30 - sales_prev_30) / max(1, sales_prev_30)
        search_trend = (searches_30 - searches_prev_30) / max(1, searches_prev_30)
        target_qty = int(math.ceil(forecast_daily * horizon_days))
        suggested_qty = max(0, target_qty - available_qty - available_incoming)
        coverage_days = None if forecast_daily <= 0 else round((available_qty + available_incoming) / forecast_daily)

        last_sale_day = _iso_day(sold["last_sale_at"])
        days_since_sale = (today - last_sale_day).days if last_sale_day else None
        regularity = min(1.0, sale_days_90 / max(3.0, min(float(sales_90), 12.0))) if sales_90 else 0.0

        if sales_90 == 0 and searches_30 == 0:
            score = 0
            suggested_qty = 0
        else:
            velocity_points = min(30.0, sales_30 * 1.5 + sales_90 * 0.20)
            if available_qty == 0 and (sales_90 > 0 or searches_30 >= 2):
                stock_points = 25.0
            elif coverage_days is None:
                stock_points = 0.0
            elif coverage_days <= 14:
                stock_points = 22.0
            elif coverage_days <= 30:
                stock_points = 16.0
            elif coverage_days <= horizon_days:
                stock_points = 8.0
            else:
                stock_points = 0.0
            search_points = min(12.0, searches_30 * 1.1) + min(8.0, search_clients_30 * 2.0)
            trend_points = min(8.0, max(0.0, sales_trend) * 5.0) + min(4.0, max(0.0, search_trend) * 2.0)
            regularity_points = regularity * 7.0
            recency_points = 6.0 if days_since_sale is not None and days_since_sale <= 30 else (3.0 if days_since_sale is not None and days_since_sale <= 90 else 0.0)
            incoming_relief = min(25.0, (available_incoming / max(1, target_qty)) * 25.0) if target_qty else 0.0
            score = int(round(max(0.0, min(100.0, velocity_points + stock_points + search_points + trend_points + regularity_points + recency_points - incoming_relief))))

        level_key, level_label = _level(score)
        confidence_key, confidence_label = _confidence(sales_90, sale_days_90, buyers_90, searches_30, search_clients_30)
        stockout_date = today + timedelta(days=coverage_days) if coverage_days is not None and coverage_days <= 3650 else None

        reasons = []
        if sales_30:
            reasons.append(f"sprzedano {sales_30} szt. w 30 dni")
        if sales_90:
            reasons.append(f"sprzedano {sales_90} szt. w 90 dni")
        if available_qty <= 1 and (sales_90 or searches_30):
            reasons.append(f"dostępny stan to {available_qty} szt.")
        if searches_30:
            reasons.append(f"{searches_30} wyszukiwań od {search_clients_30} klientów")
        if available_incoming:
            reasons.append(f"{available_incoming} szt. niezarezerwowane w drodze obniża priorytet")
        elif sales_90 or searches_30:
            reasons.append("brak niezarezerwowanego towaru w drodze")
        if sales_trend > 0.25:
            reasons.append("popyt sprzedażowy rośnie")

        result.append({
            **product,
            "stock_qty": stock_qty,
            "reserved_qty": reserved_qty,
            "available_qty": available_qty,
            "incoming_qty": incoming_qty,
            "reserved_incoming": reserved_incoming,
            "available_incoming": available_incoming,
            "sales_30": sales_30,
            "sales_prev_30": sales_prev_30,
            "sales_90": sales_90,
            "sale_days_90": sale_days_90,
            "buyers_90": buyers_90,
            "last_sale_at": sold["last_sale_at"],
            "days_since_sale": days_since_sale,
            "searches_30": searches_30,
            "searches_prev_30": searches_prev_30,
            "search_clients_30": search_clients_30,
            "unresolved_searches_30": int(searched["unresolved"]),
            "sales_trend": round(sales_trend, 2),
            "search_trend": round(search_trend, 2),
            "avg_daily_sales": round(forecast_daily, 3),
            "avg_monthly_sales": round(forecast_daily * 30.0, 1),
            "coverage_days": coverage_days,
            "estimated_stockout": stockout_date.isoformat() if stockout_date else "",
            "target_days": horizon_days,
            "target_qty": target_qty,
            "suggested_qty": suggested_qty,
            "reorder_score": score,
            "priority": level_key,
            "priority_label": level_label,
            "confidence": confidence_key,
            "confidence_label": confidence_label,
            "reasons": reasons,
        })

    return sorted(result, key=lambda row: (row["reorder_score"], row["suggested_qty"], row["sales_30"]), reverse=True)


def recommended_replenishments(rows: list[dict], limit: int = 10) -> list[dict]:
    """Usuwa martwe SKU i pozycje wystarczająco zabezpieczone dostawą."""
    recommended = [
        row for row in rows
        if row.get("suggested_qty", 0) > 0
        and row.get("reorder_score", 0) >= 15
        and (row.get("sales_90", 0) > 0 or row.get("searches_30", 0) >= 2)
    ]
    return recommended[:max(1, int(limit or 10))]
