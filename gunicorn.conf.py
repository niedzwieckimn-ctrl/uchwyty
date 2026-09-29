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
    # The app can be imported before a fork by deployment tooling. Threads do
    # not survive that fork; make sure the serving process owns a live worker.
    import app as backend
    import ksef_scheduler
    thread = ksef_scheduler.start_worker(backend)
    worker.log.info('KSEF_WORKER_READY pid=%s alive=%s', worker.pid,
                    bool(thread and thread.is_alive()))
