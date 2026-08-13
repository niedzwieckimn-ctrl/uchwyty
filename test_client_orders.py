import uuid
from unittest.mock import Mock

import pytest

import app as backend


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "DB_PATH", str(tmp_path / "test.db"))
    backend.init_db()
    c = backend.conn()
    c.execute(
        "INSERT INTO products(id, sku, model, name, ean, created_at) VALUES(?,?,?,?,?,?)",
        (990001, "TEST-001", "TEST", "Produkt testowy", "", backend.now_iso()),
    )
    c.commit()
    c.close()
    monkeypatch.setattr(backend, "supabase_enabled", lambda: True)
    monkeypatch.setattr(backend, "maybe_pull_shared_from_supabase", lambda **kwargs: None)
    monkeypatch.setattr(backend, "_authenticated_client_user", lambda: {"id": "user-1", "email": "test@example.com", "name": "Test"})
    monkeypatch.setattr(backend, "_order_by_idempotency_key", lambda key: None)
    return backend.app.test_client()


def headers():
    return {"Authorization": "Bearer test", "Idempotency-Key": str(uuid.uuid4())}


def payload(qty=1, product_id=990001):
    return {"note": "test", "items": [{"product_id": product_id, "sku": "TEST-001", "qty": qty}]}


def test_create_order_and_send_email(client, monkeypatch):
    monkeypatch.setattr(backend, "remote_first_create_order", lambda *a, **k: 123)
    monkeypatch.setattr(backend, "_send_saved_order_confirmation", lambda order_id: {"ok": True})
    response = client.post("/api/client/orders", json=payload(), headers=headers())
    assert response.status_code == 200
    assert response.get_json()["email"]["ok"] is True


def test_missing_token_returns_401(client, monkeypatch):
    monkeypatch.setattr(backend, "_authenticated_client_user", lambda: None)
    response = client.post("/api/client/orders", json=payload(), headers=headers())
    assert response.status_code == 401


def test_invalid_token_returns_401(client, monkeypatch):
    monkeypatch.setattr(backend, "_authenticated_client_user", lambda: None)
    response = client.post("/api/client/orders", json=payload(), headers=headers())
    assert response.get_json() == {"ok": False, "error": "Brak autoryzacji"}


def test_empty_order_returns_400(client):
    response = client.post("/api/client/orders", json={"items": []}, headers=headers())
    assert response.status_code == 400


def test_zero_quantity_returns_400(client):
    response = client.post("/api/client/orders", json=payload(qty=0), headers=headers())
    assert response.status_code == 400


def test_unknown_product_returns_400(client):
    response = client.post("/api/client/orders", json=payload(product_id=999999), headers=headers())
    assert response.status_code == 400


def test_item_write_failure_does_not_report_success(client, monkeypatch):
    monkeypatch.setattr(backend, "remote_first_create_order", Mock(side_effect=RuntimeError("item write failed")))
    response = client.post("/api/client/orders", json=payload(), headers=headers())
    assert response.status_code == 500
    assert response.get_json()["ok"] is False


def test_email_failure_keeps_created_order(client, monkeypatch):
    monkeypatch.setattr(backend, "remote_first_create_order", lambda *a, **k: 124)
    monkeypatch.setattr(backend, "_send_saved_order_confirmation", lambda order_id: {"ok": False, "error": "Resend unavailable"})
    response = client.post("/api/client/orders", json=payload(), headers=headers())
    body = response.get_json()
    assert response.status_code == 200
    assert body["ok"] is True
    assert body["email"]["pending_retry"] is True


def test_same_idempotency_key_returns_existing_order(client, monkeypatch):
    monkeypatch.setattr(backend, "_order_by_idempotency_key", lambda key: {"id": 125, "order_no": "ZAM-TEST", "customer_email": "test@example.com"})
    create = Mock(side_effect=AssertionError("must not create"))
    monkeypatch.setattr(backend, "remote_first_create_order", create)
    monkeypatch.setattr(backend, "_send_saved_order_confirmation", lambda order_id: {"ok": True, "duplicate": True})
    response = client.post("/api/client/orders", json=payload(), headers=headers())
    assert response.status_code == 200
    assert response.get_json()["duplicate"] is True
    create.assert_not_called()


