"""Durable, bounded Supabase persistence for the Remanent domain.

The application keeps the existing SQLite domain model and transactions.  On
Render, each changed remanent scope is serialized to one private Supabase row
before the SQLite transaction is acknowledged.  A fresh Render process restores
those rows once, on the first remanent access.  This avoids publishing the broad
warehouse database and keeps normal page reads local.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Callable


SCHEMA_VERSION = 1
GLOBAL_SCOPE = "global"

SESSION_TABLES = (
    ("internal_inventory_count_sessions", ("session_id",)),
    ("internal_inventory_count_items", ("remote_key",)),
    ("internal_count_activity", ("remote_key",)),
    ("internal_inventory_voice_clarifications", ("session_id", "conversation_id")),
    ("internal_remanent_snapshots", ("session_id", "product_id")),
    ("remanent_valuation_settings", ("session_id",)),
)

GLOBAL_TABLES = (
    ("internal_remanent_entries", ("id",), "upsert"),
    ("internal_remanent_imports", ("import_id",), "upsert"),
    ("remanent_source_installation", ("id",), "upsert"),
    ("remanent_source_heads", ("installation_id", "import_id"), "upsert"),
    ("remanent_source_bundles", ("bundle_id",), "ignore"),
    ("remanent_source_lines", ("bundle_id", "line_id"), "ignore"),
    ("remanent_source_decisions", ("id",), "ignore"),
    ("remanent_source_commits", ("commit_key",), "upsert"),
)


class RemanentPersistenceError(ValueError):
    """A controlled failure: local transaction must not be presented as saved."""


_lock = threading.RLock()
_enabled: Callable[[], bool] | None = None
_remote_select: Callable[[], list] | None = None
_remote_persist: Callable[[str, dict, str, int], int] | None = None
_active = False
_hydrated = False
_hydrating = False
_revisions: dict[str, int] = {}


def configure(*, enabled, remote_select, remote_persist) -> None:
    global _enabled, _remote_select, _remote_persist
    _enabled = enabled
    _remote_select = remote_select
    _remote_persist = remote_persist


def activate() -> None:
    global _active
    _active = True


def reset_runtime_state() -> None:
    """Forget only the process cache; used by restart tests and recovery."""
    global _hydrated, _hydrating
    with _lock:
        _hydrated = False
        _hydrating = False
        _revisions.clear()


def remote_required() -> bool:
    forced = (os.environ.get("REMANENT_STORAGE_MODE") or "").strip().lower() == "supabase"
    return bool(_active and (forced or os.environ.get("RENDER")) and _enabled and _enabled())


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _checksum(state: dict) -> str:
    return hashlib.sha256(_canonical(state).encode("utf-8")).hexdigest()


def _table_exists(db, table: str) -> bool:
    return bool(db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone())


def _rows(db, table: str, where: str = "", params=()) -> list[dict]:
    if not _table_exists(db, table):
        return []
    sql = f'SELECT * FROM "{table}"'
    if where:
        sql += " WHERE " + where
    return [dict(row) for row in db.execute(sql, params).fetchall()]


def _session_state(db, session_id: str) -> dict:
    tables = {}
    for table, _keys in SESSION_TABLES:
        rows = _rows(db, table, "session_id=?", (session_id,))
        if table in {"internal_inventory_count_items", "internal_count_activity"}:
            local_id = "item_id" if table == "internal_inventory_count_items" else "id"
            rows.sort(key=lambda row: int(row.get(local_id) or 0))
            for row in rows:
                row.pop(local_id, None)
        tables[table] = rows
    return {
        "schema_version": SCHEMA_VERSION,
        "scope_id": "session:" + session_id,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "tables": tables,
    }


def _global_state(db) -> dict:
    tables = {table: _rows(db, table) for table, _keys, _mode in GLOBAL_TABLES}
    return {
        "schema_version": SCHEMA_VERSION,
        "scope_id": GLOBAL_SCOPE,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "tables": tables,
    }


def _state_for_scope(db, scope_id: str) -> dict:
    if scope_id == GLOBAL_SCOPE:
        return _global_state(db)
    if scope_id.startswith("session:") and len(scope_id) > len("session:"):
        return _session_state(db, scope_id.split(":", 1)[1])
    raise RemanentPersistenceError("Nieprawidłowy zakres trwałego zapisu remanentu")


def _persist_scope(db, scope_id: str) -> None:
    if _remote_persist is None:
        raise RemanentPersistenceError("Trwały zapis remanentu w Supabase nie jest skonfigurowany")
    state = _state_for_scope(db, scope_id)
    expected = int(_revisions.get(scope_id, 0))
    try:
        revision = int(_remote_persist(scope_id, state, _checksum(state), expected))
    except Exception as exc:
        # A stale revision or transport error must leave the SQLite transaction
        # uncommitted.  The next request reloads the authoritative remote state.
        global _hydrated
        _hydrated = False
        raise RemanentPersistenceError(
            "Nie potwierdzono trwałego zapisu remanentu w Supabase. "
            "Zmiana nie została zatwierdzona; odśwież stronę i spróbuj ponownie."
        ) from exc
    if revision <= expected:
        _hydrated = False
        raise RemanentPersistenceError("Supabase nie potwierdził nowej wersji remanentu")
    _revisions[scope_id] = revision


def persist_dirty_before_commit(db) -> None:
    """Called by the application's serialized SQLite connection."""
    if not _table_exists(db, "internal_remanent_sync_dirty"):
        return
    scopes = [str(row[0]) for row in db.execute(
        "SELECT scope_id FROM internal_remanent_sync_dirty ORDER BY scope_id"
    ).fetchall()]
    if not scopes:
        return
    if _hydrating:
        db.execute("DELETE FROM internal_remanent_sync_dirty")
        return
    if not remote_required():
        db.execute("DELETE FROM internal_remanent_sync_dirty")
        return
    if not _hydrated:
        raise RemanentPersistenceError(
            "Remanent nie został jeszcze odtworzony z Supabase. Odśwież stronę."
        )
    with _lock:
        for scope_id in scopes:
            _persist_scope(db, scope_id)
        db.execute("DELETE FROM internal_remanent_sync_dirty")


