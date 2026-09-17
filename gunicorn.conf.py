"""Keep the worker heartbeat independent of long-lived AI SSE responses.

Render loads this file with its existing `gunicorn app:app` start command.
Do not increase the worker timeout or multiply application/background-job workers.
"""
worker_class = 'gthread'
workers = 1
threads = 2


def post_worker_init(worker):
    import logging
    for name in ('agent_runtime', 'agent_streaming', 'agent_conversation'):
        log = logging.getLogger(name)
        log.setLevel(logging.INFO)
        log.handlers = list(worker.log.error_log.handlers)
        log.propagate = False
