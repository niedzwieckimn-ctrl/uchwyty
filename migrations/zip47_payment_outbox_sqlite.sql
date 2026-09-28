-- SQLite schema snapshot extracted from the ZIP47 Python initializers.
-- Source of truth and automatic upgrade: app.init_db() / module.initialize().
-- Additive CREATE statements only; no business rows, UUID seeds or destructive rollback.
-- See docs/ZIP47_MIGRATIONS.md for existing-column upgrades and order.
-- Source invoice_payment_sync.py SHA256 00fa990b2aff69c28b384e88948b4c7566cc19eebdba702896530a309ec17b7a

CREATE TABLE IF NOT EXISTS invoice_payment_sync_links(
        invoice_id INTEGER NOT NULL, table_name TEXT NOT NULL, record_id INTEGER NOT NULL,
        PRIMARY KEY(invoice_id,table_name,record_id));

CREATE TABLE IF NOT EXISTS invoice_payment_sync_outbox(
        table_name TEXT NOT NULL, record_id INTEGER NOT NULL, revision INTEGER NOT NULL,
        payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'PENDING',
        error_code TEXT NOT NULL DEFAULT '', error_message TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL, synced_at TEXT,
        lease_token TEXT, lease_until TEXT, PRIMARY KEY(table_name,record_id));
