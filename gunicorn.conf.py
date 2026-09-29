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