def _upsert(db, table: str, rows: list[dict], keys: tuple[str, ...], mode: str = "upsert") -> None:
    if not rows or not _table_exists(db, table):
        return
    local_columns = {str(row[1]) for row in db.execute(f'PRAGMA table_info("{table}")')}
    for row in rows:
        columns = [name for name in row if name in local_columns]
        if not columns or any(key not in columns for key in keys):
            raise RemanentPersistenceError(f"Niepełny stan tabeli {table} w Supabase")
        quoted = ",".join(f'"{name}"' for name in columns)
        placeholders = ",".join("?" for _ in columns)
        if mode == "ignore":
            where = " AND ".join(f'"{key}"=?' for key in keys)
            existing = db.execute(
                f'SELECT * FROM "{table}" WHERE {where}', tuple(row[key] for key in keys)
            ).fetchone()
            if existing is not None:
                if any(existing[name] != row[name] for name in columns):
                    raise RemanentPersistenceError(
                        f"Lokalny niezmienny rekord {table} różni się od zapisu w Supabase"
                    )
                continue
            sql = f'INSERT OR IGNORE INTO "{table}" ({quoted}) VALUES ({placeholders})'
        else:
            updates = [name for name in columns if name not in keys]
            if updates:
                conflict = ",".join(f'"{name}"' for name in keys)
                assignments = ",".join(f'"{name}"=excluded."{name}"' for name in updates)
                sql = (f'INSERT INTO "{table}" ({quoted}) VALUES ({placeholders}) '
                       f'ON CONFLICT({conflict}) DO UPDATE SET {assignments}')
            else:
                sql = f'INSERT OR IGNORE INTO "{table}" ({quoted}) VALUES ({placeholders})'
        db.execute(sql, tuple(row[name] for name in columns))


def _restore_global(db, state: dict) -> None:
    tables = state.get("tables") or {}
    for table, keys, mode in GLOBAL_TABLES:
        rows = tables.get(table, [])
        if not isinstance(rows, list):
            raise RemanentPersistenceError(f"Nieprawidłowy stan tabeli {table} w Supabase")
        _upsert(db, table, rows, keys, mode)


