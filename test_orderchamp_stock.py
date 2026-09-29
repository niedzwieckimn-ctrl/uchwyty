"""Offline tests: all HTTP is mocked; no app startup or production credentials."""
import hashlib
import json
import socket
import sqlite3

import pytest
import requests

import orderchamp_stock_sync as sync
import run_orderchamp_stock as cli
from inventory_analytics import build_replenishment_analysis
from orderchamp_client import API_URL, CONNECTION_QUERY, VARIANT_QUERY, OrderchampClient, OrderchampError
from test_inventory_analytics import make_db, add_product

TOKEN = "synthetic-test-credential"


@pytest.fixture(autouse=True)
def no_network_or_credentials(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Real network disabled in Orderchamp tests")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(requests.sessions.Session, "request", blocked)
    monkeypatch.delenv("ORDERCHAMP_API_TOKEN", raising=False)
    monkeypatch.delenv("ORDERCHAMP_API_URL", raising=False)


class Response:
    def __init__(self, payload=None, status=200, headers=None):
        self.payload, self.status_code = payload, status
        self.headers = headers or {}
        self.closed = False

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload

    def close(self):
        self.closed = True


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self):
        self.closed = True


def client_for(*responses):
    session = Session(responses)
    waits = []
    client = OrderchampClient(TOKEN, session=session, sleep=waits.append, monotonic=lambda: 100.0)
    return client, session, waits


def connected():
    return Response({"data": {"productVariants": {"nodes": []}}})


def variant(sku, quantity=24, available=21, policy="DENY", has_next=False):
    return {"id": "variant-" + sku, "sku": sku, "inventoryQuantity": quantity,
            "inventoryPolicy": policy, "inventoryLevels": {
                "nodes": [{"id": "level-" + sku, "quantity": quantity,
                           "availableQuantity": available, "updatedAt": "2026-09-29T08:00:00Z",
                           "location": {"id": "warehouse-1", "isPrimary": True}}],
                "pageInfo": {"hasNextPage": has_next}}}


def found(sku, **kwargs):
    return Response({"data": {"productVariantBySku": variant(sku, **kwargs)}})


def missing():
    return Response({"data": {"productVariantBySku": None}})


def database(tmp_path, products=(('A', 24),)):
    connect = make_db(tmp_path)
    for i, (sku, qty) in enumerate(products, 1):
        add_product(connect, product_id=i, sku=sku, stock=qty)
    return tmp_path / "analytics.db", connect


def test_exact_sku_mapping_and_only_documented_read_fields():
    client, session, _ = client_for(found("CH010-AB-N28"))
    result = client.resolve_variant_by_sku("CH010-AB-N28")
    assert result["id"] == "variant-CH010-AB-N28"
    assert result["levels"][0]["available_quantity"] == 21
    url, kwargs = session.calls[0]
    assert url == API_URL
    assert kwargs["json"] == {"query": VARIANT_QUERY, "variables": {"sku": "CH010-AB-N28"}}
    assert kwargs["allow_redirects"] is False and kwargs["timeout"] == (5, 20)
    assert kwargs["headers"]["Authorization"] == "Bearer " + TOKEN
    assert session.trust_env is False
    for forbidden in ("image", "price", "customer", "mutation"):
        assert forbidden not in VARIANT_QUERY.lower()


def test_stock_source_matches_screen_engine_including_allocations_and_incoming(tmp_path):
    path, connect = database(tmp_path, (("A", 24), ("B", 0), ("C", -3), ("ARCHIVED", 100)))
    with connect() as c:
        c.executescript("""
        INSERT INTO orders VALUES(1,1,'unused@example.invalid','confirmed',0,'2026-09-29');
        INSERT INTO order_items VALUES(1,1,1,5);
        INSERT INTO invoice_allocations VALUES(1,1,1,1,1,2);
        INSERT INTO order_items VALUES(2,1,2,3);
        INSERT INTO china_packages VALUES(1,'shipped');
        INSERT INTO china_items VALUES(1,1,2,80);
        UPDATE products SET archived=1 WHERE sku='ARCHIVED';
        """)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    expected = {row["sku"]: row["available_qty"] for row in build_replenishment_analysis(connect)}
    client, session, _ = client_for(connected(), found("A"), found("B"), found("C"))
    report = sync.dry_run_stock_sync(client, path)
    assert {row["sku"]: row["would_send"] for row in report["rows"]} == expected == {"A": 21, "B": 0, "C": 0}
    assert report["summary"]["synchronized"] == 0
    assert report["summary"]["matched"] == 3
    assert report["writes_enabled"] is False
    assert all(call[1]["json"]["query"].startswith("query ") for call in session.calls)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("value,expected", [(24, 24), (0, 0), (-6, 0)])
