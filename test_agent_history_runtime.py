"""Protocol/acceptance fixtures use a scripted model, not a language resolver.
These tests prove history and tools reach the model, not live model comprehension.
"""
import json
from pathlib import Path
import pytest
import agent_runtime as runtime
import agent_conversation as conversations
import business_operations as operations
import internal_rbac as rbac
import app as backend
from test_agent_runtime import isolated,owner,tool,respond

@pytest.mark.parametrize(('question','answer','followup','operation','args'),[
    ('Pokaż Avery.','Avery 128 (id=2) i Avery 160 (id=1).','Ten mniejszy.','inventory.product.get',{'product_id':2}),
    ('Pokaż ostatnie zamówienie Magmar.','Zamówienie 2608251: produkt id=2, Avery 128.','Mam te uchwyty na stanie?','inventory.product.get',{'product_id':2}),
    ('Który klient kupił ostatnio Andre?','Klient AM Interiors (id=10).','A jego ostatnia faktura?','invoices.search',{'customer_id':10,'limit':1}),
    ('Pokaż dwa ostatnie potwierdzone.','Zamówienia id=10 oraz id=11.','A obydwa mogę wysłać?','orders.get',{'id':10}),
])
def test_acceptance_history_is_verbatim_and_model_supplies_selectors(question,answer,followup,operation,args):
    # Seed an older product discussion to catch stale ACTIVE_CONTEXT routing.
    first=runtime.run_agent_turn(owner(),'Pokaż Leo',runtime.FakeModelProvider([tool('inventory.product.get',{'product_id':3}),respond('Leo id=3.')]))
    cid=first['conversation_id']
    runtime.run_agent_turn(owner(),question,runtime.FakeModelProvider([respond(answer)]),conversation_id=cid)
    def decide(kwargs):
        messages=[(x['role'],x['content']) for x in kwargs['input_items'] if x.get('role') in ('user','assistant')]
        assert messages[-3:]==[('user',question),('assistant',answer),('user',followup)]
        assert 'ACTIVE_CONTEXT' not in json.dumps(kwargs)
        return tool(operation,args)
    provider=runtime.FakeModelProvider([decide,respond('Sprawdziłem dostępne dane.')])
    result=runtime.run_agent_turn(owner(),followup,provider,conversation_id=cid)
    assert result['status']=='SUCCESS'
    assert provider.calls[0]['tool_choice']=='auto'
    result_data=json.loads(provider.calls[1]['input_items'][-1]['output'])
    assert result_data['ok'] is True

@pytest.mark.parametrize('question',['show me ostatnie zamówienie od Magmar i sprawdź czy mamy wszystko','co magmar ostatnio bral?','sprawdź tamte dwa','zxq-new-word'])
def test_arbitrary_language_is_passed_unchanged_to_model(question):
    p=runtime.FakeModelProvider([respond('Potrzebuję doprecyzowania.')])
    result=runtime.run_agent_turn(owner(),question,p)
    assert result['status']=='SUCCESS' and result['tool_calls']==0
    assert p.calls[0]['input_items'][-1]=={'role':'user','content':question}


def test_missing_operation_is_normal_text_not_guessed_data():
    text='Nie mam operacji pozwalającej bezpiecznie sprawdzić tę informację.'
    result=runtime.run_agent_turn(owner(),'Jaka jest temperatura w magazynie?',runtime.FakeModelProvider([respond(text)]))
    assert result['message']==text and result['tool_calls']==0


def test_no_posthoc_numeric_parser_or_response_tools():
    text='FVAT 8/09/2026: 1 234,56 PLN; 24 sztuki.'
    p=runtime.FakeModelProvider([respond(text)])
    assert runtime.run_agent_turn(owner(),'Przykład formatowania',p)['message']==text
    assert not any(x['name'].startswith('assistant.') for x in p.calls[0]['tools'])
    assert 'numeric_claims' not in p.calls[0]['instructions']
    for file in ['agent_runtime.py','agent_conversation.py']:
        source=(Path(__file__).parent/file).read_text(encoding='utf-8-sig')
        for removed in ['resolve_reference','selection_candidate_id','_NUMERIC_LITERAL','_business_numeric_mentions','def update_context','ACTIVE_CONTEXT']:
            assert removed not in source


