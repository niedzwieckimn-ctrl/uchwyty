"""Preserve repeated physical observations without changing stock on count."""

from datetime import datetime, timezone
import json


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
    import inventory_voice_context
    inventory_voice_context.initialize(db)


def cancel_adjustments(db, session_id, product_id):
    """Invalidate only this observation's unused authorizations in the caller's tx.

    No commit here: the new observation/COUNT_ONLY choice, cancellations and audit
    either all commit or all roll back. Completed executions remain immutable.
    """
    import internal_audit
    from internal_rbac import load_actor_context
    from business_operations import ControlledOperationError
    rows = db.execute("""SELECT * FROM internal_approval_requests
        WHERE operation='inventory.adjust' AND entity_id=? AND status IN ('PENDING','APPROVED')""",
        (str(product_id),)).fetchall()
    selected = []
    for row in rows:
        payload = json.loads(row['safe_payload'] or '{}')
        if (payload.get('count_session_id') == session_id
                and str(payload.get('product_id')) == str(product_id)):
            executions = db.execute('SELECT * FROM internal_operation_executions WHERE approval_id=?',
                                    (row['approval_id'],)).fetchall()
            if any(execution['status'] == 'RUNNING' for execution in executions):
                raise ControlledOperationError('ADJUSTMENT_IN_PROGRESS',
                    'Trwa wykonanie poprzedniej korekty. Sprawdź jej wynik przed ponownym liczeniem.', status='CONFLICT')
            selected.append((row, executions))
    if not selected:
        return []
    owner = db.execute('SELECT created_by FROM internal_inventory_count_sessions WHERE session_id=?',
                       (session_id,)).fetchone()
    actor = load_actor_context(owner['created_by']) if owner else None
    if actor is None:
        raise ControlledOperationError('COUNT_SESSION_ACCESS_DENIED', 'Brak właściciela liczenia.', status='DENIED')
    now = datetime.now(timezone.utc).isoformat()
    reason = 'Wynik liczenia zastąpiono lub pozostawiono bez korekty magazynu.'
    cancelled = []
    for row, executions in selected:
        changed = db.execute("""UPDATE internal_approval_requests SET status='CANCELLED',resolved_at=?,
            resolution_reason=?,updated_at=? WHERE approval_id=? AND status IN ('PENDING','APPROVED')""",
            (now, reason, now, row['approval_id'])).rowcount
        if not changed:
            continue
        for execution in executions:
            if execution['status'] not in {'CREATED','PENDING_APPROVAL','AUTHORIZED'}:
                continue
            db.execute("""UPDATE internal_operation_executions SET status='CONFLICT',
                error_code='COUNT_OBSERVATION_SUPERSEDED',safe_error_message=?,completed_at=?,updated_at=?
                WHERE execution_id=? AND status IN ('CREATED','PENDING_APPROVAL','AUTHORIZED')""",
                (reason,now,now,execution['execution_id']))
            internal_audit.record_audit_event('business_operation.conflict',result='CONFLICT',actor_context=actor,
                permission=row['permission'],entity_type=row['entity_type'],entity_id=row['entity_id'],
                approval_id=row['approval_id'],correlation_id=execution['correlation_id'],
                error_code='COUNT_OBSERVATION_SUPERSEDED',reason=reason,
                before_state={'execution_id':execution['execution_id'],'status':execution['status']},
                after_state={'execution_id':execution['execution_id'],'status':'CONFLICT'},transaction_connection=db)
        internal_audit.record_audit_event('approval.cancelled',result='SUCCESS',actor_context=actor,
            permission=row['permission'],entity_type=row['entity_type'],entity_id=row['entity_id'],
            approval_id=row['approval_id'],correlation_id=row['correlation_id'],reason=reason,
            before_state={'status':row['status']},after_state={'status':'CANCELLED'},transaction_connection=db)
        db.execute("""UPDATE internal_inventory_count_sessions SET pending_approval_id=NULL,
            voice_state='WAIT_PRODUCT',active_product_id=NULL WHERE session_id=? AND pending_approval_id=?""",
            (session_id,row['approval_id']))
        cancelled.append(row['approval_id'])
    return cancelled


def supersede(db, session_id, product_id):
    cancel_adjustments(db, session_id, product_id)
    changed = db.execute("UPDATE internal_inventory_count_items SET status='SUPERSEDED' WHERE session_id=? AND product_id=? AND status<>'SUPERSEDED'", (session_id, product_id)).rowcount
    if changed:
        # Invalidates approvals based on an older count even when stock did
        # not change between two explicit observations.
        db.execute('INSERT INTO internal_inventory_versions VALUES(?,1) ON CONFLICT(product_id) DO UPDATE SET version=version+1', (product_id,))