def test_successful_email_is_not_sent_again(client, monkeypatch):
    monkeypatch.setattr(backend, "_email_event_already_ok", lambda key: True)
    sender = Mock()
    monkeypatch.setattr(backend, "send_order_confirmation", sender)
    c = backend.conn()
    c.execute(
        "INSERT INTO orders(id, order_no, customer_name, customer_email, status, created_at) VALUES(?,?,?,?,?,?)",
        (126, "ZAM-126", "Test", "test@example.com", "new", backend.now_iso()),
    )
    c.commit()
    c.close()
    result = backend._send_saved_order_confirmation(126)
    assert result["duplicate"] is True
    sender.assert_not_called()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("pl", "pl"), ("de", "de"), ("en", "en"), ("es", "es"), ("it", "it"), (None, "pl"), ("xx", "pl")],
)
def test_client_language_normalization(raw, expected):
    assert backend.normalize_client_language(raw) == expected


def test_client_profile_uses_verified_email(client, monkeypatch):
    lookup = Mock(return_value={
        "id": 77, "name": "Test", "email": "test@example.com", "language": "de"
    })
    monkeypatch.setattr(backend, "_client_profile_for_email", lookup)
    response = client.get("/api/client/profile", headers={"Authorization": "Bearer test"})
    assert response.status_code == 200
    assert response.get_json()["customer"]["language"] == "de"
    lookup.assert_called_once_with("test@example.com")


def test_profile_language_patch_is_normalized(client, monkeypatch):
    monkeypatch.setattr(backend, "_client_profile_for_email", lambda email: {
        "id": 77, "name": "Test", "email": email, "language": "pl"
    })
    update = Mock()
    monkeypatch.setattr(backend, "supabase_update_rows", update)
    response = client.patch(
        "/api/client/profile",
        json={"language": "xx"},
        headers={"Authorization": "Bearer test"},
    )
    assert response.status_code == 200
    assert response.get_json()["customer"]["language"] == "pl"
    update.assert_called_once_with("customers", {"language": "pl"}, {"id": 77})


def test_order_pdf_is_owned_and_uses_profile_language(client, monkeypatch):
    c = backend.conn()
    c.execute(
        "INSERT INTO pricing(model, net_price, gross_price, created_at) VALUES(?,?,?,?)",
        ("TEST", 10, 12.3, backend.now_iso()),
    )
    c.execute(
        "INSERT INTO orders(id, order_no, customer_name, customer_email, status, created_at) VALUES(?,?,?,?,?,?)",
        (901, "ZAM-PDF-901", "Test", "test@example.com", "confirmed", backend.now_iso()),
    )
    c.execute(
        "INSERT INTO order_items(order_id, product_id, sku, qty, created_at) VALUES(?,?,?,?,?)",
        (901, 990001, "TEST-001", 2, backend.now_iso()),
    )
    c.commit()
    c.close()
    monkeypatch.setattr(backend, "_client_profile_for_email", lambda email: {
        "id": 77, "name": "Test", "email": email, "language": "it"
    })
    response = client.get(
        "/api/client/orders/901/pdf",
        headers={"Authorization": "Bearer test", "Origin": "https://panel.example"},
    )
    assert response.status_code == 200
    assert response.data.startswith(b"%PDF")
    assert "_it.pdf" in response.headers["Content-Disposition"]
    assert response.headers["Cache-Control"] == "no-store"


def test_order_pdf_rejects_other_customer(client):
    c = backend.conn()
    c.execute(
        "INSERT INTO orders(id, order_no, customer_name, customer_email, status, created_at) VALUES(?,?,?,?,?,?)",
        (902, "ZAM-PDF-902", "Other", "other@example.com", "confirmed", backend.now_iso()),
    )
    c.commit()
    c.close()
    response = client.get("/api/client/orders/902/pdf", headers={"Authorization": "Bearer test"})
    assert response.status_code == 403