def memory_call(term='lofra',meaning='zamówienie spakowane bez etykiety',confirmed=True,version=0):
    return tool('agent.terminology.remember',{'term':term,'meaning':meaning,'confirmed_by_user':confirmed,'expected_version':version})


def test_unknown_word_confirmation_and_new_conversation_memory():
    p=runtime.FakeModelProvider([tool('agent.terminology.search',{'query':'lofra'}),respond('Co u Ciebie oznacza lofra?')])
    first=runtime.run_agent_turn(owner(),'Sprawdź lofry.',p)
    db=backend.conn();assert db.execute('SELECT COUNT(*) FROM internal_agent_terminology').fetchone()[0]==0;db.close()
    def confirmed(kwargs):
        assert kwargs['input_items'][-1]['content']=='Tak nazywamy zamówienia spakowane, ale jeszcze bez etykiety.'
        return memory_call()
    p=runtime.FakeModelProvider([confirmed,respond('Zapamiętałem znaczenie.')])
    saved=runtime.run_agent_turn(owner(),'Tak nazywamy zamówienia spakowane, ale jeszcze bez etykiety.',p,conversation_id=first['conversation_id'])
    assert saved['status']=='SUCCESS'
    assert json.loads(p.calls[1]['input_items'][-1]['output'])['ok'] is True
    next_provider=runtime.FakeModelProvider([respond('Lofry to zamówienia spakowane bez etykiety.')])
    runtime.run_agent_turn(owner(),'Co znaczy lofra?',next_provider)
    assert 'zamówienie spakowane bez etykiety' in next_provider.calls[0]['input_items'][0]['content']
    db=backend.conn();row=db.execute('SELECT * FROM internal_agent_terminology').fetchone()
    assert row['scope']=='company' and row['source']=='confirmed_by_user'
    assert row['confirmed_by_actor_id']==owner().actor_id
    assert db.execute("SELECT COUNT(*) FROM internal_audit_log WHERE operation='agent.terminology.remembered'").fetchone()[0]==1
    db.close()


def test_uncertain_definition_is_temporary_until_model_observes_confirmation():
    first=runtime.run_agent_turn(owner(),'Andre to chyba CH101',runtime.FakeModelProvider([respond('Czy Andre oznacza u Was CH101?')]))
    db=backend.conn();assert db.execute('SELECT COUNT(*) FROM internal_agent_terminology').fetchone()[0]==0;db.close()
    p=runtime.FakeModelProvider([memory_call('Andre','CH101'),respond('Zapamiętałem.')])
    result=runtime.run_agent_turn(owner(),'Tak.',p,conversation_id=first['conversation_id'])
    assert result['status']=='SUCCESS'
    assert json.loads(p.calls[1]['input_items'][-1]['output'])['ok'] is True


def test_false_confirmation_and_stale_version_cannot_write_memory():
    p=runtime.FakeModelProvider([memory_call(confirmed=False),respond('Potrzebuję potwierdzenia.')])
    runtime.run_agent_turn(owner(),'lofra?',p)
    assert json.loads(p.calls[1]['input_items'][-1]['output'])['error_code']=='CONFIRMATION_REQUIRED'
    p=runtime.FakeModelProvider([memory_call(),respond('Zapamiętałem.')])
    runtime.run_agent_turn(owner(),'Lofra oznacza zamówienie spakowane bez etykiety.',p)
    p=runtime.FakeModelProvider([memory_call(meaning='inne znaczenie',version=0),respond('Muszę sprawdzić aktualną definicję.')])
    runtime.run_agent_turn(owner(),'Zmień znaczenie.',p)
    assert json.loads(p.calls[1]['input_items'][-1]['output'])['error_code']=='MEMORY_VERSION_CONFLICT'


