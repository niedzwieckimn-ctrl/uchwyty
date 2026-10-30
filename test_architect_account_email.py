import threading
from architect_account_email import request_attempt, delivery_state, read_delivery


def test_acceptance_is_not_delivery_and_errors_are_visible():
    assert delivery_state({'status':'DELIVERED','provider_id':'p','provider_status':'accepted'})['label']=='Wysłano'
    assert delivery_state({'status':'DELIVERED','provider_id':'p','provider_status':'delivered'})['label']=='Dostarczono'
    for status in ['RETRY','UNKNOWN','BLOCKED']:
        assert delivery_state({'status':status})['label']=='Błąd wysyłki'
    assert delivery_state({'status':'PENDING'})['label']=='W kolejce'
    assert delivery_state({'status':'DELIVERING'})['label']=='Wysyłanie'


def test_immediate_wakeup_is_nonblocking_and_coalesces_clicks(monkeypatch):
    monkeypatch.delenv('PORTAL_EMAIL_WORKER_BEARER',raising=False)
    entered=threading.Event();release=threading.Event();finished=threading.Event();calls=[]
    def remote(path, **kwargs):
        calls.append((path,kwargs));entered.set();release.wait(2);finished.set();return {'accepted':1}
    job='11111111-1111-4111-8111-111111111111'
    try:
        assert request_attempt(remote,job)
        assert entered.wait(1)
        assert request_attempt(remote,job)
        assert len(calls)==1
        assert calls[0][0]=='/functions/v1/portal-email'
        assert calls[0][1]['payload']=={'job_id':job}
    finally:
        release.set();assert finished.wait(1)


def test_status_query_is_scoped_to_architect_and_job():
    calls=[]
    def remote(path, **kwargs):calls.append(kwargs);return [{'id':'job','status':'PENDING'}]
    owner='11111111-1111-4111-8111-111111111111';job='22222222-2222-4222-8222-222222222222'
    assert read_delivery(remote,owner,job)['id']=='job'
    assert calls[0]['params']['architect_id']=='eq.'+owner
    assert calls[0]['params']['id']=='eq.'+job


def test_worker_bearer_override_is_server_only_and_mocked(monkeypatch):
    from architect_account_email import _wake
    import urllib.request
    monkeypatch.setenv('PORTAL_EMAIL_WORKER_BEARER','synthetic-worker-credential')
    monkeypatch.setenv('SUPABASE_URL','https://fixture.example.invalid')
    calls=[]
    class Response:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def read(self,n): return b'{}'
    def fake_urlopen(req,timeout): calls.append((req,timeout));return Response()
    monkeypatch.setattr(urllib.request,'urlopen',fake_urlopen)
    _wake(lambda *a,**k: (_ for _ in ()).throw(AssertionError('wrong credential route')),'11111111-1111-4111-8111-111111111111')
    assert calls[0][0].full_url=='https://fixture.example.invalid/functions/v1/portal-email'
    assert calls[0][0].get_header('Authorization')=='Bearer synthetic-worker-credential'
    assert calls[0][1]==12
