import json
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

import app as backend
import internal_approval as approval
import internal_audit as audit
import internal_concurrency as concurrency
import internal_rbac as rbac


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "DB_PATH", str(tmp_path / "approval.db"))
    backend.init_db()
    monkeypatch.setattr(backend, "maybe_pull_shared_from_supabase", lambda **kwargs: None)
    monkeypatch.setattr(backend, "trigger_background_supabase_sync", lambda **kwargs: None)
    return tmp_path


def _create_actor(actor_type="HUMAN", role_key="OWNER"):
    actor_id = str(uuid.uuid4())
    db = backend.conn()
    try:
        now = backend.now_iso()
        db.execute(
            """INSERT INTO internal_actors(actor_id,actor_type,display_name,status,created_at,updated_at)
               VALUES(?,?,?,'active',?,?)""",
            (actor_id, actor_type, "Approval test", now, now),
        )
        if role_key:
            db.execute(
                "INSERT INTO internal_actor_roles(actor_id,role_key,assigned_at) VALUES(?,?,?)",
                (actor_id, role_key, now),
            )
        db.commit()
    finally:
        db.close()
    return rbac.load_actor_context(actor_id, request_id=f"req-{actor_id[:8]}")


def _owner():
    return rbac.load_actor_context(rbac.BOOTSTRAP_OWNER_ACTOR_ID, request_id="request-owner")


def _resource(actor=None):
    return concurrency.create_versioned_resource(
        "approval-pilot", {"amount": 0}, actor_context=actor or _owner()
    )


def _request(requester, resource, payload=None, operation="internal.test.change_setting"):
    return approval.request_approval(
        requester,
        operation,
        payload=payload or {"amount": 2000},
        entity_type="internal_versioned_resource",
        entity_id=resource.resource_id,
        expected_entity_version=resource.version,
        correlation_id="approval-process-1",
        reason="Izolowany test",
    )


def _approve(approval_id, approver=None):
    approval.approve_request(approval_id, approver or _create_actor(), reason="Zatwierdzam test")


def _row(approval_id):
    db = backend.conn()
    try:
        return db.execute(
            "SELECT * FROM internal_approval_requests WHERE approval_id=?", (approval_id,)
        ).fetchone()
    finally:
        db.close()


def test_green_with_permission_needs_no_approval(isolated):
    result = approval.evaluate_operation(_owner(), "internal.test.read_status")
    assert result.allowed is True and result.requires_approval is False
    assert result.risk_level == "GREEN"


def test_green_without_permission_is_denied(isolated):
    actor = _create_actor("HUMAN", "MAGAZYN")
    result = approval.evaluate_operation(actor, "internal.test.read_status")
    assert result.allowed is False and result.reason == "PERMISSION_DENIED"


def test_yellow_creates_pending_and_cannot_execute_before_approval(isolated):
    requester, resource = _owner(), _resource()
    approval_id = _request(requester, resource)
    assert _row(approval_id)["status"] == "PENDING"
    with pytest.raises(approval.ApprovalDenied) as exc:
        approval.execute_pilot_change(
            approval_id, requester, resource.resource_id,
            payload={"amount": 2000}, expected_entity_version=1,
        )
    assert exc.value.code == "INVALID_APPROVAL_STATUS"
    assert concurrency.get_versioned_resource(resource.resource_id).payload == {"amount": 0}


def test_authorized_human_approves_and_pilot_executes_once(isolated):
    requester, resource = _owner(), _resource()
    approval_id = _request(requester, resource)
    _approve(approval_id)
    changed = approval.execute_pilot_change(
        approval_id, requester, resource.resource_id,
        payload={"amount": 2000}, expected_entity_version=1,
    )
    assert changed.payload == {"amount": 2000}
    assert changed.version == 2
    assert _row(approval_id)["status"] == "CONSUMED"
    with pytest.raises(approval.ApprovalDenied) as exc:
        approval.execute_pilot_change(
            approval_id, requester, resource.resource_id,
            payload={"amount": 2000}, expected_entity_version=1,
        )
    assert exc.value.code == "INVALID_APPROVAL_STATUS"


