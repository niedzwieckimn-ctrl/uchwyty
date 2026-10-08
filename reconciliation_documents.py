"""Content-addressed PDFs in private Storage; legacy inline evidence remains readable.

Uploads and downloads happen before any SQLite write transaction. A failed
upload/readback never removes the queued evidence or advances its revision.
"""
import base64
import copy
import hashlib
import os
from pathlib import Path
import re
import threading

SECTIONS = ('fulfillment_documents', 'fulfillment_document_history')
_verified = set()
_lock = threading.Lock()


def enabled(b):
    return bool(getattr(b, 'SUPABASE_URL', '') and
                os.environ.get('RECONCILIATION_DOCUMENT_STORAGE', '1') == '1')


def object_path(digest):
    if not re.fullmatch('[0-9a-f]{64}', str(digest)):
        raise ValueError('Nieprawidłowa suma kontrolna PDF.')
    return 'fulfillment/pdf/' + digest + '.pdf'


def checked(content, digest):
    if not content or len(content) > 10_000_000 or hashlib.sha256(content).hexdigest() != digest:
        raise ValueError('Suma kontrolna trwałego dokumentu nie zgadza się.')
    return content


def cache_path(b, digest):
    object_path(digest)
    return Path(b.DATA_DIR) / 'fulfillment-cache' / (digest + '.pdf')


def content(b, doc):
    digest = doc['file_hash']
    if doc.get('pdf_base64'):
        return checked(base64.b64decode(doc['pdf_base64'], validate=True), digest)
    ref = doc.get('storage_ref')
    if not ref or ref != b.supabase_storage_ref(object_path(digest)):
        raise ValueError('Nieprawidłowe odwołanie do trwałego dokumentu.')
    path = cache_path(b, digest)
    if path.is_file():
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() == digest:
            return data
    data = checked(b.supabase_storage_download_bytes(ref)[0], digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.' + os.urandom(8).hex() + '.tmp')
    temporary.write_bytes(data)
    temporary.replace(path)
    return data


def hydrate(b, payload):
    """Adapt either wire format to the unchanged reconciliation rules."""
    result = copy.deepcopy(payload)
    files = {}
    for section in SECTIONS:
        for doc in result.get(section, []):
            if 'storage_ref' not in doc:
                continue
            digest = doc['file_hash']
            # Validate every reference, including duplicate document records.
            if doc['storage_ref'] != b.supabase_storage_ref(object_path(digest)):
                raise ValueError('Nieprawidłowe odwołanie do trwałego dokumentu.')
            if digest not in files:
                files[digest] = base64.b64encode(content(b, doc)).decode('ascii')
            doc['pdf_base64'] = files[digest]
            doc.pop('storage_ref')
    return result


def externalize(b, payload):
    if not enabled(b):
        return payload
    result = copy.deepcopy(payload)
    for section in SECTIONS:
        for doc in result.get(section, []):
            digest = doc['file_hash']
            ref = b.supabase_storage_ref(object_path(digest))
            data = content(b, doc)
            key = (b.SUPABASE_URL, ref)
            with _lock:
                if key not in _verified:
                    path = cache_path(b, digest)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
                    uploaded = b.supabase_storage_upload_file(str(path), object_path(digest))
                    if uploaded != ref:
                        raise ValueError('Nie potwierdzono lokalizacji trwałego PDF.')
                    checked(b.supabase_storage_download_bytes(ref)[0], digest)
                    _verified.add(key)
            doc.pop('pdf_base64', None)
            doc['storage_ref'] = ref
    return result
