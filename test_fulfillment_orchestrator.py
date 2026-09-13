import json
import uuid
from pathlib import Path

import pytest
import app as b
import business_operations as ops
import fulfillment_operations as f
import internal_rbac as rbac
import internal_approval as approvals


@pytest.fixture
def flow(tmp_path, monkeypatch):
    monkeypatch.setattr(b, 'DB_PATH', str(tmp_path / 'flow.db'))
    monkeypatch.setattr(b, 'DATA_DIR', str(tmp_path))
    monkeypatch.setattr(b, 'supabase_enabled', lambda: False)
    monkeypatch.setattr(b, 'maybe_pull_shared_from_supabase', lambda **kw: None)
    monkeypatch.setattr(ops, '_freshness_provider', None)
    monkeypatch.setattr(ops, '_write_success_observer', None)
    monkeypatch.setattr(b, '_client_profile_for_email', lambda email: {'name': 'Magmar', 'nip': '1234567890', 'address': 'Testowa 1, 00-001 Warszawa', 'phone': '501502503'})
    monkeypatch.setattr(b, '_send_orders_packed_email', lambda *a, **kw: {'ok': True})
    monkeypatch.setattr(b, 'inpost_config_summary', lambda: {'configured': True, 'missing': []})
    pickup = []
    monkeypatch.setattr(b, 'enqueue_automatic_inpost_pickup', lambda sid: pickup.append(sid))
    monkeypatch.setattr(b, 'inpost_pickup_status', lambda sid: {'state': 'queued', 'label': 'Oczekuje na podjazd'} if str(sid) in pickup else {})
    calls = []
    def create(receiver, parcel, reference, service, options):
        calls.append((receiver, parcel, reference, service, options))
        return {'id': 123456, 'tracking_number': 'TRACK-TEST', 'status': 'confirmed'}
    monkeypatch.setattr(b, 'create_courier_shipment', create)
    monkeypatch.setattr(b, 'inpost_get_shipment', lambda sid: {'id': sid, 'tracking_number': 'TRACK-TEST', 'status': 'confirmed'})
    monkeypatch.setattr(b, 'inpost_get_label', lambda *a: b'%PDF-1.4\nlabel test')
    b.init_db()
    c = b.conn()
    c.execute("INSERT INTO products(id,sku,model,name,created_at) VALUES(701,'ANDRE-128-AB','Andre','Andre 128 AB',?)", (b.now_iso(),))
    c.execute('INSERT INTO stock(product_id,qty) VALUES(701,100)')
    c.execute("INSERT INTO orders(id,order_no,customer_name,customer_email,customer_address,customer_phone,status,currency,created_at) VALUES(702,'MAG-702','Magmar','test@example.invalid','Testowa 1, 00-001 Warszawa','501502503','confirmed','PLN',?)", (b.now_iso(),))
    c.execute("INSERT INTO order_items(id,order_id,product_id,sku,qty,unit_net_price,currency,created_at) VALUES(703,702,701,'ANDRE-128-AB',2,10,'PLN',?)", (b.now_iso(),))
    c.commit(); c.close()
    with b.app.test_request_context(): b._refresh_domain_route_context()
    return {'calls': calls, 'pickup': pickup, 'dir': tmp_path}


def actor():
    return rbac.load_actor_context(rbac.AI_OWNER_ASSISTANT_ACTOR_ID, delegated_by_actor_id=rbac.BOOTSTRAP_OWNER_ACTOR_ID)


def state():
    with b.app.test_request_context():
        result = ops.execute_business_operation(actor(), 'orders.fulfillment.state', {'order_id': 702})
    assert result.status == 'SUCCESS', result
    return result.data['state']


def run(name, approve=True, **values):
    data = {'order_id': 702, 'expected_version': state()['expected_version'], 'idempotency_key': str(uuid.uuid4()), **values}
    if name == 'shipping.shipment.create' and len(state()['package']['order_ids']) > 1:
        data['package_fingerprint'] = state()['package']['fingerprint']
    with b.app.test_request_context():
        b._refresh_domain_route_context()
        result = ops.execute_business_operation(actor(), name, data)
        if approve and result.status == 'PENDING_APPROVAL':
            approvals.approve_request(result.approval_id, rbac.load_actor_context(rbac.BOOTSTRAP_OWNER_ACTOR_ID))
            result = ops.execute_business_operation(actor(), name, data, approval_id=result.approval_id)
    return result


