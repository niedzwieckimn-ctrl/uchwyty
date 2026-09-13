import copy
import json
import uuid
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest
import agent_runtime as runtime
import invoice_numbering
import reconciliation_store
from test_fulfillment_orchestrator import flow, actor, state, run, success, docs, requirements, b, f, ops, approvals, rbac


def human():
    return rbac.load_actor_context(rbac.BOOTSTRAP_OWNER_ACTOR_ID)


def tool(name, payload):
    return runtime.ProviderResponse(tool_calls=(runtime.ToolCall(str(uuid.uuid4()), name, json.dumps(payload)),))


def turn(message, replies, cid=''):
    with b.app.test_request_context():
        return runtime.run_agent_turn(human(), message, runtime.FakeModelProvider(replies), conversation_id=cid)


def proposal(cid=''):
    payload = {'order_id': 702, 'expected_version': state()['expected_version'], 'idempotency_key': str(uuid.uuid4())}
    return turn('Przygotuj listę pakową.', [tool('orders.packing_list.generate', payload), runtime.ProviderResponse(text='Potrzebna jest zgoda.')], cid)


def test_natural_human_approval_uses_existing_engine(flow):
    first = proposal()
    assert first['status'] == 'SUCCESS', first
    aid = first['pending_approvals'][0]['approval_id']
    result = turn('Zatwierdzam.', [tool('approval.decide', {'approval_id': aid, 'decision': 'approve'}), runtime.ProviderResponse(text='Lista została przygotowana.')], first['conversation_id'])
    assert result['status'] == 'SUCCESS', result
    assert approvals.get_request_snapshot(aid)['status'] == 'CONSUMED'
    assert state()['packing_list']['current']


def test_ai_cannot_approve_and_two_pending_are_ambiguous(flow):
    first = proposal(); cid = first['conversation_id']
    second = proposal(cid)
    aid = first['pending_approvals'][0]['approval_id']
    with b.app.test_request_context():
        denied = ops.execute_business_operation(actor(), 'approval.decide', {'approval_id': aid, 'decision': 'approve'})
    assert denied.status == 'DENIED'
    turn('Zatwierdzam.', [tool('approval.decide', {'approval_id': aid, 'decision': 'approve'}), runtime.ProviderResponse(text='Wybierz decyzję.')], cid)
    assert approvals.get_request_snapshot(aid)['status'] == 'PENDING'
    assert approvals.get_request_snapshot(second['pending_approvals'][0]['approval_id'])['status'] == 'PENDING'


def test_preflight_closed_order_never_creates_approval(flow):
    c = b.conn(); c.execute("UPDATE orders SET status='shipped' WHERE id=702"); c.commit(); c.close()
    result = run('orders.packing_list.generate', approve=False)
    assert result.error_code == 'ORDER_CLOSED'
    assert not result.approval_id
    c = b.conn(); assert c.execute('SELECT COUNT(*) FROM internal_approval_requests').fetchone()[0] == 0; c.close()


def test_number_gap_deleted_highest_and_parallel_reservations(flow):
    docs()
    c = b.conn()
    c.execute("UPDATE invoices SET invoice_no='FVAT 8/09/2026'")
    invoice_numbering.initialize(c)
    c.commit(); c.close()
    assert invoice_numbering.reserve(b, '2026-09-13') == 'FVAT 9/09/2026'
    c = b.conn(); c.execute('DELETE FROM invoices'); c.commit(); c.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: invoice_numbering.reserve(b, '2026-09-13'), range(2)))
    assert set(results) == {'FVAT 10/09/2026', 'FVAT 11/09/2026'}


def test_partial_invoice_outcome_and_retry(flow, monkeypatch):
    success('orders.packing_list.generate')
    original = b.resume_invoice_job
    monkeypatch.setattr(b, 'resume_invoice_job', lambda iid: (_ for _ in ()).throw(RuntimeError('PDF failure')))
    result = run('orders.invoice.create')
    assert result.status == 'FAILED'
    assert result.data['state']['invoices'][0]['record_created']
    assert result.data['state']['invoices'][0]['number_assigned']
    monkeypatch.setattr(b, 'resume_invoice_job', original)
    success('orders.invoice.create')
    assert len(state()['invoices']) == 1 and state()['invoices'][0]['pdf_available']


