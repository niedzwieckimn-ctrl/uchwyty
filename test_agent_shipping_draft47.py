"""Real SQLite/BO/approval flow; providers and external side effects are fakes."""
import json
import socket
import urllib.request

import pytest

import agent_conversation
import agent_runtime as runtime
from test_fulfillment_orchestrator import flow, actor, state, b, ops, approvals, rbac


ORDER_IDS = [702, 704, 706, 708]
FIRST_MESSAGE = ('Zamawiam kuriera Impost do tej paczki. Wymiary paczki 45, 20 na 20. '
                 'Powiadomienie SMS i powiadomienie e-mail też zamawiaj.')


def _human():
    return rbac.load_actor_context(rbac.BOOTSTRAP_OWNER_ACTOR_ID)


def _tool(name, arguments, key='tool'):
    return runtime.ProviderResponse(tool_calls=(runtime.ToolCall(key, name, json.dumps(arguments)),))


def _turn(message, responses=(), cid=''):
    provider = runtime.FakeModelProvider(list(responses))
    with b.app.test_request_context():
        b._refresh_domain_route_context()
        result = runtime.run_agent_turn(_human(), message, provider, conversation_id=cid)
    return result, provider


@pytest.fixture
def parcel(flow, monkeypatch):
    def network_forbidden(*args, **kwargs):
        raise AssertionError('network forbidden in parcel acceptance test')
    monkeypatch.setattr(socket, 'create_connection', network_forbidden)
    monkeypatch.setattr(urllib.request, 'urlopen', network_forbidden)
    b.app.secret_key = 'parcel-draft-acceptance'
    now = b.now_iso()
    db = b.conn()
    db.execute('UPDATE order_items SET qty=8 WHERE id=703')
    db.execute('UPDATE stock SET qty=3 WHERE product_id=701')
    db.execute("UPDATE orders SET status='partially_shipped',shipped_at=? WHERE id=702", (now,))
    db.execute("""INSERT INTO invoices(id,order_id,invoice_no,issue_date,sell_date,payment_type,
        total_net,total_gross,created_at) VALUES(801,702,'FV/HISTORICAL','2026-09-01',
        '2026-09-01','transfer',20,24.6,?)""", (now,))
    db.execute("""INSERT INTO invoice_allocations(invoice_id,order_id,order_item_id,product_id,sku,qty,created_at)
        VALUES(801,702,703,701,'ANDRE-128-AB',2,?)""", (now,))
    for order_id, product_id, ordered, available in [(704,710,8,5),(706,711,10,2),(708,712,6,4)]:
        sku = 'PARCEL-' + str(product_id)
        db.execute('INSERT INTO products(id,sku,model,name,created_at) VALUES(?,?,?,?,?)',
                   (product_id,sku,'Fixture','Fixture '+str(product_id),now))
        db.execute('INSERT INTO stock(product_id,qty) VALUES(?,?)', (product_id,available))
        db.execute("""INSERT INTO orders(id,order_no,customer_name,customer_email,customer_address,
            customer_phone,status,currency,created_at) VALUES(?,?,'Magmar','test@example.invalid',
            'Testowa 1, 00-001 Warszawa','501502503','confirmed','PLN',?)""",
                   (order_id,'MAG-'+str(order_id),now))
        db.execute("""INSERT INTO order_items(id,order_id,product_id,sku,qty,unit_net_price,currency,created_at)
            VALUES(?,?,?,?,?,10,'PLN',?)""", (order_id+100,order_id,product_id,sku,ordered,now))
    db.commit()
    db.close()
    generated = []
    def pdf(order, items, metadata, invoice_pdf_path=''):
        path = flow['dir'] / 'actual-four-order-parcel.pdf'
        path.write_bytes(b'%PDF-1.4\nfixture packing list')
        generated.append([dict(item) for item in items])
        return str(path)
    monkeypatch.setattr(b, 'generate_invoice_packing_list_pdf', pdf)
    with b.app.test_request_context():
        b._refresh_domain_route_context()
        preview = ops.execute_business_operation(actor(), 'orders.packing_list.preview',
            {'order_id':702,'packing_scope':'selected','packing_order_ids':ORDER_IDS})
        assert preview.status == 'SUCCESS', preview
        proposal = preview.data['preview']
        assert proposal['order_ids'] == ORDER_IDS and proposal['total_quantity'] == 14
        payload = {'order_id':702,'expected_version':preview.data['state']['expected_version'],
            'idempotency_key':'fixture-four-order-parcel', 'packing_scope':'selected',
            'packing_order_ids':ORDER_IDS, 'packing_scope_fingerprint':proposal['fingerprint'],
            'packing_items':proposal['approval_items'],'total_quantity':proposal['total_quantity']}
        pending = ops.execute_business_operation(actor(),'orders.packing_list.generate',payload)
        assert pending.status == 'PENDING_APPROVAL', pending
        approvals.approve_request(pending.approval_id,_human())
        packed = ops.execute_business_operation(actor(),'orders.packing_list.generate',payload,
                                               approval_id=pending.approval_id)
        assert packed.status == 'SUCCESS', packed
    current = state()
    assert current['package']['order_ids'] == ORDER_IDS
    assert current['package']['packed_quantity'] == 14
    assert current['package']['ordered_quantity'] == 32
    assert current['package']['remaining_quantity'] == 16
    assert current['package']['ready_for_shipment'] is True
    assert len(generated) == 1
    return {**flow, 'state':current, 'generated':generated}


