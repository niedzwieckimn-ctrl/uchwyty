import json
import os
from pathlib import Path

import agent_runtime as runtime
import app as backend
import business_operations as operations
import internal_rbac as rbac
from test_business_operations import _owner
from test_first_supervised_write import ai, isolated, order


def _artifact(items, kind):
    return next(item for item in items if item['type'] == kind)


def _two_products_with_images():
    image_dir = Path(backend.DB_PATH).parent / 'product_images'
    image_dir.mkdir(exist_ok=True)
    db = backend.conn()
    now = backend.now_iso()
    for product_id, sku, name in (
        (702, 'AVERY-FIRST', 'Avery pierwszy'),
        (703, 'AVERY-SECOND', 'Avery drugi'),
    ):
        image_path = image_dir / f'{product_id}.png'
        image_path.write_bytes(b'png-test-data')
        db.execute(
            'INSERT INTO products(id,sku,model,name,created_at) VALUES(?,?,?,?,?)',
            (product_id, sku, 'Avery 160', name, now),
        )
        db.execute('INSERT INTO stock(product_id,qty) VALUES(?,?)', (product_id, product_id - 690))
        cursor = db.execute(
            'INSERT INTO product_images(stored_path,filename,created_at) VALUES(?,?,?)',
            (f'product_images/{product_id}.png', f'{product_id}.png', now),
        )
        db.execute(
            'INSERT INTO product_image_assignments(product_id,image_id,created_at) VALUES(?,?,?)',
            (product_id, cursor.lastrowid, now),
        )
    db.commit()
    db.close()


def _search_turn():
    provider = runtime.FakeModelProvider([
        runtime.ProviderResponse(tool_calls=(runtime.ToolCall(
            'search-products', 'inventory.product.search', json.dumps({'query': 'Avery'}),
        ),), model='fake'),
        runtime.ProviderResponse(text='Znalazłem dwa produkty.', model='fake'),
    ])
    return runtime.run_agent_turn(_owner(), 'Pokaż Avery.', provider)


def _followup(conversation_id, product_id, message):
    provider = runtime.FakeModelProvider([
        runtime.ProviderResponse(tool_calls=(runtime.ToolCall(
            f'get-{product_id}', 'inventory.product.get', json.dumps({'product_id': product_id}),
        ),), model='fake'),
        runtime.ProviderResponse(text=f'Wybrany produkt {product_id}.', model='fake'),
    ])
    result = runtime.run_agent_turn(_owner(), message, provider, conversation_id=conversation_id)
    return result, provider


def test_a_direct_product_uses_relative_existing_image(order, isolated):
    _two_products_with_images()
    result = operations.execute_business_operation(ai(), 'inventory.product.search', {'query': 'AVERY-FIRST'})
    artifacts = backend.build_business_artifacts('inventory.product.search', result.data)
    assert _artifact(artifacts, 'product_card')['id'] == 702
    image = _artifact(artifacts, 'product_image')
    assert image['product_id'] == 702
    with isolated.session_transaction() as current:
        current['admin_authenticated'] = True
        rbac.bind_bootstrap_owner_session(current)
    response = isolated.get(image['url'])
    assert response.status_code == 200 and response.data == b'png-test-data'


def test_b_c_followup_first_and_second_re_read_trusted_products(order):
    _two_products_with_images()
    initial = _search_turn()
    first, first_provider = _followup(initial['conversation_id'], 702, 'pierwszy')
    assert _artifact(first['artifacts'], 'product_image')['product_id'] == 702
    history_json = json.dumps(first_provider.calls[0]['input_items'], ensure_ascii=False)
    assert 'trusted_artifact_evidence' in history_json
    assert initial['conversation_id'] in history_json
    source_output = next(
        item['output'] for item in first_provider.calls[0]['input_items']
        if item.get('type') == 'function_call_output'
        and initial['conversation_id'] in item.get('output', '')
    )
    sources = json.loads(source_output)
    assert {item['entity_id'] for item in sources} == {702, 703}
    assert all(item['conversation_id'] == initial['conversation_id'] for item in sources)
    assert all(item['source_turn_id'] == initial['agent_run_id'] for item in sources)

    second, _provider = _followup(initial['conversation_id'], 703, 'ten drugi')
    assert _artifact(second['artifacts'], 'product_image')['product_id'] == 703


