"""Protocol/acceptance fixtures use a scripted model, not a language resolver.
These tests prove history and tools reach the model, not live model comprehension.
"""
import json
from pathlib import Path
import time
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
    assert set(result['timings'])=={
        'acquire_turn_ms','memory_load_ms','context_history_build_ms','supabase_business_reads_ms',
        'model_request_ms','tool_execution_ms','second_model_pass_ms','parallel_read_batch_ms',
        'parallel_read_sequential_estimate_ms','context_build_ms',
        'first_model_call_ms','business_operation_ms','final_model_call_ms','total_ms','tool_calls_count',
    }
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


def test_four_independent_green_reads_use_one_parallel_batch(monkeypatch):
    reads = [
        ('inventory.summary', {}),
        ('orders.summary', {'period':'today'}),
        ('invoices.overdue', {}),
        ('china.orders.summary', {'scope':'active'}),
    ]
    original = operations.execute_business_operation

    def delayed_read(*args, **kwargs):
        if args[1] in {name for name,_arguments in reads}:
            time.sleep(0.08)
        return original(*args, **kwargs)

    monkeypatch.setattr(operations, 'execute_business_operation', delayed_read)
    sequential_provider = runtime.FakeModelProvider([
        *[tool(name, arguments, f'seq-{index}') for index,(name,arguments) in enumerate(reads)],
        respond('Podsumowanie gotowe.'),
    ])
    sequential_started = time.perf_counter()
    sequential = runtime.run_agent_turn(owner(), 'co mam dziś do zrobienia?', sequential_provider)
    sequential_ms = (time.perf_counter()-sequential_started)*1000

    batch_calls = tuple(runtime.ToolCall(f'batch-{index}',name,json.dumps(arguments))
                        for index,(name,arguments) in enumerate(reads))

    def synthesize(kwargs):
        assert kwargs['tool_choice'] == 'none' and kwargs['tools'] == []
        outputs = [item for item in kwargs['input_items'] if item.get('type') == 'function_call_output']
        assert len(outputs) == 4 and all(json.loads(item['output']).get('ok') is True for item in outputs)
        return respond('Podsumowanie gotowe.')

    batch_provider = runtime.FakeModelProvider([
        runtime.ProviderResponse(tool_calls=batch_calls,model='fake-model'),
        synthesize,
    ])
    batch_started = time.perf_counter()
    batched = runtime.run_agent_turn(owner(), 'co mam dziś do zrobienia?', batch_provider)
    batch_ms = (time.perf_counter()-batch_started)*1000

    assert sequential['status']==batched['status']=='SUCCESS'
    assert len(sequential_provider.calls)==5
    assert len(batch_provider.calls)==2 and batched['tool_calls']==4
    assert batched['timings']['parallel_read_batch_ms'] > 0
    assert batched['timings']['parallel_read_sequential_estimate_ms'] > batched['timings']['parallel_read_batch_ms']*2
    assert batch_ms < sequential_ms*0.7