def test_recount_preserves_history_and_requires_new_adjustment(flow):
    session = str(uuid.uuid4())
    def count(quantity, version):
        return ops.execute_business_operation(actor(), 'inventory.count.record', {'product_id': 701, 'count_session_id': session,
            'counted_quantity': quantity, 'expected_version': version, 'idempotency_key': str(uuid.uuid4())})
    first = count(4, 0)
    assert first.status == 'SUCCESS', first
    payload = {'product_id': 701, 'count_session_id': session, 'expected_version': first.data['version'], 'idempotency_key': 'adjust-four'}
    pending = ops.execute_business_operation(actor(), 'inventory.adjust', payload)
    approvals.approve_request(pending.approval_id, human())
    adjusted = ops.execute_business_operation(actor(), 'inventory.adjust', payload, approval_id=pending.approval_id)
    assert adjusted.status == 'SUCCESS'
    latest = count(1, adjusted.data['version'])
    assert latest.status == 'SUCCESS', latest
    assert latest.data['expected_quantity'] == 4 and latest.data['difference'] == -3
    c = b.conn()
    assert c.execute('SELECT qty FROM stock WHERE product_id=701').fetchone()[0] == 4
    assert [r[0] for r in c.execute('SELECT status FROM internal_inventory_count_items ORDER BY item_id')] == ['SUPERSEDED', 'PENDING_ADJUSTMENT']
    c.close()
    new = ops.execute_business_operation(actor(), 'inventory.adjust', dict(payload, expected_version=latest.data['version'], idempotency_key='adjust-one'))
    assert new.status == 'PENDING_APPROVAL' and new.approval_id != pending.approval_id


def read(name):
    with b.app.test_request_context():
        result = ops.execute_business_operation(actor(), name, {'order_id': 702})
    assert result.status == 'SUCCESS', result
    return result.data['preview']


def legacy_docs():
    docs()
    c = b.conn(); c.execute('DELETE FROM fulfillment_documents'); c.execute('DELETE FROM fulfillment_document_intents'); c.commit(); c.close()


def test_legacy_document_adoption_does_not_regenerate(flow, monkeypatch):
    legacy_docs()
    proof = read('orders.documents.adoption.preview')
    assert proof['status'] == 'SAFE', proof
    monkeypatch.setattr(b, 'generate_order_invoice_pdf', lambda *a: pytest.fail('regeneration forbidden'))
    success('orders.documents.adopt', preview_fingerprint=proof['fingerprint'])
    assert state()['invoice']['current'] and state()['packing_list']['current']
    assert len(state()['invoices']) == 1


def test_legacy_document_mismatch_blocked(flow):
    legacy_docs()
    c = b.conn(); c.execute('UPDATE order_items SET qty=3 WHERE id=703'); c.commit(); c.close()
    assert read('orders.documents.adoption.preview')['status'] == 'CONFLICT'


def test_legacy_shipment_adoption_uses_get_only(flow, monkeypatch):
    docs(); requirements()
    c = b.conn(); c.execute("UPDATE orders SET inpost_shipment_id='123456',carrier='inpost' WHERE id=702"); c.commit(); c.close()
    proof = read('shipping.shipment.adoption.preview')
    assert proof['status'] == 'SAFE', proof
    success('shipping.shipment.adopt', preview_fingerprint=proof['fingerprint'])
    assert not flow['calls']
    assert not state()['shipment']['parameters_need_review']
    monkeypatch.setattr(b, 'inpost_get_shipment', lambda sid: {'id': sid, 'receiver': {'phone': '000000000'}})
    assert read('shipping.shipment.adoption.preview')['status'] == 'CONFLICT'


def test_capabilities_are_discovered_without_provider_instruction(flow):
    with b.app.test_request_context():
        result = ops.execute_business_operation(actor(), 'shipping.capabilities', {})
    assert result.status == 'SUCCESS', result
    assert result.data['capabilities'][0]['provider'] == 'inpost'
    assert 'InPost' not in runtime.SYSTEM_INSTRUCTIONS and 'China' not in runtime.SYSTEM_INSTRUCTIONS
    names = {t['name'] for t in runtime._tool_descriptors(actor(), human())}
    assert {'shipping.capabilities', 'orders.summary', 'orders.fulfillment.readiness'} <= names


@pytest.mark.parametrize('reset_metadata', [True, False])
def test_reconciliation_metadata_and_pdf_survive_local_reset(flow, monkeypatch, reset_metadata):
    legacy_docs()
    proof = read('orders.documents.adoption.preview')
    success('orders.documents.adopt', preview_fingerprint=proof['fingerprint'])
    cloud = {}; calls = []
    def remote(path, method='GET', payload=None, **kw):
        calls.append(method)
        if method == 'GET':
            return [copy.deepcopy(cloud)] if cloud else []
        assert payload['p_expected_revision'] == cloud.get('revision', 0)
        cloud.update(revision=cloud.get('revision', 0) + 1, payload=copy.deepcopy(payload['p_payload']))
        return {'saved': True, 'revision': cloud['revision']}
    monkeypatch.setattr(b, 'supabase_enabled', lambda: True)
    monkeypatch.setattr(b, 'supabase_request', remote)
    reconciliation_store.publish(b, 702)
    c = b.conn()
    for row in c.execute('SELECT path FROM fulfillment_documents'):
        Path(row[0]).unlink()
    if reset_metadata:
        for table in list(reconciliation_store.TABLES) + ['fulfillment_reconciliation_versions']:
            c.execute('DELETE FROM ' + table)
    c.commit(); c.close()
    calls.clear()
    result = state()
    assert result['invoice']['current'] and result['packing_list']['current']
    assert set(calls) == {'GET'}


