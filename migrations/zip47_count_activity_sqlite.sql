-- SQLite schema snapshot extracted from the ZIP47 Python initializers.
-- Source of truth and automatic upgrade: app.init_db() / module.initialize().
-- Additive CREATE statements only; no business rows, UUID seeds or destructive rollback.
-- See docs/ZIP47_MIGRATIONS.md for existing-column upgrades and order.
-- Source inventory_count_lifecycle.py SHA256 8d8ae1937a8fa6fbe7e2563effae7d386c9a6905ed641309638584e44a1c8057
-- Source inventory_voice_context.py SHA256 36371a4f62a0c786d7476c9d25ce81bf27937a19d54d079f87dfd58d76c6a3c6

CREATE TABLE IF NOT EXISTS internal_count_activity(
        id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
        event TEXT NOT NULL CHECK(event IN ('start','resume','pause','complete')),
        happened_at TEXT NOT NULL, actor_id TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS internal_inventory_voice_clarifications(
        session_id TEXT NOT NULL,
        conversation_id TEXT NOT NULL,
        context_json TEXT NOT NULL,
        PRIMARY KEY(session_id,conversation_id)
    );

CREATE INDEX IF NOT EXISTS count_activity_session ON internal_count_activity(session_id,id);