def success(name, **values):
    result = run(name, **values)
    assert result.status == 'SUCCESS', result
    return result.data['state']


def docs():
    success('orders.packing_list.generate')
    success('orders.invoice.create')


def remote_invoice_pdf(monkeypatch):
    stored = {}

    def upload(invoice_id, invoice_no, invoice_pdf_path, packing_pdf_path=''):
        reference = f'supabase://invoices/{invoice_id}/invoice.pdf'
        stored[reference] = Path(invoice_pdf_path).read_bytes()
        return reference

    monkeypatch.setattr(b, 'upload_invoice_pdfs_to_supabase', upload)
    monkeypatch.setattr(b, 'supabase_storage_download_bytes', lambda reference: (stored[reference], 'invoice.pdf'))


def test_fresh_remote_invoice_is_current_after_create(flow, monkeypatch):
    remote_invoice_pdf(monkeypatch)
    success('orders.packing_list.generate')
    success('orders.invoice.create')

    assert state()['invoice']['current'] is True


def test_invoice_is_stale_after_order_changes(flow, monkeypatch):
    remote_invoice_pdf(monkeypatch)
    success('orders.packing_list.generate')
    success('orders.invoice.create')
    db = b.conn()
    db.execute('UPDATE order_items SET qty=qty+1 WHERE id=703')
    db.commit(); db.close()

    assert state()['invoice']['current'] is False


def requirements(**override):
    return success('shipping.requirements.update', carrier='inpost', length=45, width=30, height=15,
        dimension_unit='cm', weight_unit='kg', sms=True, email=True, **({'weight': 3} | override))


def test_normal_fulfillment_real_document_services(flow):
    assert state()['readiness']['complete']
    docs()
    s = state()
    assert s['packing_list']['current'] and s['invoice']['current']
    assert not s['editable']
    requirements()
    success('shipping.shipment.create')
    s = success('shipping.shipment.refresh')
    assert s['label']['current'] and s['shipment']['tracking'] == 'TRACK-TEST'
    assert len(flow['calls']) == 1
    assert flow['calls'][0][1]['length'] == 450
    assert flow['calls'][0][1]['weight'] == 3
    with b.app.test_request_context():
        ready = ops.execute_business_operation(actor(), 'orders.documents.print_ready', {'order_id': 702})
    assert ready.status == 'SUCCESS', ready
    assert len(ready.data['documents']) == 3
    c = b.conn()
    assert c.execute('SELECT qty FROM stock WHERE product_id=701').fetchone()[0] == 98
    c.close()


def test_ready_order_with_stale_invoice_can_create_shipment(flow):
    docs()
    c = b.conn()
    c.execute("UPDATE fulfillment_documents SET content_hash='stale' WHERE order_id=702 AND kind='invoice'")
    c.commit(); c.close()
    requirements()
    assert state()['invoice']['current'] is False

    pending = run('shipping.shipment.create', approve=False)
    assert pending.status == 'PENDING_APPROVAL'
    success('shipping.shipment.create')
    assert len(flow['calls']) == 1


def test_not_ready_order_still_blocks_shipment(flow):
    c = b.conn()
    c.execute('UPDATE stock SET qty=0 WHERE product_id=701')
    c.commit(); c.close()
    requirements()

    result = run('shipping.shipment.create', approve=False)
    assert result.status == 'FAILED' and result.error_code == 'ORDER_NOT_READY'
    assert not result.approval_id
    assert not flow['calls']


def test_only_weight_missing_and_invalid_numbers(flow):
    success('shipping.requirements.update', carrier='inpost', length=45, width=30, height=15, dimension_unit='cm', weight_unit='kg', sms=False, email=False)
    assert state()['requirements']['missing_fields'] == ['weight']
    for weight in (0, -1, True, '3 kg', float('inf')):
        assert run('shipping.requirements.update', weight=weight).status != 'SUCCESS'
    success('shipping.requirements.update', weight=3)
    assert state()['requirements']['missing_fields'] == []