def prepare_combined():
    docs(); requirements()
    c = b.conn()
    c.execute("INSERT INTO orders(id,order_no,customer_name,customer_email,customer_address,customer_phone,status,currency,created_at) SELECT 802,'MAG-802',customer_name,customer_email,customer_address,customer_phone,'confirmed',currency,created_at FROM orders WHERE id=702")
    c.execute("INSERT INTO order_items(id,order_id,product_id,sku,qty,unit_net_price,currency,created_at) SELECT 803,802,product_id,sku,2,unit_net_price,currency,created_at FROM order_items WHERE id=703")
    c.commit(); c.close()
    for name in ('orders.packing_list.generate', 'orders.invoice.create'):
        with b.app.test_request_context():
            b._refresh_domain_route_context()
            s = f.state({'order_id': 802})['state']
            payload = {'order_id': 802, 'expected_version': s['expected_version'], 'idempotency_key': str(uuid.uuid4())}
            result = ops.execute_business_operation(actor(), name, payload)
            assert result.status == 'PENDING_APPROVAL', result
            approvals.approve_request(result.approval_id, human())
            result = ops.execute_business_operation(actor(), name, payload, approval_id=result.approval_id)
            assert result.status == 'SUCCESS', result
    c = b.conn(); c.execute("UPDATE orders SET packed_at='2026-09-13T12:00:00' WHERE id IN (702,802)"); c.commit(); c.close()


def test_combined_package_and_all_documents(flow, monkeypatch):
    from io import BytesIO
    from pypdf import PdfWriter, PdfReader
    prepare_combined()
    s = state()
    assert s['package']['order_ids'] == [702, 802]
    pending = run('shipping.shipment.create', approve=False)
    snap = approvals.get_request_snapshot(pending.approval_id)
    assert json.loads(snap['safe_payload'])['expected_version'] == s['expected_version']
    assert json.loads(snap['safe_payload'])['package_fingerprint'] == s['package']['fingerprint']
    success('shipping.shipment.create')
    assert len(flow['calls']) == 1
    c = b.conn(); assert {r[0] for r in c.execute('SELECT inpost_shipment_id FROM orders')} == {'123456'}; c.close()
    writer = PdfWriter(); writer.add_blank_page(width=100, height=100); pdf = BytesIO(); writer.write(pdf)
    monkeypatch.setattr(b, 'inpost_get_label', lambda *a: pdf.getvalue())
    success('shipping.shipment.refresh')
    with b.app.test_request_context():
        result = ops.execute_business_operation(actor(), 'orders.documents.print_ready', {'order_id': 702})
        assert result.status == 'SUCCESS', result
        assert result.data['documents'][0]['document_type'] == 'package'
        stream = f.package_pdf({'order_id': 702}, human())
    assert len(PdfReader(stream).pages) >= 5


def test_combined_member_change_invalidates_approval(flow):
    prepare_combined()
    payload = {'order_id': 702, 'expected_version': state()['expected_version'], 'package_fingerprint': state()['package']['fingerprint'], 'idempotency_key': 'combined-stale'}
    with b.app.test_request_context():
        pending = ops.execute_business_operation(actor(), 'shipping.shipment.create', payload)
        assert pending.status == 'PENDING_APPROVAL', pending
        approvals.approve_request(pending.approval_id, human())
        c = b.conn(); c.execute("UPDATE orders SET note='changed member' WHERE id=802"); c.commit(); c.close()
        result = ops.execute_business_operation(actor(), 'shipping.shipment.create', payload, approval_id=pending.approval_id)
    assert result.status == 'CONFLICT', result
    assert not flow['calls']


def test_natural_approval_rechecks_stale_order(flow):
    first = proposal(); aid = first['pending_approvals'][0]['approval_id']
    c = b.conn(); c.execute("UPDATE orders SET status='shipped' WHERE id=702"); c.commit(); c.close()
    turn('Zatwierdzam.', [tool('approval.decide', {'approval_id': aid, 'decision': 'approve'}), runtime.ProviderResponse(text='Stan zamówienia się zmienił.')], first['conversation_id'])
    assert approvals.get_request_snapshot(aid)['status'] == 'PENDING'
    assert not state()['packing_list']['current']