def test_unauthorized_human_cannot_approve(isolated):
    approval_id = _request(_owner(), _resource())
    with pytest.raises(approval.ApprovalDenied) as exc:
        approval.approve_request(approval_id, _create_actor("HUMAN", "MAGAZYN"))
    assert exc.value.code == "APPROVER_PERMISSION_DENIED"


@pytest.mark.parametrize("actor_type,role", [("AI_AGENT", "AI_OWNER_ASSISTANT"), ("SYSTEM", "SYSTEM_DATA_SYNC")])
def test_non_human_cannot_approve_red(isolated, actor_type, role):
    requester, resource = _owner(), _resource()
    approval_id = _request(requester, resource, operation="internal.test.red_action")
    with pytest.raises(approval.ApprovalDenied) as exc:
        approval.approve_request(approval_id, _create_actor(actor_type, role))
    assert exc.value.code == "HUMAN_APPROVER_REQUIRED"


def test_red_always_requires_human_approval(isolated):
    result = approval.evaluate_operation(_owner(), "internal.test.red_action")
    assert result.allowed and result.requires_approval and result.risk_level == "RED"


def test_expired_approval_cannot_be_approved_or_executed(isolated):
    approval_id = _request(_owner(), _resource())
    db = backend.conn()
    try:
        db.execute("UPDATE internal_approval_requests SET expires_at='2000-01-01T00:00:00+00:00' WHERE approval_id=?", (approval_id,))
        db.commit()
    finally:
        db.close()
    with pytest.raises(approval.ApprovalDenied):
        approval.approve_request(approval_id, _create_actor())
    assert _row(approval_id)["status"] == "EXPIRED"


def test_rejected_and_cancelled_approvals_cannot_execute(isolated):
    requester = _owner()
    for action in ("reject", "cancel"):
        resource = _resource()
        approval_id = _request(requester, resource)
        if action == "reject":
            approval.reject_request(approval_id, _create_actor(), reason="Nie")
        else:
            approval.cancel_request(approval_id, requester, reason="Anuluj")
        with pytest.raises(approval.ApprovalDenied):
            approval.execute_pilot_change(
                approval_id, requester, resource.resource_id,
                payload={"amount": 2000}, expected_entity_version=1,
            )
        assert _row(approval_id)["status"] == action.upper() + ("ED" if action == "reject" else "LED")


def test_payload_change_from_2000_to_20000_is_stale(isolated):
    requester, resource = _owner(), _resource()
    approval_id = _request(requester, resource, {"amount": 2000})
    _approve(approval_id)
    with pytest.raises(approval.StaleApproval) as exc:
        approval.execute_pilot_change(
            approval_id, requester, resource.resource_id,
            payload={"amount": 20000}, expected_entity_version=1,
        )
    assert exc.value.code == "STALE_APPROVAL"
    assert concurrency.get_versioned_resource(resource.resource_id).payload == {"amount": 0}


def test_operation_version_change_is_stale(isolated):
    requester, resource = _owner(), _resource()
    approval_id = _request(requester, resource)
    _approve(approval_id)
    with pytest.raises(approval.StaleApproval) as exc:
        approval.authorize_execution(
            approval_id, requester, "internal.test.change_setting",
            operation_version=2, payload={"amount": 2000},
            entity_type="internal_versioned_resource", entity_id=resource.resource_id,
            expected_entity_version=1, current_entity_version=1,
        )
    assert exc.value.code == "POLICY_NOT_FOUND"


def test_entity_version_change_causes_conflict(isolated):
    requester, resource = _owner(), _resource()
    approval_id = _request(requester, resource)
    _approve(approval_id)
    concurrency.update_versioned_resource(resource.resource_id, 1, {"amount": 10}, actor_context=requester)
    with pytest.raises(approval.StaleApproval) as exc:
        approval.execute_pilot_change(
            approval_id, requester, resource.resource_id,
            payload={"amount": 2000}, expected_entity_version=1,
        )
    assert exc.value.code == "ENTITY_VERSION_CONFLICT"


