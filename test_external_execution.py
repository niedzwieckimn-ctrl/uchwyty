import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

import app as backend
import business_operations as operations
import external_execution as external
import internal_approval as approval
import internal_rbac as rbac


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "DB_PATH", str(tmp_path / "external.db"))
    backend.init_db()
    monkeypatch.setattr(backend, "maybe_pull_shared_from_supabase", lambda **kwargs: None)
    monkeypatch.setattr(backend, "trigger_background_supabase_sync", lambda **kwargs: None)
    adapter = external.IsolatedTestAdapter()
    external.register_adapter("test", adapter)
    return adapter


def _owner():
    return rbac.load_actor_context(rbac.BOOTSTRAP_OWNER_ACTOR_ID, request_id="external-owner")


def _approver():
    actor_id = str(uuid.uuid4())
    db = backend.conn()
    try:
        now = backend.now_iso()
        db.execute(
            "INSERT INTO internal_actors(actor_id,actor_type,display_name,status,created_at,updated_at) VALUES(?, 'HUMAN','Approver','active',?,?)",
            (actor_id, now, now),
        )
        db.execute("INSERT INTO internal_actor_roles(actor_id,role_key,assigned_at) VALUES(?,'OWNER',?)", (actor_id, now))
        db.commit()
    finally:
        db.close()
    return rbac.load_actor_context(actor_id, request_id="external-approver")


def _queued(scenario="success", key=None, value="safe-value"):
    actor = _owner()
    payload = {"scenario": scenario, "value": value}
    first = operations.execute_business_operation(
        actor, "internal.test.external.execute", payload,
        idempotency_key=key or f"key-{uuid.uuid4()}", correlation_id="external-flow",
    )
    assert first.status == "PENDING_APPROVAL"
    approval.approve_request(first.approval_id, _approver(), reason="Test adapter")
    resumed = operations.execute_business_operation(
        actor, "internal.test.external.execute", payload,
        idempotency_key=key or "unused", approval_id=first.approval_id,
    ) if key else operations.execute_business_operation(
        actor, "internal.test.external.execute", payload,
        idempotency_key=_execution(first.execution_id)["idempotency_key"], approval_id=first.approval_id,
    )
    assert resumed.status == "AUTHORIZED"
    return first.execution_id, first.approval_id


def _execution(execution_id):
    db = backend.conn()
    try:
        return db.execute("SELECT * FROM internal_operation_executions WHERE execution_id=?", (execution_id,)).fetchone()
    finally:
        db.close()


def _approval(approval_id):
    db = backend.conn()
    try:
        return db.execute("SELECT * FROM internal_approval_requests WHERE approval_id=?", (approval_id,)).fetchone()
    finally:
        db.close()


def test_success_consumes_approval_only_when_send_starts(isolated):
    execution_id, approval_id = _queued()
    assert _approval(approval_id)["status"] == "APPROVED"
    result = external.process_next("worker-a")
    assert result["execution_id"] == execution_id and result["queue_status"] == external.SUCCEEDED
    assert _approval(approval_id)["status"] == "CONSUMED"
    assert isolated.create_count == 1


def test_failure_before_send_retries_without_consuming_approval(isolated):
    execution_id, approval_id = _queued("timeout_before_send")
    result = external.process_next("worker-a")
    assert result["queue_status"] == external.FAILED_RETRYABLE
    assert _approval(approval_id)["status"] == "APPROVED"
    assert isolated.create_count == 0 and not isolated.execute_calls


@pytest.mark.parametrize("scenario", ["lost_response", "timeout_after_side_effect"])
def test_lost_success_becomes_unknown_and_reconciles_without_duplicate(isolated, scenario):
    execution_id, _ = _queued(scenario)
    first = external.process_next("worker-a")
    assert first["queue_status"] == external.UNKNOWN and isolated.create_count == 1
    reconciled = external.reconcile_next("reconciler-a")
    assert reconciled["queue_status"] == external.RECONCILED_SUCCESS
    assert isolated.create_count == 1
    assert external.process_next("worker-b") is None


def test_two_workers_claim_once(isolated):
    execution_id, _ = _queued("success")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda worker: external.process_next(worker), ("worker-a", "worker-b")))
    handled = [item for item in results if item is not None]
    assert len(handled) == 1 and handled[0]["execution_id"] == execution_id
    assert isolated.create_count == 1