def test_shipment_missing_parameters_can_be_completed(flow):
    docs()
    c = b.conn(); c.execute("UPDATE orders SET inpost_shipment_id='123456',carrier='inpost' WHERE id=702"); c.commit(); c.close()
    proof = read('shipping.shipment.adoption.preview')
    assert 'weight' in proof['missing_fields']
    requirements()
    proof = read('shipping.shipment.adoption.preview')
    assert proof['missing_fields'] == []
    success('shipping.shipment.adopt', preview_fingerprint=proof['fingerprint'])
    assert not flow['calls']


def test_recount_invalidates_unconsumed_old_approval(flow):
    session = str(uuid.uuid4())
    def count(n, version):
        return ops.execute_business_operation(actor(), 'inventory.count.record', {'product_id': 701, 'count_session_id': session,
            'counted_quantity': n, 'expected_version': version, 'idempotency_key': str(uuid.uuid4())})
    first = count(4, 0)
    payload = {'product_id': 701, 'count_session_id': session, 'expected_version': first.data['version'], 'idempotency_key': 'old-count'}
    pending = ops.execute_business_operation(actor(), 'inventory.adjust', payload)
    second = count(1, first.data['version'])
    assert second.status == 'SUCCESS'
    approvals.approve_request(pending.approval_id, human())
    result = ops.execute_business_operation(actor(), 'inventory.adjust', payload, approval_id=pending.approval_id)
    assert result.status == 'CONFLICT'
    c = b.conn(); assert c.execute('SELECT qty FROM stock WHERE product_id=701').fetchone()[0] == 100; c.close()


@pytest.mark.parametrize('accepted', [False, True])
def test_metadata_timeout_never_reports_unconfirmed_persistence(flow, monkeypatch, accepted):
    docs()
    cloud = {}; available = [False]
    def remote(path, method='GET', payload=None, **kw):
        if method == 'GET':
            return [copy.deepcopy(cloud)] if cloud else []
        if accepted or available[0]:
            cloud.update(revision=payload['p_expected_revision']+1, payload=copy.deepcopy(payload['p_payload']))
        if not available[0]:
            raise TimeoutError('metadata response lost')
        return {'saved': True, 'revision': cloud['revision']}
    monkeypatch.setattr(b, 'supabase_enabled', lambda: True)
    monkeypatch.setattr(b, 'supabase_request', remote)
    with pytest.raises(TimeoutError):
        reconciliation_store.publish(b, 702)
    s = state()
    if accepted:
        assert s['persistence']['durable']
    else:
        assert s['next_step'] == 'reconcile_metadata' and not s['persistence']['durable']
        assert run('shipping.requirements.update', weight=3).error_code == 'RECONCILIATION_PENDING'
        available[0] = True
        success('orders.fulfillment.reconcile')
        assert state()['persistence']['durable']


def test_legacy_price_change_blocks_adoption(flow):
    legacy_docs()
    c = b.conn(); c.execute('UPDATE order_items SET unit_net_price=999 WHERE id=703'); c.commit(); c.close()
    assert read('orders.documents.adoption.preview')['status'] == 'CONFLICT'


def test_explicit_choice_of_one_pending_decision(flow):
    first = proposal(); second = proposal(first['conversation_id'])
    aid = first['pending_approvals'][0]['approval_id']
    turn('Zatwierdzam pierwszą z tych dwóch decyzji.', [tool('approval.decide', {'approval_id': aid, 'decision': 'approve', 'selected_explicitly': True}),
         runtime.ProviderResponse(text='Decyzja zapisana.')], first['conversation_id'])
    assert approvals.get_request_snapshot(aid)['status'] == 'CONSUMED'
    assert approvals.get_request_snapshot(second['pending_approvals'][0]['approval_id'])['status'] == 'PENDING'


def test_new_approval_cannot_be_consumed_in_same_model_turn(flow):
    payload = {'order_id': 702, 'expected_version': state()['expected_version'], 'idempotency_key': 'same-turn'}
    def self_approve(kwargs):
        output = json.loads(kwargs['input_items'][-1]['output'])
        return tool('approval.decide', {'approval_id': output['approval_id'], 'decision': 'approve'})
    result = turn('Przygotuj listę.', [tool('orders.packing_list.generate', payload), self_approve, runtime.ProviderResponse(text='Oczekuje na decyzję.')])
    assert result['status'] == 'SUCCESS', result
    assert not state()['packing_list']['current']
    assert approvals.get_request_snapshot(result['pending_approvals'][0]['approval_id'])['status'] == 'PENDING'