def test_duplicate_shipment_different_keys(flow):
    docs(); requirements()
    success('shipping.shipment.create')
    success('shipping.shipment.create')
    assert len(flow['calls']) == 1


def test_timeout_recovers_without_second_post(flow, monkeypatch):
    import inpost_module
    docs(); requirements()
    calls = []
    def lost(*args):
        calls.append(args)
        raise TimeoutError('accepted but response lost')
    monkeypatch.setattr(b, 'create_courier_shipment', lost)
    result = run('shipping.shipment.create')
    assert result.status == 'FAILED'
    assert state()['shipment']['attempt_state'] == 'UNKNOWN'
    retry = run('shipping.shipment.create')
    assert retry.status == 'FAILED' and retry.error_code == 'SHIPMENT_RECOVERY_REQUIRED' and not retry.approval_id
    assert len(calls) == 1
    monkeypatch.setattr(inpost_module, 'find_shipment_by_reference', lambda *args: {'id': 123456, 'tracking_number': 'TRACK-TEST'})
    success('shipping.shipment.refresh')
    assert state()['shipment']['exists']
    assert len(calls) == 1


def test_partial_invoice_failure_preserves_packing_and_resumes(flow, monkeypatch):
    success('orders.packing_list.generate')
    original = b.resume_invoice_job
    monkeypatch.setattr(b, 'resume_invoice_job', lambda iid: (_ for _ in ()).throw(RuntimeError('simulated PDF upload failure')))
    assert run('orders.invoice.create').status == 'FAILED'
    s = state()
    assert s['packing_list']['current'] and not s['invoice']['current']
    assert run('shipping.shipment.create').status == 'FAILED'
    assert not flow['calls']
    monkeypatch.setattr(b, 'resume_invoice_job', original)
    success('orders.invoice.create')
    assert state()['invoice']['current']
    c = b.conn(); assert c.execute('SELECT COUNT(*) FROM invoices').fetchone()[0] == 1; c.close()


def test_post_invoice_amendment_preserves_shipment_and_stales_docs(flow):
    c = b.conn()
    c.execute("INSERT INTO products(id,sku,model,name,created_at) VALUES(704,'LEO-N29-BB','Leo','Leo N29 BB',?)", (b.now_iso(),))
    c.execute('INSERT INTO stock(product_id,qty) VALUES(704,50)')
    c.commit(); c.close()
    docs(); requirements(); success('shipping.shipment.create'); success('shipping.shipment.refresh')
    old = state()
    iid = old['invoices'][0]['invoice_id']
    import invoice_amendment
    preview = invoice_amendment.preview({'invoice_id': iid})
    args = {'invoice_id': iid, 'expected_version': preview['expected_version'], 'idempotency_key': 'amend-full'}
    with b.app.test_request_context():
        pending = ops.execute_business_operation(actor(), 'invoices.remove', args)
        approvals.approve_request(pending.approval_id, rbac.load_actor_context(rbac.BOOTSTRAP_OWNER_ACTOR_ID))
        result = ops.execute_business_operation(actor(), 'invoices.remove', args, approval_id=pending.approval_id)
        assert result.status == 'SUCCESS', result
    assert state()['editable']
    success('orders.items.add', product_id=701, quantity=6, documents_change_confirmed=True)
    success('orders.items.add', product_id=704, quantity=10, documents_change_confirmed=True)
    s = state()
    assert not s['packing_list']['current'] and not s['invoice']['current']
    assert s['shipment']['exists'] and s['shipment']['parameters_need_review']
    docs()
    success('shipping.shipment.confirm_parameters', human_confirmed=True)
    success('shipping.shipment.refresh')
    with b.app.test_request_context():
        result = ops.execute_business_operation(actor(), 'orders.documents.print_ready', {'order_id': 702})
    assert result.status == 'SUCCESS', result
    assert len(flow['calls']) == 1
    c = b.conn()
    assert c.execute('SELECT qty FROM stock WHERE product_id=701').fetchone()[0] == 92
    assert c.execute('SELECT qty FROM stock WHERE product_id=704').fetchone()[0] == 40
    c.close()