def _start():
    result, _ = _turn(FIRST_MESSAGE, [
        _tool('orders.fulfillment.state', {'order_id':702}),
        runtime.ProviderResponse(text='Do tej paczki brakuje tylko masy w kilogramach.')])
    assert result['status'] == 'SUCCESS', result
    return result['conversation_id']


def test_four_order_actual_parcel_weight_followup_prepares_one_approval(parcel):
    cid = _start()
    # A newer order card is not a new parcel selection.
    card, _ = _turn('Pokaż zamówienie MAG-708.', [
        _tool('orders.get', {'id':708}), runtime.ProviderResponse(text='Oto zamówienie.')], cid)
    assert card['status'] == 'SUCCESS', card
    result, provider = _turn('Pięć kilogramów.', cid=cid)
    assert result['status'] == 'SUCCESS', result
    assert provider.calls == []
    assert result['tool_calls'] == 3
    assert len(result['pending_approvals']) == 1
    pending = result['pending_approvals'][0]
    assert pending['operation'] == 'shipping.shipment.create' and pending['order_id'] == 702
    assert '14 szt.' in result['message'] and '4 zamówienia' in result['message']
    assert '5 kg' in result['message']
    current = state()
    assert current['package']['packing_list_id'] == parcel['state']['package']['packing_list_id']
    assert current['package']['batch_id'] == parcel['state']['package']['batch_id']
    assert current['package']['order_ids'] == ORDER_IDS
    assert current['requirements']['known'] == {
        'carrier':'inpost','length':45.0,'width':20.0,'height':20.0,'dimension_unit':'cm',
        'sms':True,'email':True,'weight':5.0,'weight_unit':'kg','weight_source':'manual'}
    assert parcel['calls'] == [] and parcel['pickup'] == []
    repeated, again_provider = _turn('5 kg', cid=cid)
    assert repeated['status'] == 'SUCCESS' and again_provider.calls == []
    assert repeated['pending_approvals'][0]['approval_id'] == pending['approval_id']
    assert len(parcel['generated']) == 1 and parcel['calls'] == []
    db = b.conn()
    assert db.execute("SELECT COUNT(*) FROM internal_approval_requests WHERE operation='shipping.shipment.create'").fetchone()[0] == 1
    assert db.execute('SELECT COUNT(*) FROM invoices').fetchone()[0] == 1
    db.close()


