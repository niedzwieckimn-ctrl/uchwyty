from types import SimpleNamespace
import pytest
import app as backend
import internal_rbac as rbac
import ksef_automation
from test_internal_rbac import isolated, _legacy_admin_session, _create_actor


def test_health_is_public_read_only_and_reports_missing_worker(isolated, monkeypatch):
    monkeypatch.setattr(ksef_automation.scheduler, '_worker', None)
    monkeypatch.setattr(ksef_automation, 'run_batch', lambda *a, **k: pytest.fail('health ran a batch'))
    result = isolated.get('/health/ksef-worker')
    assert result.status_code == 503 and result.json['worker_alive'] is False
    monkeypatch.setattr(ksef_automation.scheduler, '_worker', SimpleNamespace(is_alive=lambda: True))
    result = isolated.get('/health/ksef-worker')
    assert result.status_code == 200 and result.json['version'] == ksef_automation.VERSION
    assert set(result.json) == {'ok', 'version', 'worker_alive'}


def test_diagnostics_require_login_and_owner(isolated):
    assert isolated.get('/api/admin/ksef/automation').status_code in (401, 403)
    _legacy_admin_session(isolated, _create_actor(rbac.ACTOR_HUMAN, 'MAGAZYN'))
    assert isolated.get('/api/admin/ksef/automation').status_code == 403


def test_owner_dry_run_requires_csrf_and_never_sends(isolated, monkeypatch):
    _legacy_admin_session(isolated)
    calls=[]
    monkeypatch.setattr(ksef_automation, 'run_batch', lambda *a, **k: calls.append(k) or {'ok': True, 'invoice_ids': []})
    assert isolated.post('/admin/ksef/automation').status_code == 403
    assert isolated.post('/admin/ksef/automation', data={'csrf_token':'test'}, headers={'Origin':'https://foreign.test'}).status_code == 403
    result = isolated.post('/admin/ksef/automation', data={'csrf_token':'test'})
    assert result.status_code == 200 and calls == [{'send':False}]


def test_missing_configuration_does_not_claim_ambiguous_attempt(isolated, monkeypatch, tmp_path):
    from routes import invoices
    monkeypatch.setattr(invoices,'require_complete_invoice',lambda i:None)
    monkeypatch.setattr(invoices,'load_ksef_doc',lambda i:{'status':'draft'})
    monkeypatch.setattr(invoices,'build_invoice_ksef_payload',lambda i:({'invoice_no':'TEST'}, {}, [], []))
    monkeypatch.setattr(invoices,'build_ksef_draft_xml',lambda *a:'<test/>')
    monkeypatch.setattr(invoices,'ksef_schema_path',lambda:str(tmp_path/'absent.xsd'))
    monkeypatch.setattr(invoices,'ksef_xml_path',lambda *a:str(tmp_path/'test.xml'))
    monkeypatch.setattr(invoices,'send_invoice_to_ksef',lambda *a,**k:pytest.fail('unexpected send'))
    monkeypatch.setattr(invoices,'ksef_attempt',lambda *a,**k:pytest.fail('unexpected claim'))
    monkeypatch.setattr(invoices,'ksef_config_summary',lambda:{'configured':False,'missing':['KSEF_TOKEN']})
    changes=[]
    monkeypatch.setattr(invoices,'upsert_ksef_doc',lambda *a,**k:changes.append(k))
    with backend.app.test_request_context('/invoices/100/ksef/send',method='POST'):
        assert backend.app.view_functions['invoice_ksef_send'](100).status_code == 302
    assert 'KSEF_TOKEN' in changes[0]['last_error']
