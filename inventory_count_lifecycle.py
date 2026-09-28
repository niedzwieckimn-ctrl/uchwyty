"""Shared count persistence and measured work intervals for UI and agent."""
from datetime import datetime, timezone
from pathlib import Path
import os


def now():
    return datetime.now(timezone.utc).isoformat()


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS internal_count_activity(
        id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
        event TEXT NOT NULL CHECK(event IN ('start','resume','pause','complete')),
        happened_at TEXT NOT NULL, actor_id TEXT NOT NULL)''')
    db.execute('CREATE INDEX IF NOT EXISTS count_activity_session ON internal_count_activity(session_id,id)')


def assert_storage(db):
    """SQLite on Render requires an actual mounted volume and a single instance.

    A deployment flag is not evidence of a mounted volume. Database location
    must resolve inside that volume; ordinary local installations use local DB.
    """
    if not os.environ.get('RENDER'):
        return
    root = Path(os.environ.get('REMANENT_DURABLE_ROOT') or '/var/data').resolve()
    files = [Path(row[2]).resolve() for row in db.execute('PRAGMA database_list') if row[1] == 'main' and row[2]]
    if (not os.path.ismount(root) or not files or not files[0].is_relative_to(root)
            or os.environ.get('REMANENT_STORAGE_MODE') != 'sqlite_single_instance'
            or os.environ.get('WEB_CONCURRENCY', '1') != '1'):
        raise ValueError('COUNT_STORAGE_NOT_DURABLE: wymagany SQLite na zamontowanym trwałym dysku, '
                         'REMANENT_DURABLE_ROOT i REMANENT_STORAGE_MODE=sqlite_single_instance; jedna instancja i WEB_CONCURRENCY=1.')


def event(db, session_id, actor_id, action):
    assert_storage(db)
    previous = db.execute('SELECT event FROM internal_count_activity WHERE session_id=? ORDER BY id DESC LIMIT 1', (session_id,)).fetchone()
    if previous and ((action in {'start','resume'} and previous[0] in {'start','resume'}) or previous[0] == action):
        return timing(db, session_id)
    db.execute('INSERT INTO internal_count_activity(session_id,event,happened_at,actor_id) VALUES(?,?,?,?)',
               (session_id, action, now(), actor_id))
    return timing(db, session_id)


def timing(db, session_id):
    rows = db.execute('SELECT event,happened_at FROM internal_count_activity WHERE session_id=? ORDER BY id', (session_id,)).fetchall()
    if not rows:
        return {'active_seconds': None, 'calendar_seconds': None, 'paused': False, 'timing_complete': False,
                'measured_from': None}
    current = datetime.now(timezone.utc)
    start = None
    active = 0.0
    for row in rows:
        stamp = datetime.fromisoformat(row['happened_at'])
        if row['event'] in {'start','resume'}:
            start = stamp
        elif start is not None:
            active += max(0, (stamp-start).total_seconds())
            start = None
    if start is not None:
        active += max(0, (current-start).total_seconds())
    last = datetime.fromisoformat(rows[-1]['happened_at']) if rows[-1]['event'] == 'complete' else current
    return {'active_seconds': int(active), 'calendar_seconds': max(0,int((last-datetime.fromisoformat(rows[0]['happened_at'])).total_seconds())),
            'paused': rows[-1]['event'] == 'pause', 'timing_complete': rows[0]['event'] == 'start',
            'measured_from': rows[0]['happened_at']}


def unresolved(db, session_id):
    pending = db.execute("SELECT COUNT(*) FROM internal_inventory_count_items WHERE session_id=? AND status='PENDING_ADJUSTMENT'", (session_id,)).fetchone()[0]
    # Binding is in safe_payload, so unrelated sessions never block this one.
    import json
    approvals = sum(json.loads(row['safe_payload'] or '{}').get('count_session_id') == session_id for row in db.execute(
        "SELECT safe_payload FROM internal_approval_requests WHERE operation='inventory.adjust' AND status IN ('PENDING','APPROVED')"))
    return int(pending), int(approvals)
