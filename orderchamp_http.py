"""Admin-only HTTP entry points to the unchanged Orderchamp READ service."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
from functools import wraps

import requests
from flask import Blueprint, jsonify, render_template, request, session

from internal_rbac import ACTOR_HUMAN, ALLOW, current_actor_context
from orderchamp_client import API_URL, OrderchampClient, OrderchampError
from orderchamp_stock_sync import dry_run_stock_sync, push_one_stock, read_local_availability


class HttpBudget:
    """Limit network/retry time for HTTP without modifying CLI client behavior."""
    def __init__(self, seconds=12, *, clock=time.monotonic, sleeper=time.sleep):
        self.clock, self.sleeper = clock, sleeper
        self.deadline = clock() + seconds

    def remaining(self):
        remaining = self.deadline - self.clock()
        if remaining <= 0.1:
            raise OrderchampError("HTTP_TIME_BUDGET_EXCEEDED")
        return remaining

    def sleep(self, delay):
        if delay + 0.1 >= self.remaining():
            raise OrderchampError("HTTP_TIME_BUDGET_EXCEEDED")
        self.sleeper(delay)


class BudgetSession(requests.Session):
    def __init__(self, budget):
        super().__init__()
        self.budget = budget

    def post(self, url, **kwargs):
        remaining = self.budget.remaining()
        # Shorter transport timeouts only for HTTP. No queries/retries are copied.
        kwargs["timeout"] = (min(2, remaining / 2), min(3, remaining / 2))
        result = super().post(url, **kwargs)
        try:
            self.budget.remaining()
        except OrderchampError:
            result.close()
            raise
        return result


def http_client(budget):
    return OrderchampClient(
        os.environ.get("ORDERCHAMP_API_TOKEN", ""),
        os.environ.get("ORDERCHAMP_API_URL", API_URL),
        session=BudgetSession(budget), sleep=budget.sleep, monotonic=budget.clock,
    )


ERROR_STATUS = {
    "LOCAL_SKU_NOT_FOUND": 404, "INVALID_LOCAL_SKU": 422,
    "LOCAL_DATABASE_NOT_FOUND": 503, "LOCAL_AVAILABILITY_READ_FAILED": 503,
    "TOKEN_MISSING_OR_INVALID": 503, "API_URL_NOT_ALLOWED": 503,
    "HTTP_TIME_BUDGET_EXCEEDED": 504, "TIMEOUT": 504,
    "AUTH_OR_SCOPE_ERROR": 502, "INVALID_API_RESPONSE": 502,
    "GRAPHQL_ERROR": 502, "INVALID_JSON": 502, "HTTP_ERROR": 502,
    "HTTP_CLIENT_ERROR": 502, "NETWORK_ERROR": 502,
    "UPSTREAM_UNAVAILABLE": 502, "RATE_LIMITED": 429,
    "RATE_LIMIT_WAIT_TOO_LONG": 429, "REMOTE_SKU_MISMATCH": 502,
    "STOCK_SET_PRECONDITION_FAILED": 409, "LOCAL_STOCK_CHANGED": 409,
    "REMOTE_STOCK_SCOPE_UNSAFE": 409, "REMOTE_STOCK_CHANGED_OR_RESERVED": 409,
    "REMOTE_BELOW_LOCAL_ORDER_RECONCILIATION_REQUIRED": 409,
    "INITIAL_SEED_ORDERS_PRESENT": 409,
    "INVALID_STOCK_SET_INPUT": 400, "MUTATION_REJECTED": 502,
    "MUTATION_OUTCOME_UNKNOWN": 502, "STOCK_SET_NOT_VERIFIED": 502,
}


def _redact(value):
    token = os.environ.get("ORDERCHAMP_API_TOKEN", "")
    def walk(item):
        if isinstance(item, str):
            return item.replace(token, "[REDACTED]") if token else item
        if isinstance(item, list):
            return [walk(part) for part in item]
        if isinstance(item, dict):
            return {walk(key): walk(part) for key, part in item.items()}
        return item
    return walk(value)


def _json(value, status=200):
    response = jsonify(_redact(value))
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    response.vary.add("Cookie")
    return response


def _error(code, status):
    return _json({"ok": False, "mode": "read_only", "writes_enabled": False,
                  "error_code": code}, status)


def _push_error(code, status):
    uncertain = code in {'MUTATION_OUTCOME_UNKNOWN', 'STOCK_SET_NOT_VERIFIED'}
    return _json({'ok': False, 'mode': 'stock_push',
                  'outcome': 'unknown' if uncertain else 'not_written',
                  'error_code': code}, status)


def _admin_only(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        if not session.get("admin_authenticated"):
            return _error("ADMIN_LOGIN_REQUIRED", 401)
        try:
            actor = current_actor_context()
        except Exception:
            return _error("AUTHORIZATION_UNAVAILABLE", 503)
        if actor is None:
            return _error("ADMIN_LOGIN_REQUIRED", 401)
        if (actor.actor_type != ACTOR_HUMAN or "OWNER" not in actor.roles
                or actor.permission_decision("system.jobs") != ALLOW):
            return _error("ADMIN_ROLE_REQUIRED", 403)
        if request.method == "POST":
            origin = request.headers.get("Origin", "").rstrip("/")
            if origin and origin != request.host_url.rstrip("/"):
                return _error("ORIGIN_MISMATCH", 403)
            expected = session.get("csrf_token")
            supplied = request.headers.get("X-CSRF-Token", "")
            if (not isinstance(expected, str) or not expected or
                    not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))):
                return _error("CSRF_REQUIRED", 403)
        return function(*args, **kwargs)
    return wrapped


def register_routes(app, db_path, *, refresh_stock=None):
    """db_path is a trusted backend callable; callers cannot choose the database."""
    blueprint = Blueprint("orderchamp_diagnostics", __name__)
    busy = threading.Lock()

    @blueprint.after_request
    def no_cache(response):
        response.headers["Cache-Control"] = "no-store"
        response.vary.add("Cookie")
        return response

    @blueprint.get("/admin/orderchamp")
    @_admin_only
    def page():
        return render_template("orderchamp_diagnostics.html")

    def execute(action):
        if not request.is_json:
            return _error("JSON_REQUIRED", 415)
        if request.content_length is None or request.content_length > 2048:
            return _error("BODY_TOO_LARGE_OR_UNKNOWN", 413)
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return _error("INVALID_JSON_BODY", 400)
        allowed = set() if action in ("connection", "orders", "catalog") else {"sku", "offset", "limit", "catalog_version"}
        if set(body) - allowed:
            return _error("UNKNOWN_PARAMETER", 400)
        if "sku" in body:
            sku = body["sku"]
            if not isinstance(sku, str) or not sku or sku != sku.strip() or len(sku) > 256:
                return _error("INVALID_SKU", 400)
            if set(body) != {"sku"}:
                return _error("SKU_AND_PAGINATION_ARE_EXCLUSIVE", 400)
        else:
            sku = None
        offset = body.get("offset", 0)
        if type(offset) is not int or offset < 0:
            return _error("INVALID_OFFSET", 400)
        if type(body.get("limit", 1)) is not int or body.get("limit", 1) != 1:
            return _error("HTTP_LIMIT_MUST_BE_ONE", 400)
        version = body.get("catalog_version")
        if version is not None and (not isinstance(version, str) or len(version) != 64):
            return _error("INVALID_CATALOG_VERSION", 400)
        if action == "dry_run" and sku is None and offset > 0 and version is None:
            return _error("CATALOG_VERSION_REQUIRED", 400)
        if not busy.acquire(blocking=False):
            return _error("DIAGNOSTIC_BUSY", 409)
        client = None
        try:
            budget = HttpBudget()
            pagination = None
            if action == "catalog":
                local = read_local_availability(db_path())
                positive = sorted({row["sku"] for row in local
                                   if isinstance(row["sku"], str) and row["sku"]
                                   and row["sku"] == row["sku"].strip()
                                   and type(row["available_qty"]) is int
                                   and row["available_qty"] > 0})
                return _json({"mode": "local_positive_catalog", "total_local_sku": len(local),
                              "positive_sku": len(positive), "skus": positive})
            if action == "dry_run" and sku is None:
                local = read_local_availability(db_path())
                identity = sorted((row["id"], row["sku"]) for row in local)
                current_version = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
                if version is not None and version != current_version:
                    return _error("CATALOG_CHANGED_RESTART", 409)
                catalog = sorted({row["sku"] for row in local}, key=lambda value: str(value or ""))
                if not catalog:
                    return _error("EMPTY_LOCAL_CATALOG", 422)
                if offset >= len(catalog):
                    return _error("OFFSET_OUT_OF_RANGE", 400)
                sku = catalog[offset]
                pagination = {"offset": offset, "limit": 1, "total_sku": len(catalog),
                              "next_offset": offset + 1 if offset + 1 < len(catalog) else None,
                              "has_more": offset + 1 < len(catalog), "catalog_version": current_version}
                if not isinstance(sku, str):
                    return _json({"ok": False, "error_code": "INVALID_LOCAL_SKU",
                                  "writes_enabled": False, "pagination": pagination}, 422)
            budget.remaining()
            client = http_client(budget)
            if action == "connection":
                return _json(client.test_connection())
            if action == "orders":
                return _json(client.probe_orders())
            # Exact same service as CLI. Always one explicit SKU, never full HTTP scan.
            report = dry_run_stock_sync(client, db_path(), sku=sku)
            report.pop("local_database", None)
            if pagination:
                report["pagination"] = pagination
            status = 200
            if report["summary"]["errors"]:
                status = 502
                if any(row.get("error_code") in {"TIMEOUT", "HTTP_TIME_BUDGET_EXCEEDED"} for row in report["rows"]):
                    status = 504
            elif report["summary"]["missing"]:
                status = 404
            return _json(report, status)
        except OrderchampError as exc:
            code = exc.code if exc.code in ERROR_STATUS else "ORDERCHAMP_DIAGNOSTIC_ERROR"
            return _error(code, ERROR_STATUS.get(code, 502))
        except Exception:
            # Never include exception text/tracebacks or arbitrary upstream responses.
            app.logger.warning("ORDERCHAMP_HTTP_DIAGNOSTIC_FAILED")
            return _error("DIAGNOSTIC_FAILED", 500)
        finally:
            try:
                if client is not None:
                    client.close()
            finally:
                busy.release()

    @blueprint.post("/api/admin/orderchamp/test-connection")
    @_admin_only
    def test_connection():
        return execute("connection")

    @blueprint.post("/api/admin/orderchamp/probe-orders")
    @_admin_only
    def probe_orders():
        return execute("orders")

    @blueprint.post("/api/admin/orderchamp/local-catalog")
    @_admin_only
    def local_catalog():
        return execute("catalog")

    @blueprint.post("/api/admin/orderchamp/dry-run")
    @_admin_only
    def dry_run():
        return execute("dry_run")

    @blueprint.post("/api/admin/orderchamp/push-one")
    @blueprint.post("/api/admin/orderchamp/seed-one")
    @_admin_only
    def push_one():
        if not request.is_json or request.content_length is None or request.content_length > 2048:
            return _push_error('INVALID_JSON_BODY', 400)
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or set(body) != {'sku', 'expected_local', 'expected_remote_updated_at'}:
            return _push_error('STOCK_SET_PRECONDITION_FAILED', 409)
        sku, expected, updated = (body['sku'], body['expected_local'], body['expected_remote_updated_at'])
        if (not isinstance(sku, str) or not sku or sku != sku.strip() or len(sku) > 256
                or type(expected) is not int or not 0 <= expected <= 2147483647
                or not isinstance(updated, str) or not updated or len(updated) > 80):
            return _push_error('STOCK_SET_PRECONDITION_FAILED', 409)
        if not busy.acquire(blocking=False):
            return _push_error('DIAGNOSTIC_BUSY', 409)
        client = None
        try:
            if refresh_stock is None:
                return _push_error('LOCAL_AVAILABILITY_READ_FAILED', 503)
            # A write must never use the TTL-based local business snapshot.
            refresh_stock()
            client = http_client(HttpBudget(seconds=25))
            result = push_one_stock(client, db_path(), sku=sku, expected_local=expected,
                                    expected_remote_updated_at=updated,
                                    **({'initial_seed': True} if request.path.endswith('/seed-one') else {}))
            return _json(result)
        except OrderchampError as exc:
            code = exc.code if exc.code in ERROR_STATUS else 'MUTATION_OUTCOME_UNKNOWN'
            return _push_error(code, ERROR_STATUS.get(code, 502))
        except Exception:
            app.logger.warning('ORDERCHAMP_STOCK_PUSH_FAILED')
            return (_push_error('MUTATION_OUTCOME_UNKNOWN', 502) if client is not None
                    else _push_error('LOCAL_AVAILABILITY_READ_FAILED', 503))
        finally:
            try:
                if client is not None:
                    client.close()
            finally:
                busy.release()

    app.register_blueprint(blueprint)
