-- Local SQLite only. Additive and repeatable. Startup runs this same migration.
CREATE TABLE IF NOT EXISTS inpost_reconciliation (
 shipment_id TEXT PRIMARY KEY,
 root_order_id INTEGER NOT NULL DEFAULT 0,
 revision INTEGER NOT NULL DEFAULT 0,
 observation TEXT NOT NULL DEFAULT '{}',
 apply_state TEXT NOT NULL DEFAULT 'none',
 applied_revision INTEGER NOT NULL DEFAULT 0,
 applied_at TEXT NOT NULL DEFAULT '',
 retry_at REAL NOT NULL DEFAULT 0,
 sync_revision INTEGER NOT NULL DEFAULT 0,
 sync_payload TEXT NOT NULL DEFAULT '{}',
 sync_token TEXT,
 sync_until REAL NOT NULL DEFAULT 0,
 notification_payload TEXT NOT NULL DEFAULT '{}',
 receipt_error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_inpost_reconciliation_apply ON inpost_reconciliation(apply_state,retry_at);