def _restore_session(db, state: dict) -> None:
    scope_id = str(state.get("scope_id") or "")
    session_id = scope_id.split(":", 1)[1] if scope_id.startswith("session:") else ""
    tables = state.get("tables") or {}
    sessions = tables.get("internal_inventory_count_sessions", [])
    if not session_id or not isinstance(sessions, list) or len(sessions) > 1:
        raise RemanentPersistenceError("Nieprawidłowy zapis sesji remanentu w Supabase")
    if not sessions:
        return
    if str(sessions[0].get("session_id")) != session_id:
        raise RemanentPersistenceError("Identyfikator sesji remanentu nie zgadza się z zakresem")

    # Closed yearly counts have SQLite immutability triggers.  Insert a missing
    # parent temporarily as OPEN, restore its children, then apply final status.
    final_session = dict(sessions[0])
    existing = db.execute(
        "SELECT status FROM internal_inventory_count_sessions WHERE session_id=?", (session_id,)
    ).fetchone()
    if final_session.get("status") == "COMPLETED" and (existing is None or existing[0] != "COMPLETED"):
        temporary = dict(final_session, status="OPEN", completed_at=None)
        _upsert(db, "internal_inventory_count_sessions", [temporary], ("session_id",))
    else:
        _upsert(db, "internal_inventory_count_sessions", [final_session], ("session_id",))

    for table, keys in SESSION_TABLES[1:]:
        rows = tables.get(table, [])
        if not isinstance(rows, list):
            raise RemanentPersistenceError(f"Nieprawidłowy stan tabeli {table} w Supabase")
        if any(str(row.get("session_id")) != session_id for row in rows):
            raise RemanentPersistenceError(f"Obcy rekord w zakresie sesji {session_id}")
        _upsert(db, table, rows, keys)
    _upsert(db, "internal_inventory_count_sessions", [final_session], ("session_id",))

    # A pending approval is local runtime state.  After a restart it may point
    # to no approval row; clear only the dangling pointer and keep the observed
    # discrepancy for a fresh human decision.
    db.execute("""UPDATE internal_inventory_count_sessions
                     SET pending_approval_id=NULL,active_product_id=NULL,voice_state='WAIT_PRODUCT'
                   WHERE session_id=? AND pending_approval_id IS NOT NULL
                     AND NOT EXISTS(SELECT 1 FROM internal_approval_requests
                                    WHERE approval_id=internal_inventory_count_sessions.pending_approval_id)""",
               (session_id,))


def _validate_remote_row(row: dict) -> tuple[str, dict, int]:
    scope_id = str(row.get("scope_id") or "")
    state = row.get("state_json")
    revision = int(row.get("revision") or 0)
    if not isinstance(state, dict) or state.get("schema_version") != SCHEMA_VERSION:
        raise RemanentPersistenceError("Nieobsługiwana wersja trwałego zapisu remanentu")
    if state.get("scope_id") != scope_id or row.get("checksum") != _checksum(state) or revision < 1:
        raise RemanentPersistenceError("Niespójny trwały zapis remanentu w Supabase")
    if scope_id != GLOBAL_SCOPE and not scope_id.startswith("session:"):
        raise RemanentPersistenceError("Nieprawidłowy zakres remanentu w Supabase")
    return scope_id, state, revision


def _local_session_ids(db) -> list[str]:
    if not _table_exists(db, "internal_inventory_count_sessions"):
        return []
    return [str(row[0]) for row in db.execute(
        "SELECT session_id FROM internal_inventory_count_sessions ORDER BY created_at"
    ).fetchall()]