def test_green_batch_final_synthesis_prevents_extra_tools_and_returns_http_200():
    reads = [
        ('inventory.summary', {}),
        ('orders.summary', {'period':'today'}),
        ('invoices.overdue', {}),
        ('china.orders.summary', {'scope':'active'}),
    ]
    first_calls = tuple(runtime.ToolCall(f'initial-{index}', name, json.dumps(arguments))
                        for index, (name, arguments) in enumerate(reads))

    def synthesis(kwargs):
        # With the old auto/tool-enabled pass this branch would request three more tools
        # and trip TOOL_LIMIT_EXCEEDED after the four completed reads.
        if kwargs['tool_choice'] == 'auto' or kwargs['tools']:
            return runtime.ProviderResponse(tool_calls=tuple(
                runtime.ToolCall(f'extra-{index}', 'inventory.product.search',
                                 json.dumps({'query':f'Avery {index}'}))
                for index in range(3)))
        outputs = [item for item in kwargs['input_items'] if item.get('type') == 'function_call_output']
        assert len(outputs) == 4
        assert all(json.loads(item['output']).get('ok') is True for item in outputs)
        instructions = kwargs['instructions']
        assert 'Użyj wyłącznie wyników narzędzi już dostarczonych' in instructions
        assert 'Nie imituj wywołania narzędzia w tekście' in instructions
        assert '1. Pilne wysyłki' in instructions
        assert '2. Płatności po terminie' in instructions
        assert '3. Braki wymagające działania' in instructions
        assert '4. Pozostałe ważne rzeczy' in instructions
        return respond('1. Pilne wysyłki\n- Brak pilnych wysyłek.\n2. Płatności po terminie\n- Jedna zaległa faktura.\n3. Braki wymagające działania\n- Brak potwierdzenia pokrycia SKU w tym przebiegu.\n4. Pozostałe ważne rzeczy\n- Cztery odczyty uwzględnione.')

    backend.AGENT_MODEL_PROVIDER = runtime.FakeModelProvider([
        runtime.ProviderResponse(tool_calls=first_calls, model='fake-model'), synthesis,
    ])
    client = backend.app.test_client()
    with client.session_transaction() as session:
        session['admin_authenticated'] = True
        session['csrf_token'] = 'csrf'

    response = client.post('/api/internal/ai/chat', json={'message':'co mam dziś do zrobienia?'})

    assert response.status_code == 200
    payload = response.get_json()
    assert payload['status'] == 'SUCCESS' and payload['tool_calls'] == 4
    assert 'Cztery odczyty uwzględnione.' in payload['message']
    assert 'to=functions' not in payload['message']
    assert '{"id":45}' not in payload['message']
    assert 'china.orders.get' not in payload['message']
    assert 'Sprawdzam jeszcze' not in payload['message']


def test_first_planning_pass_bounds_wide_briefing_and_returns_http_200():
    reads = (
        ('orders.fulfillment.readiness', {}),
        ('invoices.overdue', {}),
        ('orders.summary', {'period':'today'}),
        ('china.orders.summary', {'scope':'active'}),
        ('inventory.replenishment.ranking', {}),
    )

    def planning(kwargs):
        assert kwargs['tool_choice'] == 'auto' and kwargs['tools']
        instructions = kwargs['instructions']
        assert 'maksymalnie 6 wywołań narzędzi' in instructions
        assert 'pilne wysyłki/readiness, płatności po terminie' in instructions
        assert 'aktywne P/O i pokrycie braków' in instructions
        return runtime.ProviderResponse(tool_calls=tuple(
            runtime.ToolCall(f'briefing-{index}', name, json.dumps(arguments))
            for index, (name, arguments) in enumerate(reads)
        ), model='fake-model')

    def synthesis(kwargs):
        assert kwargs['tool_choice'] == 'none' and kwargs['tools'] == []
        outputs = [item for item in kwargs['input_items'] if item.get('type') == 'function_call_output']
        assert len(outputs) == 5
        return respond('1. Pilne wysyłki\n- Jedno zamówienie wymaga działania.\n2. Płatności po terminie\n- Jedna zaległa faktura.\n3. Braki wymagające działania\n- Sprawdzono pokrycie dostawami.\n4. Pozostałe ważne rzeczy\n- Ranking zapasów uwzględniony.')

    backend.AGENT_MODEL_PROVIDER = runtime.FakeModelProvider([planning, synthesis])
    client = backend.app.test_client()
    with client.session_transaction() as session:
        session['admin_authenticated'] = True
        session['csrf_token'] = 'csrf'

    response = client.post('/api/internal/ai/chat', json={'message':'co mam dziś do zrobienia?'})

    assert response.status_code == 200
    payload = response.get_json()
    assert payload['status'] == 'SUCCESS' and payload['tool_calls'] == 5
    assert 'Ranking zapasów uwzględniony.' in payload['message']