def test_crash_before_send_lease_expiry_allows_safe_takeover(isolated):
    execution_id, approval_id = _queued("crash_before_send")
    now = datetime.now(timezone.utc) + timedelta(seconds=1)
    with pytest.raises(external.SimulatedWorkerCrash):
        external.process_next("dead-worker", lease_seconds=10, now=now)
    assert external.get_execution(execution_id)["queue_status"] == external.LEASED
    assert _approval(approval_id)["status"] == "APPROVED"
    result = external.process_next("replacement", now=now + timedelta(seconds=11))
    assert result["queue_status"] == external.SUCCEEDED and isolated.create_count == 1


def test_crash_after_remote_success_uses_reconciliation_not_post(isolated):
    execution_id, _ = _queued("crash_after_remote_success")
    now = datetime.now(timezone.utc) + timedelta(seconds=1)
    with pytest.raises(external.SimulatedWorkerCrash):
        external.process_next("dead-worker", lease_seconds=10, now=now)
    assert isolated.create_count == 1
    assert external.process_next("replacement", now=now + timedelta(seconds=11)) is None
    assert external.get_execution(execution_id)["queue_status"] == external.UNKNOWN
    result = external.reconcile_next("reconciler", now=now + timedelta(seconds=11))
    assert result["queue_status"] == external.RECONCILED_SUCCESS and isolated.create_count == 1


def test_http_400_is_permanent(isolated):
    _, approval_id = _queued("http_400")
    result = external.process_next("worker")
    assert result["queue_status"] == external.FAILED_PERMANENT
    assert _approval(approval_id)["status"] == "CONSUMED"
    assert isolated.create_count == 0


def test_rate_limit_retries_with_same_external_key(isolated):
    execution_id, _ = _queued("rate_limit")
    now = datetime.now(timezone.utc) + timedelta(seconds=1)
    first = external.process_next("worker", now=now)
    assert first["queue_status"] == external.FAILED_RETRYABLE
    key = first["external_idempotency_key"]
    second = external.process_next("worker", now=now + timedelta(hours=1))
    assert second["queue_status"] == external.SUCCEEDED
    assert second["external_idempotency_key"] == key and isolated.create_count == 1
    assert isolated.execute_calls[key] == 2


def test_http_500_is_classified_retryable_and_adapter_duplicate_is_idempotent(isolated):
    _, _ = _queued("http_500")
    now = datetime.now(timezone.utc) + timedelta(seconds=1)
    assert external.process_next("worker", now=now)["queue_status"] == external.FAILED_RETRYABLE
    assert external.process_next("worker", now=now + timedelta(hours=1))["queue_status"] == external.SUCCEEDED
    prepared = {"scenario": "duplicate", "value": "x", "idempotency_key": "external-direct-key"}
    first = isolated.execute(prepared, "external-direct-key")
    second = isolated.execute(prepared, "external-direct-key")
    assert first.resource_id == second.resource_id and second.status == "duplicate"


def test_queued_record_survives_worker_restart(isolated):
    execution_id, _ = _queued("success")
    replacement_adapter = external.IsolatedTestAdapter()
    external.register_adapter("test", replacement_adapter)
    result = external.process_next("restarted-worker")
    assert result["execution_id"] == execution_id and result["queue_status"] == external.SUCCEEDED
    assert replacement_adapter.create_count == 1


def test_reconciliation_can_confirm_remote_absence(isolated):
    execution_id, _ = _queued("remote_absent")
    assert external.process_next("worker")["queue_status"] == external.UNKNOWN
    result = external.reconcile_next("reconciler")
    assert result["queue_status"] == external.RECONCILED_FAILED
    assert isolated.create_count == 0


def test_still_unknown_is_rescheduled_and_never_resent(isolated):
    execution_id, _ = _queued("still_unknown")
    now = datetime.now(timezone.utc) + timedelta(seconds=1)
    external.process_next("worker", now=now)
    result = external.reconcile_next("reconciler", now=now)
    assert result["queue_status"] == external.UNKNOWN
    assert result["reconciliation_attempt_count"] == 1
    assert external.process_next("worker", now=now + timedelta(hours=2)) is None
    assert isolated.execute_calls[result["external_idempotency_key"]] == 1