def ensure_ready(db) -> None:
    """Hydrate once per process; fail closed when remote durability is required."""
    global _hydrated, _hydrating
    if not remote_required() or _hydrated:
        return
    if db.in_transaction:
        raise RemanentPersistenceError(
            "Odtworzenie remanentu musi nastąpić przed rozpoczęciem zapisu. Odśwież stronę."
        )
    if _remote_select is None or _remote_persist is None:
        raise RemanentPersistenceError("Trwały zapis remanentu w Supabase nie jest skonfigurowany")
    with _lock:
        if _hydrated:
            return
        try:
            remote_rows = _remote_select() or []
            if not isinstance(remote_rows, list):
                raise RemanentPersistenceError("Supabase zwrócił nieprawidłowy stan remanentu")
            remote = {}
            for row in remote_rows:
                scope_id, state, revision = _validate_remote_row(row)
                remote[scope_id] = state
                _revisions[scope_id] = revision

            # One-time migration path: preserve a local session not yet present
            # remotely before the remote snapshot becomes authoritative.
            local_scopes = ["session:" + sid for sid in _local_session_ids(db)]
            if GLOBAL_SCOPE not in remote:
                state = _global_state(db)
                revision = int(_remote_persist(GLOBAL_SCOPE, state, _checksum(state), 0))
                _revisions[GLOBAL_SCOPE] = revision
                remote[GLOBAL_SCOPE] = state
            for scope_id in local_scopes:
                if scope_id not in remote:
                    state = _state_for_scope(db, scope_id)
                    revision = int(_remote_persist(scope_id, state, _checksum(state), 0))
                    _revisions[scope_id] = revision
                    remote[scope_id] = state

            _hydrating = True
            db.execute("BEGIN IMMEDIATE")
            try:
                if GLOBAL_SCOPE in remote:
                    _restore_global(db, remote[GLOBAL_SCOPE])
                for scope_id in sorted(key for key in remote if key.startswith("session:")):
                    _restore_session(db, remote[scope_id])
                if _table_exists(db, "internal_remanent_sync_dirty"):
                    db.execute("DELETE FROM internal_remanent_sync_dirty")
                # Bypass the application's commit hook while restoring.
                sqlite3.Connection.commit(db)
            except Exception:
                sqlite3.Connection.rollback(db)
                raise
            finally:
                _hydrating = False
            _hydrated = True
        except RemanentPersistenceError:
            _hydrated = False
            raise
        except Exception as exc:
            _hydrated = False
            raise RemanentPersistenceError(
                "Nie można połączyć remanentu z Supabase. Spróbuj ponownie po przywróceniu połączenia."
            ) from exc


def initialize_schema(db) -> None:
    """Install local dirty-scope triggers after all remanent tables exist."""
    for table, id_column in (("internal_inventory_count_items", "item_id"),
                             ("internal_count_activity", "id")):
        if not _table_exists(db, table):
            continue
        columns = {str(row[1]) for row in db.execute(f'PRAGMA table_info("{table}")')}
        if "remote_key" not in columns:
            db.execute(f'ALTER TABLE "{table}" ADD COLUMN remote_key TEXT')
        db.execute(f"""UPDATE "{table}" SET remote_key=lower(hex(randomblob(16)))
                        WHERE remote_key IS NULL OR remote_key=''""")
        db.execute(f'CREATE UNIQUE INDEX IF NOT EXISTS uq_{table}_remote_key ON "{table}"(remote_key)')
        db.execute(f'''CREATE TRIGGER IF NOT EXISTS {table}_assign_remote_key
            AFTER INSERT ON "{table}" WHEN NEW.remote_key IS NULL OR NEW.remote_key=''
            BEGIN
              UPDATE "{table}" SET remote_key=lower(hex(randomblob(16)))
               WHERE "{id_column}"=NEW."{id_column}";
            END''')
    db.execute("""CREATE TABLE IF NOT EXISTS internal_remanent_sync_dirty(
                    scope_id TEXT PRIMARY KEY,
                    marked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )""")
    session_tables = {
        "internal_inventory_count_sessions": "session_id",
        "internal_inventory_count_items": "session_id",
        "internal_count_activity": "session_id",
        "internal_inventory_voice_clarifications": "session_id",
        "internal_remanent_snapshots": "session_id",
        "remanent_valuation_settings": "session_id",
    }
    for table, column in session_tables.items():
        if not _table_exists(db, table):
            continue
        for action, ref in (("insert", "NEW"), ("update", "NEW"), ("delete", "OLD")):
            db.execute(f'''CREATE TRIGGER IF NOT EXISTS remanent_sync_{table}_{action}
                AFTER {action.upper()} ON "{table}"
                BEGIN
                  INSERT INTO internal_remanent_sync_dirty(scope_id)
                  VALUES('session:' || {ref}."{column}")
                  ON CONFLICT(scope_id) DO UPDATE SET marked_at=CURRENT_TIMESTAMP;
                END''')
    for table, _keys, _mode in GLOBAL_TABLES:
        if not _table_exists(db, table):
            continue
        for action in ("insert", "update", "delete"):
            db.execute(f'''CREATE TRIGGER IF NOT EXISTS remanent_sync_{table}_{action}
                AFTER {action.upper()} ON "{table}"
                BEGIN
                  INSERT INTO internal_remanent_sync_dirty(scope_id) VALUES('{GLOBAL_SCOPE}')
                  ON CONFLICT(scope_id) DO UPDATE SET marked_at=CURRENT_TIMESTAMP;
                END''')
