"""Durability checks for the bounded Supabase Remanent bridge."""
import copy
import os

import pytest

import app as backend
import remanent
import remanent_remote
from test_business_operations import isolated, _owner


@pytest.fixture
def remote_store(isolated, monkeypatch):
    store = {}
    calls = {"select": 0, "persist": 0, "fail": False}

    def select():
        calls["select"] += 1
        return [copy.deepcopy(row) for row in store.values()]

    def persist(scope_id, state, checksum, expected_revision):
        calls["persist"] += 1
        if calls["fail"]:
            raise OSError("synthetic network interruption")
        current = store.get(scope_id)
        revision = int(current["revision"]) if current else 0
        if revision != expected_revision:
            raise RuntimeError("revision conflict")
        revision += 1
        store[scope_id] = {
            "scope_id": scope_id,
            "state_json": copy.deepcopy(state),
            "checksum": checksum,
            "schema_version": 1,
            "revision": revision,
            "updated_at": "2026-10-10T12:00:00+00:00",
        }
        return revision

    monkeypatch.setenv("REMANENT_STORAGE_MODE", "supabase")
    monkeypatch.setattr(remanent_remote, "_enabled", lambda: True)
    monkeypatch.setattr(remanent_remote, "_remote_select", select)
    monkeypatch.setattr(remanent_remote, "_remote_persist", persist)
    monkeypatch.setattr(remanent_remote, "_active", True)
    remanent_remote.reset_runtime_state()
    yield store, calls
    remanent_remote.reset_runtime_state()


def _product(db):
    db.execute("""INSERT INTO products(id,sku,model,name,archived,created_at)
                  VALUES(1,'REMOTE-1','Andre','Uchwyt',0,?)""", (backend.now_iso(),))
    db.execute("INSERT INTO stock(product_id,qty) VALUES(1,3)")
    db.commit()


def test_remanent_restores_session_and_global_entries_after_fresh_database(remote_store):
    store, calls = remote_store
    db = backend.conn()
    _product(db)
    owner = _owner()
    sid = remanent.create_draft(db, owner.actor_id, 2026)
    remanent.add_entry(
        db, sid, owner.actor_id, product_id=1, kind="opening",
        period_start="2026-01-01", period_end="2026-01-01", quantity=3,
        unit_value_pln="4.20", source="Stan potwierdzony",
    )
    db.commit()
    db.close()

    assert "global" in store and "session:" + sid in store
    assert calls["select"] == 1

    # A Render restart starts with a fresh ephemeral SQLite file.
    os.remove(backend.DB_PATH)
    remanent_remote.reset_runtime_state()
    backend.init_db()
    db = backend.conn()
    _product(db)
    remanent_remote.ensure_ready(db)
    try:
        assert db.execute(
            "SELECT inventory_year FROM internal_inventory_count_sessions WHERE session_id=?", (sid,)
        ).fetchone()[0] == 2026
        entry = db.execute(
            "SELECT quantity,unit_value_pln,source FROM internal_remanent_entries"
        ).fetchone()
        assert tuple(entry) == (3, "4.20", "Stan potwierdzony")
    finally:
        db.close()
    assert calls["select"] == 2


def test_network_failure_rolls_back_local_remanent_write(remote_store):
    _store, calls = remote_store
    db = backend.conn()
    remanent_remote.ensure_ready(db)
    calls["fail"] = True
    with pytest.raises(remanent_remote.RemanentPersistenceError, match="nie została zatwierdzona"):
        remanent.create_draft(db, _owner().actor_id, 2026)
    assert db.execute(
        "SELECT COUNT(*) FROM internal_inventory_count_sessions WHERE inventory_year IS NOT NULL"
    ).fetchone()[0] == 0
    db.close()
