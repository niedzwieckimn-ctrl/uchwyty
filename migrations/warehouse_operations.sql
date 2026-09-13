-- Local, internal warehouse-operation state. Idempotent SQLite migration.
CREATE TABLE IF NOT EXISTS internal_inventory_versions(
    product_id INTEGER PRIMARY KEY,
    version INTEGER NOT NULL DEFAULT 0
);
CREATE TRIGGER IF NOT EXISTS internal_inventory_version_update AFTER UPDATE ON stock
BEGIN
    INSERT INTO internal_inventory_versions(product_id,version) VALUES(NEW.product_id,1)
    ON CONFLICT(product_id) DO UPDATE SET version=version+1;
END;
CREATE TRIGGER IF NOT EXISTS internal_inventory_version_delete AFTER DELETE ON stock
BEGIN
    INSERT INTO internal_inventory_versions(product_id,version) VALUES(OLD.product_id,1)
    ON CONFLICT(product_id) DO UPDATE SET version=version+1;
END;

CREATE TABLE IF NOT EXISTS internal_inventory_count_sessions(
    session_id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'OPEN' CHECK(status IN ('OPEN','COMPLETED','CANCELLED')),
    created_by TEXT NOT NULL REFERENCES internal_actors(actor_id),
    created_at TEXT NOT NULL,
    completed_at TEXT,
    cancelled_at TEXT
);
CREATE TABLE IF NOT EXISTS internal_inventory_count_items(
    item_id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES internal_inventory_count_sessions(session_id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    expected_quantity INTEGER NOT NULL CHECK(expected_quantity >= 0),
    counted_quantity INTEGER NOT NULL CHECK(counted_quantity >= 0),
    difference INTEGER NOT NULL,
    stock_version INTEGER NOT NULL CHECK(stock_version >= 0),
    status TEXT NOT NULL CHECK(status IN ('MATCHED','PENDING_ADJUSTMENT','ADJUSTED')),
    note TEXT,
    created_by TEXT NOT NULL REFERENCES internal_actors(actor_id),
    created_at TEXT NOT NULL,
    adjusted_execution_id TEXT,
    adjusted_at TEXT,
    UNIQUE(session_id, product_id)
);
CREATE INDEX IF NOT EXISTS idx_inventory_count_items_session
    ON internal_inventory_count_items(session_id, product_id);

CREATE TABLE IF NOT EXISTS internal_packing_shortages(
    shortage_id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    missing_quantity INTEGER NOT NULL CHECK(missing_quantity > 0),
    note TEXT,
    reported_by TEXT NOT NULL REFERENCES internal_actors(actor_id),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_packing_shortages_order
    ON internal_packing_shortages(order_id, created_at);

CREATE TABLE IF NOT EXISTS internal_business_write_actors(
    execution_id TEXT PRIMARY KEY,
    human_id TEXT NOT NULL REFERENCES internal_actors(actor_id)
);
