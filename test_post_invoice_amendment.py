"""Exercise the real UI removal helper against an isolated SQLite database."""
import json

import pytest
from werkzeug.exceptions import Conflict

import app as b
import invoice_stock
import invoice_amendment
import business_operations as operations
import internal_rbac as rbac
import internal_approval as approval


@pytest.fixture
def amendment(tmp_path, monkeypatch):
    monkeypatch.setattr(b, 'DB_PATH', str(tmp_path / 'amendment.db'))
    monkeypatch.setattr(b, 'supabase_enabled', lambda: False)
    monkeypatch.setattr(b, 'maybe_pull_shared_from_supabase', lambda **kw: None)
    monkeypatch.setattr(operations, '_freshness_provider', None)
    monkeypatch.setattr(operations, '_write_success_observer', None)
    b.init_db()
    c = b.conn()
    c.execute("INSERT INTO products(id,sku,model,name,created_at) VALUES(91001,'AMEND','Andre','Test',?)", (b.now_iso(),))
    c.execute('INSERT INTO stock(product_id,qty) VALUES(91001,20)')
    for oid, iid in [(91002, 91003), (91004, 91005)]:
        c.execute("INSERT INTO orders(id,order_no,customer_name,customer_email,status,warehouse_issued,inpost_shipment_id,tracking_no,carrier,created_at) VALUES(?,?, 'Test','test@example.invalid','packed',1,'123456','TRACK','inpost',?)", (oid, str(oid), b.now_iso()))
        c.execute("INSERT INTO order_items(id,order_id,product_id,sku,qty,created_at) VALUES(?,?,91001,'AMEND',2,?)", (iid, oid, b.now_iso()))
    c.execute("INSERT INTO invoices(id,order_id,invoice_no,issue_date,sell_date,payment_type,created_at) VALUES(91006,91002,'AMEND/1','2026-09-13','2026-09-13','przelew',?)", (b.now_iso(),))
    rows = [{'order_item_id': iid, 'source_order_id': oid, 'qty': 2} for oid, iid in [(91002, 91003), (91004, 91005)]]
    for row in rows:
        c.execute('INSERT INTO invoice_allocations(invoice_id,order_id,order_item_id,qty,created_at) VALUES(91006,?,?,2,?)', (row['source_order_id'], row['order_item_id'], b.now_iso()))
    invoice_stock.apply_local(c, 91006, rows)
    c.commit()
    c.close()
    pdf = tmp_path / 'invoice.pdf'
    packing = tmp_path / 'packing.pdf'
    pdf.write_bytes(b'test invoice')
    packing.write_bytes(b'test packing')
    b.upsert_invoice_meta(91006, str(pdf), json.dumps(rows))
    monkeypatch.setattr(b, 'invoice_pdf_exists', lambda *args: (True, str(pdf)))
    monkeypatch.setattr(b, 'packing_list_pdf_path_for_invoice', lambda *args: str(packing))
    return pdf, packing


def snapshot():
    c = b.conn()
    try:
        return {
            'orders': [dict(r) for r in c.execute('SELECT * FROM orders ORDER BY id')],
            'stock': [dict(r) for r in c.execute('SELECT * FROM stock ORDER BY product_id')],
            'invoices': [dict(r) for r in c.execute('SELECT * FROM invoices ORDER BY id')],
            'allocations': [dict(r) for r in c.execute('SELECT * FROM invoice_allocations ORDER BY id')],
        }
    finally:
        c.close()


def test_existing_removal_unlocks_all_allocated_orders_preserves_shipment(amendment):
    before = snapshot()
    assert before['stock'][0]['qty'] == 16
    with b.app.test_request_context():
        b._delete_invoice_everywhere(91006)
    after = snapshot()
    assert after['invoices'] == after['allocations'] == []
    assert after['stock'][0]['qty'] == 20
    for old, new in zip(before['orders'], after['orders']):
        assert new['warehouse_issued'] == 0
        for key in ('status', 'inpost_shipment_id', 'tracking_no', 'carrier', 'packed_at'):
            assert new[key] == old[key]
    assert all(not path.exists() for path in amendment)
    c = b.conn()
    try:
        assert c.execute('SELECT COUNT(*) FROM invoice_meta WHERE invoice_id=91006').fetchone()[0] == 0
        assert c.execute('SELECT SUM(qty) FROM invoice_stock_applied WHERE invoice_id=91006').fetchone()[0] == 0
    finally:
        c.close()