def test_permission_revocation_before_execution_denies(isolated):
    requester, resource = _owner(), _resource()
    approval_id = _request(requester, resource)
    _approve(approval_id)
    db = backend.conn()
    try:
        db.execute("DELETE FROM internal_role_permissions WHERE role_key='OWNER' AND permission_key='internal.test.change_setting'")
        db.commit()
    finally:
        db.close()
    with pytest.raises(approval.ApprovalDenied) as exc:
        approval.execute_pilot_change(
            approval_id, requester, resource.resource_id,
            payload={"amount": 2000}, expected_entity_version=1,
        )
    assert exc.value.code == "PERMISSION_REVOKED"


def test_policy_becoming_red_invalidates_yellow_approval(isolated):
    requester, resource = _owner(), _resource()
    approval_id = _request(requester, resource)
    _approve(approval_id)
    db = backend.conn()
    try:
        db.execute("UPDATE internal_approval_policies SET risk_level='RED' WHERE policy_id='policy-internal-test-change'")
        db.commit()
    finally:
        db.close()
    with pytest.raises(approval.StaleApproval) as exc:
        approval.execute_pilot_change(
            approval_id, requester, resource.resource_id,
            payload={"amount": 2000}, expected_entity_version=1,
        )
    assert exc.value.code == "POLICY_CHANGED"


def test_risk_spoofing_is_ignored(isolated):
    result = approval.evaluate_operation(
        _owner(), "internal.test.red_action", claimed_risk_level="GREEN"
    )
    assert result.risk_level == "RED" and result.requires_approval


def test_ai_may_request_but_may_not_approve(isolated):
    ai = _create_actor("AI_AGENT", "AI_OWNER_ASSISTANT")
    resource = _resource()
    approval_id = _request(ai, resource)
    assert _row(approval_id)["requesting_actor_type"] == "AI_AGENT"
    with pytest.raises(approval.ApprovalDenied) as exc:
        approval.approve_request(approval_id, ai)
    assert exc.value.code == "HUMAN_APPROVER_REQUIRED"


def test_actor_type_spoofing_is_rejected(isolated):
    ai = _create_actor("AI_AGENT", "AI_OWNER_ASSISTANT")
    forged = replace(ai, actor_type="HUMAN")
    with pytest.raises(approval.ApprovalDenied) as exc:
        approval.evaluate_operation(forged, "internal.test.change_setting")
    assert exc.value.code == "UNTRUSTED_ACTOR"


def test_approver_lost_permission_cannot_decide(isolated):
    approval_id = _request(_owner(), _resource())
    approver = _create_actor()
    db = backend.conn()
    try:
        db.execute("DELETE FROM internal_actor_roles WHERE actor_id=?", (approver.actor_id,))
        db.commit()
    finally:
        db.close()
    with pytest.raises(approval.ApprovalDenied) as exc:
        approval.approve_request(approval_id, approver)
    assert exc.value.code == "APPROVER_PERMISSION_DENIED"


def test_approver_permission_is_rechecked_before_execution(isolated):
    requester, resource = _owner(), _resource()
    approval_id = _request(requester, resource)
    approver = _create_actor()
    _approve(approval_id, approver)
    db = backend.conn()
    try:
        db.execute("DELETE FROM internal_actor_roles WHERE actor_id=?", (approver.actor_id,))
        db.commit()
    finally:
        db.close()
    with pytest.raises(approval.ApprovalDenied) as exc:
        approval.execute_pilot_change(
            approval_id, requester, resource.resource_id,
            payload={"amount": 2000}, expected_entity_version=1,
        )
    assert exc.value.code == "APPROVER_PERMISSION_REVOKED"


