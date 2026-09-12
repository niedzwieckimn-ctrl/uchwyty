-- Local SQLite only. Idempotent; initialized with Business Operations.
-- Sidecar versions avoid changing the customer-facing orders schema.
CREATE TABLE IF NOT EXISTS internal_order_versions(
    order_id INTEGER PRIMARY KEY,
    version INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS internal_order_notes(
    note_id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    note TEXT NOT NULL,
    actor_id TEXT NOT NULL REFERENCES internal_actors(actor_id),
    created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS internal_order_version_update AFTER UPDATE ON orders
BEGIN
    INSERT INTO internal_order_versions(order_id,version) VALUES(NEW.id,1)
    ON CONFLICT(order_id) DO UPDATE SET version=version+1;
END;
CREATE TRIGGER IF NOT EXISTS internal_order_version_delete AFTER DELETE ON orders
BEGIN
    INSERT INTO internal_order_versions(order_id,version) VALUES(OLD.id,1)
    ON CONFLICT(order_id) DO UPDATE SET version=version+1;
END;
CREATE TRIGGER IF NOT EXISTS internal_order_note_version AFTER INSERT ON internal_order_notes
BEGIN
    INSERT INTO internal_order_versions(order_id,version) VALUES(NEW.order_id,1)
    ON CONFLICT(order_id) DO UPDATE SET version=version+1;
END;

CREATE TABLE IF NOT EXISTS internal_order_write_actors(
    execution_id TEXT PRIMARY KEY,
    human_id TEXT NOT NULL REFERENCES internal_actors(actor_id)
);