def test_remaining_budget_after_five_green_reads_allows_one_then_synthesizes_http_200():
    first_five = tuple(runtime.ToolCall(
        f'first-{index}', 'inventory.product.search', json.dumps({'query':f'Avery {index}'})
    ) for index in range(5))

    def remaining_one(kwargs):
        assert kwargs['tool_choice'] == 'auto' and kwargs['tools']
        assert 'Pozostały budżet to 1.' in kwargs['instructions']
        assert 'maksymalnie 1 nowych wywołań narzędzi' in kwargs['instructions']
        return runtime.ProviderResponse(tool_calls=(runtime.ToolCall(
            'sixth', 'inventory.product.search', json.dumps({'query':'Leo'})),), model='fake-model')

    def final(kwargs):
        assert kwargs['tool_choice'] == 'none' and kwargs['tools'] == []
        outputs = [item for item in kwargs['input_items'] if item.get('type') == 'function_call_output']
        assert len(outputs) == 6
        return respond('Podsumowanie powstało z sześciu wykonanych odczytów.')

    backend.AGENT_MODEL_PROVIDER = runtime.FakeModelProvider([
        runtime.ProviderResponse(tool_calls=first_five, model='fake-model'), remaining_one, final,
    ])
    client = backend.app.test_client()
    with client.session_transaction() as session:
        session['admin_authenticated'] = True
        session['csrf_token'] = 'csrf'

    response = client.post('/api/internal/ai/chat', json={'message':'Przygotuj szerokie podsumowanie.'})

    assert response.status_code == 200
    payload = response.get_json()
    assert payload['status'] == 'SUCCESS' and payload['tool_calls'] == 6
    assert payload['message'] == 'Podsumowanie powstało z sześciu wykonanych odczytów.'


def test_provider_exceeding_remaining_budget_still_hits_tool_limit_guard():
    first_five = tuple(runtime.ToolCall(
        f'first-{index}', 'inventory.product.search', json.dumps({'query':f'Avery {index}'})
    ) for index in range(5))

    def exceed_remaining(kwargs):
        assert 'Pozostały budżet to 1.' in kwargs['instructions']
        return runtime.ProviderResponse(tool_calls=(
            runtime.ToolCall('sixth', 'inventory.product.search', json.dumps({'query':'Leo'})),
            runtime.ToolCall('seventh', 'inventory.product.search', json.dumps({'query':'Andre'})),
        ), model='fake-model')

    result = runtime.run_agent_turn(owner(), 'Przygotuj szerokie podsumowanie.',
                                    runtime.FakeModelProvider([
                                        runtime.ProviderResponse(tool_calls=first_five, model='fake-model'),
                                        exceed_remaining,
                                    ]))

    assert result['status'] == 'FAILED'
    assert result['error_code'] == 'TOOL_LIMIT_EXCEEDED'
    assert result['tool_calls'] == 5


def test_first_pass_above_global_limit_is_still_rejected_before_execution():
    calls = tuple(runtime.ToolCall(
        f'call-{index}', 'inventory.product.search', json.dumps({'query':f'Avery {index}'})
    ) for index in range(runtime.MAX_TOOL_CALLS_PER_TURN + 1))

    result = runtime.run_agent_turn(owner(), 'Przygotuj szerokie podsumowanie.',
                                    runtime.FakeModelProvider([
                                        runtime.ProviderResponse(tool_calls=calls, model='fake-model')]))

    assert result['status'] == 'FAILED'
    assert result['error_code'] == 'TOOL_LIMIT_EXCEEDED'
    assert result['tool_calls'] == 0