def test_legacy_document_without_provenance_is_not_duplicated(flow):
    docs()
    c = b.conn()
    c.execute('DELETE FROM fulfillment_documents')
    c.execute('DELETE FROM fulfillment_document_intents')
    c.commit(); c.close()
    assert state()['next_step'] == 'review_existing_invoice'
    assert run('orders.invoice.create').status == 'FAILED'
    c = b.conn(); assert c.execute('SELECT COUNT(*) FROM invoices').fetchone()[0] == 1; c.close()


def test_combined_package_fails_closed_before_provider(flow, monkeypatch):
    docs(); requirements()
    expected_version = state()['expected_version']
    original = b._packed_package_orders
    monkeypatch.setattr(b, '_packed_package_orders', lambda cur, order: original(cur, order) + [{'id': 999}])
    with b.app.test_request_context():
        result = ops.execute_business_operation(actor(), 'shipping.shipment.create',
            {'order_id': 702, 'expected_version': expected_version, 'idempotency_key': 'invalid-member'})
    assert result.error_code == 'NOT_FOUND'
    assert not flow['calls']


def test_version_conflict_and_resume_without_conversation(flow):
    before = state()
    success('orders.items.update', item_id=703, quantity=3)
    with b.app.test_request_context():
        result = ops.execute_business_operation(actor(), 'shipping.requirements.update',
            {'order_id': 702, 'expected_version': before['expected_version'], 'idempotency_key': 'old', 'weight': 3})
    assert result.status == 'CONFLICT'
    docs()
    f.configure(b)
    s = state()
    assert s['packing_list']['current'] and s['invoice']['current']
    assert s['next_step'] == 'shipping_requirements'


def test_item_update_remove_reuse_services_without_stock_changes(flow):
    success('orders.items.add', product_id=701, quantity=6)
    c = b.conn()
    added = c.execute('SELECT MAX(id) FROM order_items').fetchone()[0]
    c.close()
    success('orders.items.update', item_id=added, quantity=10)
    success('orders.items.remove', item_id=added)
    c = b.conn()
    assert c.execute('SELECT COUNT(*) FROM order_items').fetchone()[0] == 1
    assert c.execute('SELECT qty FROM stock WHERE product_id=701').fetchone()[0] == 100
    c.close()


def test_packing_edit_requires_confirmation_and_stales_document(flow):
    success('orders.packing_list.generate')
    assert run('orders.items.update', item_id=703, quantity=3).error_code == 'DOCUMENTS_CONFIRMATION_REQUIRED'
    success('orders.items.update', item_id=703, quantity=3, documents_change_confirmed=True)
    assert not state()['packing_list']['current']


def test_file_overwrite_and_missing_label_never_print_ready(flow, monkeypatch):
    docs(); requirements(); success('shipping.shipment.create')
    monkeypatch.setattr(b, 'inpost_get_label', lambda *a: (_ for _ in ()).throw(RuntimeError('label unavailable')))
    assert run('shipping.shipment.refresh').status == 'FAILED'
    assert state()['shipment']['exists'] and not state()['label']['current']
    with b.app.test_request_context():
        result = ops.execute_business_operation(actor(), 'orders.documents.print_ready', {'order_id': 702})
    assert result.error_code == 'STALE_DOCUMENT'
    c = b.conn()
    path = c.execute("SELECT path FROM fulfillment_documents WHERE kind='packing_list'").fetchone()[0]
    c.close()
    Path(path).write_bytes(b'different document')
    assert not state()['packing_list']['current']
    assert len(flow['calls']) == 1


def test_changed_parcel_keeps_shipment_and_requires_human(flow):
    docs(); requirements(); success('shipping.shipment.create'); success('shipping.shipment.refresh')
    success('shipping.requirements.update', weight=4)
    assert state()['shipment']['parameters_need_review']
    pending = run('shipping.shipment.confirm_parameters', approve=False, human_confirmed=True)
    assert pending.error_code == 'PARCEL_CHANGED'
    assert run('shipping.shipment.confirm_parameters', human_confirmed=True).error_code == 'PARCEL_CHANGED'
    with b.app.test_request_context():
        result = ops.execute_business_operation(actor(), 'orders.documents.print_ready', {'order_id': 702})
    assert result.error_code == 'SHIPMENT_REVIEW_REQUIRED'
    assert len(flow['calls']) == 1