def test_pending_parameter_change_keeps_business_requirements_unchanged(parcel):
    cid = _start()
    first, _ = _turn('5 kg', cid=cid)
    assert first['status'] == 'SUCCESS', first
    changed, provider = _turn('6 kg', cid=cid)
    assert changed['status'] == 'CONFLICT' and changed['error_code'] == 'PENDING_SHIPMENT_CHANGED', changed
    assert provider.calls == [] and parcel['calls'] == []
    assert state()['requirements']['known']['weight'] == 5
    assert changed['pending_approvals'][0]['approval_id'] == first['pending_approvals'][0]['approval_id']


def test_explicit_other_parcel_drops_old_draft_before_interpreting_weight(parcel):
    cid = _start()
    result, provider = _turn('Zamawiam kuriera do MAG-999. Waga 5 kg.',
        [runtime.ProviderResponse(text='Najpierw wskaż właściwą paczkę.')],cid)
    assert result['status'] == 'SUCCESS' and len(provider.calls) == 1
    assert result['tool_calls'] == 0 and result['pending_approvals'] == []
    result, provider = _turn('Pięć kilogramów.',
        [runtime.ProviderResponse(text='Do której paczki przypisać masę?')],cid)
    assert result['tool_calls'] == 0 and len(provider.calls) == 1
    assert state()['requirements']['known'] == {} and parcel['calls'] == []


def test_natural_rejection_reaches_existing_human_approval_gate(parcel):
    cid = _start()
    first, _ = _turn('5 kg',cid=cid)
    aid = first['pending_approvals'][0]['approval_id']
    result, provider = _turn('Odrzuć zamówienie kuriera.', [
        _tool('approval.decide',{'approval_id':aid,'decision':'reject'}),
        runtime.ProviderResponse(text='Odrzucono propozycję zamówienia kuriera.')],cid)
    assert result['status'] == 'SUCCESS', result
    assert len(provider.calls) == 2 and result['decisions'] == [{'approval_id':aid,'decision':'reject'}]
    assert approvals.get_request_snapshot(aid)['status'] == 'REJECTED'
    assert parcel['calls'] == []
    followup, provider = _turn('5 kg', [runtime.ProviderResponse(text='Masa jest zachowana. Propozycja została odrzucona.')],cid)
    assert followup['status'] == 'SUCCESS' and followup['pending_approvals'] == []
    assert len(provider.calls) == 1 and parcel['calls'] == []


def test_new_or_reset_conversation_does_not_inherit_parcel_parameters(parcel):
    cid = _start()
    result, provider = _turn('Pięć kilogramów.', [runtime.ProviderResponse(text='Do której paczki przypisać masę?')])
    assert result['status'] == 'SUCCESS' and len(provider.calls) == 1
    assert result['tool_calls'] == 0 and result['pending_approvals'] == []
    agent_conversation.reset_conversation(_human(), actor(), cid)
    result, provider = _turn('Pięć kilogramów.', [runtime.ProviderResponse(text='Do której paczki przypisać masę?')],cid)
    assert result['status'] == 'SUCCESS' and len(provider.calls) == 1
    assert result['tool_calls'] == 0 and result['pending_approvals'] == []
    assert parcel['calls'] == []


def test_changed_actual_parcel_scope_blocks_write_after_weight(parcel, monkeypatch):
    cid = _start()
    original = ops._HANDLERS['orders.fulfillment.state']
    def moved(*args, **kwargs):
        result = original(*args, **kwargs)
        result['state']['package']['batch_id'] += 1000
        return result
    monkeypatch.setitem(ops._HANDLERS, 'orders.fulfillment.state', moved)
    result, provider = _turn('Pięć kilogramów.', cid=cid)
    assert result['status'] == 'DENIED' and result['error_code'] == 'PARCEL_SCOPE_CHANGED', result
    assert result['tool_calls'] == 1 and provider.calls == []
    assert result['pending_approvals'] == [] and parcel['calls'] == []