def test_shortage_coverage_fetches_ordered_and_shipped_po_details_and_skips_planned(monkeypatch):
    shortages = {'ok':True, 'results':[
        {'sku':'SKU-A', 'missing_quantity':5},
        {'sku':'SKU-B', 'missing_quantity':4},
        {'sku':'SKU-C', 'missing_quantity':2},
    ], 'count':3, 'ready_count':0, 'truncated':False}
    purchase_orders = {'ok':True, 'results':[
        {'id':11, 'po_number':'PO-11', 'order_status':'shipped'},
        {'id':12, 'po_number':'PO-12', 'order_status':'ordered'},
        {'id':13, 'po_number':'PO-13', 'order_status':'planned'},
    ], 'count':3, 'truncated':False}
    po_details = {
        11:{'id':11, 'order_status':'shipped', 'items':[{'sku':'SKU-A', 'quantity':5}]},
        12:{'id':12, 'order_status':'ordered', 'items':[{'sku':'SKU-B', 'quantity':4}]},
    }
    executed = []

    def successful(operation, data):
        definition = operations.OPERATION_REGISTRY[operation]
        return operations.OperationResult(
            status='SUCCESS', data=data, operation=operation,
            operation_version=definition.operation_version, execution_id=f'exec-{len(executed)}',
            request_id='request', correlation_id='correlation',
        )

    def execute(_actor, operation, arguments, **_kwargs):
        executed.append((operation, dict(arguments)))
        if operation == 'orders.fulfillment.readiness':
            return successful(operation, shortages)
        if operation == 'china.orders.search':
            assert arguments == {'active_only':True}
            return successful(operation, purchase_orders)
        if operation == 'china.orders.get':
            assert arguments['id'] != 13
            return successful(operation, {'ok':True, 'record':po_details[arguments['id']]})
        raise AssertionError(f'unexpected operation: {operation}')

    def plan_shortages(kwargs):
        instructions = kwargs['instructions']
        assert 'Sama china.orders.search, lista P/O ani łączna liczba sztuk nie potwierdza pokrycia SKU.' in instructions
        assert 'Status planned całkowicie pomijaj jako pokrycie' in instructions
        return tool('orders.fulfillment.readiness', {})

    def plan_po_list(kwargs):
        outputs = [json.loads(item['output']) for item in kwargs['input_items']
                   if item.get('type') == 'function_call_output']
        assert outputs[-1] == shortages
        return tool('china.orders.search', {'active_only':True})

    def plan_po_details(kwargs):
        outputs = [json.loads(item['output']) for item in kwargs['input_items']
                   if item.get('type') == 'function_call_output']
        assert outputs[-1] == purchase_orders
        assert 'Pozostały budżet to 4.' in kwargs['instructions']
        return runtime.ProviderResponse(tool_calls=tuple(
            runtime.ToolCall(f'po-{po_id}', 'china.orders.get', json.dumps({'id':po_id}))
            for po_id in (11, 12)
        ), model='fake-model')

    def synthesize(kwargs):
        outputs = [json.loads(item['output']) for item in kwargs['input_items']
                   if item.get('type') == 'function_call_output']
        detail_records = [item['record'] for item in outputs if item.get('record')]
        assert {item['id'] for item in detail_records} == {11, 12}
        coverage = {}
        for record in detail_records:
            for item in record['items']:
                coverage[item['sku']] = coverage.get(item['sku'], 0) + item['quantity']
        uncovered = {
            row['sku']:max(row['missing_quantity'] - coverage.get(row['sku'], 0), 0)
            for row in shortages['results']
        }
        assert uncovered == {'SKU-A':0, 'SKU-B':0, 'SKU-C':2}
        return respond('SKU-C — brak 2 szt.; brak pokrycia w zamówionych lub wysłanych dostawach.')

    monkeypatch.setattr(operations, 'execute_business_operation', execute)
    backend.AGENT_MODEL_PROVIDER = runtime.FakeModelProvider([
        plan_shortages, plan_po_list, plan_po_details, synthesize,
    ])
    client = backend.app.test_client()
    with client.session_transaction() as session:
        session['admin_authenticated'] = True
        session['csrf_token'] = 'csrf'

    response = client.post('/api/internal/ai/chat', json={
        'message':'Które produkty blokują realizację zamówień i nie mają pokrycia w dostawach z Chin?',
    })

    assert response.status_code == 200
    payload = response.get_json()
    assert payload['status'] == 'SUCCESS' and payload['tool_calls'] == 4
    assert payload['message'].startswith('SKU-C — brak 2 szt.')
    assert [arguments['id'] for operation, arguments in executed
            if operation == 'china.orders.get'] == [11, 12]
    assert all(arguments.get('id') != 13 for operation, arguments in executed
               if operation == 'china.orders.get')