def test_self_approval_is_denied_by_policy(isolated):
    requester, resource = _owner(), _resource()
    approval_id = _request(requester, resource)
    with pytest.raises(approval.ApprovalDenied) as exc:
        approval.approve_request(approval_id, requester)
    assert exc.value.code == "SELF_APPROVAL_DENIED"


def test_two_parallel_executions_only_one_succeeds(isolated):
    requester, resource = _owner(), _resource()
    approval_id = _request(requester, resource)
    _approve(approval_id)

    def execute():
        try:
            approval.execute_pilot_change(
                approval_id, requester, resource.resource_id,
                payload={"amount": 2000}, expected_entity_version=1,
            )
            return "SUCCESS"
        except (approval.ApprovalDenied, concurrency.OptimisticConcurrencyConflict):
            return "DENIED"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: execute(), range(2)))
    assert sorted(results) == ["DENIED", "SUCCESS"]
    assert concurrency.get_versioned_resource(resource.resource_id).version == 2


def test_approval_events_use_existing_audit_outbox_and_correlation(isolated):
    requester, resource = _owner(), _resource()
    approval_id = _request(requester, resource)
    _approve(approval_id)
    approval.execute_pilot_change(
        approval_id, requester, resource.resource_id,
        payload={"amount": 2000}, expected_entity_version=1,
    )
    db = backend.conn()
    try:
        rows = db.execute(
            """SELECT a.operation,a.correlation_id,o.status
               FROM internal_audit_log a JOIN internal_audit_outbox o ON o.audit_id=a.audit_id
               WHERE a.approval_id=? ORDER BY a.occurred_at,a.audit_id""",
            (approval_id,),
        ).fetchall()
    finally:
        db.close()
    assert {row["operation"] for row in rows} >= {
        "approval.requested", "approval.approved", "approval.consumed"
    }
    assert {row["correlation_id"] for row in rows} == {"approval-process-1"}
    assert all(row["status"] == "PENDING" for row in rows)


def test_safe_payload_and_audit_do_not_store_secrets(isolated):
    requester, resource = _owner(), _resource()
    approval_id = _request(
        requester, resource,
        {"amount": 2000, "api_token": "never-store-this", "nested": {"password": "also-secret"}},
    )
    row = _row(approval_id)
    assert "never-store-this" not in row["safe_payload"]
    assert "also-secret" not in row["safe_payload"]
    assert json.loads(row["safe_payload"])["api_token"] == audit.REDACTED
    db = backend.conn()
    try:
        audit_rows = db.execute(
            "SELECT before_state,after_state,reason,error_message FROM internal_audit_log WHERE approval_id=?",
            (approval_id,),
        ).fetchall()
    finally:
        db.close()
    encoded = json.dumps([tuple(row) for row in audit_rows])
    assert "never-store-this" not in encoded and "also-secret" not in encoded


def test_request_and_audit_outbox_are_atomic(isolated, monkeypatch):
    requester, resource = _owner(), _resource()
    monkeypatch.setattr(
        approval, "record_audit_event",
        lambda *args, **kwargs: (_ for _ in ()).throw(sqlite3.DatabaseError("audit failed")),
    )
    with pytest.raises(sqlite3.DatabaseError, match="audit failed"):
        _request(requester, resource)
    db = backend.conn()
    try:
        assert db.execute("SELECT COUNT(*) FROM internal_approval_requests").fetchone()[0] == 0
    finally:
        db.close()


def test_approval_transition_and_audit_outbox_are_atomic(isolated, monkeypatch):
    approval_id = _request(_owner(), _resource())
    approver = _create_actor()
    monkeypatch.setattr(
        approval, "record_audit_event",
        lambda *args, **kwargs: (_ for _ in ()).throw(sqlite3.DatabaseError("audit failed")),
    )
    with pytest.raises(sqlite3.DatabaseError, match="audit failed"):
        approval.approve_request(approval_id, approver)
    assert _row(approval_id)["status"] == "PENDING"