def test_forged_memory_provenance_denied():
    args={'term':'x','meaning':'y','confirmed_by_user':True,'expected_version':0,'source_run_id':'invented'}
    result=runtime.run_agent_turn(owner(),'hello',runtime.FakeModelProvider([tool('agent.terminology.remember',args)]))
    assert result['error_code']=='INVALID_MEMORY_SOURCE'


def test_http_chat_never_refreshes_all_tables(monkeypatch):
    def forbidden(*a,**k):raise AssertionError('AI must not refresh data')
    monkeypatch.setattr(backend,'pull_shared_tables_from_supabase',forbidden)
    backend.AGENT_MODEL_PROVIDER=runtime.FakeModelProvider([tool('inventory.summary',{}),respond('Odczytałem stan.')])
    client=backend.app.test_client()
    with client.session_transaction() as session:session['admin_authenticated']=True;session['csrf_token']='csrf'
    assert client.post('/api/internal/ai/chat',json={'message':'Ile mam sztuk?'}).status_code==200


def test_two_model_round_trips_one_operation_and_timing_fields():
    p=runtime.FakeModelProvider([tool('inventory.product.search',{'query':'Avery 160'}),respond('24 sztuki.')])
    result=runtime.run_agent_turn(owner(),'Ile mam Avery 160?',p)
    assert len(p.calls)==2 and result['tool_calls']==1
    assert set(result['timings'])=={'context_build_ms','first_model_call_ms','business_operation_ms','final_model_call_ms','total_ms','tool_calls_count'}
    assert all(v>=0 for v in result['timings'].values())
    assert result['timings']['tool_calls_count']==1


def test_parallel_model_calls_are_all_returned_with_matching_outputs():
    p=runtime.FakeModelProvider([runtime.ProviderResponse(tool_calls=(
        runtime.ToolCall('a','inventory.product.get','{"product_id":1}'),
        runtime.ToolCall('b','inventory.product.get','{"product_id":2}'))),respond('24 i 8 sztuk.')])
    result=runtime.run_agent_turn(owner(),'Oba warianty',p)
    assert result['status']=='SUCCESS' and result['tool_calls']==2
    outputs=[i for i in p.calls[-1]['input_items'] if i.get('type')=='function_call_output']
    assert [i['call_id'] for i in outputs]==['a','b']


def test_business_failure_is_returned_to_model_for_normal_explanation(monkeypatch):
    def fail(*a):raise operations.ControlledOperationError('UNAVAILABLE','Brak dostępnych danych')
    monkeypatch.setitem(operations._HANDLERS,'inventory.summary',fail)
    p=runtime.FakeModelProvider([tool('inventory.summary',{}),respond('Nie mogę teraz sprawdzić stanu.')])
    result=runtime.run_agent_turn(owner(),'stan?',p)
    assert result['status']=='SUCCESS'
    assert json.loads(p.calls[-1]['input_items'][-1]['output'])['error_code']=='UNAVAILABLE'


def test_store_false_replays_encrypted_reasoning_and_plain_text(monkeypatch):
    sent=[]
    bodies=[{'output':[{'type':'reasoning','id':'r1','summary':[],'encrypted_content':'opaque'},
        {'type':'function_call','call_id':'c1','name':'inventory__summary','arguments':'{}'}]},
        {'output':[{'type':'message','role':'assistant','content':[{'type':'output_text','text':'38 sztuk.'}]}]}]
    class Response:
        headers={};status_code=200
        def __init__(self,body):self.body=body
        def raise_for_status(self):pass
        def json(self):return self.body
    def post(url,**kwargs):
        sent.append(json.loads(json.dumps(kwargs['json'])))
        return Response(bodies.pop(0))
    monkeypatch.setattr(runtime.requests,'post',post)
    result=runtime.run_agent_turn(owner(),'Ile sztuk?',runtime.OpenAIResponsesProvider(model='test',api_key='test'))
    assert result['message']=='38 sztuk.' and len(sent)==2
    assert all(s['store'] is False and 'previous_response_id' not in s for s in sent)
    assert sent[1]['input'][1]['encrypted_content']=='opaque'
    assert sent[1]['input'][-1]['call_id']=='c1'
    assert all(s['tool_choice']=='auto' for s in sent)