@pytest.mark.parametrize('state', ['sending', 'processing', 'unknown', 'sent', 'accepted'])
def test_protected_ksef_state_blocks_before_side_effects(amendment, state):
    b.upsert_ksef_doc(91006, state)
    before = snapshot()
    with b.app.test_request_context(), pytest.raises(Conflict):
        b._delete_invoice_everywhere(91006)
    assert snapshot() == before
    assert all(path.exists() for path in amendment)


@pytest.mark.parametrize('protection', ['number', 'sent_at', 'attempt'])
def test_ksef_identity_or_attempt_blocks_even_other_status(amendment, protection):
    c = b.conn()
    if protection == 'attempt':
        c.execute("INSERT INTO ksef_attempts VALUES(91006,'','',?)", (b.now_iso(),))
    else:
        c.execute("INSERT INTO ksef_documents(invoice_id,status,ksef_number,sent_at,updated_at) VALUES(91006,'error',?,?,?)", ('KSEF/TEST' if protection == 'number' else '', b.now_iso() if protection == 'sent_at' else '', b.now_iso()))
    c.commit()
    c.close()
    before = snapshot()
    with b.app.test_request_context(), pytest.raises(Conflict):
        b._delete_invoice_everywhere(91006)
    assert snapshot() == before


@pytest.mark.parametrize('state', ['draft', 'ready', 'error'])
def test_unprotected_local_invoice_can_be_removed(amendment, state):
    b.upsert_ksef_doc(91006, state)
    with b.app.test_request_context():
        b._delete_invoice_everywhere(91006)
    assert not snapshot()['invoices']


def test_existing_item_add_works_after_existing_removal(amendment):
    with b.app.test_request_context('/orders/91002/items/add', method='POST', data={'product_id': 91001, 'qty': 6}):
        b._refresh_domain_route_context()
        assert b.order_item_add(91002)[1] == 400
        b._delete_invoice_everywhere(91006)
        response = b.order_item_add(91002)
        assert response.status_code == 302
    c = b.conn()
    try:
        assert c.execute('SELECT SUM(qty) FROM order_items WHERE order_id=91002').fetchone()[0] == 8
        assert c.execute('SELECT qty FROM stock WHERE product_id=91001').fetchone()[0] == 20
    finally:
        c.close()


def actors():
    human = rbac.load_actor_context(rbac.BOOTSTRAP_OWNER_ACTOR_ID)
    ai = rbac.load_actor_context(rbac.AI_OWNER_ASSISTANT_ACTOR_ID, delegated_by_actor_id=human.actor_id)
    return human, ai


def proposed(key='remove-test'):
    human, ai = actors()
    state = operations.execute_business_operation(ai, invoice_amendment.READ, {'invoice_id': 91006})
    assert state.status == 'SUCCESS', state
    args = {'invoice_id': 91006, 'expected_version': state.data['expected_version'], 'idempotency_key': key}
    result = operations.execute_business_operation(ai, invoice_amendment.WRITE, args)
    return human, ai, args, result


