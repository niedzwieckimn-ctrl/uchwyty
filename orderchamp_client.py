"""Isolated Orderchamp inventory client; only one stock SET mutation is allowed."""
from __future__ import annotations

import os
import logging
import time
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

API_URL = "https://api.orderchamp.com/v1/graphql"
CONNECTION_QUERY = """query StockConnectionTest {
  productVariants(first: 1) { nodes { id sku } }
}"""
ORDERS_PROBE_QUERY = """query StockSalesProbe {
  orders(first: 1, sort: CREATED_AT_DESC, includeUnconfirmed: true, includeCancelled: true) {
    totalCount
    nodes { id createdAt updatedAt status isConfirmed isCancelled }
  }
}"""
VARIANT_QUERY = """query StockDryRunVariant($sku: String!) {
  productVariantBySku(sku: $sku) {
    id sku inventoryQuantity inventoryPolicy
    inventoryLevels(first: 100) {
      nodes {
        id quantity availableQuantity updatedAt
        location { id isPrimary }
      }
      pageInfo { hasNextPage }
    }
  }
}"""
STOCK_SET_MUTATION = """mutation SetOneInventoryLevel($input: InventoryLevelBulkAdjustInput!) {
  inventoryLevelBulkAdjust(input: $input) {
    clientMutationId
    inventoryLevels { id quantity availableQuantity updatedAt }
    userErrors { field message }
  }
}"""


