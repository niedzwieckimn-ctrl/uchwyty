-- One physical-count register: session/items already exist in warehouse_operations.sql.
-- Additional tables hold source evidence and a frozen accounting snapshot.
CREATE TABLE IF NOT EXISTS internal_remanent_entries(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  inventory_year INTEGER NOT NULL,
  product_id INTEGER NOT NULL REFERENCES products(id),
  kind TEXT NOT NULL CHECK(kind IN ('opening','historical_sales_manual','historical_sales_import',
    'historical_purchase_manual','historical_purchase_import','unit_value_manual','unit_value_import','purchase_coverage')),
  period_start TEXT NOT NULL,
  period_end TEXT NOT NULL,
  quantity INTEGER NOT NULL DEFAULT 0,
  unit_value_pln TEXT,
  source TEXT NOT NULL,
  document_no TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  import_id TEXT,
  import_row INTEGER,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  voided_by TEXT,
  voided_at TEXT,
  void_reason TEXT,
  CHECK(period_start <= period_end),
  UNIQUE(import_id,import_row)
);
CREATE INDEX IF NOT EXISTS idx_remanent_entries_year_product
  ON internal_remanent_entries(inventory_year,product_id,kind,period_start,period_end);
CREATE TABLE IF NOT EXISTS internal_remanent_imports(
  import_id TEXT PRIMARY KEY,
  file_sha256 TEXT NOT NULL,
  kind TEXT NOT NULL,
  period_start TEXT NOT NULL,
  period_end TEXT NOT NULL,
  row_count INTEGER NOT NULL,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(file_sha256,kind,period_start,period_end)
);
CREATE TABLE IF NOT EXISTS internal_remanent_snapshots(
  snapshot_id TEXT NOT NULL UNIQUE,
  session_id TEXT NOT NULL REFERENCES internal_inventory_count_sessions(session_id),
  product_id INTEGER NOT NULL REFERENCES products(id),
  sku TEXT NOT NULL,
  name TEXT NOT NULL,
  model TEXT NOT NULL DEFAULT '',
  variant TEXT NOT NULL DEFAULT '',
  unit TEXT NOT NULL DEFAULT 'szt.',
  system_stock_at_start INTEGER NOT NULL,
  last_inventory_count INTEGER,
  opening_stock INTEGER NOT NULL,
  historical_purchases INTEGER NOT NULL,
  purchases_from_app INTEGER NOT NULL,
  purchases_known INTEGER NOT NULL DEFAULT 0,
  historical_sales_manual INTEGER NOT NULL,
  historical_sales_import INTEGER NOT NULL,
  sales_from_app INTEGER NOT NULL,
  document_stock INTEGER,
  unit_value_pln TEXT,
  source_json TEXT NOT NULL,
  counted_final INTEGER,
  assumed_zero INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(session_id,product_id)
);
CREATE TRIGGER IF NOT EXISTS remanent_closed_snapshot_update
BEFORE UPDATE ON internal_remanent_snapshots
WHEN (SELECT status FROM internal_inventory_count_sessions WHERE session_id=OLD.session_id)='COMPLETED'
 AND (NEW.snapshot_id IS NOT OLD.snapshot_id OR NEW.session_id IS NOT OLD.session_id
  OR NEW.product_id IS NOT OLD.product_id OR NEW.sku IS NOT OLD.sku
  OR NEW.name IS NOT OLD.name OR NEW.model IS NOT OLD.model OR NEW.variant IS NOT OLD.variant
  OR NEW.unit IS NOT OLD.unit OR NEW.system_stock_at_start IS NOT OLD.system_stock_at_start
  OR NEW.last_inventory_count IS NOT OLD.last_inventory_count OR NEW.opening_stock IS NOT OLD.opening_stock
  OR NEW.historical_purchases IS NOT OLD.historical_purchases OR NEW.purchases_from_app IS NOT OLD.purchases_from_app
  OR NEW.purchases_known IS NOT OLD.purchases_known OR NEW.historical_sales_manual IS NOT OLD.historical_sales_manual
  OR NEW.historical_sales_import IS NOT OLD.historical_sales_import OR NEW.sales_from_app IS NOT OLD.sales_from_app
  OR NEW.counted_final IS NOT OLD.counted_final OR NEW.source_json IS NOT OLD.source_json
  OR NEW.unit_value_pln IS NOT OLD.unit_value_pln OR NEW.document_stock IS NOT OLD.document_stock)
BEGIN SELECT RAISE(ABORT,'CLOSED_REMANENT_IMMUTABLE'); END;
CREATE TRIGGER IF NOT EXISTS remanent_closed_assumed_zero_update
BEFORE UPDATE ON internal_remanent_snapshots
WHEN (SELECT status FROM internal_inventory_count_sessions WHERE session_id=OLD.session_id)='COMPLETED'
 AND NEW.assumed_zero IS NOT OLD.assumed_zero