def test_removal_requires_human_and_retry_does_not_repeat(amendment, monkeypatch):
    human, ai, args, pending = proposed()
    assert pending.status == 'PENDING_APPROVAL', pending
    assert snapshot()['invoices']
    with pytest.raises(approval.ApprovalDenied):
        approval.approve_request(pending.approval_id, ai)
    approval.approve_request(pending.approval_id, human)
    real = b._delete_invoice_everywhere
    calls = []
    def remove(iid):
        calls.append(iid)
        return real(iid)
    monkeypatch.setattr(b, '_delete_invoice_everywhere', remove)
    with b.app.test_request_context():
        result = operations.execute_business_operation(ai, invoice_amendment.WRITE, args, approval_id=pending.approval_id)
        retry = operations.execute_business_operation(ai, invoice_amendment.WRITE, args, approval_id=pending.approval_id)
    assert result.status == retry.status == 'SUCCESS', result
    assert calls == [91006]
    assert all(o['editable'] and o['shipment_exists'] for o in result.data['affected_orders'])
    c = b.conn()
    try:
        row = c.execute("SELECT before_state,after_state FROM internal_audit_log WHERE operation='invoices.remove' ORDER BY rowid DESC LIMIT 1").fetchone()
        assert row and 'removed' in row['after_state'] and 'AMEND/1' in row['before_state']
    finally:
        c.close()


def test_change_after_approval_conflicts_without_removal(amendment):
    human, ai, args, pending = proposed()
    approval.approve_request(pending.approval_id, human)
    c = b.conn()
    c.execute('UPDATE order_items SET qty=3 WHERE id=91003')
    c.commit()
    c.close()
    with b.app.test_request_context():
        result = operations.execute_business_operation(ai, invoice_amendment.WRITE, args, approval_id=pending.approval_id)
    assert result.status == 'CONFLICT', result
    assert snapshot()['invoices']


def test_ksef_starts_after_approval_blocks_removal(amendment):
    human, ai, args, pending = proposed()
    approval.approve_request(pending.approval_id, human)
    b.upsert_ksef_doc(91006, 'processing')
    with b.app.test_request_context():
        result = operations.execute_business_operation(ai, invoice_amendment.WRITE, args, approval_id=pending.approval_id)
    assert result.status in {'CONFLICT', 'DENIED'}, result
    assert snapshot()['invoices']


def test_uncertain_removal_cannot_be_replayed_with_new_key(amendment, monkeypatch):
    human, ai, args, pending = proposed()
    approval.approve_request(pending.approval_id, human)
    calls = []
    def timeout(iid):
        calls.append(iid)
        raise TimeoutError('simulated interruption')
    monkeypatch.setattr(b, '_delete_invoice_everywhere', timeout)
    with b.app.test_request_context():
        result = operations.execute_business_operation(ai, invoice_amendment.WRITE, args, approval_id=pending.approval_id)
        again = operations.execute_business_operation(ai, invoice_amendment.WRITE, {**args, 'idempotency_key': 'new-key'})
    assert result.error_code == 'REMOVAL_REQUIRES_RECONCILIATION', result
    assert again.status == 'DENIED', again
    assert calls == [91006]


def test_shipped_order_is_not_amended(amendment):
    c = b.conn()
    c.execute("UPDATE orders SET status='shipped' WHERE id=91004")
    c.commit()
    c.close()
    state = invoice_amendment.preview({'invoice_id': 91006})
    assert not state['removable']
    assert 'wysłane' in state['blocker']


def test_other_invoice_can_keep_an_order_locked(amendment):
    c = b.conn()
    c.execute("INSERT INTO invoices(id,order_id,invoice_no,issue_date,sell_date,payment_type,created_at) VALUES(91007,91002,'AMEND/2','2026-09-13','2026-09-13','przelew',?)", (b.now_iso(),))
    c.execute('INSERT INTO invoice_allocations(invoice_id,order_id,order_item_id,qty,created_at) VALUES(91007,91002,91003,2,?)', (b.now_iso(),))
    c.commit()
    c.close()
    with b.app.test_request_context():
        b._delete_invoice_everywhere(91006)
    after = snapshot()['orders']
    assert after[0]['warehouse_issued'] == 1
    assert after[1]['warehouse_issued'] == 0


def test_initiator_permission_revocation_prevents_removal(amendment):
    human, ai, args, pending = proposed()
    approval.approve_request(pending.approval_id, human)
    c = b.conn()
    c.execute('DELETE FROM internal_actor_roles WHERE actor_id=?', (human.actor_id,))
    c.commit()
    c.close()
    with b.app.test_request_context():
        result = operations.execute_business_operation(ai, invoice_amendment.WRITE, args, approval_id=pending.approval_id)
    assert result.status == 'DENIED'
    assert snapshot()['invoices']