def test_candidate_uses_central_available_and_clamps_negative(tmp_path, monkeypatch, value, expected):
    monkeypatch.setattr(sync, "read_local_availability", lambda _: [{"id": 1, "sku": "A", "available_qty": value}])
    client, _, _ = client_for(connected(), found("A"))
    report = sync.dry_run_stock_sync(client, tmp_path / "unused")
    assert report["rows"][0]["would_send"] == expected


@pytest.mark.parametrize("value", [None, "3", 1.5, True, 2147483648])
def test_invalid_quantity_is_error_not_zero(tmp_path, monkeypatch, value):
    monkeypatch.setattr(sync, "read_local_availability", lambda _: [{"id": 1, "sku": "A", "available_qty": value}])
    client, _, _ = client_for(connected())
    report = sync.dry_run_stock_sync(client, tmp_path / "unused")
    assert report["rows"][0]["error_code"] == "INVALID_LOCAL_AVAILABLE"
    assert report["rows"][0]["would_send"] is None


def test_missing_and_one_sku_error_do_not_stop_full_dry_run(tmp_path):
    path, _ = database(tmp_path, (("A", 24), ("B", 0), ("C", 5), ("D", 7)))
    error = Response({"errors": [{"message": "private error " + TOKEN}]})
    client, _, _ = client_for(connected(), found("A"), missing(), error, found("D"))
    report = sync.dry_run_stock_sync(client, path)
    assert report["summary"] == {"local_sku": 4, "matched": 2, "missing": 1,
                                "errors": 1, "synchronized": 0, "skipped": 4,
                                "warning_rows": 3, "http_requests": 5}
    assert report["rows"][-1]["status"] == "MATCHED"
    assert TOKEN not in json.dumps(report)


@pytest.mark.parametrize("exception,code", [(requests.Timeout, "TIMEOUT"), (requests.ConnectionError, "NETWORK_ERROR")])
def test_transient_network_errors_have_bounded_retry(exception, code):
    client, session, waits = client_for(*(exception(TOKEN) for _ in range(3)))
    with pytest.raises(OrderchampError, match=code) as exc:
        client.resolve_variant_by_sku("A")
    assert len(session.calls) == 3 and 1.0 in waits and 2.0 in waits
    assert TOKEN not in str(exc.value)


def test_http_429_retry_after_then_success():
    first = Response(status=429, headers={"Retry-After": "4"})
    client, session, waits = client_for(first, found("A"))
    assert client.resolve_variant_by_sku("A")["sku"] == "A"
    assert 4.0 in waits and len(session.calls) == 2 and first.closed


def test_graphql_throttling_then_success():
    client, session, _ = client_for(Response({"errors": [{"message": "Throttled"}]}), found("A"))
    assert client.resolve_variant_by_sku("A")
    assert len(session.calls) == 2


def test_repeated_429_stops_after_three_attempts():
    client, session, _ = client_for(*(Response(status=429) for _ in range(3)))
    with pytest.raises(OrderchampError, match="RATE_LIMITED"):
        client.resolve_variant_by_sku("A")
    assert len(session.calls) == 3


def test_long_retry_after_stops_run_network_without_retrying_too_early():
    client, session, _ = client_for(Response(status=429, headers={"Retry-After": "120"}))
    for sku in ("A", "B"):
        with pytest.raises(OrderchampError, match="RATE_LIMIT_WAIT_TOO_LONG"):
            client.resolve_variant_by_sku(sku)
    assert len(session.calls) == 1


@pytest.mark.parametrize("status", [500, 502, 503])
def test_retry_5xx_read(status):
    client, session, _ = client_for(Response(status=status), found("A"))
    assert client.resolve_variant_by_sku("A")
    assert len(session.calls) == 2


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failure_not_retried_for_every_product(status):
    client, session, _ = client_for(Response(status=status))
    for sku in ("A", "B"):
        with pytest.raises(OrderchampError, match="AUTH_OR_SCOPE_ERROR"):
            client.resolve_variant_by_sku(sku)
    assert len(session.calls) == 1


@pytest.mark.parametrize("response,code", [
    (Response(status=302), "HTTP_ERROR"),
    (Response(status=400), "HTTP_ERROR"),
    (Response(ValueError(TOKEN)), "INVALID_JSON"),
    (Response([]), "INVALID_API_RESPONSE"),
    (Response({"data": {}}), "INVALID_API_RESPONSE"),
    (Response({"data": {"productVariantBySku": None}, "errors": [{"message": TOKEN}]}), "GRAPHQL_ERROR"),
    (found("other-sku"), "REMOTE_SKU_MISMATCH"),
])
def test_bad_responses_do_not_become_missing_or_leak_details(response, code):
    client, session, _ = client_for(response)
    with pytest.raises(OrderchampError, match=code) as exc:
        client.resolve_variant_by_sku("A")
    assert TOKEN not in str(exc.value) and len(session.calls) == 1 and response.closed


