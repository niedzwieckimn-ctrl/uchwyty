import uuid

import pytest

import app as backend
import internal_rbac as rbac


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "DB_PATH", str(tmp_path / "rbac.db"))
    backend.init_db()
    backend.app.secret_key = "rbac-test-secret"
    monkeypatch.setattr(backend, "maybe_pull_shared_from_supabase", lambda **kwargs: None)
    monkeypatch.setattr(backend, "trigger_background_supabase_sync", lambda **kwargs: None)
    return backend.app.test_client()


def _create_actor(actor_type, role_key=None):
    actor_id = str(uuid.uuid4())
    db = backend.conn()
    try:
        now = backend.now_iso()
        db.execute(
            """INSERT INTO internal_actors(
                   actor_id,actor_type,display_name,status,created_at,updated_at
               ) VALUES(?,?,?,'active',?,?)""",
            (actor_id, actor_type, "Test actor", now, now),
        )
        if role_key:
            db.execute(
                "INSERT INTO internal_actor_roles(actor_id,role_key,assigned_at) VALUES(?,?,?)",
                (actor_id, role_key, now),
            )
        db.commit()
    finally:
        db.close()
    return actor_id


def _legacy_admin_session(client, actor_id=None):
    with client.session_transaction() as current:
        current["admin_authenticated"] = True
        current["csrf_token"] = "test"
        if actor_id:
            current["internal_actor_id"] = actor_id
            current["internal_session_id"] = str(uuid.uuid4())


def _insert_product():
    db = backend.conn()
    try:
        db.execute(
            "INSERT INTO products(id,sku,model,name,created_at) VALUES(1,'RBAC-1','RBAC','Test',?)",
            (backend.now_iso(),),
        )
        db.execute("INSERT INTO stock(product_id,qty) VALUES(1,3)")
        db.commit()
    finally:
        db.close()


def test_bootstrap_owner_is_human_owner_with_inventory_permission(isolated):
    actor = rbac.load_actor_context(rbac.BOOTSTRAP_OWNER_ACTOR_ID)
    assert actor.actor_type == rbac.ACTOR_HUMAN
    assert actor.roles == ("OWNER",)
    assert actor.permission_decision("inventory.read") == rbac.ALLOW


def test_legacy_login_binds_bootstrap_owner(isolated, monkeypatch):
    monkeypatch.setattr(backend, "ADMIN_USERNAME", "legacy-admin")
    monkeypatch.setattr(backend, "ADMIN_PASSWORD", "correct-password")
    monkeypatch.setattr(backend, "ADMIN_PASSWORD_HASH", "")
    response = isolated.post(
        "/login", data={"username": "legacy-admin", "password": "correct-password"}
    )
    assert response.status_code == 302
    with isolated.session_transaction() as current:
        assert current["admin_authenticated"] is True
        assert current["internal_actor_id"] == rbac.BOOTSTRAP_OWNER_ACTOR_ID
        assert current["internal_session_id"]


def test_magazyn_has_inventory_read_and_no_financial_permissions(isolated):
    actor_id = _create_actor(rbac.ACTOR_HUMAN, "MAGAZYN")
    actor = rbac.load_actor_context(actor_id)
    assert actor.permission_decision("inventory.read") == rbac.ALLOW
    assert actor.permission_decision("invoices.read") == rbac.DENY
    assert actor.permission_decision("ksef.send") == rbac.DENY
    assert actor.permission_decision("cashflow.read") == rbac.DENY


def test_accounting_has_no_administrative_permissions(isolated):
    actor_id = _create_actor(rbac.ACTOR_HUMAN, "KSIEGOWOSC")
    actor = rbac.load_actor_context(actor_id)
    assert actor.permission_decision("invoices.read") == rbac.ALLOW
    assert actor.permission_decision("system.users_manage") == rbac.DENY
    assert actor.permission_decision("system.company_manage") == rbac.DENY
    assert actor.permission_decision("system.sync") == rbac.DENY


def test_unknown_actor_gets_no_context_or_permissions(isolated):
    assert rbac.load_actor_context(str(uuid.uuid4())) is None


def test_actor_context_distinguishes_human_system_and_ai_agent(isolated):
    human = rbac.load_actor_context(rbac.BOOTSTRAP_OWNER_ACTOR_ID)
    system = rbac.load_actor_context(next(iter(rbac.SYSTEM_ACTORS)))
    ai_id = _create_actor(rbac.ACTOR_AI_AGENT, "AI_WAREHOUSE")
    ai = rbac.load_actor_context(ai_id)
    assert (human.actor_type, system.actor_type, ai.actor_type) == (
        rbac.ACTOR_HUMAN,
        rbac.ACTOR_SYSTEM,
        rbac.ACTOR_AI_AGENT,
    )
    assert ai.roles == ("AI_WAREHOUSE",)


def test_migrated_endpoint_allows_bootstrap_owner(isolated):
    _insert_product()
    _legacy_admin_session(isolated)
    response = isolated.get("/api/product/1")
    assert response.status_code == 200
    assert response.get_json()["sku"] == "RBAC-1"


def test_migrated_endpoint_denies_actor_without_permission(isolated):
    actor_id = _create_actor(rbac.ACTOR_HUMAN, "KSIEGOWOSC")
    _legacy_admin_session(isolated, actor_id)
    response = isolated.get("/api/product/1")
    assert response.status_code == 403


def test_request_cannot_escalate_role_or_permission(isolated):
    actor_id = _create_actor(rbac.ACTOR_HUMAN, "KSIEGOWOSC")
    _legacy_admin_session(isolated, actor_id)
    response = isolated.get(
        "/api/product/1?role=OWNER&permission=inventory.read",
        headers={"X-Role": "OWNER", "X-Actor-ID": rbac.BOOTSTRAP_OWNER_ACTOR_ID},
    )
    assert response.status_code == 403


def test_unknown_actor_is_denied_by_migrated_endpoint(isolated):
    _legacy_admin_session(isolated, str(uuid.uuid4()))
    response = isolated.get("/api/product/1")
    assert response.status_code == 401


def test_unmigrated_endpoint_keeps_legacy_admin_behavior(isolated):
    actor_id = _create_actor(rbac.ACTOR_HUMAN, "KSIEGOWOSC")
    _legacy_admin_session(isolated, actor_id)
    response = isolated.get("/orders")
    assert response.status_code == 200


def test_actor_cannot_use_role_from_another_actor_category(isolated):
    actor_id = _create_actor(rbac.ACTOR_HUMAN, "AI_WAREHOUSE")
    actor = rbac.load_actor_context(actor_id)
    assert actor.roles == ()
    assert actor.permission_decision("inventory.read") == rbac.DENY


def test_only_selected_internal_endpoint_has_new_rbac_decorator(isolated):
    assert backend.app.view_functions["api_product"].required_permission == "inventory.read"
    assert not hasattr(backend.app.view_functions["orders"], "required_permission")
    assert not hasattr(backend.app.view_functions["api_client_orders_create"], "required_permission")
