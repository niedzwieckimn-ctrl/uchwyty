import json
import sqlite3
import uuid

import pytest
from flask import Flask

import app as backend
import internal_audit as audit
import internal_concurrency as concurrency
import internal_rbac as rbac


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "DB_PATH", str(tmp_path / "audit.db"))
    backend.init_db()
    backend.app.secret_key = "audit-test-secret"
    monkeypatch.setattr(backend, "maybe_pull_shared_from_supabase", lambda **kwargs: None)
    monkeypatch.setattr(backend, "trigger_background_supabase_sync", lambda **kwargs: None)
    return backend.app.test_client()


def _owner(request_id="request-test"):
    return rbac.load_actor_context(
        rbac.BOOTSTRAP_OWNER_ACTOR_ID,
        request_id=request_id,
        session_id="session-test",
    )


def _row(audit_id):
    db = backend.conn()
    try:
        return db.execute(
            "SELECT * FROM internal_audit_log WHERE audit_id=?", (audit_id,)
        ).fetchone()
    finally:
        db.close()


def _create_actor(role_key):
    actor_id = str(uuid.uuid4())
    db = backend.conn()
    try:
        now = backend.now_iso()
        db.execute(
            """INSERT INTO internal_actors(
                actor_id,actor_type,display_name,status,created_at,updated_at
            ) VALUES(?, 'HUMAN', 'Audit test', 'active', ?, ?)""",
            (actor_id, now, now),
        )
        db.execute(
            "INSERT INTO internal_actor_roles(actor_id,role_key,assigned_at) VALUES(?,?,?)",
            (actor_id, role_key, now),
        )
        db.commit()
    finally:
        db.close()
    return actor_id


def _login_as(client, actor_id):
    with client.session_transaction() as current:
        current["admin_authenticated"] = True
        current["internal_actor_id"] = actor_id
        current["internal_session_id"] = "session-test"


def _insert_product():
    db = backend.conn()
    try:
        db.execute(
            "INSERT INTO products(id,sku,model,name,created_at) VALUES(1,'AUDIT-1','A','Test',?)",
            (backend.now_iso(),),
        )
        db.execute("INSERT INTO stock(product_id,qty) VALUES(1,2)")
        db.commit()
    finally:
        db.close()


def test_success_records_actor_operation_version_permission_and_request(isolated):
    actor = _owner("request-success")
    audit_id = audit.record_audit_event(
        "inventory.product.read",
        result=audit.SUCCESS,
        actor_context=actor,
        entity_type="product",
        entity_id="1",
    )
    row = _row(audit_id)
    assert row["result"] == "SUCCESS"
    assert row["actor_id"] == rbac.BOOTSTRAP_OWNER_ACTOR_ID
    assert row["actor_type"] == "HUMAN"
    assert json.loads(row["roles_json"]) == ["OWNER"]
    assert row["operation"] == "inventory.product.read"
    assert row["operation_version"] == 1
    assert row["permission"] == "inventory.read"
    assert row["request_id"] == "request-success"


def test_denied_can_be_recorded_with_trusted_actor(isolated):
    actor_id = _create_actor("KSIEGOWOSC")
    actor = rbac.load_actor_context(actor_id, request_id="request-denied")
    audit_id = audit.record_audit_event(
        "security.permission.denied",
        result=audit.DENIED,
        actor_context=actor,
        permission="inventory.read",
    )
    row = _row(audit_id)
    assert row["result"] == "DENIED"
    assert row["actor_id"] == actor_id
    assert row["permission"] == "inventory.read"
    assert row["risk_level"] == "YELLOW"


def test_request_cannot_replace_actor_or_risk_level(isolated):
    _insert_product()
    _login_as(isolated, rbac.BOOTSTRAP_OWNER_ACTOR_ID)
    response = isolated.get(
        "/api/product/1",
        headers={"X-Actor-ID": str(uuid.uuid4()), "X-Risk-Level": "RED"},
    )
    assert response.status_code == 200
    db = backend.conn()
    try:
        row = db.execute(
            "SELECT * FROM internal_audit_log WHERE operation='inventory.product.read'"
        ).fetchone()
    finally:
        db.close()
    assert row["actor_id"] == rbac.BOOTSTRAP_OWNER_ACTOR_ID
    assert row["risk_level"] == "GREEN"


def test_sanitizer_masks_secrets_cards_and_nested_values(isolated):
    cleaned = audit.sanitize_audit_data({
        "password": "secret-password",
        "authorization_header": "Bearer secret-token",
        "card_number": "4111111111111111",
        "nested": {"api_key": "top-secret", "safe": "ok"},
    })
    assert cleaned["password"] == audit.REDACTED
    assert cleaned["authorization_header"] == audit.REDACTED
    assert cleaned["card_number"] == audit.MASKED
    assert cleaned["nested"]["api_key"] == audit.REDACTED
    assert cleaned["nested"]["safe"] == "ok"
    text = audit.sanitize_audit_text("request failed authorization=secret Bearer abc.def")
    assert "secret" not in text
    assert "abc.def" not in text
    assert audit.REDACTED in text


