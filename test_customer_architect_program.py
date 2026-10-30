import pytest
import app as b
import routes.customers as customers
from test_auth_resilience import admin_client
from test_internal_rbac import _create_actor

@pytest.fixture
def program_admin(admin_client, monkeypatch):
    monkeypatch.setattr(b,'_rate_hits',{})
    admin_client.post('/login',data={'username':'admin','password':'correct-test-only'})
    db=b.conn()
    db.execute("INSERT INTO customers(id,name,address,email,language,price_list,created_at) VALUES(101,'Salon A','Testowa 1','test@example.invalid','pl','pln','2026-10-28')")
    db.commit(); db.close()
    calls=[]
    def remote(path,**kwargs):
        calls.append((path,kwargs))
        if path == '/rest/v1/customers':
            return [dict(id=101,name='Salon A',address='Testowa 1',email='test@example.invalid',phone='',nip='',language='pl',price_list='pln')]
        return [] if 'memberships' in path else {'ok':True}
    monkeypatch.setattr(b,'supabase_enabled',lambda:True)
    monkeypatch.setattr(b,'supabase_request',remote)
    with admin_client.session_transaction() as state: token=state['csrf_token']
    return admin_client,token,calls

def test_profile_default_off_and_reuses_existing_membership(program_admin):
    client,token,calls=program_admin
    result=client.get('/customers/101/edit')
    assert result.status_code==200
    html=result.get_data(as_text=True)
    assert 'Program Architektów: WYŁĄCZONY' in html
    assert token in html
    result=client.post('/customers/101/architect-program',data={'csrf_token':token,'active':'1','store_name':'Salon A','city':'Warszawa'})
    assert result.status_code==302
    payload=calls[-1][1]['payload']
    assert payload['p_customer_id']==101 and payload['p_active'] is True
    assert calls[-1][0]=='/rest/v1/rpc/architect_set_distributor'

def test_customer_toggle_requires_csrf(program_admin):
    client,token,calls=program_admin
    result=client.post('/customers/101/architect-program',data={'active':'1'})
    assert result.status_code==403 and calls==[]

def test_remote_customer_profile_survives_empty_local_cache(program_admin):
    client,token,calls=program_admin
    db=b.conn(); db.execute('DELETE FROM customers WHERE id=101'); db.commit(); db.close()
    result=client.get('/customers/101/edit')
    assert result.status_code==200 and 'Salon A' in result.get_data(as_text=True)

def test_warehouse_role_cannot_change_membership(program_admin):
    client,token,calls=program_admin
    actor=_create_actor('HUMAN','MAGAZYN')
    with client.session_transaction() as state: state['internal_actor_id']=actor
    result=client.post('/customers/101/architect-program',data={'csrf_token':token,'active':'1'})
    assert result.status_code==403 and calls==[]

def test_disabling_absent_membership_does_not_create_or_delete_data(program_admin):
    client,token,calls=program_admin
    result=client.post('/customers/101/architect-program',data={'csrf_token':token,'active':'0'})
    assert result.status_code==302
    assert all('/rpc/' not in path for path,_ in calls)