def test_generic_read_shortage_analysis_uses_one_query_then_tool_free_synthesis(monkeypatch):
    monkeypatch.setenv('AGENT_GENERIC_READ_ENABLED', '1')
    executed = []
    query_payload = {'queries': [
        {'key':'blocked_orders', 'entity':'orders',
         'select':['number', 'fulfillment_ready', 'fulfillment_missing_items'],
         'where':[{'field':'fulfillment_ready', 'op':'eq', 'value':False}]},
        {'key':'uncovered_products', 'entity':'inventory',
         'select':['sku', 'coverage_status', 'covered_by_stock_and_confirmed_incoming'],
         'where':[{'field':'covered_by_stock_and_confirmed_incoming', 'op':'eq', 'value':False}]},
    ]}
    query_result = {'ok':True, 'results':[
        {'key':'blocked_orders', 'rows':[{'number':'ZAM-1', 'fulfillment_ready':False,
            'fulfillment_missing_items':[{'sku':'SKU-A', 'shortage_quantity':3}]}]},
        {'key':'uncovered_products', 'rows':[{'sku':'SKU-A', 'coverage_status':'Problem',
            'covered_by_stock_and_confirmed_incoming':False}]},
    ]}

    def execute(_actor, operation, arguments, **_kwargs):
        executed.append((operation, dict(arguments)))
        assert operation == 'business.query'
        definition = operations.OPERATION_REGISTRY[operation]
        return operations.OperationResult(
            status='SUCCESS', data=query_result, operation=operation,
            operation_version=definition.operation_version, execution_id='generic-read',
            request_id='request', correlation_id='correlation',
        )

    def plan(kwargs):
        names = {item['name'] for item in kwargs['tools']}
        assert 'business.query' in names
        assert {'orders.fulfillment.readiness', 'orders.fulfillment.state',
                'orders.packing.check', 'shipping.capabilities'} <= names
        assert 'orders.packing.confirm' in names
        assert not ({'orders.search', 'orders.get', 'orders.summary',
                     'inventory.product.search', 'inventory.product.get', 'inventory.summary',
                     'china.orders.search', 'china.orders.get', 'china.orders.summary'} & names)
        assert {'invoices.search', 'customers.search', 'business.sales.summary'} <= names
        assert 'Preferuj\npola opisane przez business.describe_schema jako computed' in kwargs['instructions']
        assert 'Nie\nrekonstruuj readiness, braków, pokrycia rezerwacji' in kwargs['instructions']
        assert 'Sama china.orders.search' not in kwargs['instructions']
        return tool('business.query', query_payload, call_id='generic-query')

    def synthesize(kwargs):
        assert kwargs['tools'] == [] and kwargs['tool_choice'] == 'none'
        outputs = [json.loads(item['output']) for item in kwargs['input_items']
                   if item.get('type') == 'function_call_output']
        assert outputs == [query_result]
        assert 'Użyj wyłącznie wyników narzędzi już dostarczonych' in kwargs['instructions']
        return respond('SKU-A — brak 3 szt. po uwzględnieniu zamówionych P/O; planned pominięto.')

    monkeypatch.setattr(operations, 'execute_business_operation', execute)
    backend.AGENT_MODEL_PROVIDER = runtime.FakeModelProvider([plan, synthesize])
    client = backend.app.test_client()
    with client.session_transaction() as session:
        session['admin_authenticated'] = True
        session['csrf_token'] = 'csrf'

    response = client.post('/api/internal/ai/chat', json={'message':
        'Jakich produktów brakuje mi do pokrycia zamówień klientów po uwzględnieniu tego, '
        'co już jest zamówione w aktywnych P/O? Planned nie traktuj jako pokrycie.'})

    assert response.status_code == 200
    payload = response.get_json()
    assert payload['status'] == 'SUCCESS' and payload['error_code'] == ''
    assert payload['tool_calls'] == 1
    assert executed == [('business.query', query_payload)]
    assert len(backend.AGENT_MODEL_PROVIDER.calls) == 2


