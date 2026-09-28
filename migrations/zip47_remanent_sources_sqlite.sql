-- SQLite schema snapshot extracted from the ZIP47 Python initializers.
-- Source of truth and automatic upgrade: app.init_db() / module.initialize().
-- Additive CREATE statements only; no business rows, UUID seeds or destructive rollback.
-- See docs/ZIP47_MIGRATIONS.md for existing-column upgrades and order.
-- Source remanent_sources.py SHA256 b203f58c8ff83004c661529511058f8c69d90628245b027252719c1dbd9bf2b6

CREATE TABLE IF NOT EXISTS remanent_source_bundles(bundle_id TEXT PRIMARY KEY,installation_id TEXT NOT NULL,import_id TEXT NOT NULL,
        version INTEGER NOT NULL,basis TEXT NOT NULL,payload_hash TEXT NOT NULL,created_at TEXT NOT NULL,
        UNIQUE(installation_id,import_id,version,basis));

CREATE TABLE IF NOT EXISTS remanent_source_commits(commit_key TEXT PRIMARY KEY,session_id TEXT NOT NULL,
        payload_hash TEXT NOT NULL,decisions_json TEXT NOT NULL,created_by TEXT NOT NULL,created_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS remanent_source_decisions(id INTEGER PRIMARY KEY AUTOINCREMENT,installation_id TEXT NOT NULL,
        import_id TEXT NOT NULL,version INTEGER NOT NULL,line_id TEXT NOT NULL,decision_json TEXT NOT NULL,
        created_by TEXT NOT NULL,created_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS remanent_source_heads(installation_id TEXT NOT NULL,import_id TEXT NOT NULL,version INTEGER NOT NULL,
        PRIMARY KEY(installation_id,import_id));

CREATE TABLE IF NOT EXISTS remanent_source_installation(id INTEGER PRIMARY KEY CHECK(id=1),installation_id TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS remanent_source_lines(bundle_id TEXT NOT NULL,line_id TEXT NOT NULL,product_id INTEGER NOT NULL,
        payload_json TEXT NOT NULL,decision_json TEXT NOT NULL,created_by TEXT NOT NULL,
        PRIMARY KEY(bundle_id,line_id));

CREATE TABLE IF NOT EXISTS remanent_valuation_settings(session_id TEXT PRIMARY KEY,method TEXT NOT NULL,basis TEXT NOT NULL,
        confirmed_by TEXT NOT NULL,confirmed_at TEXT NOT NULL, manual_basis_confirmed INTEGER NOT NULL DEFAULT 0);

CREATE INDEX IF NOT EXISTS remanent_source_decision_lookup ON remanent_source_decisions(installation_id,import_id,version,line_id,id);

CREATE TRIGGER IF NOT EXISTS remanent_source_bundles_immutable_delete BEFORE DELETE ON remanent_source_bundles
        BEGIN SELECT RAISE(ABORT,'SOURCE_VERSION_IMMUTABLE'); END;

CREATE TRIGGER IF NOT EXISTS remanent_source_bundles_immutable_update BEFORE UPDATE ON remanent_source_bundles
        BEGIN SELECT RAISE(ABORT,'SOURCE_VERSION_IMMUTABLE'); END;

CREATE TRIGGER IF NOT EXISTS remanent_source_decisions_no_delete BEFORE DELETE ON remanent_source_decisions
        BEGIN SELECT RAISE(ABORT,'RECONCILIATION_HISTORY_IMMUTABLE'); END;

CREATE TRIGGER IF NOT EXISTS remanent_source_decisions_no_update BEFORE UPDATE ON remanent_source_decisions
        BEGIN SELECT RAISE(ABORT,'RECONCILIATION_HISTORY_IMMUTABLE'); END;

CREATE TRIGGER IF NOT EXISTS remanent_source_lines_immutable_delete BEFORE DELETE ON remanent_source_lines
        BEGIN SELECT RAISE(ABORT,'SOURCE_VERSION_IMMUTABLE'); END;

CREATE TRIGGER IF NOT EXISTS remanent_source_lines_immutable_update BEFORE UPDATE ON remanent_source_lines
        BEGIN SELECT RAISE(ABORT,'SOURCE_VERSION_IMMUTABLE'); END;

CREATE TRIGGER IF NOT EXISTS remanent_valuation_settings_no_started_delete BEFORE DELETE ON remanent_valuation_settings
    WHEN NOT EXISTS(SELECT 1 FROM internal_inventory_count_sessions WHERE session_id=OLD.session_id AND phase='DRAFT' AND status='OPEN')
    BEGIN SELECT RAISE(ABORT,'VALUATION_SETTINGS_FROZEN'); END;

CREATE TRIGGER IF NOT EXISTS remanent_valuation_settings_no_started_insert BEFORE INSERT ON remanent_valuation_settings
    WHEN NOT EXISTS(SELECT 1 FROM internal_inventory_count_sessions WHERE session_id=NEW.session_id AND phase='DRAFT' AND status='OPEN')
    BEGIN SELECT RAISE(ABORT,'VALUATION_SETTINGS_FROZEN'); END;

CREATE TRIGGER IF NOT EXISTS remanent_valuation_settings_no_started_update BEFORE UPDATE ON remanent_valuation_settings
    WHEN NOT EXISTS(SELECT 1 FROM internal_inventory_count_sessions WHERE session_id=OLD.session_id AND phase='DRAFT' AND status='OPEN')
      OR NOT EXISTS(SELECT 1 FROM internal_inventory_count_sessions WHERE session_id=NEW.session_id AND phase='DRAFT' AND status='OPEN')
    BEGIN SELECT RAISE(ABORT,'VALUATION_SETTINGS_FROZEN'); END;