def test_states_are_diffed_sanitized_and_size_limited(isolated):
    actor = _owner()
    audit_id = audit.record_audit_event(
        "internal.versioned_resource.update",
        result=audit.SUCCESS,
        actor_context=actor,
        before_state={"unchanged": 1, "password": "old", "value": "old"},
        after_state={"unchanged": 1, "password": "new", "value": "x" * 100_000},
    )
    row = _row(audit_id)
    before = json.loads(row["before_state"])
    after = json.loads(row["after_state"])
    assert "unchanged" not in before
    assert before["password"] == audit.REDACTED
    assert after["password"] == audit.REDACTED
    assert len(row["after_state"].encode("utf-8")) <= audit.MAX_STATE_BYTES


def test_audit_log_is_append_only_in_application_database(isolated):
    audit_id = audit.record_audit_event(
        "security.login", result=audit.SUCCESS, actor_context=_owner()
    )
    db = backend.conn()
    try:
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            db.execute(
                "UPDATE internal_audit_log SET result='FAILED' WHERE audit_id=?", (audit_id,)
            )
        db.rollback()
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            db.execute("DELETE FROM internal_audit_log WHERE audit_id=?", (audit_id,))
    finally:
        db.close()


def test_correlation_id_and_request_id_are_persisted(isolated):
    audit_id = audit.record_audit_event(
        "security.login",
        result=audit.SUCCESS,
        actor_context=_owner("request-correlation"),
        correlation_id="order-process-123",
    )
    row = _row(audit_id)
    assert row["correlation_id"] == "order-process-123"
    assert row["request_id"] == "request-correlation"


def test_permission_denial_is_audited_and_business_function_is_not_called(isolated):
    called = []
    test_app = Flask("audit-denial-test")
    test_app.secret_key = "audit-denial-secret"

    @test_app.get("/audit-test-denial")
    @rbac.require_permission("inventory.read", operation="inventory.product.read")
    def audit_test_denial():
        called.append(True)
        return "should not execute"

    actor_id = _create_actor("KSIEGOWOSC")
    client = test_app.test_client()
    _login_as(client, actor_id)
    response = client.get("/audit-test-denial")
    assert response.status_code == 403
    assert called == []
    db = backend.conn()
    try:
        row = db.execute(
            "SELECT result,permission,actor_id FROM internal_audit_log ORDER BY occurred_at DESC"
        ).fetchone()
    finally:
        db.close()
    assert tuple(row) == ("DENIED", "inventory.read", actor_id)


def test_bootstrap_login_is_audited(isolated, monkeypatch):
    monkeypatch.setattr(backend, "ADMIN_USERNAME", "audit-admin")
    monkeypatch.setattr(backend, "ADMIN_PASSWORD", "correct-password")
    monkeypatch.setattr(backend, "ADMIN_PASSWORD_HASH", "")
    response = isolated.post(
        "/login", data={"username": "audit-admin", "password": "correct-password"}
    )
    assert response.status_code == 302
    db = backend.conn()
    try:
        row = db.execute(
            "SELECT operation,result,actor_id FROM internal_audit_log WHERE operation='security.login'"
        ).fetchone()
    finally:
        db.close()
    assert tuple(row) == ("security.login", "SUCCESS", rbac.BOOTSTRAP_OWNER_ACTOR_ID)


def test_optimistic_concurrency_detects_conflict_and_preserves_newer_state(isolated):
    actor = _owner("request-version")
    resource = concurrency.create_versioned_resource(
        "pilot", {"value": "initial"}, actor_context=actor, resource_id=str(uuid.uuid4())
    )
    updated = concurrency.update_versioned_resource(
        resource.resource_id, 1, {"value": "newer"}, actor_context=actor
    )
    assert updated.version == 2
    with pytest.raises(concurrency.OptimisticConcurrencyConflict) as exc:
        concurrency.update_versioned_resource(
            resource.resource_id, 1, {"value": "stale"}, actor_context=actor
        )
    assert exc.value.current_version == 2
    final = concurrency.get_versioned_resource(resource.resource_id)
    assert final.version == 2
    assert final.payload == {"value": "newer"}
    db = backend.conn()
    try:
        conflict = db.execute(
            "SELECT * FROM internal_audit_log WHERE result='CONFLICT'"
        ).fetchone()
    finally:
        db.close()
    assert conflict["error_code"] == "VERSION_CONFLICT"
    assert conflict["entity_version_before"] == 2
    assert conflict["entity_version_after"] is None


def test_audit_read_service_requires_system_audit_permission(isolated):
    assert audit.fetch_audit_events(_owner(), limit=5) == []
    magazyn_id = _create_actor("MAGAZYN")
    with pytest.raises(PermissionError):
        audit.fetch_audit_events(rbac.load_actor_context(magazyn_id))


def test_idempotency_and_external_metadata_are_supported(isolated):
    audit_id = audit.record_audit_event(
        "security.login",
        result=audit.NOOP,
        actor_context=_owner(),
        idempotency_key="existing-key",
        is_replay=True,
        external_integration="inpost",
        external_request_id="external-1",
        external_result="ALREADY_PROCESSED",
    )
    row = _row(audit_id)
    assert row["idempotency_key"] == "existing-key"
    assert row["is_replay"] == 1
    assert row["external_integration"] == "INPOST"
    assert row["external_result"] == "ALREADY_PROCESSED"
