"""Stock comparison and guarded one-SKU stock SET."""
from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from inventory_analytics import build_replenishment_analysis
from orderchamp_client import OrderchampError


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def read_local_availability(db_path):
    """Reuse the /stock engine inside a short, read-only SQLite snapshot."""
    path = Path(db_path).resolve()
    if not path.is_file():
        raise OrderchampError("LOCAL_DATABASE_NOT_FOUND")
    connection = None
    try:
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        # The engine closes the connection before returning. Finally also covers errors.
        rows = build_replenishment_analysis(lambda: connection)
        return [{key: row[key] for key in ("id", "sku", "available_qty")} for row in rows]
    except (sqlite3.Error, KeyError, TypeError, ValueError, OverflowError):
        raise OrderchampError("LOCAL_AVAILABILITY_READ_FAILED") from None
    finally:
        if connection is not None:
            connection.close()


def _candidate_quantity(value):
    # Do not silently round fractional/corrupt values or treat missing data as zero.
    if type(value) is not int or value > 2147483647:
        raise OrderchampError("INVALID_LOCAL_AVAILABLE")
    return max(0, value)


def dry_run_stock_sync(client, db_path, *, sku=None):
    started_at = utc_now()
    local = read_local_availability(db_path)
    if sku is not None:
        local = [row for row in local if row["sku"] == sku]
        if not local:
            raise OrderchampError("LOCAL_SKU_NOT_FOUND")
    counts = Counter(row["sku"] for row in local if isinstance(row["sku"], str))
    report = {
        "schema_version": 1, "mode": "dry_run", "started_at": started_at,
        "availability_source": "inventory_analytics.build_replenishment_analysis.available_qty",
        "local_snapshot_at": utc_now(), "local_database": str(Path(db_path).resolve()),
        "writes_enabled": False,
        "would_send_definition": "local available clamped to zero; SET only for a single unreserved primary level without increasing its current quantity",
        "write_blockers": [],
        "source_freshness": "local SQLite snapshot; Supabase refresh not triggered",
        "rows": [], "warnings": [],
    }
    if not local:
        report["warnings"].append("EMPTY_LOCAL_CATALOG")
    connection_test = client.test_connection()
    report["connection_test"] = connection_test
    for row in sorted(local, key=lambda r: str(r.get("sku") or "")):
        item = {"product_id": row["id"], "sku": row["sku"],
                "local_available": row["available_qty"], "would_send": None,
                "status": "ERROR", "warnings": [], "write_eligible": False}
        try:
            code = row["sku"]
            if not isinstance(code, str) or not code or code != code.strip():
                raise OrderchampError("INVALID_LOCAL_SKU")
            if counts[code] != 1:
                raise OrderchampError("DUPLICATE_LOCAL_SKU")
            item["would_send"] = _candidate_quantity(row["available_qty"])
            variant = client.resolve_variant_by_sku(code)
            if variant is None:
                item["status"] = "MISSING"
                item["warnings"].append("REMOTE_SKU_NOT_FOUND")
            else:
                item.update(status="MATCHED", remote=variant)
                if variant["inventory_policy"] == "CONTINUE":
                    item["warnings"].append("REMOTE_BACKORDERS_ALLOWED")
                if not variant["levels_complete"]:
                    item["warnings"].append("REMOTE_LEVELS_INCOMPLETE")
                primary = [level for level in variant["levels"] if level["is_primary"] is True]
                if len(primary) != 1:
                    item["warnings"].append("PRIMARY_LOCATION_UNRESOLVED")
                elif primary[0]["quantity"] is None or primary[0]["available_quantity"] is None:
                    item["warnings"].append("REMOTE_QUANTITY_UNKNOWN")
                elif primary[0]["quantity"] != primary[0]["available_quantity"]:
                    item["warnings"].append("REMOTE_QUANTITY_DIFFERS_FROM_AVAILABLE")
                elif item["would_send"] is not None and item["would_send"] > primary[0]["quantity"]:
                    item["warnings"].append("REMOTE_BELOW_LOCAL_ORDER_RECONCILIATION_REQUIRED")
                if len(variant["levels"]) > 1:
                    item["warnings"].append("MULTIPLE_REMOTE_LOCATIONS")
                item["write_eligible"] = bool(
                    variant["inventory_policy"] == "DENY" and variant["levels_complete"]
                    and len(variant["levels"]) == 1 and primary == variant["levels"]
                    and primary[0]["quantity"] is not None
                    and primary[0]["quantity"] == primary[0]["available_quantity"]
                    and variant["inventory_quantity"] == primary[0]["quantity"]
                    and item["would_send"] <= primary[0]["quantity"])
        except OrderchampError as exc:
            item["error_code"] = exc.code
        report["rows"].append(item)
    statuses = Counter(item["status"] for item in report["rows"])
    report["summary"] = {
        "local_sku": len(local), "matched": statuses["MATCHED"],
        "missing": statuses["MISSING"], "errors": statuses["ERROR"],
        "synchronized": 0, "skipped": len(local),
        "warning_rows": sum(bool(row["warnings"]) for row in report["rows"]),
        "http_requests": client.request_count,
    }
    report["completed_at"] = utc_now()
    return report