def test_approved_parcel_executes_one_fake_shipment_and_never_issues_stock(parcel):
    cid = _start()
    result, _ = _turn('Pięć kilogramów.', cid=cid)
    assert result['status'] == 'SUCCESS', result
    pending = result['pending_approvals'][0]
    snapshot = approvals.get_request_snapshot(pending['approval_id'])
    payload = json.loads(snapshot['safe_payload'])
    db = b.conn()
    before_stock = [tuple(row) for row in db.execute('SELECT product_id,qty FROM stock ORDER BY product_id')]
    db.close()
    with b.app.test_request_context():
        approvals.approve_request(pending['approval_id'],_human())
        executed = ops.execute_business_operation(actor(), 'shipping.shipment.create', payload,
                                                  approval_id=pending['approval_id'])
        assert executed.status == 'SUCCESS', executed
        repeated = ops.execute_business_operation(actor(), 'shipping.shipment.create', payload,
                                                  approval_id=pending['approval_id'])
        assert repeated.status == 'SUCCESS', repeated
    assert len(parcel['calls']) == 1
    assert parcel['pickup'] == []
    db = b.conn()
    assert [tuple(row) for row in db.execute('SELECT product_id,qty FROM stock ORDER BY product_id')] == before_stock
    assert [row[0] for row in db.execute('SELECT DISTINCT inpost_shipment_id FROM orders WHERE id IN (702,704,706,708)')] == ['123456']
    db.close()
    assert state()['package']['order_ids'] == ORDER_IDS


def test_timeout_followup_refreshes_provider_result_without_second_shipment_post(parcel, monkeypatch):
    import inpost_module
    cid = _start()
    prepared, _ = _turn('5 kg',cid=cid)
    pending = prepared['pending_approvals'][0]
    payload = json.loads(approvals.get_request_snapshot(pending['approval_id'])['safe_payload'])
    calls = []
    def timeout(*args):
        calls.append(args)
        raise TimeoutError('accepted but response lost')
    monkeypatch.setattr(b,'create_courier_shipment',timeout)
    with b.app.test_request_context():
        approvals.approve_request(pending['approval_id'],_human())
        result = ops.execute_business_operation(actor(),'shipping.shipment.create',payload,
                                                approval_id=pending['approval_id'])
    assert result.status == 'FAILED', result
    assert len(calls) == 1 and state()['shipment']['attempt_state'] == 'UNKNOWN'
    monkeypatch.setattr(inpost_module,'find_shipment_by_reference',
                        lambda *args:{'id':123456,'tracking_number':'TRACK-TEST'})
    recovered, provider = _turn('5 kg',cid=cid)
    assert recovered['status'] == 'SUCCESS', recovered
    assert provider.calls == [] and len(calls) == 1
    assert state()['shipment']['exists'] is True
    assert [item['operation'] for item in recovered['pending_approvals']] == ['shipping.pickup.request']
    assert parcel['pickup'] == []


@pytest.mark.parametrize('receipt', ['redirect', 'missing_id', 'not_saved', 'partial_save'])
def test_shipping_requires_provider_receipt_saved_for_every_approved_member(parcel, monkeypatch, receipt):
    from types import SimpleNamespace
    cid = _start()
    prepared, _ = _turn('5 kg', cid=cid)
    pending = prepared['pending_approvals'][0]
    payload = json.loads(approvals.get_request_snapshot(pending['approval_id'])['safe_payload'])

    def fake_service(*args, **kwargs):
        if receipt == 'redirect':
            return SimpleNamespace(status_code=302)
        if receipt == 'missing_id':
            return {'ok': True}
        if receipt == 'partial_save':
            db = b.conn()
            db.execute("UPDATE orders SET inpost_shipment_id='123456' WHERE id=702")
            db.commit()
            db.close()
        return {'ok': True, 'shipment_id': '123456'}

    monkeypatch.setattr(b, 'order_inpost_create_service', fake_service)
    with b.app.test_request_context():
        approvals.approve_request(pending['approval_id'], _human())
        result = ops.execute_business_operation(actor(), 'shipping.shipment.create', payload,
                                                approval_id=pending['approval_id'])
    assert result.status == 'FAILED', result
    assert result.error_code == ('SHIPMENT_NOT_CONFIRMED' if receipt in ('redirect', 'missing_id')
                                 else 'SHIPMENT_PERSISTENCE_UNCONFIRMED')
    assert parcel['calls'] == [] and parcel['pickup'] == []


