"""Opt-in local STT audio diagnostics. Never serve this directory over HTTP."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import logging
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import uuid


DEBUG_AUDIO_DIR = Path(tempfile.gettempdir()) / 'niedzwieccy-voice-debug'
MAX_DEBUG_AUDIO_FILES = 3
DEBUG_AUDIO_TTL_SECONDS = 3600
_CLEANUP_INTERVAL_SECONDS = 60
_AUDIO_NAME = re.compile(r'\d{8}T\d{12}Z_[0-9a-f]{32}\.(webm|mp4|ogg|mp3|wav)')
_lock = threading.Lock()
_janitor_lock = threading.Lock()
_janitor = None


def audio_extension(mime_type):
    return {'audio/webm': '.webm', 'audio/mp4': '.mp4', 'audio/ogg': '.ogg',
            'audio/mpeg': '.mp3', 'audio/wav': '.wav', 'audio/x-wav': '.wav'}.get(
                mime_type.split(';', 1)[0].strip().lower(), '')


def debug_audio_enabled():
    return os.environ.get('VOICE_DEBUG_SAVE_AUDIO', '0') == '1'


@contextmanager
def _storage_lock():
    """Serialize rotation across threads and Gunicorn workers; no SQLite locks."""
    with _lock:
        root = Path(DEBUG_AUDIO_DIR)
        if root.is_symlink():
            raise OSError('Unsafe debug directory')
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name != 'nt':
            root.chmod(0o700)
        lock_path = root / '.lock'
        if lock_path.is_symlink():
            raise OSError('Unsafe debug lock')
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(fd, 'r+b') as lockfile:
            if os.name == 'nt':
                import msvcrt
                if os.fstat(lockfile.fileno()).st_size == 0:
                    lockfile.write(b'0'); lockfile.flush()
                lockfile.seek(0)
                msvcrt.locking(lockfile.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lockfile.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield root
            finally:
                if os.name == 'nt':
                    lockfile.seek(0)
                    msvcrt.locking(lockfile.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lockfile.fileno(), fcntl.LOCK_UN)


def _prune(root, *, keep=MAX_DEBUG_AUDIO_FILES, purge=False):
    now = time.time()
    remaining = []
    for path in root.iterdir():
        if not _AUDIO_NAME.fullmatch(path.name):
            continue
        if path.is_symlink() or purge or now - path.stat().st_mtime >= DEBUG_AUDIO_TTL_SECONDS:
            path.unlink(missing_ok=True)
        else:
            remaining.append(path)
    remaining.sort(key=lambda path: path.name, reverse=True)
    for path in remaining[keep:]:
        path.unlink(missing_ok=True)
    return min(len(remaining), keep)


def cleanup_debug_audio(*, purge=False):
    if not Path(DEBUG_AUDIO_DIR).exists():
        return 0
    with _storage_lock() as root:
        return _prune(root, purge=purge or not debug_audio_enabled())


def _expire_audio():
    global _janitor
    while not threading.Event().wait(_CLEANUP_INTERVAL_SECONDS):
        try:
            with _janitor_lock:
                if cleanup_debug_audio() == 0:
                    _janitor = None
                    return
        except OSError as exc:
            logging.getLogger(__name__).warning('VOICE_DEBUG_CLEANUP_ERROR exception_type=%s', type(exc).__name__)


def _start_janitor():
    global _janitor
    with _janitor_lock:
        if _janitor is None or not _janitor.is_alive():
            _janitor = threading.Thread(target=_expire_audio, name='voice-debug-cleanup', daemon=True)
            _janitor.start()


def save_debug_audio(audio, mime_type, *, is_admin):
    """Return a random file ID only after a successful opt-in administrator save."""
    if not is_admin:
        return None
    if not debug_audio_enabled():
        cleanup_debug_audio(purge=True)
        return None
    extension = audio_extension(mime_type)
    if not extension or not audio:
        return None
    with _storage_lock() as root:
        _prune(root, keep=MAX_DEBUG_AUDIO_FILES - 1)
        stamp_format = '%Y%m%dT%H%M%S%fZ'
        stamp = datetime.now(timezone.utc).strftime(stamp_format)
        newest = max((p.name[:22] for p in root.iterdir() if _AUDIO_NAME.fullmatch(p.name)), default='')
        if stamp <= newest:
            stamp = (datetime.strptime(newest, stamp_format) + timedelta(microseconds=1)).strftime(stamp_format)
        file_id = stamp + '_' + uuid.uuid4().hex + extension
        path = root / file_id
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(audio)
        except OSError:
            path.unlink(missing_ok=True)
            raise
    _start_janitor()
    return file_id


if __name__ == '__main__':
    # Local administrator cleanup after diagnosis; no network or application bootstrap.
    cleanup_debug_audio(purge=True)