def test_generic_read_mode_is_not_used_for_operational_preflight(monkeypatch):
    monkeypatch.setenv('AGENT_GENERIC_READ_ENABLED', '1')

    def inspect(kwargs):
        names = {item['name'] for item in kwargs['tools']}
        assert {'business.query', 'orders.fulfillment.readiness',
                'orders.fulfillment.state', 'orders.packing.check'} <= names
        assert {'orders.search', 'orders.get', 'inventory.summary', 'china.orders.search'} <= names
        assert 'Preferuj\npola opisane przez business.describe_schema jako computed' not in kwargs['instructions']
        return respond('Potrzebuję wskazania zamówienia.')

    result = runtime.run_agent_turn(
        owner(), 'Czy mogę realizować i pakować to zamówienie?',
        runtime.FakeModelProvider([inspect]),
    )
    assert result['status'] == 'SUCCESS' and result['tool_calls'] == 0


def test_daily_briefing_prefers_generic_read_catalog(monkeypatch):
    monkeypatch.setenv('AGENT_GENERIC_READ_ENABLED', '1')

    def inspect(kwargs):
        names = {item['name'] for item in kwargs['tools']}
        assert 'business.query' in names
        assert not ({'orders.summary', 'inventory.summary', 'china.orders.summary'} & names)
        assert {'invoices.overdue', 'business.sales.summary',
                'orders.fulfillment.readiness'} <= names
        assert 'Preferuj\npola opisane przez business.describe_schema jako computed' in kwargs['instructions']
        assert 'pilne wysyłki/readiness, płatności po terminie' not in kwargs['instructions']
        return respond('Brak danych do briefingu.')

    result = runtime.run_agent_turn(
        owner(), 'Co mam dziś do zrobienia?', runtime.FakeModelProvider([inspect]),
    )
    assert result['status'] == 'SUCCESS' and result['tool_calls'] == 0


def test_parallel_green_reads_keep_three_results_when_one_source_is_unavailable(monkeypatch):
    reads = [
        ('inventory.summary', {}),
        ('orders.summary', {'period':'today'}),
        ('invoices.overdue', {}),
        ('china.orders.summary', {'scope':'active'}),
    ]
    original = operations.execute_business_operation

    def one_unavailable(*args, **kwargs):
        if args[1] == 'invoices.overdue':
            raise operations.ControlledOperationError(
                'DATA_UNAVAILABLE', 'Supabase HTTP 504 Gateway Timeout')
        return original(*args, **kwargs)

    def final_from_partial_data(kwargs):
        outputs = [item for item in kwargs['input_items'] if item.get('type') == 'function_call_output']
        assert len(outputs) == 4
        decoded = {item['call_id']:json.loads(item['output']) for item in outputs}
        assert sum(value.get('ok') is True for value in decoded.values()) == 3
        assert decoded['batch-2'] == {
            'ok':False, 'status':'FAILED', 'error_code':'DATA_UNAVAILABLE',
            'error':'Dane dla tej części podsumowania są chwilowo niedostępne.',
            'partial_result':None,
        }
        return respond('Podsumowanie przygotowane z dostępnych danych. Nie udało się pobrać zaległych faktur.')

    calls = tuple(runtime.ToolCall(f'batch-{index}', name, json.dumps(arguments))
                  for index, (name, arguments) in enumerate(reads))
    backend.AGENT_MODEL_PROVIDER = runtime.FakeModelProvider([
        runtime.ProviderResponse(tool_calls=calls, model='fake-model'),
        final_from_partial_data,
    ])
    monkeypatch.setattr(operations, 'execute_business_operation', one_unavailable)
    client = backend.app.test_client()
    with client.session_transaction() as session:
        session['admin_authenticated'] = True
        session['csrf_token'] = 'csrf'

    response = client.post('/api/internal/ai/chat', json={'message':'co mam dziś do zrobienia?'})

    assert response.status_code == 200
    payload = response.get_json()
    assert payload['status'] == 'SUCCESS' and payload['tool_calls'] == 4
    assert 'Nie udało się pobrać zaległych faktur.' in payload['message']


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
    assert result['message']==text.strip()
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
        runtime.ToolCall('a','inventory.summary','{}'),runtime.ToolCall('b','finance.transfer','{}')))])
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