def test_new_operation_available_only_with_permissions(amendment):
    import agent_runtime as runtime
    human, ai = actors()
    descriptors = runtime._tool_descriptors(ai, human)
    assert {invoice_amendment.READ, invoice_amendment.WRITE} <= {d['name'] for d in descriptors}
    assert operations.OPERATION_REGISTRY[invoice_amendment.WRITE].risk_level == 'RED'
    assert not any(d['name'] in {'ksef.send', 'invoice.send_to_ksef'} for d in descriptors)


def test_agent_proposes_and_human_http_action_executes(amendment):
    import agent_runtime as runtime
    human, ai = actors()
    state = invoice_amendment.preview({'invoice_id': 91006})
    args = {'invoice_id': 91006, 'expected_version': state['expected_version'], 'idempotency_key': 'runtime-remove'}
    provider = runtime.FakeModelProvider([
        runtime.ProviderResponse(tool_calls=(runtime.ToolCall('preview', invoice_amendment.READ, json.dumps({'invoice_id': 91006})),)),
        runtime.ProviderResponse(tool_calls=(runtime.ToolCall('remove', invoice_amendment.WRITE, json.dumps(args)),)),
        runtime.ProviderResponse(text='Usunięcie obecnej faktury wymaga Twojego zatwierdzenia.'),
    ])
    result = runtime.run_agent_turn(human, 'Tak, usuń obecną fakturę, żeby zmienić zamówienie.', provider)
    assert result['status'] == 'SUCCESS', result
    pending = result['pending_approvals'][0]
    assert pending['invoice_number'] == 'AMEND/1'
    assert set(pending['order_numbers']) == {'91002', '91004'}
    assert snapshot()['invoices']
    client = b.app.test_client()
    with client.session_transaction() as session:
        session['admin_authenticated'] = True
        rbac.bind_bootstrap_owner_session(session)
    response = client.post(f"/api/internal/ai/approvals/{pending['approval_id']}/approve", json={})
    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.get_json()['status'] == 'SUCCESS', response.get_json()
    assert not snapshot()['invoices']


def test_remote_cleanup_not_confirmed_is_not_reported_as_success(amendment, monkeypatch):
    human, ai, args, pending = proposed()
    approval.approve_request(pending.approval_id, human)
    real = b._delete_invoice_everywhere
    def remove(iid):
        result = real(iid)
        monkeypatch.setattr(b, 'supabase_enabled', lambda: True)
        return result
    monkeypatch.setattr(b, '_delete_invoice_everywhere', remove)
    monkeypatch.setattr(b, 'supabase_request', lambda path, **kw: [{'invoice_id': 91006}] if path.endswith('/invoice_meta') else [])
    with b.app.test_request_context():
        result = operations.execute_business_operation(ai, invoice_amendment.WRITE, args, approval_id=pending.approval_id)
    assert result.status == 'FAILED'
    assert result.error_code == 'REMOVAL_REQUIRES_RECONCILIATION'
    assert not snapshot()['invoices']


def test_two_approved_keys_do_not_remove_same_invoice_twice(amendment, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    human, ai, args1, pending1 = proposed('parallel-1')
    _, _, args2, pending2 = proposed('parallel-2')
    approval.approve_request(pending1.approval_id, human)
    approval.approve_request(pending2.approval_id, human)
    barrier = threading.Barrier(2)
    calls = []
    real = b._delete_invoice_everywhere
    def remove(iid):
        calls.append(iid)
        return real(iid)
    monkeypatch.setattr(b, '_delete_invoice_everywhere', remove)
    def execute(pair):
        args, pending = pair
        barrier.wait(timeout=10)
        with b.app.test_request_context():
            return operations.execute_business_operation(ai, invoice_amendment.WRITE, args, approval_id=pending.approval_id)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(execute, [(args1, pending1), (args2, pending2)]))
    assert [r.status for r in results].count('SUCCESS') == 1, results
    assert calls == [91006]