def push_one_stock(client, db_path, *, sku, expected_local, expected_remote_updated_at,
                   initial_seed=False):
    """Set exactly one proven, unreserved primary level from a fresh local read.

    A changed local quantity or Orderchamp level makes the prior dry-run stale.
    A timeout after mutation has an unknown outcome; caller must inspect again.
    """
    local = [row for row in read_local_availability(db_path) if row['sku'] == sku]
    if len(local) != 1 or type(expected_local) is not int or not isinstance(expected_remote_updated_at, str):
        raise OrderchampError('STOCK_SET_PRECONDITION_FAILED')
    quantity = _candidate_quantity(local[0]['available_qty'])
    if quantity != expected_local:
        raise OrderchampError('LOCAL_STOCK_CHANGED')
    variant = client.resolve_variant_by_sku(sku)
    if not variant or not variant['levels_complete'] or len(variant['levels']) != 1:
        raise OrderchampError('REMOTE_STOCK_SCOPE_UNSAFE')
    level = variant['levels'][0]
    if level['is_primary'] is not True or variant['inventory_policy'] != 'DENY':
        raise OrderchampError('REMOTE_STOCK_SCOPE_UNSAFE')
    if (level['updated_at'] != expected_remote_updated_at or
            level['quantity'] is None or level['available_quantity'] is None or
            level['quantity'] != level['available_quantity'] or
            variant['inventory_quantity'] != level['quantity']):
        raise OrderchampError('REMOTE_STOCK_CHANGED_OR_RESERVED')
    # A lower remote count may reflect a sale not yet reserved locally. A SET
    # that increases it would silently put the sold units back on sale.
    if quantity > level['quantity']:
        if not initial_seed or level['quantity'] != 0:
            raise OrderchampError('REMOTE_BELOW_LOCAL_ORDER_RECONCILIATION_REQUIRED')
        # This one-time exception is safe only before the first channel order.
        # Re-read the level after the order check so a stale zero cannot be used.
        if client.probe_orders()['all_order_count'] != 0:
            raise OrderchampError('INITIAL_SEED_ORDERS_PRESENT')
        latest = client.resolve_variant_by_sku(sku)
        if (not latest or latest['id'] != variant['id'] or
                latest['inventory_policy'] != 'DENY' or
                not latest['levels_complete'] or len(latest['levels']) != 1 or
                latest['levels'][0] != level or latest['inventory_quantity'] != 0):
            raise OrderchampError('REMOTE_STOCK_CHANGED_OR_RESERVED')
    if level['quantity'] == quantity:
        return {'ok': True, 'sku': sku, 'quantity': quantity, 'status': 'ALREADY_CURRENT',
                'wrote': False}
    client.set_inventory_level(level['id'], quantity)
    verified = client.resolve_variant_by_sku(sku)
    if (not verified or not verified['levels_complete'] or len(verified['levels']) != 1 or
            verified['levels'][0]['id'] != level['id'] or
            verified['levels'][0]['quantity'] != quantity):
        raise OrderchampError('STOCK_SET_NOT_VERIFIED')
    return {'ok': True, 'sku': sku, 'quantity': quantity, 'status': 'VERIFIED', 'wrote': True}