class OrderchampError(Exception):
    """Only locally defined codes; never include remote bodies or credentials."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _quantity(value):
    if value is not None and type(value) is not int:
        raise OrderchampError("INVALID_API_RESPONSE")
    return value


def _string(value):
    if not isinstance(value, str) or not value:
        raise OrderchampError("INVALID_API_RESPONSE")
    return value


class OrderchampClient:
    read_queries = ()
    def __init__(self, token: str, api_url: str = API_URL, *, session=None,
                 sleep=time.sleep, monotonic=time.monotonic):
        if not token or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise OrderchampError("TOKEN_MISSING_OR_INVALID")
        # A configurable arbitrary host/redirect could leak the Bearer credential.
        if api_url != API_URL:
            raise OrderchampError("API_URL_NOT_ALLOWED")
        self._token = token
        self._session = session if session is not None else requests.Session()
        self._session.trust_env = False  # Do not replace Bearer with .netrc auth.
        self._sleep = sleep
        self._monotonic = monotonic
        self._next_request = 0.0
        self._fatal_error = None
        self.request_count = 0

    @classmethod
    def from_env(cls):
        return cls(os.environ.get("ORDERCHAMP_API_TOKEN", ""),
                   os.environ.get("ORDERCHAMP_API_URL", API_URL))

    def close(self):
        self._session.close()

    def redact(self, text: str) -> str:
        return text.replace(self._token, "[REDACTED]")

    def _retry_delay(self, attempt, retry_after):
        delay = float(2 ** attempt)
        if retry_after:
            try:
                delay = max(delay, float(retry_after))
            except (ValueError, TypeError):
                try:
                    until = parsedate_to_datetime(retry_after)
                    if until.tzinfo is None:
                        until = until.replace(tzinfo=timezone.utc)
                    delay = max(delay, (until - datetime.now(timezone.utc)).total_seconds())
                except (ValueError, TypeError, OverflowError):
                    pass
        if not 0 <= delay <= 60:
            # Do not retry sooner than the server requested, or sleep indefinitely.
            self._fatal_error = "RATE_LIMIT_WAIT_TOO_LONG"
            raise OrderchampError(self._fatal_error)
        self._sleep(delay)

    def _read(self, query, variables):
        # Exact allowlist: dry run cannot submit arbitrary GraphQL or mutations.
        if query not in (CONNECTION_QUERY, VARIANT_QUERY, ORDERS_PROBE_QUERY, *self.read_queries):
            raise OrderchampError("READ_QUERY_NOT_ALLOWED")
        if self._fatal_error:
            raise OrderchampError(self._fatal_error)
        last_error = "NETWORK_ERROR"
        for attempt in range(3):
            self._sleep(max(0.0, self._next_request - self._monotonic()))
            self._next_request = self._monotonic() + 0.5
            retry_after = None
            self.request_count += 1
            response = None
            try:
                response = self._session.post(
                    API_URL, headers={"Authorization": "Bearer " + self._token,
                                      "Accept": "application/json"},
                    json={"query": query, "variables": variables},
                    timeout=(5, 20), allow_redirects=False,
                )
                status = response.status_code
                if status in (401, 403):
                    self._fatal_error = "AUTH_OR_SCOPE_ERROR"
                    raise OrderchampError(self._fatal_error)
                if status == 429 or 500 <= status < 600:
                    last_error = "RATE_LIMITED" if status == 429 else "UPSTREAM_UNAVAILABLE"
                    retry_after = response.headers.get("Retry-After")
                elif status != 200:
                    raise OrderchampError("HTTP_ERROR")
                else:
                    try:
                        payload = response.json()
                    except ValueError:
                        raise OrderchampError("INVALID_JSON") from None
                    if not isinstance(payload, dict):
                        raise OrderchampError("INVALID_API_RESPONSE")
                    errors = payload.get("errors")
                    if errors:
                        if (isinstance(errors, list) and all(
                            isinstance(err, dict) and err.get("message") == "Throttled"
                            for err in errors
                        )):
                            last_error = "RATE_LIMITED"
                        else:
                            # Log only fixed categories, never the upstream message,
                            # queried SKU, response body, or Bearer token.
                            messages = [str(err.get("message") or "").lower()
                                        for err in errors if isinstance(err, dict)]
                            hints = [name for name, terms in {
                                "inventory_levels": ("inventorylevels", "inventory level"),
                                "pagination": ("first", "pagination", "page size"),
                                "field_validation": ("cannot query field", "unknown field"),
                                "authorization": ("permission", "forbidden", "access denied"),
                            }.items() if any(term in message for message in messages for term in terms)]
                            logging.getLogger(__name__).warning(
                                "ORDERCHAMP_GRAPHQL_READ_ERROR query=%s hints=%s count=%s",
                                "variant" if query == VARIANT_QUERY else
                                "orders" if query == ORDERS_PROBE_QUERY else "connection",
                                ",".join(hints) or "unknown", len(errors))
                            # Partial data must not turn an error into NOT FOUND.
                            raise OrderchampError("GRAPHQL_ERROR")
                    else:
                        data = payload.get("data")
                        if not isinstance(data, dict):
                            raise OrderchampError("INVALID_API_RESPONSE")
                        return data
            except requests.Timeout:
                last_error = "TIMEOUT"
            except requests.ConnectionError:
                last_error = "NETWORK_ERROR"
            except requests.RequestException:
                raise OrderchampError("HTTP_CLIENT_ERROR") from None
            finally:
                if response is not None:
                    response.close()
            if attempt < 2:
                self._retry_delay(attempt, retry_after)
        raise OrderchampError(last_error)

    def test_connection(self):
        data = self._read(CONNECTION_QUERY, {})
        connection = data.get("productVariants")
        if not isinstance(connection, dict) or not isinstance(connection.get("nodes"), list):
            raise OrderchampError("INVALID_API_RESPONSE")
        for node in connection["nodes"]:
            if not isinstance(node, dict):
                raise OrderchampError("INVALID_API_RESPONSE")
            _string(node.get("id"))
            if not isinstance(node.get("sku"), str):
                raise OrderchampError("INVALID_API_RESPONSE")
        return {"connected": True, "products_read": True, "write_checked": False}

    def probe_orders(self):
        """Read only a count and one recent order, without customer information."""
        connection = self._read(ORDERS_PROBE_QUERY, {}).get("orders")
        if (not isinstance(connection, dict) or type(connection.get("totalCount")) is not int
                or connection["totalCount"] < 0 or not isinstance(connection.get("nodes"), list)
                or len(connection["nodes"]) > 1):
            raise OrderchampError("INVALID_API_RESPONSE")
        recent = []
        for node in connection["nodes"]:
            if (not isinstance(node, dict) or type(node.get("isConfirmed")) is not bool
                    or type(node.get("isCancelled")) is not bool):
                raise OrderchampError("INVALID_API_RESPONSE")
            recent.append({"id": _string(node.get("id")),
                           "created_at": _string(node.get("createdAt")),
                           "updated_at": _string(node.get("updatedAt")),
                           "status": _string(node.get("status")),
                           "is_confirmed": node["isConfirmed"],
                           "is_cancelled": node["isCancelled"]})
        return {"orders_read": True, "all_order_count": connection["totalCount"],
                "recent": recent, "writes_enabled": False}

    def resolve_variant_by_sku(self, sku):
        data = self._read(VARIANT_QUERY, {"sku": sku})
        if "productVariantBySku" not in data:
            raise OrderchampError("INVALID_API_RESPONSE")
        variant = data["productVariantBySku"]
        if variant is None:
            return None
        if not isinstance(variant, dict):
            raise OrderchampError("INVALID_API_RESPONSE")
        if variant.get("sku") != sku:
            raise OrderchampError("REMOTE_SKU_MISMATCH")
        variant_id = _string(variant.get("id"))
        policy = variant.get("inventoryPolicy")
        if policy not in ("DENY", "CONTINUE"):
            raise OrderchampError("INVALID_API_RESPONSE")
        connection = variant.get("inventoryLevels")
        if not isinstance(connection, dict) or not isinstance(connection.get("nodes"), list):
            raise OrderchampError("INVALID_API_RESPONSE")
        page = connection.get("pageInfo")
        if not isinstance(page, dict) or type(page.get("hasNextPage")) is not bool:
            raise OrderchampError("INVALID_API_RESPONSE")
        levels = []
        for level in connection["nodes"]:
            if not isinstance(level, dict) or not isinstance(level.get("location"), dict):
                raise OrderchampError("INVALID_API_RESPONSE")
            location = level["location"]
            if location.get("isPrimary") is not None and type(location["isPrimary"]) is not bool:
                raise OrderchampError("INVALID_API_RESPONSE")
            levels.append({
                "id": _string(level.get("id")),
                "quantity": _quantity(level.get("quantity")),
                "available_quantity": _quantity(level.get("availableQuantity")),
                "updated_at": _string(level.get("updatedAt")),
                "location_id": _string(location.get("id")),
                "is_primary": location.get("isPrimary"),
            })
        return {"id": variant_id, "sku": sku,
                "inventory_quantity": _quantity(variant.get("inventoryQuantity")),
                "inventory_policy": policy, "levels": levels,
                "levels_complete": not page["hasNextPage"]}

    def set_inventory_level(self, level_id, quantity):
        """Single SET. Ambiguous transport failure is never retried automatically."""
        if not isinstance(level_id, str) or not level_id or type(quantity) is not int or not 0 <= quantity <= 2147483647:
            raise OrderchampError("INVALID_STOCK_SET_INPUT")
        if self._fatal_error:
            raise OrderchampError(self._fatal_error)
        self._sleep(max(0.0, self._next_request - self._monotonic()))
        self._next_request = self._monotonic() + 0.5
        self.request_count += 1
        response = None
        try:
            response = self._session.post(API_URL,
                headers={"Authorization": "Bearer " + self._token, "Accept": "application/json"},
                json={"query": STOCK_SET_MUTATION, "variables": {"input": {
                    "clientMutationId": str(uuid.uuid4()), "inventoryLevels": [{
                        "inventoryLevelId": level_id, "action": "SET", "adjustment": quantity,
                    }]}}}, timeout=(5, 20), allow_redirects=False)
            if response.status_code in (401, 403):
                self._fatal_error = "AUTH_OR_SCOPE_ERROR"
                raise OrderchampError(self._fatal_error)
            if response.status_code != 200:
                raise OrderchampError("MUTATION_OUTCOME_UNKNOWN")
            try:
                payload = response.json()
            except ValueError:
                raise OrderchampError("MUTATION_OUTCOME_UNKNOWN") from None
            if not isinstance(payload, dict) or payload.get("errors"):
                raise OrderchampError("MUTATION_REJECTED")
            result = (payload.get("data") or {}).get("inventoryLevelBulkAdjust")
            if not isinstance(result, dict):
                raise OrderchampError("MUTATION_OUTCOME_UNKNOWN")
            if result.get("userErrors"):
                raise OrderchampError("MUTATION_REJECTED")
            levels = result.get("inventoryLevels")
            if not isinstance(levels, list) or len(levels) != 1 or not isinstance(levels[0], dict):
                raise OrderchampError("MUTATION_OUTCOME_UNKNOWN")
            updated = levels[0]
            if updated.get("id") != level_id or _quantity(updated.get("quantity")) != quantity:
                raise OrderchampError("MUTATION_OUTCOME_UNKNOWN")
            return {"level_id": level_id, "quantity": quantity}
        except (requests.Timeout, requests.ConnectionError, requests.RequestException):
            raise OrderchampError("MUTATION_OUTCOME_UNKNOWN") from None
        finally:
            if response is not None:
                response.close()