def test_invalid_approval_before_first_send_causes_no_external_call(isolated):
    execution_id, approval_id = _queued("success")
    db = backend.conn()
    try:
        db.execute("UPDATE internal_approval_requests SET status='CANCELLED' WHERE approval_id=?", (approval_id,))
        db.commit()
    finally:
        db.close()
    result = external.process_next("worker")
    assert result["queue_status"] == external.FAILED_PERMANENT
    assert isolated.create_count == 0 and not isolated.execute_calls


def test_changed_snapshot_fails_fingerprint_check_before_send(isolated):
    execution_id, _ = _queued("success")
    db = backend.conn()
    try:
        db.execute(
            "UPDATE internal_external_execution_queue SET safe_input_snapshot=? WHERE execution_id=?",
            (json.dumps({"scenario": "success", "value": "changed"}), execution_id),
        )
        db.commit()
    finally:
        db.close()
    result = external.process_next("worker")
    assert result["queue_status"] == external.FAILED_PERMANENT
    assert isolated.create_count == 0


def test_changed_source_object_does_not_change_approved_snapshot(isolated):
    actor = _owner()
    payload = {"scenario": "success", "value": "approved-value"}
    key = "immutable-source"
    pending = operations.execute_business_operation(
        actor, "internal.test.external.execute", payload, idempotency_key=key
    )
    approval.approve_request(pending.approval_id, _approver(), reason="Approve original")
    queued = operations.execute_business_operation(
        actor, "internal.test.external.execute", payload,
        idempotency_key=key, approval_id=pending.approval_id,
    )
    payload["value"] = "changed-after-approval"
    external.process_next("worker")
    record = external.get_execution(queued.execution_id)
    assert json.loads(record["safe_input_snapshot"])["value"] == "approved-value"
    assert json.loads(record["safe_external_request_snapshot"])["value"] == "approved-value"


def test_snapshots_and_errors_are_sanitized(isolated):
    secret = "super-secret-value"
    execution_id, _ = _queued("success", value=f"api_token={secret}")
    external.process_next("worker")
    db = backend.conn()
    try:
        row = db.execute("SELECT * FROM internal_external_execution_queue WHERE execution_id=?", (execution_id,)).fetchone()
        audit_rows = db.execute("SELECT before_state,after_state,error_message FROM internal_audit_log WHERE entity_id=?", (execution_id,)).fetchall()
    finally:
        db.close()
    serialized = json.dumps(dict(row), default=str) + json.dumps([tuple(x) for x in audit_rows])
    assert secret not in serialized
    assert "[REDACTED]" in serialized


def test_adapter_exception_secret_is_removed_from_record_audit_and_health(isolated):
    execution_id, _ = _queued("secret_error")
    result = external.process_next("worker")
    health = external.queue_health(_owner())
    db = backend.conn()
    try:
        audit_rows = db.execute("SELECT error_message,after_state FROM internal_audit_log WHERE entity_id=?", (execution_id,)).fetchall()
    finally:
        db.close()
    serialized = json.dumps(result, default=str) + json.dumps(health) + json.dumps([tuple(row) for row in audit_rows])
    assert "super-secret-adapter-token" not in serialized
    assert "[REDACTED]" in serialized


def test_health_requires_permission_and_exposes_only_aggregates(isolated):
    _queued("success")
    health = external.queue_health(_owner())
    assert health["queued"] == 1 and "safe_input_snapshot" not in health
    warehouse = rbac.load_actor_context("", request_id="none")
    with pytest.raises(PermissionError):
        external.queue_health(warehouse)
    assert backend.app.view_functions["api_external_execution_health"].required_permission == "system.audit_read"


def test_expected_audit_chain_exists(isolated):
    execution_id, _ = _queued("lost_response")
    external.process_next("worker")
    external.reconcile_next("reconciler")
    db = backend.conn()
    try:
        events = [row[0] for row in db.execute(
            "SELECT operation FROM internal_audit_log WHERE entity_id=? ORDER BY occurred_at", (execution_id,)
        ).fetchall()]
    finally:
        db.close()
    for expected in (
        "external_execution.queued", "external_execution.claimed", "external_execution.started",
        "external_execution.unknown", "external_execution.reconciliation_started",
        "external_execution.reconciled_success",
    ):
        assert expected in events
