import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

import app as backend
import internal_audit as audit
import internal_audit_outbox as outbox
import internal_rbac as rbac


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "DB_PATH", str(tmp_path / "outbox.db"))
    backend.init_db()
    backend.app.secret_key = "outbox-test-secret"
    monkeypatch.setattr(backend, "maybe_pull_shared_from_supabase", lambda **kwargs: None)
    monkeypatch.setattr(backend, "trigger_background_supabase_sync", lambda **kwargs: None)
    return backend.app.test_client()


def _owner(request_id="outbox-request"):
    return rbac.load_actor_context(
        rbac.BOOTSTRAP_OWNER_ACTOR_ID,
        request_id=request_id,
        session_id="outbox-session",
    )


def _event(correlation_id="outbox-process"):
    return audit.record_audit_event(
        "security.login",
        result=audit.SUCCESS,
        actor_context=_owner(),
        correlation_id=correlation_id,
    )


def _outbox_row(audit_id):
    db = backend.conn()
    try:
        return db.execute(
            "SELECT * FROM internal_audit_outbox WHERE audit_id=?", (audit_id,)
        ).fetchone()
    finally:
        db.close()


def _counts():
    db = backend.conn()
    try:
        return (
            db.execute("SELECT COUNT(*) FROM internal_audit_log").fetchone()[0],
            db.execute("SELECT COUNT(*) FROM internal_audit_outbox").fetchone()[0],
        )
    finally:
        db.close()


def test_audit_and_outbox_are_created_atomically(isolated):
    audit_id = _event()
    assert _counts() == (1, 1)
    assert _outbox_row(audit_id)["status"] == "PENDING"


def test_outbox_failure_rolls_back_audit_in_same_transaction(isolated):
    db = backend.conn()
    try:
        db.execute(
            """CREATE TRIGGER reject_test_outbox BEFORE INSERT ON internal_audit_outbox
               BEGIN SELECT RAISE(ABORT, 'test outbox failure'); END"""
        )
        db.commit()
    finally:
        db.close()
    with pytest.raises(sqlite3.DatabaseError, match="test outbox failure"):
        _event()
    assert _counts() == (0, 0)


@pytest.mark.parametrize(
    "failure",
    [ConnectionError("offline"), TimeoutError("timeout"), RuntimeError("HTTP 500")],
)
def test_remote_failures_keep_event_for_retry(isolated, failure):
    audit_id = _event()

    def fail(_payload):
        raise failure

    assert outbox.process_one(worker_id="worker-a", remote_sender=fail, now=100) is False
    row = _outbox_row(audit_id)
    assert row["status"] == "RETRY"
    assert row["attempt_count"] == 1
    assert row["next_attempt_at"] == 105
    assert _counts() == (1, 1)


def test_retry_increases_attempt_count_and_event_can_later_deliver(isolated):
    audit_id = _event()
    attempts = []

    def flaky(payload):
        attempts.append(payload["audit_id"])
        if len(attempts) == 1:
            raise ConnectionError("connection lost")

    assert outbox.process_one(worker_id="worker-a", remote_sender=flaky, now=100) is False
    assert outbox.process_one(worker_id="worker-a", remote_sender=flaky, now=104) is None
    assert outbox.process_one(worker_id="worker-a", remote_sender=flaky, now=105) is True
    row = _outbox_row(audit_id)
    assert row["status"] == "DELIVERED"
    assert row["attempt_count"] == 2
    assert row["delivered_at"]


def test_active_lease_blocks_second_worker_and_expired_lease_is_reclaimed(isolated):
    audit_id = _event()
    first = outbox.claim_one("worker-a", now=100, lease_seconds=10)
    assert first["audit_id"] == audit_id
    assert outbox.claim_one("worker-b", now=105, lease_seconds=10) is None
    second = outbox.claim_one("worker-b", now=111, lease_seconds=10)
    assert second["audit_id"] == audit_id
    assert second["lease_owner"] == "worker-b"
    assert second["attempt_count"] == 2


def test_two_parallel_claims_cannot_both_win(isolated):
    audit_id = _event()
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda worker: outbox.claim_one(worker, now=100), ("a", "b")))
    winners = [claim for claim in claims if claim is not None]
    assert len(winners) == 1
    assert winners[0]["audit_id"] == audit_id


