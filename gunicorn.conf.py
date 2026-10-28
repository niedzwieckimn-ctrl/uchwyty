"""One SQLite-owning process; concurrent HTTP requests without duplicating jobs.

Loaded automatically by `gunicorn app:app` when run from this directory.
Explicit command-line options still take precedence.
"""
import os

bind = '0.0.0.0:' + os.environ.get('PORT', '8000')
workers = 1
worker_class = 'gthread'
threads = 4
preload_app = False


def post_worker_init(worker):
    # Start all SQLite-using threads only in the serving child. Forking while
    # an audit delivery opens SQLite can strand SQLite's own native mutex,
    # before the Python transaction lock or SQLite busy timeout can help.
    import app as backend
    import inpost_pickups
    import inpost_tracking
    import ksef_scheduler
    import architect_order_email
    inpost_pickups.start_worker(backend)
    inpost_tracking.start_worker(backend)
    thread = ksef_scheduler.start_worker(backend)
    audit_started = backend.start_audit_outbox_worker() if backend.supabase_enabled() else False
    email_started = architect_order_email.start_worker(backend)
    worker.log.info('BACKGROUND_WORKERS_READY import_pid=%s serving_pid=%s preload=%s audit_started=%s',
                    backend._APP_IMPORT_PID, worker.pid, worker.cfg.preload_app,
                    audit_started)
    worker.log.info('ARCHITECT_EMAIL_WORKER_READY started=%s', email_started)
    worker.log.info('KSEF_WORKER_READY pid=%s alive=%s', worker.pid,
                    bool(thread and thread.is_alive()))