def test_duplicate_sku_not_silently_overwritten(tmp_path):
    path, _ = database(tmp_path, (("A", 1), ("A", 8), ("B", 3)))
    client, session, _ = client_for(connected(), found("B"))
    report = sync.dry_run_stock_sync(client, path)
    assert report["summary"]["errors"] == 2 and report["summary"]["matched"] == 1
    assert all(r["error_code"] == "DUPLICATE_LOCAL_SKU" for r in report["rows"][:2])
    assert len(session.calls) == 2


def test_single_sku_reads_only_requested_variant(tmp_path):
    path, _ = database(tmp_path, (("A", 1), ("B", 8)))
    client, session, _ = client_for(connected(), found("B"))
    report = sync.dry_run_stock_sync(client, path, sku="B")
    assert report["summary"]["local_sku"] == 1
    assert report["rows"][0]["would_send"] == 8
    assert len(session.calls) == 2


def test_unknown_local_sku_is_not_empty_success(tmp_path):
    path, _ = database(tmp_path)
    client, session, _ = client_for()
    with pytest.raises(OrderchampError, match="LOCAL_SKU_NOT_FOUND"):
        sync.dry_run_stock_sync(client, path, sku="a")
    assert session.calls == []


def test_missing_database_never_created(tmp_path):
    path = tmp_path / "missing.db"
    with pytest.raises(OrderchampError, match="LOCAL_DATABASE_NOT_FOUND"):
        sync.read_local_availability(path)
    assert not path.exists()


def test_corrupt_database_is_error(tmp_path):
    path = tmp_path / "bad.db"
    path.write_text("not sqlite")
    with pytest.raises(OrderchampError, match="LOCAL_AVAILABILITY_READ_FAILED"):
        sync.read_local_availability(path)


def test_engine_connection_is_read_only_and_in_transaction(tmp_path, monkeypatch):
    path, _ = database(tmp_path)
    def engine(factory):
        connection = factory()
        assert connection.in_transaction
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("UPDATE stock SET qty=0")
        return []
    monkeypatch.setattr(sync, "build_replenishment_analysis", engine)
    assert sync.read_local_availability(path) == []


def test_remote_policy_quantity_and_incomplete_location_warnings(tmp_path):
    path, _ = database(tmp_path)
    client, _, _ = client_for(connected(), found("A", policy="CONTINUE", has_next=True))
    report = sync.dry_run_stock_sync(client, path)
    assert report["rows"][0]["warnings"] == ["REMOTE_BACKORDERS_ALLOWED", "REMOTE_LEVELS_INCOMPLETE", "REMOTE_QUANTITY_DIFFERS_FROM_AVAILABLE"]
    assert not report["writes_enabled"]


@pytest.mark.parametrize("url", ["http://api.orderchamp.com/v1/graphql", "https://other.example/graphql", "https://token@api.orderchamp.com/v1/graphql"])
def test_endpoint_does_not_allow_credential_exfiltration(url):
    with pytest.raises(OrderchampError, match="API_URL_NOT_ALLOWED"):
        OrderchampClient(TOKEN, url)


def test_mutation_cannot_be_passed_to_read_transport():
    client, session, _ = client_for()
    with pytest.raises(OrderchampError, match="READ_QUERY_NOT_ALLOWED"):
        client._read("mutation { anyWrite }", {})
    assert session.calls == []


def test_cli_report_and_token_redaction(tmp_path, monkeypatch, capsys):
    path, _ = database(tmp_path, ((TOKEN, 24),))
    client, session, _ = client_for(connected(), found(TOKEN, available=24))
    monkeypatch.setattr(cli.OrderchampClient, "from_env", lambda: client)
    report = tmp_path / "report.json"
    assert cli.main(["dry-run", "--db", str(path), "--report", str(report)]) == 0
    contents = report.read_text(encoding="utf-8")
    assert TOKEN not in contents + capsys.readouterr().out
    assert json.loads(contents)["summary"]["matched"] == 1
    assert session.closed


def test_cli_does_not_overwrite_existing_report(tmp_path, capsys):
    path = tmp_path / "existing.json"
    path.write_text("preserve")
    assert cli.main(["dry-run", "--report", str(path)]) == 2
    assert path.read_text() == "preserve"
    assert "REPORT_PATH_MUST_BE_NEW_JSON" in capsys.readouterr().err


def test_cli_missing_token_is_safe_error(capsys):
    assert cli.main(["test-connection"]) == 2
    assert "TOKEN_MISSING_OR_INVALID" in capsys.readouterr().err


def test_cli_has_no_write_command():
    with pytest.raises(SystemExit) as exc:
        cli.main(["sync-all", "--write"])
    assert exc.value.code == 2


def test_cli_empty_catalog_fails(tmp_path, monkeypatch, capsys):
    path, _ = database(tmp_path, ())
    client, _, _ = client_for(connected())
    monkeypatch.setattr(cli.OrderchampClient, "from_env", lambda: client)
    assert cli.main(["dry-run", "--db", str(path)]) == 2
    assert "EMPTY_LOCAL_CATALOG" in capsys.readouterr().out