def test_successful_delivery_marks_delivered(isolated):
    audit_id = _event()
    remote = {}

    def send(payload):
        remote.setdefault(payload["audit_id"], payload)

    assert outbox.process_one(worker_id="worker-a", remote_sender=send, now=100) is True
    assert list(remote) == [audit_id]
    assert _outbox_row(audit_id)["status"] == "DELIVERED"


def test_restart_keeps_pending_rows(isolated):
    audit_id = _event()
    backend.init_db()
    claimed = outbox.claim_one("worker-after-restart", now=100)
    assert claimed["audit_id"] == audit_id


def test_uncertain_post_retry_is_idempotent_by_audit_id(isolated):
    audit_id = _event()
    remote = {}

    def idempotent_insert(payload):
        remote.setdefault(payload["audit_id"], payload)

    first = outbox.claim_one("worker-crashed", now=100, lease_seconds=10)
    idempotent_insert(outbox.remote_payload(first))
    # Simulated process death: no local mark_delivered. After lease expiry the
    # next worker repeats the same audit_id and the remote row remains singular.
    second = outbox.claim_one("worker-restarted", now=111, lease_seconds=10)
    assert outbox.deliver_claimed(second, "worker-restarted", idempotent_insert, now=111)
    assert list(remote) == [audit_id]
    assert _outbox_row(audit_id)["status"] == "DELIVERED"
    assert _outbox_row(audit_id)["attempt_count"] == 2


def test_supabase_sender_uses_audit_id_ignore_duplicates(isolated, monkeypatch):
    calls = []

    def capture(path, **kwargs):
        calls.append((path, kwargs))

    monkeypatch.setattr(backend, "supabase_request", capture)
    backend._send_audit_to_supabase({"audit_id": "fixed-audit-id"})
    path, kwargs = calls[0]
    assert path == "/rest/v1/internal_audit_log"
    assert kwargs["params"] == {"on_conflict": "audit_id"}
    assert kwargs["prefer"] == "resolution=ignore-duplicates,return=minimal"
    assert kwargs["payload"]["audit_id"] == "fixed-audit-id"


def test_health_reports_pending_retry_age_success_and_last_error(isolated):
    first = _event("process-one")
    second = _event("process-two")

    def fail(_payload):
        raise RuntimeError("temporary failure")

    outbox.process_one(worker_id="worker-a", remote_sender=fail, now=100)
    outbox.process_one(worker_id="worker-a", remote_sender=lambda _payload: None, now=100)
    health = outbox.queue_health(now=200)
    assert health["pending"] == 1
    assert health["retry"] == 1
    assert health["delivered"] == 1
    assert health["oldest_pending_age_seconds"] is not None
    assert health["last_success_at"]
    assert health["last_error"] == "temporary failure"
    assert {_outbox_row(first)["status"], _outbox_row(second)["status"]} == {
        "RETRY", "DELIVERED"
    }


def test_worker_delivery_does_not_create_recursive_audit(isolated):
    _event()
    assert _counts() == (1, 1)
    outbox.process_one(worker_id="worker-a", remote_sender=lambda _payload: None, now=100)
    assert _counts() == (1, 1)


def test_system_delivery_actor_has_only_delivery_permission(isolated):
    actor = rbac.load_actor_context(outbox.SYSTEM_AUDIT_DELIVERY_ACTOR_ID)
    assert actor.actor_type == "SYSTEM"
    assert actor.roles == ("SYSTEM_AUDIT_DELIVERY",)
    assert actor.permission_decision("system.audit_deliver") == "ALLOW"
    assert actor.permission_decision("system.audit_read") == "DENY"
    assert actor.permission_decision("inventory.adjust") == "DENY"


def test_health_endpoint_requires_audit_read_permission(isolated):
    _event()
    with isolated.session_transaction() as current:
        current["admin_authenticated"] = True
        current["internal_actor_id"] = rbac.BOOTSTRAP_OWNER_ACTOR_ID
        current["internal_session_id"] = str(uuid.uuid4())
    response = isolated.get("/api/internal/audit-outbox/status")
    assert response.status_code == 200
    assert response.get_json()["outbox"]["pending"] == 1
    assert backend.app.view_functions[
        "internal_audit_outbox_status"
    ].required_permission == "system.audit_read"
