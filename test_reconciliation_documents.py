import base64
import copy
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
import reconciliation_documents as docs
import reconciliation_migration as migration
import reconciliation_store as store
from test_packing_correction import ready, pack
from test_packing_ui_reconciliation import packing_cloud
from test_multi_order_packing_agent import multi_order_flow


@pytest.fixture
def storage(tmp_path, monkeypatch):
    monkeypatch.setenv('RECONCILIATION_DOCUMENT_STORAGE', '1')
    docs._verified.clear()
    objects, uploads, downloads = {}, [], []
    def ref(path): return 'supabase://private/' + path
    def upload(path, key, **kwargs):
        objects[ref(key)] = Path(path).read_bytes(); uploads.append(ref(key)); return ref(key)
    def download(key):
        downloads.append(key); return objects[key], key.split('/')[-1]
    b = SimpleNamespace(DATA_DIR=str(tmp_path), DB_PATH=str(tmp_path/'test.db'), SUPABASE_URL='https://test.invalid',
        supabase_storage_ref=ref, supabase_storage_upload_file=upload, supabase_storage_download_bytes=download)
    data = b'%PDF-1.4\nimmutable original'
    document = dict(order_id=1, file_hash=hashlib.sha256(data).hexdigest(), pdf_base64=base64.b64encode(data).decode())
    payload = {'fulfillment_documents': [document], 'fulfillment_document_history': [dict(document),dict(document,order_id=2)]}
    return b, payload, objects, uploads, downloads


def test_upload_once_and_restore_after_cold_cache(storage):
    b, payload, objects, uploads, downloads = storage
    wire = docs.externalize(b, payload)
    assert len(uploads) == 1 and len(downloads) == 1
    assert all('pdf_base64' not in d for s in docs.SECTIONS for d in wire[s])
    assert docs.hydrate(b, wire) == payload
    docs.cache_path(b, payload['fulfillment_documents'][0]['file_hash']).unlink()
    assert docs.hydrate(b, wire) == payload
    assert len(downloads) == 2  # Shared file downloaded once for all copies.
    assert docs.externalize(b, payload) == wire and len(uploads) == 1


def test_corrupted_upload_never_returns_a_reference(storage):
    b, payload, objects, uploads, downloads = storage
    b.supabase_storage_download_bytes = lambda ref: (b'corrupt', 'bad.pdf')
    with pytest.raises(ValueError): docs.externalize(b, payload)
    assert not docs._verified
    assert 'pdf_base64' in payload['fulfillment_documents'][0]


def test_bad_legacy_bytes_and_foreign_refs_are_rejected(storage):
    b, payload, *_ = storage
    bad = copy.deepcopy(payload); bad['fulfillment_documents'][0]['pdf_base64'] = 'YQ=='
    with pytest.raises(ValueError): docs.externalize(b, bad)
    wire = docs.externalize(b, payload)
    wire['fulfillment_document_history'][1]['storage_ref'] = 'supabase://other/private.pdf'
    with pytest.raises(ValueError): docs.hydrate(b, wire)


def test_migration_backs_up_and_does_not_overwrite_concurrent_revision(storage):
    b, payload, objects, uploads, downloads = storage
    record = dict(revision=4,payload=payload)
    calls = []
    def request(path, method='GET', **kwargs):
        if method == 'GET': return [copy.deepcopy(record)]
        calls.append(kwargs['payload'])
        assert any('/backups/' in ref for ref in objects)
        return {'saved': False, 'revision': 5}
    b.supabase_request = request
    assert migration.convert(b, 1) == 'conflicts'
    assert record == dict(revision=4,payload=payload)
    assert calls[0]['p_expected_revision'] == 4


def test_warm_read_checks_revision_and_refetches_changed_payload(storage):
    b, payload, *_ = storage
    calls = []
    remote = dict(revision=1,payload=docs.externalize(b,payload))
    def request(path, **kwargs):
        select = kwargs['params']['select']; calls.append(select)
        return [copy.deepcopy(remote) if select == 'revision,payload' else {'revision':remote['revision']}]
    b.supabase_request = request
    assert store._read_remote(b,1)[0]['revision'] == 1
    assert store._read_remote(b,1)[0]['revision'] == 1
    remote['revision'] = 2
    assert store._read_remote(b,1)[0]['revision'] == 2
    assert calls == ['revision,payload','revision','revision','revision,payload']


def test_storage_pending_ack_loss_and_cold_cache_preserve_business_evidence(ready, storage, monkeypatch):
    import app as b
    client, cloud, _, sent = ready
    fake, _, _, uploads, downloads = storage
    for name in ('supabase_storage_ref', 'supabase_storage_upload_file', 'supabase_storage_download_bytes'):
        monkeypatch.setattr(b, name, getattr(fake,name))
    monkeypatch.setattr(b, 'SUPABASE_URL', fake.SUPABASE_URL)
    original = b.supabase_request
    def lose_ack(path, method='GET', **kwargs):
        result = original(path, method=method, **kwargs)
        if method == 'POST' and path.endswith('save_fulfillment_reconciliation'):
            raise TimeoutError('Saved, but acknowledgement lost')
        return result
    monkeypatch.setattr(b, 'supabase_request', lose_ack)
    assert pack(client,qty=1).status_code == 503
    assert any('storage_ref' in d for r in cloud.values() for d in r['payload']['fulfillment_documents'])
    monkeypatch.setattr(b,'supabase_request', original)
    c = b.conn()
    ids = [r[0] for r in c.execute('SELECT order_id FROM fulfillment_reconciliation_pending')]
    c.close()
    for oid in ids: store.retry_pending(b,oid,packing_only=True)
    c = b.conn()
    assert c.execute('SELECT COUNT(*) FROM fulfillment_reconciliation_pending').fetchone()[0] == 0
    files = [r[0] for r in c.execute('SELECT path FROM fulfillment_document_history')]
    stock = list(map(tuple,c.execute('SELECT * FROM stock ORDER BY product_id')))
    allocations = list(map(tuple,c.execute('SELECT * FROM invoice_allocations ORDER BY id')))
    c.close()
    for path in set(files): Path(path).unlink(missing_ok=True)
    for path in (Path(b.DATA_DIR)/'fulfillment-cache').glob('*.pdf'): path.unlink()
    store.restore(b,103)
    c = b.conn()
    assert all(Path(r[0]).is_file() for r in c.execute('SELECT path FROM fulfillment_document_history WHERE order_id=103'))
    assert list(map(tuple,c.execute('SELECT * FROM stock ORDER BY product_id'))) == stock
    assert list(map(tuple,c.execute('SELECT * FROM invoice_allocations ORDER BY id'))) == allocations
    c.close()
    assert not sent