def test_d_artifact_evidence_does_not_cross_conversations(order):
    _two_products_with_images()
    _search_turn()
    provider = runtime.FakeModelProvider([
        runtime.ProviderResponse(text='Nie mam wskazanego produktu.', model='fake'),
    ])
    result = runtime.run_agent_turn(_owner(), 'pierwszy', provider)
    assert result['artifacts'] == []
    assert 'trusted_artifact_evidence' not in json.dumps(provider.calls[0]['input_items'])


def test_e_f_no_image_and_llm_text_cannot_create_image(order):
    db = backend.conn()
    db.execute(
        "INSERT INTO products(id,sku,model,name,created_at) VALUES(704,'NO-IMAGE','Avery 160','Bez zdjęcia',?)",
        (backend.now_iso(),),
    )
    db.execute('INSERT INTO stock(product_id,qty) VALUES(704,1)')
    db.commit()
    db.close()
    result = operations.execute_business_operation(ai(), 'inventory.product.get', {'product_id': 704})
    artifacts = backend.build_business_artifacts('inventory.product.get', result.data)
    assert _artifact(artifacts, 'product_card')['id'] == 704
    assert not any(item['type'] == 'product_image' for item in artifacts)

    provider = runtime.FakeModelProvider([
        runtime.ProviderResponse(text='product_id=702, /stock/images/999', model='fake'),
    ])
    invented = runtime.run_agent_turn(_owner(), 'Pokaż obraz.', provider)
    assert invented['artifacts'] == []


def _invoice_and_packing(order_id, tmp_path, create_packing=True):
    invoice_pdf = tmp_path / 'FV-812.pdf'
    invoice_pdf.write_bytes(b'%PDF invoice')
    packing_pdf = Path(backend.packing_list_pdf_path_for_invoice(str(invoice_pdf), 'FV/812'))
    if create_packing:
        packing_pdf.write_bytes(b'%PDF packing existing')
    db = backend.conn()
    now = backend.now_iso()
    db.execute(
        """INSERT INTO invoices(id,order_id,invoice_no,issue_date,sell_date,payment_type,payment_to,
               buyer_name,total_net,total_gross,created_at,currency)
           VALUES(812,?,'FV/812','2026-09-01','2026-09-01','transfer','2026-09-15',
                  'Kraft',10,12.3,?,'PLN')""",
        (order_id, now),
    )
    db.execute(
        "INSERT INTO invoice_meta(invoice_id,pdf_path,invoice_items_json,updated_at) VALUES(812,?,'[]',?)",
        (str(invoice_pdf), now),
    )
    db.commit()
    db.close()
    return packing_pdf


def test_g_existing_packing_list_produces_read_only_document_link(order, isolated, tmp_path, monkeypatch):
    packing_pdf = _invoice_and_packing(order, tmp_path)
    result = operations.execute_business_operation(ai(), 'orders.get', {'id': order})
    artifacts = backend.build_business_artifacts('orders.get', result.data)
    link = _artifact(artifacts, 'document_link')
    assert link == {
        'type': 'document_link', 'document_type': 'packing_list',
        'label': 'Lista pakowa',
        'url': '/api/internal/ai/documents/packing-lists/812',
        'order_id': order, 'invoice_id': 812,
    }

    monkeypatch.setattr(backend, 'generate_invoice_packing_list_pdf', lambda *_a, **_k: (_ for _ in ()).throw(AssertionError('generation forbidden')))
    monkeypatch.setattr(backend, 'mark_orders_packed', lambda *_a, **_k: (_ for _ in ()).throw(AssertionError('write forbidden')))
    before_mtime = os.path.getmtime(packing_pdf)
    with isolated.session_transaction() as current:
        current['admin_authenticated'] = True
        rbac.bind_bootstrap_owner_session(current)
    response = isolated.get(link['url'])
    assert response.status_code == 200 and response.data == b'%PDF packing existing'
    assert os.path.getmtime(packing_pdf) == before_mtime
    db = backend.conn()
    assert db.execute('SELECT status FROM orders WHERE id=?', (order,)).fetchone()['status'] == 'new'
    db.close()


def test_h_missing_packing_list_has_no_link_and_route_is_404(order, isolated, tmp_path):
    _invoice_and_packing(order, tmp_path, create_packing=False)
    result = operations.execute_business_operation(ai(), 'orders.get', {'id': order})
    artifacts = backend.build_business_artifacts('orders.get', result.data)
    assert _artifact(artifacts, 'order_card')['id'] == order
    assert not any(item.get('document_type') == 'packing_list' for item in artifacts)
    with isolated.session_transaction() as current:
        current['admin_authenticated'] = True
        rbac.bind_bootstrap_owner_session(current)
    assert isolated.get('/api/internal/ai/documents/packing-lists/812').status_code == 404