def test_structured_shipping_blocks_unfinished_invoice_of_current_parcel(parcel):
    from fulfillment_operations import request_view
    db = b.conn()
    db.execute('UPDATE packing_batches SET invoice_id=801 WHERE id=?', (parcel['state']['package']['batch_id'],))
    db.execute("UPDATE invoices SET publication_state='preparing' WHERE id=801")
    db.commit()
    db.close()
    with b.app.test_request_context():
        b._refresh_domain_route_context()
        result = b.order_inpost_create_service(702, request=request_view(), structured=True)
    assert isinstance(result, dict) and result['ok'] is False, result
    assert 'faktur' in result['error'].lower()
    assert parcel['calls'] == [] and parcel['pickup'] == []


def test_full_parcel_dialogue_from_discovery_through_shipment_and_single_pickup(parcel):
    discovered, _ = _turn('Czy mam jakąś spakowaną paczkę, ale niewysłaną?', [
        _tool('orders.fulfillment.state', {'order_id':702}),
        runtime.ProviderResponse(text='Potwierdzona paczka obejmuje cztery zamówienia i 14 spakowanych sztuk.')])
    assert discovered['status'] == 'SUCCESS', discovered
    cid = discovered['conversation_id']
    detailed, _ = _turn('Podaj szczegóły', [_tool('orders.get',{'id':708}),
        runtime.ProviderResponse(text='To ostatnie zamówienie w tej samej paczce.')],cid)
    assert detailed['status'] == 'SUCCESS', detailed
    listing, provider = _turn('Pokaż listę pakową.', cid=cid)
    assert listing['status'] == 'SUCCESS' and provider.calls == [], listing
    assert listing['tool_calls'] == 2  # fresh exact package + its LP
    assert state()['package']['order_ids'] == ORDER_IDS
    dimensions, provider = _turn(FIRST_MESSAGE,cid=cid)
    assert dimensions['status'] == 'SUCCESS' and provider.calls == [], dimensions
    assert 'masę' in dimensions['message'] and dimensions['pending_approvals'] == []
    assert state()['requirements']['known'] == {}  # only a conversational draft yet
    prepared, provider = _turn('Pięć kilogramów.',cid=cid)
    assert prepared['status'] == 'SUCCESS' and provider.calls == [], prepared
    assert len(prepared['pending_approvals']) == 1
    assert parcel['calls'] == [] and parcel['pickup'] == []

    def approve(pending):
        payload = json.loads(approvals.get_request_snapshot(pending['approval_id'])['safe_payload'])
        with b.app.test_request_context():
            approvals.approve_request(pending['approval_id'],_human())
            result = ops.execute_business_operation(actor(),pending['operation'],payload,
                                                    approval_id=pending['approval_id'])
        assert result.status == 'SUCCESS', result

    approve(prepared['pending_approvals'][0])
    assert len(parcel['calls']) == 1 and parcel['pickup'] == []
    pickup, _ = _turn('5 kg',cid=cid)
    assert [item['operation'] for item in pickup['pending_approvals']] == ['shipping.pickup.request']
    approve(pickup['pending_approvals'][0])
    assert parcel['pickup'] == ['123456']
    repeated, provider = _turn('5 kg',cid=cid)
    assert repeated['status'] == 'SUCCESS' and provider.calls == [], repeated
    assert repeated['pending_approvals'] == [] and len(parcel['calls']) == 1
    assert parcel['pickup'] == ['123456'] and 'kolejce' in repeated['message']