def test_recipient_fields_persist_without_changing_customer_or_order(flow, monkeypatch):
    monkeypatch.setattr(b, '_client_profile_for_email', lambda email: {})
    c = b.conn(); c.execute("UPDATE orders SET customer_phone='' WHERE id=702"); c.commit(); c.close()
    assert 'recipient.phone' in state()['requirements']['missing_fields']
    docs(); requirements()
    success('shipping.requirements.update', recipient_phone='600700800')
    assert state()['requirements']['missing_fields'] == []
    success('shipping.shipment.create')
    assert flow['calls'][0][0]['phone'] == '600700800'
    c = b.conn(); assert c.execute('SELECT customer_phone FROM orders WHERE id=702').fetchone()[0] == ''; c.close()


def test_recipient_change_invalidates_shipping_approval(flow, monkeypatch):
    docs(); requirements()
    payload = {'order_id': 702, 'expected_version': state()['expected_version'], 'idempotency_key': 'recipient-race'}
    with b.app.test_request_context():
        pending = ops.execute_business_operation(actor(), 'shipping.shipment.create', payload)
        approvals.approve_request(pending.approval_id, rbac.load_actor_context(rbac.BOOTSTRAP_OWNER_ACTOR_ID))
        monkeypatch.setattr(b, '_client_profile_for_email', lambda email: {'address': 'Inna 2, 00-001 Warszawa', 'phone': '501502503'})
        result = ops.execute_business_operation(actor(), 'shipping.shipment.create', payload, approval_id=pending.approval_id)
    assert result.status == 'CONFLICT'
    assert not flow['calls']


def test_lease_blocks_live_ui_and_recovers_orphan_marker(flow):
    with b.app.test_request_context(), f.ui_write(702):
        assert run('shipping.requirements.update', weight=3).status == 'FAILED'
    c = b.conn(); c.execute("INSERT INTO fulfillment_locks VALUES(702,'dead-process')"); c.commit(); c.close()
    success('shipping.requirements.update', weight=3)


def test_cloud_claim_after_local_loss_never_posts_twice(flow, monkeypatch):
    payload = {'length': 450, 'width': 300, 'height': 150, 'weight': 3}
    cloud = {}
    def rpc(path, method='GET', payload=None, **kw):
        if path.endswith('/fulfillment_reconciliation'):
            return []
        if path.endswith('claim_fulfillment_shipment'):
            if cloud:
                return {'acquired': False, 'claim': dict(cloud)}
            cloud.update(reference=payload['p_reference'], payload=payload['p_payload'], content_hash=payload['p_content_hash'],
                         state='SENDING', provider_json=None, created_at=payload['p_created_at'])
            return {'acquired': True, 'claim': dict(cloud)}
        cloud.update(payload)
    monkeypatch.setattr(b, 'supabase_enabled', lambda: True)
    monkeypatch.setattr(b, 'supabase_request', rpc)
    with b.app.test_request_context():
        first = f.safe_create_shipment(702, f.receiver(f.snapshot(702)['order']), payload, '', 'inpost_courier_standard', {})
        c = b.conn(); c.execute('DELETE FROM fulfillment_shipping_members'); c.execute('DELETE FROM fulfillment_shipping_attempts'); c.commit(); c.close()
        second = f.safe_create_shipment(702, f.receiver(f.snapshot(702)['order']), payload, '', 'inpost_courier_standard', {})
    assert first == second and len(flow['calls']) == 1


def test_pickup_failure_is_not_fulfillment_done(flow, monkeypatch):
    docs(); requirements(); success('shipping.shipment.create'); success('shipping.shipment.refresh')
    monkeypatch.setattr(b, 'inpost_pickup_status', lambda sid: {'state': 'rejected', 'error': 'provider rejected'})
    s = state()
    assert s['next_step'] == 'pickup_review'
    assert not s['shipment']['pickup_confirmed']
    assert s['label']['current']