BEGIN SELECT RAISE(ABORT,'CLOSED_REMANENT_IMMUTABLE'); END;
CREATE TRIGGER IF NOT EXISTS remanent_closed_snapshot_insert
BEFORE INSERT ON internal_remanent_snapshots
WHEN (SELECT status FROM internal_inventory_count_sessions WHERE session_id=NEW.session_id)='COMPLETED'
BEGIN SELECT RAISE(ABORT,'CLOSED_REMANENT_IMMUTABLE'); END;
CREATE TRIGGER IF NOT EXISTS remanent_closed_company_update
BEFORE UPDATE ON internal_inventory_count_sessions
WHEN OLD.status='COMPLETED' AND OLD.inventory_year IS NOT NULL
 AND NEW.company_snapshot_json IS NOT OLD.company_snapshot_json
BEGIN SELECT RAISE(ABORT,'CLOSED_REMANENT_IMMUTABLE'); END;
CREATE TRIGGER IF NOT EXISTS remanent_closed_snapshot_delete
BEFORE DELETE ON internal_remanent_snapshots
WHEN (SELECT status FROM internal_inventory_count_sessions WHERE session_id=OLD.session_id)='COMPLETED'
BEGIN SELECT RAISE(ABORT,'CLOSED_REMANENT_IMMUTABLE'); END;
CREATE TRIGGER IF NOT EXISTS remanent_closed_count_update
BEFORE UPDATE ON internal_inventory_count_items
WHEN (SELECT status FROM internal_inventory_count_sessions WHERE session_id=OLD.session_id)='COMPLETED'
 AND (NEW.item_id IS NOT OLD.item_id OR NEW.session_id IS NOT OLD.session_id
  OR NEW.product_id IS NOT OLD.product_id OR NEW.expected_quantity IS NOT OLD.expected_quantity
  OR NEW.counted_quantity IS NOT OLD.counted_quantity OR NEW.status IS NOT OLD.status
  OR NEW.difference IS NOT OLD.difference OR NEW.stock_version IS NOT OLD.stock_version
  OR NEW.note IS NOT OLD.note OR NEW.created_by IS NOT OLD.created_by
  OR NEW.created_at IS NOT OLD.created_at OR NEW.adjusted_execution_id IS NOT OLD.adjusted_execution_id
  OR NEW.adjusted_at IS NOT OLD.adjusted_at)
BEGIN SELECT RAISE(ABORT,'CLOSED_REMANENT_IMMUTABLE'); END;
CREATE TRIGGER IF NOT EXISTS remanent_closed_count_insert
BEFORE INSERT ON internal_inventory_count_items
WHEN (SELECT status FROM internal_inventory_count_sessions WHERE session_id=NEW.session_id)='COMPLETED'
BEGIN SELECT RAISE(ABORT,'CLOSED_REMANENT_IMMUTABLE'); END;
CREATE TRIGGER IF NOT EXISTS remanent_closed_count_delete
BEFORE DELETE ON internal_inventory_count_items
WHEN (SELECT status FROM internal_inventory_count_sessions WHERE session_id=OLD.session_id)='COMPLETED'
BEGIN SELECT RAISE(ABORT,'CLOSED_REMANENT_IMMUTABLE'); END;
CREATE TRIGGER IF NOT EXISTS remanent_closed_session_update
BEFORE UPDATE ON internal_inventory_count_sessions
WHEN OLD.status='COMPLETED' AND OLD.inventory_year IS NOT NULL
 AND (NEW.session_id IS NOT OLD.session_id OR NEW.status IS NOT OLD.status
  OR NEW.created_by IS NOT OLD.created_by OR NEW.conversation_id IS NOT OLD.conversation_id
  OR NEW.created_at IS NOT OLD.created_at
  OR NEW.completed_at IS NOT OLD.completed_at OR NEW.cancelled_at IS NOT OLD.cancelled_at
  OR NEW.inventory_year IS NOT OLD.inventory_year OR NEW.remanent_no IS NOT OLD.remanent_no
  OR NEW.phase IS NOT OLD.phase OR NEW.as_of_date IS NOT OLD.as_of_date
  OR NEW.snapshot_at IS NOT OLD.snapshot_at OR NEW.source_fingerprint IS NOT OLD.source_fingerprint)
BEGIN SELECT RAISE(ABORT,'CLOSED_REMANENT_IMMUTABLE'); END;
CREATE TRIGGER IF NOT EXISTS remanent_closed_session_delete
BEFORE DELETE ON internal_inventory_count_sessions
WHEN OLD.status='COMPLETED' AND OLD.inventory_year IS NOT NULL
BEGIN SELECT RAISE(ABORT,'CLOSED_REMANENT_IMMUTABLE'); END;
