from contextlib import contextmanager
from io import BytesIO

import pytest
import app as backend
from routes import architects
import architect_models as models
from test_product_creation import client, isolated, _actor


def test_warehouse_file_lifecycle(client, monkeypatch):
    objects = {}
    path = '/architects/models-3d'
    def storage(path, method='GET', payload=None, **kwargs):
        if method == 'DELETE':
            for key in payload['prefixes']:
                objects.pop(key, None)
            return []
        assert method == 'POST' and path.endswith('/list/' + models.BUCKET)
        return [dict(id='object', name=k, metadata={'size':len(v)}) for k,v in objects.items()]
    @contextmanager
    def upload(req, timeout):
        key = req.full_url.rsplit('/', 1)[1]
        objects[key] = req.data.read()
        yield BytesIO(b'{}')
    monkeypatch.setattr(architects, 'supabase_request', storage)
    monkeypatch.setattr(architects, 'supabase_storage_object_url', lambda bucket,key: 'https://storage.test/'+bucket+'/'+key)
    monkeypatch.setattr(backend.urllib.request, 'urlopen', upload)
    data = dict(action='upload', csrf_token='create-test')
    name = 'Uchwyt żółty.step'
    for _ in range(2):
        res = client.post(path, data={**data,'file':(BytesIO(b'model'),name)},follow_redirects=True)
        assert res.status_code == 200 and name in res.text and 'Usuń plik' in res.text
    key, = objects
    assert objects[key] == b'model'
    assert client.post(path, data={**data,'file':(BytesIO(b'test'),'script.html')}).status_code == 400
    assert client.post(path, data={**data,'action':'delete','key':'../other-bucket/test'}).status_code == 400
    assert client.post(path, data={**data,'action':'delete','key':key,'csrf_token':'bad'}).status_code in (400,403)
    assert key in objects
    assert name not in client.post(path,data={**data,'action':'delete','key':key},follow_redirects=True).text
    assert not objects
    with client.session_transaction() as state:
        state['internal_actor_id'] = _actor(role='MAGAZYN').actor_id
    assert client.post(path,data=data).status_code == 403


def test_size_limits(monkeypatch):
    monkeypatch.setattr(models,'MAX_BYTES',4)
    for content in (b'', b'12345'):
        with pytest.raises(ValueError):
            models.upload_key('Model.skp',BytesIO(content))