def test_product_buyer_lookup_is_a_structured_operation_not_language_rule():
    db=backend.conn();db.execute("INSERT INTO order_items(order_id,product_id,sku,qty,unit_net_price,unit_gross_price,currency,created_at) VALUES(10,2,'CH101-BLK-128',2,100,123,'PLN',?)",(backend.now_iso(),));db.commit();db.close()
    ai=rbac.load_actor_context(rbac.AI_OWNER_ASSISTANT_ACTOR_ID)
    found=operations.execute_business_operation(ai,'orders.search',{'product_id':2,'limit':1})
    assert found.status=='SUCCESS' and found.data['results'][0]['customer_id']==10
    missing=operations.execute_business_operation(ai,'orders.search',{'product_id':3,'limit':1})
    assert missing.status=='SUCCESS' and missing.data['count']==0


def test_order_followup_uses_real_order_items_not_older_product():
    db=backend.conn();db.execute("INSERT INTO order_items(order_id,product_id,sku,qty,unit_net_price,unit_gross_price,currency,created_at) VALUES(10,2,'CH101-BLK-128',2,100,123,'PLN',?)",(backend.now_iso(),));db.commit();db.close()
    first=runtime.run_agent_turn(owner(),'Pokaż Leo',runtime.FakeModelProvider([tool('inventory.product.get',{'product_id':3}),respond('Leo id=3.')]))
    cid=first['conversation_id']
    second=runtime.run_agent_turn(owner(),'Ostatnie zamówienie AM Interiors',runtime.FakeModelProvider([
        tool('orders.get',{'latest':True,'customer_id':10}),respond('ZAM-TEST-10 ma Avery 128, produkt id=2.')]),conversation_id=cid)
    def select(kwargs):
        outputs=[json.loads(x['output']) for x in kwargs['input_items'] if x.get('type')=='function_call_output']
        assert outputs[-1]['record']['items'][0]['product_id']==2
        return tool('inventory.product.get',{'product_id':2})
    provider=runtime.FakeModelProvider([select,respond('8 sztuk Avery 128.')])
    result=runtime.run_agent_turn(owner(),'Mam te uchwyty na stanie?',provider,conversation_id=cid)
    assert result['status']=='SUCCESS'
    assert json.loads(provider.calls[-1]['input_items'][-1]['output'])['id']==2


def test_memory_idempotency_provenance_and_required_permission():
    human=owner();ai=rbac.load_actor_context(rbac.AI_OWNER_ASSISTANT_ACTOR_ID,delegated_by_actor_id=human.actor_id)
    cid=conversations.open_conversation(human,ai)[0];run='00000000-0000-4000-8000-000000000099'
    conversations.begin_turn(human,ai,cid,run,'Lofra oznacza spakowane bez etykiety.')
    args={'term':'lofra','meaning':'spakowane bez etykiety','confirmed_by_user':True,'expected_version':0,'source_run_id':run}
    first=operations.execute_business_operation(ai,'agent.terminology.remember',args,idempotency_key='memory-one')
    repeat=operations.execute_business_operation(ai,'agent.terminology.remember',args,idempotency_key='memory-one')
    assert first.status==repeat.status=='SUCCESS' and first.execution_id==repeat.execution_id
    # Even a different execution key cannot duplicate the write from the same source turn.
    another=operations.execute_business_operation(ai,'agent.terminology.remember',args,idempotency_key='memory-two')
    assert another.status=='SUCCESS' and another.data['version']==1
    db=backend.conn();assert db.execute('SELECT version FROM internal_agent_terminology').fetchone()[0]==1
    db.execute("UPDATE internal_role_permissions SET decision='DENY' WHERE role_key='OWNER' AND permission_key='agent.terminology.remember'");db.commit();db.close()
    denied=operations.execute_business_operation(ai,'agent.terminology.remember',{**args,'term':'other'},idempotency_key='memory-three')
    assert denied.status!='SUCCESS' and denied.error_code=='PERMISSION_DENIED'


