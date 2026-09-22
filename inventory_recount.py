"""Preserve repeated physical observations without changing stock on count."""


def initialize(db):
    sql = db.execute("SELECT sql FROM sqlite_master WHERE name='internal_inventory_count_items'").fetchone()[0]
    if 'SUPERSEDED' not in sql or 'COUNT_ONLY' not in sql:
        new_sql = sql.replace('internal_inventory_count_items', 'inventory_count_items_v2', 1)
        if 'SUPERSEDED' not in sql:
            new_sql = new_sql.replace("'ADJUSTED'", "'ADJUSTED','SUPERSEDED','COUNT_ONLY'")
        else:
            new_sql = new_sql.replace("'SUPERSEDED'", "'SUPERSEDED','COUNT_ONLY'", 1)
        new_sql = new_sql.replace(',\n    UNIQUE(session_id, product_id)', '')
        db.execute('SAVEPOINT inventory_recount_migration')
        try:
            db.execute(new_sql)
            db.execute('INSERT INTO inventory_count_items_v2 SELECT * FROM internal_inventory_count_items')
            db.execute('DROP TABLE internal_inventory_count_items')
            db.execute('ALTER TABLE inventory_count_items_v2 RENAME TO internal_inventory_count_items')
            db.execute('RELEASE inventory_recount_migration')
        except Exception:
            db.execute('ROLLBACK TO inventory_recount_migration')
            db.execute('RELEASE inventory_recount_migration')
            raise
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS inventory_active_count ON internal_inventory_count_items(session_id,product_id) WHERE status<>'SUPERSEDED'")
    db.execute('CREATE INDEX IF NOT EXISTS idx_inventory_count_items_session ON internal_inventory_count_items(session_id,product_id)')


def supersede(db, session_id, product_id):
    changed = db.execute("UPDATE internal_inventory_count_items SET status='SUPERSEDED' WHERE session_id=? AND product_id=? AND status<>'SUPERSEDED'", (session_id, product_id)).rowcount
    if changed:
        # Invalidates approvals based on an older count even when stock did
        # not change between two explicit observations.
        db.execute('INSERT INTO internal_inventory_versions VALUES(?,1) ON CONFLICT(product_id) DO UPDATE SET version=version+1', (product_id,))