def test_shipment_requires_human_approval_before_external_call(flow):
    docs(); requirements()
    result = run('shipping.shipment.create', approve=False)
    assert result.status == 'PENDING_APPROVAL'
    assert not flow['calls']


def test_inpost_ui_limits_are_validated(flow):
    for fields in ({'length': 351}, {'width': 241}, {'height': 0.01}, {'weight': 51}, {'weight': 0.001}, {'weight_unit': 'lb'}):
        assert run('shipping.requirements.update', **fields).status != 'SUCCESS'


def test_agent_resume_reads_business_state_and_exposes_only_missing_weight(flow):
    import agent_runtime as runtime
    docs()
    success('shipping.requirements.update', carrier='inpost', length=45, width=30, height=15,
            dimension_unit='cm', weight_unit='kg', sms=True, email=True)
    def inspect_result(kwargs):
        output = json.loads(kwargs['input_items'][-1]['output'])
        assert output['state']['requirements']['missing_fields'] == ['weight']
        assert output['state']['invoice']['current']
        return runtime.ProviderResponse(text='Jaka waga?')
    provider = runtime.FakeModelProvider([
        runtime.ProviderResponse(tool_calls=(runtime.ToolCall('resume', 'orders.fulfillment.state', json.dumps({'order_id': 702})),)),
        inspect_result,
    ])
    with b.app.test_request_context():
        result = runtime.run_agent_turn(rbac.load_actor_context(rbac.BOOTSTRAP_OWNER_ACTOR_ID), 'Kontynuuj realizację Magmaru.', provider)
    assert result['status'] == 'SUCCESS', result
    assert result['message'] == 'Jaka waga?'
    assert result['tool_calls'] == 1


def test_http_human_approval_executes_shipping_and_stale_pdf_is_refused(flow):
    import agent_runtime as runtime
    docs(); requirements()
    payload = {'order_id': 702, 'expected_version': state()['expected_version'], 'idempotency_key': 'http-shipment'}
    provider = runtime.FakeModelProvider([
        runtime.ProviderResponse(tool_calls=(runtime.ToolCall('ship', 'shipping.shipment.create', json.dumps(payload)),)),
        runtime.ProviderResponse(text='Nadanie wymaga zatwierdzenia.'),
    ])
    with b.app.test_request_context():
        result = runtime.run_agent_turn(rbac.load_actor_context(rbac.BOOTSTRAP_OWNER_ACTOR_ID), 'Zamawiaj kuriera.', provider)
    assert result['status'] == 'SUCCESS', result
    assert not flow['calls']
    client = b.app.test_client()
    with client.session_transaction() as session:
        session['admin_authenticated'] = True
        rbac.bind_bootstrap_owner_session(session)
    response = client.post('/api/internal/ai/approvals/' + result['pending_approvals'][0]['approval_id'] + '/approve', json={})
    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.get_json()['status'] == 'SUCCESS', response.get_json()
    assert len(flow['calls']) == 1
    url = '/api/internal/fulfillment/702/documents/packing_list'
    response = client.get(url)
    assert response.status_code == 200 and response.data.startswith(b'%PDF')
    c = b.conn(); c.execute("UPDATE orders SET note='changed' WHERE id=702"); c.commit(); c.close()
    assert client.get(url).status_code == 409


def test_new_shipping_cycle_only_after_existing_verified_history(flow):
    parcel = {'length': 450, 'width': 300, 'height': 150, 'weight': 3}
    with b.app.test_request_context():
        recipient = f.receiver(f.snapshot(702)['order'])
        f.safe_create_shipment(702, recipient, parcel, '', 'inpost_courier_standard', {})
        # No history: another key is the same shipment, even with no local ID.
        f.safe_create_shipment(702, recipient, parcel, '', 'inpost_courier_standard', {})
        assert len(flow['calls']) == 1
        c = b.conn()
        c.execute('INSERT INTO inpost_shipment_history VALUES(?,?,?,?)', (702, '123456', '{}', b.now_iso()))
        c.commit(); c.close()
        f.safe_create_shipment(702, recipient, parcel, '', 'inpost_courier_standard', {})
    assert len(flow['calls']) == 2
    assert flow['calls'][0][2] != flow['calls'][1][2]