def test_memory_policy_can_require_approval_without_writing():
    db=backend.conn();db.execute("UPDATE internal_approval_policies SET requires_approval=1 WHERE operation='agent.terminology.remember'");db.commit();db.close()
    p=runtime.FakeModelProvider([memory_call(),respond('Zapis oczekuje na zatwierdzenie.')])
    runtime.run_agent_turn(owner(),'Lofra to spakowane bez etykiety.',p)
    payload=json.loads(p.calls[-1]['input_items'][-1]['output'])
    assert payload['status']=='PENDING_APPROVAL'
    db=backend.conn();assert db.execute('SELECT COUNT(*) FROM internal_agent_terminology').fetchone()[0]==0;db.close()


def test_text_is_verbatim_and_secret_redaction_is_only_structural():
    text='Treść '*450
    p=runtime.FakeModelProvider([respond(text)])
    result=runtime.run_agent_turn(owner(),'  Cześć!  ',p)
    assert result['message']==text
    assert p.calls[0]['input_items'][-1]['content']=='  Cześć!  '
    secret='sk-abcdefghijklmnopqrstuv'
    result=runtime.run_agent_turn(owner(),'hello',runtime.FakeModelProvider([respond('api_key='+secret)]))
    assert secret not in result['message']


def test_human_permission_revoked_during_model_call_denies_operation():
    def revoke(kwargs):
        db=backend.conn();db.execute("UPDATE internal_role_permissions SET decision='DENY' WHERE role_key='OWNER' AND permission_key='inventory.read'");db.commit();db.close()
        return tool('inventory.summary',{})
    result=runtime.run_agent_turn(owner(),'stan',runtime.FakeModelProvider([revoke]))
    assert result['error_code']=='PERMISSION_DENIED'
    db=backend.conn();assert db.execute('SELECT COUNT(*) FROM internal_operation_executions').fetchone()[0]==0;db.close()


def test_invalid_arguments_are_rejected_by_gate_and_can_be_explained():
    p=runtime.FakeModelProvider([tool('inventory.product.get',{'product_id':True}),respond('Nieprawidłowy identyfikator.')])
    result=runtime.run_agent_turn(owner(),'produkt',p)
    assert result['status']=='SUCCESS'
    assert json.loads(p.calls[-1]['input_items'][-1]['output'])['error_code']=='INVALID_INPUT'

def test_history_storage_failure_returns_controlled_response_without_retry(monkeypatch):
    attempts=[]
    def fail(*a,**k):
        attempts.append(1);raise RuntimeError('storage failure')
    monkeypatch.setattr(conversations,'finish_turn',fail)
    result=runtime.run_agent_turn(owner(),'hello',runtime.FakeModelProvider([respond('hello')]))
    assert result['error_code']=='HISTORY_SAVE_FAILED' and len(attempts)==1


def test_forbidden_call_in_batch_is_denied_before_any_execution():
    p=runtime.FakeModelProvider([runtime.ProviderResponse(tool_calls=(
        runtime.ToolCall('a','inventory.summary','{}'),runtime.ToolCall('b','inventory.adjust','{}')))])
    result=runtime.run_agent_turn(owner(),'test',p)
    assert result['status']=='DENIED'
    db=backend.conn();assert db.execute('SELECT COUNT(*) FROM internal_operation_executions').fetchone()[0]==0;db.close()


def test_adapter_rejects_incomplete_partial_response(monkeypatch):
    class Response:
        status_code=200;headers={}
        def raise_for_status(self):pass
        def json(self):return {'status':'incomplete','output':[{'type':'message','content':[{'type':'output_text','text':'partial'}]}]}
    monkeypatch.setattr(runtime.requests,'post',lambda *a,**k:Response())
    result=runtime.run_agent_turn(owner(),'hello',runtime.OpenAIResponsesProvider(model='test',api_key='test'))
    assert result['status']=='FAILED' and result['message']!='partial'


def test_context_contains_backend_date_for_relative_dates():
    p=runtime.FakeModelProvider([respond('Dopytam o okres.')])
    runtime.run_agent_turn(owner(),'wczoraj?',p)
    assert operations._business_now().date().isoformat() in p.calls[0]['instructions']
