"""Bounded caches for compilation and pure reads, never rendered user pages."""
from collections import OrderedDict
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
import os
import sqlite3
import threading
import time


@lru_cache(maxsize=96)
def _compiled_template(environment, source):
    return environment.from_string(source)


def render_cached_template_string(source, **context):
    from flask import current_app, render_template
    # Flask still supplies each request's session, context processors and signals.
    # Only executable Jinja templates are shared; HTML and CSRF tokens never are.
    return render_template(_compiled_template(current_app.jinja_env, source), **context)


class SQLiteReadCache:
    """Invalidate on any committed write, including other processes and sync.

    data_version is compared on the SAME, transaction-free observer connection.
    New request connections' data_version values are not comparable. Memory/temp
    databases and unavailable observers deliberately bypass the cache.
    """
    def __init__(self, max_databases=8, max_entries=8, ttl=60):
        self.max_databases = max_databases
        self.max_entries = max_entries
        self.ttl = ttl
        self._lock = threading.RLock()
        self._databases = OrderedDict()
        self._pid = os.getpid()

    def clear(self):
        with self._lock:
            for observer, _entries in self._databases.values():
                observer.close()
            self._databases.clear()
            self._pid = os.getpid()

    def get(self, factory, key, build):
        probe = factory()
        try:
            return self._get_connection(probe, key, lambda: build(probe))
        finally:
            probe.close()

    def _get_connection(self, probe, key, build):
        # A caller may deliberately return one existing read-only connection
        # (Orderchamp) rather than create a second one. Consume it exactly once.
        if probe.in_transaction:
            return build()
        row = next((r for r in probe.execute('PRAGMA database_list') if r[1] == 'main'), None)
        path = row[2] if row else ''
        if not path:
            return build()
        try:
            stat = os.stat(path)
            identity = (str(Path(path).resolve()), stat.st_dev, stat.st_ino)
        except OSError:
            return build()
        # Single-flight: concurrent panels share one calculation per snapshot.
        with self._lock:
            if self._pid != os.getpid():
                self.clear()
            try:
                if identity not in self._databases:
                    observer = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro',
                                               uri=True, check_same_thread=False, timeout=0.1)
                    self._databases[identity] = (observer, OrderedDict())
                    while len(self._databases) > self.max_databases:
                        old, _ = self._databases.popitem(last=False)[1]
                        old.close()
                self._databases.move_to_end(identity)
                observer, entries = self._databases[identity]
                before = observer.execute('PRAGMA data_version').fetchone()[0]
            except sqlite3.Error:
                return build()
            now = time.monotonic()
            entry = entries.get(key)
            if entry and entry[0] == before and now - entry[1] < self.ttl:
                entries.move_to_end(key)
                return deepcopy(entry[2])
            value = build()
            try:
                after = observer.execute('PRAGMA data_version').fetchone()[0]
            except sqlite3.Error:
                return value
            # A write during the calculation makes this result unsuitable for reuse.
            if before == after:
                entries[key] = (after, time.monotonic(), deepcopy(value))
                entries.move_to_end(key)
                while len(entries) > self.max_entries:
                    entries.popitem(last=False)
            return value


class SignedURLCache:
    """Cache only successful URLs, with expiration measured before the HTTP call."""
    def __init__(self, limit=2048):
        self.limit = limit
        self._lock = threading.Lock()
        self._entries = OrderedDict()

    def get_many(self, namespace, paths, expires_in, fetch):
        paths = list(dict.fromkeys(paths))
        with self._lock:
            now = time.monotonic()
            result = {}
            missing = []
            for path in paths:
                key = (namespace, path, expires_in)
                entry = self._entries.get(key)
                if entry and entry[0] > now:
                    result[path] = entry[1]
                    self._entries.move_to_end(key)
                else:
                    self._entries.pop(key, None)
                    missing.append(path)
            if missing:
                signed = fetch(missing)
                safe_until = now + max(0, expires_in - min(60, expires_in * 0.1))
                for path in missing:
                    if signed.get(path):
                        result[path] = signed[path]
                        self._entries[(namespace, path, expires_in)] = (safe_until, signed[path])
                while len(self._entries) > self.limit:
                    self._entries.popitem(last=False)
            return result
